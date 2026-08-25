"""Repository hygiene checks.

These guard against leaking internal infrastructure into a public repo. They exist
because it already happened once: `uv.lock` was generated while UV_INDEX_URL pointed at
a corporate package mirror, and carried 175 references to that internal host into the
first public commit. Those env vars persist in the developer's shell, so any future
`uv lock` run outside a scrubbed environment would reintroduce it silently.

Deliberately no internal hostname appears in this file -- a guard that names what it
guards against defeats itself.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Any corporate subdomain except the well-known public ones. Internal mirrors and
# enterprise hosts must never reach a public repo; links to public documentation are fine.
PUBLIC_APPLE_HOSTS = {
    "apple.com",
    "www.apple.com",
    "developer.apple.com",
    "support.apple.com",
    "apps.apple.com",
    "itunes.apple.com",
}
INTERNAL_HOST = re.compile(r"\b[a-z0-9.-]*\.?apple\.com\b", re.IGNORECASE)


def internal_hosts(text: str) -> list[str]:
    return sorted(
        {h for h in (m.lower() for m in INTERNAL_HOST.findall(text)) if h not in PUBLIC_APPLE_HOSTS}
    )


TRACKED_TEXT_SUFFIXES = {".py", ".toml", ".lock", ".md", ".yml", ".yaml", ".cfg", ".txt"}


def repo_files() -> list[Path]:
    skip = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "dist", "build"}
    return [
        path
        for path in REPO.rglob("*")
        if path.is_file()
        and path.suffix in TRACKED_TEXT_SUFFIXES
        and not any(part in skip for part in path.parts)
    ]


def test_lockfile_uses_the_public_index() -> None:
    """uv.lock must resolve against pypi.org, not an internal mirror."""
    lock = (REPO / "uv.lock").read_text()
    assert "pypi.org" in lock, "expected the public index in uv.lock"
    leaked = internal_hosts(lock)
    assert not leaked, (
        f"uv.lock references internal hosts {leaked}. Re-lock with "
        "UV_INDEX_URL=https://pypi.org/simple UV_DEFAULT_INDEX=https://pypi.org/simple uv lock"
    )


def test_no_internal_hostnames_anywhere() -> None:
    offenders: dict[str, list[str]] = {}
    for path in repo_files():
        found = internal_hosts(path.read_text(errors="ignore"))
        if found:
            offenders[str(path.relative_to(REPO))] = found
    assert not offenders, f"internal hostnames in a public repo: {offenders}"


@pytest.mark.parametrize(
    "pattern",
    [
        r"[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}",  # Telegram bot token
        r"\bpro_[A-Za-z0-9]{20,}",  # seats.aero Partner API key
        r"\bre_[A-Za-z0-9]{20,}",  # Resend API key
        r"\bgh[pousr]_[A-Za-z0-9]{30,}",  # GitHub token
        r"hooks\.slack\.com/services/T[A-Z0-9]{6,}",  # real Slack webhook
    ],
)
def test_no_credential_shaped_literals(pattern: str) -> None:
    """Catches a real key pasted into a test fixture. The synthetic values used in
    tests/test_redact.py are deliberately too short or too obviously fake to match."""
    compiled = re.compile(pattern)
    offenders = {
        str(path.relative_to(REPO))
        for path in repo_files()
        if path.name != Path(__file__).name and compiled.search(path.read_text(errors="ignore"))
    }
    assert not offenders, f"credential-shaped literal matching {pattern!r} in {offenders}"


def test_env_file_is_gitignored() -> None:
    ignored = (REPO / ".gitignore").read_text().splitlines()
    assert ".env" in ignored
    assert "*.db" in ignored
