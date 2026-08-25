"""Mileage programs and credit-card transfer partners.

`KNOWN_SOURCES` is the set of `Source` values the seats.aero Partner API actually
returned when enumerated across busy transatlantic and transpacific O&Ds. Unknown
program names in config are rejected rather than silently matching nothing.
"""

from __future__ import annotations

KNOWN_SOURCES = frozenset(
    {
        "aeroplan",
        "alaska",
        "american",
        "british",
        "delta",
        "ethiopian",
        "etihad",
        "finnair",
        "flyingblue",
        "jetblue",
        "qantas",
        "qatar",
        "smiles",
        "united",
        "velocity",
        "virginatlantic",
    }
)

CHASE = frozenset(
    {
        "aeroplan",  # Air Canada Aeroplan
        "british",  # British Airways Executive Club
        "flyingblue",  # Air France / KLM Flying Blue
        "jetblue",  # JetBlue TrueBlue
        "united",  # United MileagePlus
        "virginatlantic",  # Virgin Atlantic Flying Club
    }
)
"""Chase Ultimate Rewards airline transfer partners that seats.aero indexes.

Chase also partners with Aer Lingus AerClub, Emirates Skywards, Iberia Plus, Singapore
KrisFlyer and Southwest Rapid Rewards, none of which seats.aero covers -- so a
Chase-restricted watch has a real blind spot. Aer Lingus matters most for Ireland,
since it operates the nonstops.

Notably absent: American AAdvantage and Alaska Mileage Plan are NOT Chase partners.
"""

TRANSFER_PARTNERS: dict[str, frozenset[str]] = {
    "chase": CHASE,
}
"""Aliases usable in config as `programs = ["@chase"]`."""


def expand_programs(programs: list[str]) -> list[str]:
    """Resolve `@alias` entries to program identifiers, validating the rest.

    Returns a de-duplicated, order-stable list. Raises ValueError on an unknown alias
    or program name -- a typo that silently matched nothing would look exactly like
    "no award space", which is the worst possible failure mode here.
    """
    resolved: list[str] = []
    for entry in programs:
        name = entry.strip().lower()
        if not name:
            continue
        if name.startswith("@"):
            alias = name[1:]
            if alias not in TRANSFER_PARTNERS:
                valid = ", ".join(f"@{key}" for key in sorted(TRANSFER_PARTNERS))
                raise ValueError(f"Unknown program alias {entry!r}. Available: {valid}")
            resolved.extend(sorted(TRANSFER_PARTNERS[alias]))
        elif name not in KNOWN_SOURCES:
            raise ValueError(
                f"Unknown mileage program {entry!r}. Known programs: "
                f"{', '.join(sorted(KNOWN_SOURCES))}. "
                "If seats.aero has added a program, add it to KNOWN_SOURCES."
            )
        else:
            resolved.append(name)
    return list(dict.fromkeys(resolved))
