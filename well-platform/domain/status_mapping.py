"""Maps RRC's raw well-symbol taxonomy onto our normalized RegulatoryStatus.

The raw strings/codes below were pulled directly from RRC's ArcGIS service
(`rrc_public/RRC_Public_Viewer_Srvs/MapServer/1`, field `GIS_SYMBOL_DESCRIPTION`,
keyed by `SYMNUM`) via a groupBy statistics query against the live service on
2026-09-16 — see ../texas-rrc-wells/README.md for how that query was made and
the full research process. This is RRC's actual, current taxonomy, not a
guess — but it's a live government dataset, so if ingestion starts seeing
`SYMNUM` values not in this table, `normalize_rrc_status` falls back to
`OTHER` rather than raising, and logs the miss (see ingestion_service.py).
"""

from __future__ import annotations

from .enums import OperationalStatus, RegulatoryStatus

# SYMNUM -> (raw description as RRC returns it, normalized RegulatoryStatus)
RRC_SYMBOL_MAP: dict[int, tuple[str, RegulatoryStatus]] = {
    2: ("Permitted Location", RegulatoryStatus.PERMITTED),
    3: ("Dry Hole", RegulatoryStatus.DRY_HOLE),
    4: ("Oil Well", RegulatoryStatus.ACTIVE_OIL),
    5: ("Gas Well", RegulatoryStatus.ACTIVE_GAS),
    6: ("Oil/Gas Well", RegulatoryStatus.ACTIVE_OIL_GAS),
    7: ("Plugged Oil Well", RegulatoryStatus.PLUGGED),
    8: ("Plugged Gas Well", RegulatoryStatus.PLUGGED),
    9: ("Canceled / Abandoned Location", RegulatoryStatus.CANCELED_OR_ABANDONED_LOCATION),
    10: ("Plugged Oil / Gas", RegulatoryStatus.PLUGGED),
    11: ("Injection / Disposal", RegulatoryStatus.INJECTION_OR_DISPOSAL),
    12: ("Core Test", RegulatoryStatus.CORE_TEST),
    16: ("Sulfur Core Test", RegulatoryStatus.CORE_TEST),
    17: ("Storage from Oil", RegulatoryStatus.STORAGE),
    18: ("Storage from Gas", RegulatoryStatus.STORAGE),
    19: ("Shut-In Oil", RegulatoryStatus.SHUT_IN),
    20: ("Shut-In Gas", RegulatoryStatus.SHUT_IN),
    21: ("Injection / Disposal from Oil", RegulatoryStatus.INJECTION_OR_DISPOSAL),
    22: ("Injection / Disposal from Gas", RegulatoryStatus.INJECTION_OR_DISPOSAL),
    23: ("Injection / Disposal from Oil/Gas", RegulatoryStatus.INJECTION_OR_DISPOSAL),
    36: ("Geothermal Well", RegulatoryStatus.GEOTHERMAL),
    73: ("Brine Mining", RegulatoryStatus.BRINE_MINING),
    74: ("Water Supply Well", RegulatoryStatus.WATER_SUPPLY),
    75: ("Water Supply from Oil", RegulatoryStatus.WATER_SUPPLY),
    76: ("Water Supply from Gas", RegulatoryStatus.WATER_SUPPLY),
    77: ("Water Supply from Oil/Gas", RegulatoryStatus.WATER_SUPPLY),
    78: ("Observation Well", RegulatoryStatus.OBSERVATION),
    79: ("Observation from Oil", RegulatoryStatus.OBSERVATION),
    80: ("Observation from Gas", RegulatoryStatus.OBSERVATION),
    81: ("Observation from Oil / Gas", RegulatoryStatus.OBSERVATION),
    86: ("Horizontal Drainhole", RegulatoryStatus.ACTIVE_OIL_GAS),
    88: ("Storage Well", RegulatoryStatus.STORAGE),
    89: ("Service", RegulatoryStatus.SERVICE),
    90: ("Service from Oil", RegulatoryStatus.SERVICE),
    91: ("Service from Gas", RegulatoryStatus.SERVICE),
    92: ("Service from Oil / Gas", RegulatoryStatus.SERVICE),
    103: ("Storage from Oil/Gas", RegulatoryStatus.STORAGE),
    104: ("Injection/Disposal from Storage", RegulatoryStatus.INJECTION_OR_DISPOSAL),
    105: ("Injection/Disposal from Storage/Oil", RegulatoryStatus.INJECTION_OR_DISPOSAL),
    106: ("Injection/Disposal from Storage/Gas", RegulatoryStatus.INJECTION_OR_DISPOSAL),
    108: ("Observation from Storage", RegulatoryStatus.OBSERVATION),
    109: ("Observation from Storage/Oil", RegulatoryStatus.OBSERVATION),
    110: ("Observation from Storage/Gas", RegulatoryStatus.OBSERVATION),
    114: ("Service from Storage/Gas", RegulatoryStatus.SERVICE),
    116: ("Plugged Storage", RegulatoryStatus.PLUGGED),
    117: ("Plugged Storage/Oil", RegulatoryStatus.PLUGGED),
    118: ("Plugged Storage/Gas", RegulatoryStatus.PLUGGED),
    124: ("Injection/Disposal from Brine Mining", RegulatoryStatus.INJECTION_OR_DISPOSAL),
    126: ("Injection/Disposal from Brine Mining / Gas", RegulatoryStatus.INJECTION_OR_DISPOSAL),
    136: ("Plugged Brine Mining", RegulatoryStatus.PLUGGED),
    140: ("Storage/Brine Mining", RegulatoryStatus.STORAGE),
    144: ("Injection/Disposal from Storage/Brine Mining", RegulatoryStatus.INJECTION_OR_DISPOSAL),
    148: ("Observation from Storage/Brine Mining", RegulatoryStatus.OBSERVATION),
    152: ("Plugged Storage/Brine Mining", RegulatoryStatus.PLUGGED),
    153: ("Plugged Storage/Brine Mining / Oil", RegulatoryStatus.PLUGGED),
}


