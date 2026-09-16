"""Enumerations shared across the domain, data, and service layers.

Two status concepts are kept deliberately separate — see docs/ARCHITECTURE.md
"Two status axes" for the full rationale:

- `RegulatoryStatus` is *derived from RRC*. It answers "what does the state
  of Texas say this well is doing." It's read-only from our side — we only
  ever set it from ingested RRC data.
- `OperationalStatus` is *ours*. It answers "what does Sigilint's monitoring
  program say about this well" — is it instrumented, is a Sigil Node
  reporting a leak, is a crew on site. Most RRC wells will never have one
  (`None`/unset) because most wells are never instrumented.
"""

from __future__ import annotations

from enum import Enum


class RegulatoryStatus(str, Enum):
    """Normalized bucket for RRC's ~40 raw `GIS_SYMBOL_DESCRIPTION` values.

    See domain/status_mapping.py for the raw-string -> bucket table. Kept
    coarser than RRC's raw taxonomy on purpose: it's what we filter/report
    on, while the raw string is preserved alongside it for anyone who needs
    the exact RRC wording.
    """

    PERMITTED = "permitted"
    ACTIVE_OIL = "active_oil"
    ACTIVE_GAS = "active_gas"
    ACTIVE_OIL_GAS = "active_oil_gas"
    SHUT_IN = "shut_in"
    DRY_HOLE = "dry_hole"
    PLUGGED = "plugged"
    CANCELED_OR_ABANDONED_LOCATION = "canceled_or_abandoned_location"
    ORPHANED = "orphaned"  # from RRC's separate Orphan Wells layer, not a symbol description
    INJECTION_OR_DISPOSAL = "injection_or_disposal"
    STORAGE = "storage"
    OBSERVATION = "observation"
    WATER_SUPPLY = "water_supply"
    BRINE_MINING = "brine_mining"
    GEOTHERMAL = "geothermal"
    SERVICE = "service"
    CORE_TEST = "core_test"
    OTHER = "other"

    @property
    def is_plugged_or_abandoned(self) -> bool:
        """Wells no longer producing under RRC's own classification —
        the RRC-side proxy for "candidate for methane/H2S monitoring."
        Doesn't include ORPHANED, which is checked separately since it
        comes from a different RRC layer and is a strictly stronger signal.
        """
        return self in (
            RegulatoryStatus.PLUGGED,
            RegulatoryStatus.CANCELED_OR_ABANDONED_LOCATION,
            RegulatoryStatus.DRY_HOLE,
        )


class OperationalStatus(str, Enum):
    """Sigilint's own monitoring-program state for a well. Unset (`None` on
    the Well object, not a member here) means "not part of the monitoring
    program" — the default for the overwhelming majority of RRC's well
    inventory. Only set once a Sigil Node (or a field crew) is involved.

    This list is intentionally short and UI-legible (it's what a control
    center like Sigil Watch renders as a status pill) rather than an
    exhaustive state machine — extend it as real operational needs surface,
    it is not required to mirror `RegulatoryStatus`.
    """

    ACTIVE = "active"           # instrumented, producing/normal baseline
    IDLE = "idle"                # instrumented, temporarily not producing
    CAPPED = "capped"            # instrumented, mechanically sealed, not abandoned
    ABANDONED = "abandoned"      # instrumented, plugged & abandoned, monitored for liability
    INSPECTION = "inspection"    # field crew currently on site
    LEAKING = "leaking"          # sensor thresholds indicate an active leak
    OFFLINE = "offline"          # was instrumented, node not reporting


class DataSourceType(str, Enum):
    """Where a given Well record or MetricReading came from — provenance,
    used for audit trails and to decide re-ingestion strategy per source.
    """

    RRC_ARCGIS_REST = "rrc_arcgis_rest"
    RRC_BULK_DOWNLOAD = "rrc_bulk_download"
    RRC_ORPHAN_LAYER = "rrc_orphan_layer"
    SIGIL_NODE = "sigil_node"
    MANUAL_ENTRY = "manual_entry"
    FIELD_CREW = "field_crew"


class IngestionRunStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class SensorDeviceStatus(str, Enum):
    ACTIVE = "active"
    OFFLINE = "offline"
    RETIRED = "retired"
