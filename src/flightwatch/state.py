"""SQLite-backed alert history.

Only prices we actually alerted at are stored. "Improvement" is therefore measured
against the last price you were told about, not against the last price observed --
otherwise a slow drift downward would never trip the improvement rule.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import DedupeConfig
from .models import Deal

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    key             TEXT PRIMARY KEY,
    watch_name      TEXT NOT NULL,
    miles           INTEGER NOT NULL,
    first_seen_at   TEXT NOT NULL,
    last_alerted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    found      INTEGER NOT NULL,
    alerted    INTEGER NOT NULL,
    error      TEXT
);
"""


@dataclass(frozen=True, slots=True)
class AlertRecord:
    key: str
    watch_name: str
    miles: int
    first_seen_at: datetime
    last_alerted_at: datetime
    return_miles: int | None = None
    """Cheapest paired return at the time we alerted, if any."""


def _now() -> datetime:
    return datetime.now(UTC)


class Store:
    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        if str(self._path) != ":memory:":
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after the first release, in place.

        Existing state files are worth keeping -- rebuilding one means a fresh wave of
        alerts for awards you've already seen.
        """
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(alerts)")}
        if "return_miles" not in columns:
            self._conn.execute("ALTER TABLE alerts ADD COLUMN return_miles INTEGER")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def get(self, key: str) -> AlertRecord | None:
        row = self._conn.execute("SELECT * FROM alerts WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        return AlertRecord(
            key=row["key"],
            watch_name=row["watch_name"],
            miles=row["miles"],
            first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
            last_alerted_at=datetime.fromisoformat(row["last_alerted_at"]),
            return_miles=row["return_miles"],
        )

    def should_alert(
        self, deal: Deal, dedupe: DedupeConfig, *, now: datetime | None = None
    ) -> bool:
        moment = now or _now()
        record = self.get(deal.availability.key)
        if record is None:
            return True

        # A return appearing where there was none is new information about a deal you
        # were already told about, and is the whole reason to pair returns at all.
        if record.return_miles is None and deal.returns:
            return True

        if dedupe.realert_on_improvement:
            if record.miles - deal.availability.miles >= dedupe.realert_on_improvement:
                return True
            # Also re-alert when the round trip as a whole gets materially cheaper, even
            # if the outbound price is unchanged.
            if record.return_miles is not None and deal.round_trip_miles is not None:
                previous_total = record.miles + record.return_miles
                if previous_total - deal.round_trip_miles >= dedupe.realert_on_improvement:
                    return True

        if dedupe.cooldown_days:
            return moment - record.last_alerted_at >= timedelta(days=dedupe.cooldown_days)
        return False

    def select_alertable(
        self, deals: list[Deal], dedupe: DedupeConfig, *, now: datetime | None = None
    ) -> list[Deal]:
        """Filter to deals worth sending, annotating each with the last alerted price."""
        moment = now or _now()
        out: list[Deal] = []
        for deal in deals:
            if not self.should_alert(deal, dedupe, now=moment):
                continue
            record = self.get(deal.availability.key)
            out.append(replace(deal, previous_miles=record.miles if record else None))
        return out

    def record_alerted(self, deals: list[Deal], *, now: datetime | None = None) -> None:
        moment = (now or _now()).isoformat()
        self._conn.executemany(
            """
            INSERT INTO alerts
                (key, watch_name, miles, first_seen_at, last_alerted_at, return_miles)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                miles = excluded.miles,
                last_alerted_at = excluded.last_alerted_at,
                watch_name = excluded.watch_name,
                return_miles = excluded.return_miles
            """,
            [
                (
                    deal.availability.key,
                    deal.watch_name,
                    deal.availability.miles,
                    moment,
                    moment,
                    deal.best_return.miles if deal.best_return else None,
                )
                for deal in deals
            ],
        )
        self._conn.commit()

    def record_run(self, *, found: int, alerted: int, error: str | None = None) -> None:
        self._conn.execute(
            "INSERT INTO runs (started_at, found, alerted, error) VALUES (?, ?, ?, ?)",
            (_now().isoformat(), found, alerted, error),
        )
        self._conn.commit()

    def prune(self, *, older_than_days: int = 120) -> int:
        """Forget awards we haven't alerted on in a while.

        Keyed on last-alerted time, not departure date: an award that keeps qualifying
        stays fresh, while one that disappeared from the cache ages out and would alert
        again from scratch if it ever returns.
        """
        cutoff = (_now() - timedelta(days=older_than_days)).isoformat()
        cursor = self._conn.execute("DELETE FROM alerts WHERE last_alerted_at < ?", (cutoff,))
        self._conn.commit()
        return cursor.rowcount

    def recent_runs(self, limit: int = 10) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