def normalize_rrc_status(symnum: int | None, description: str | None) -> RegulatoryStatus:
    """Map a raw RRC (SYMNUM, description) pair to our normalized status.

    Prefers the numeric SYMNUM (stable, RRC's actual key) and falls back to
    a case-insensitive description match if SYMNUM is missing or unknown —
    then to OTHER. Never raises: ingestion should never fail a whole batch
    because RRC introduced a new status code.
    """
    if symnum is not None and symnum in RRC_SYMBOL_MAP:
        return RRC_SYMBOL_MAP[symnum][1]

    if description:
        norm_desc = description.strip().lower()
        for _, (raw_desc, status) in RRC_SYMBOL_MAP.items():
            if raw_desc.lower() == norm_desc:
                return status

    return RegulatoryStatus.OTHER


# Default OperationalStatus to seed when a well is *first* enrolled in the
# monitoring program (a Sigil Node gets deployed on it) and no explicit
# status is supplied. This is only ever applied at enrollment time — see
# services/well_service.py `enroll_well_for_monitoring` — never silently
# during ingestion, because most RRC wells must stay operational_status=None.
DEFAULT_OPERATIONAL_STATUS_ON_ENROLLMENT: dict[RegulatoryStatus, OperationalStatus] = {
    RegulatoryStatus.PLUGGED: OperationalStatus.ABANDONED,
    RegulatoryStatus.CANCELED_OR_ABANDONED_LOCATION: OperationalStatus.ABANDONED,
    RegulatoryStatus.ORPHANED: OperationalStatus.ABANDONED,
    RegulatoryStatus.ACTIVE_OIL: OperationalStatus.ACTIVE,
    RegulatoryStatus.ACTIVE_GAS: OperationalStatus.ACTIVE,
    RegulatoryStatus.ACTIVE_OIL_GAS: OperationalStatus.ACTIVE,
    RegulatoryStatus.SHUT_IN: OperationalStatus.IDLE,
}
