"""Domain (framework-agnostic) models.

These are what the service layer works with and what an eventual API layer
would serialize directly — not tied to SQLAlchemy, PostgreSQL, or any single
persistence choice. The data layer (data/orm.py + data/repositories/) is
responsible for converting to/from these; nothing here imports SQLAlchemy.

Built with pydantic so validation is enforced at construction (bad lat/lon,
wrong enum value, etc. fail loudly at the ingestion boundary rather than
silently corrupting the database) and so these models are directly usable
as request/response schemas by a future API framework without a second
translation layer.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .enums import (
    DataSourceType,
    IngestionRunStatus,
    OperationalStatus,
    RegulatoryStatus,
    SensorDeviceStatus,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class GeoPoint(BaseModel):
    """WGS84 (NAD83-equivalent for our purposes) coordinate. Elevation is
    optional — RRC doesn't reliably provide it; may be backfilled later
    from a DEM lookup.
    """

    model_config = ConfigDict(frozen=True)

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    elevation_ft: Optional[float] = None

    @field_validator("longitude")
    @classmethod
    def _sanity_check_texas_ish(cls, v: float) -> float:
        # Not a hard domain rule (we may one day have non-Texas wells) —
        # just catches the single most common ingestion bug: swapped
        # lat/lon, which for Texas data produces a wildly out-of-range
        # longitude. Loud failure beats a silently mislocated well.
        if not (-107 <= v <= -93):
            raise ValueError(
                f"longitude {v} is outside Texas' rough bounding box "
                "(-107 to -93) — check for a swapped lat/lon"
            )
        return v


class County(BaseModel):
    """Mirrors RRC's own Counties layer (fips + name) — see
    ../texas-rrc-wells/README.md section 2. Not user-maintained data.
    """

    # fips optional: curation (services/curation_service.py) can resolve a
    # well to a county name without a FIPS code in rare cases where the raw
    # row's county_fips_hint wasn't captured — better than faking one.
    fips: Optional[str] = None
    name: str
    rrc_district: Optional[str] = None


class Operator(BaseModel):
    rrc_operator_number: Optional[str] = None
    name: str


class Well(BaseModel):
    """The core entity. One row per RRC-tracked wellbore (by API number),
    whether or not Sigilint has ever touched it. `operational_status`
    being `None` is the expected, common case — it means "in RRC's
    inventory, not part of our monitoring program."
    """

    model_config = ConfigDict(use_enum_values=False)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    api_number: str = Field(min_length=1, max_length=24)
    api_number_is_synthetic: bool = Field(
        default=False,
        description="True when RRC's own GIS row had no well-specific identifier "
        "(GIS_API5 blank — almost always an old 'Commission's hardcopy map' record) "
        "and api_number was synthesized as '<raw RRC stub>#<RRC OBJECTID>' so this "
        "row isn't falsely merged with unrelated wells that share the same stub. "
        "See docs/ARCHITECTURE.md 'The blank-GIS_API5 problem.' Never true for a "
        "real, RRC-assigned API number.",
    )
    rrc_object_id: Optional[int] = Field(
        default=None,
        description="RRC's own OBJECTID for whichever raw row was chosen as canonical "
        "(domain/dedup.py WellCluster.canonical) — see identity_method/source_feature_count "
        "for whether more than one raw row fed this well",
    )
    identity_method: str = Field(
        default="reliable_api",
        description='How this well\'s identity was resolved: "reliable_api" (RRC\'s own '
        'API number trusted directly), "spatial" (RRC gave no reliable identifier; '
        "resolved by clustering nearby raw rows — see domain/dedup.py), or "
        '"unclustered_no_location" (no reliable API and no usable coordinates either)',
    )
    source_feature_count: int = Field(
        default=1, ge=1, description="How many raw RRC GIS rows were merged into this well"
    )
    well_number: Optional[str] = None
    lease_name: Optional[str] = None

    location: GeoPoint
    county: Optional[County] = None
    operator: Optional[Operator] = None

    regulatory_status: RegulatoryStatus
    regulatory_status_raw: Optional[str] = Field(
        default=None,
        description="RRC's exact GIS_SYMBOL_DESCRIPTION text, preserved verbatim",
    )
    is_orphaned: bool = Field(
        default=False,
        description="From RRC's separate Orphan Wells layer — a stronger, "
        "narrower signal than regulatory_status alone (see domain/enums.py)",
    )

    operational_status: Optional[OperationalStatus] = Field(
        default=None,
        description="Unset unless this well is enrolled in Sigilint's monitoring program",
    )

    location_reliability_code: Optional[str] = None
    location_source: Optional[str] = None

    data_source: DataSourceType = DataSourceType.RRC_ARCGIS_REST
    source_synced_at: datetime = Field(default_factory=_utcnow)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    @property
    def is_monitored(self) -> bool:
        return self.operational_status is not None

    @property
    def is_monitoring_candidate(self) -> bool:
        """Not yet instrumented, but regulatory status makes it the kind of
        well Sigilint's Texas program cares about (POC-03: abandoned-well
        methane/H2S monitoring). Used by WellService.list_monitoring_candidates.
        """
        return not self.is_monitored and (
            self.is_orphaned or self.regulatory_status.is_plugged_or_abandoned
        )


class SensorDevice(BaseModel):
    """A physical Sigil Node deployed at a well. A well can, in principle,
    have more than one device over its lifetime (replacement/upgrade), so
    this is a separate entity rather than fields on Well.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    well_id: uuid.UUID
    external_id: str = Field(description="Physical device serial / hardware ID")
    device_type: str = "sigil_node"
    status: SensorDeviceStatus = SensorDeviceStatus.ACTIVE
    installed_at: datetime = Field(default_factory=_utcnow)
    last_seen_at: Optional[datetime] = None


class MetricReading(BaseModel):
    """One (metric, value, time) sample from a well — a sensor reading, a
    manually logged pump rate, whatever. `metric_code` is a free-form
    string rather than a closed enum on purpose (see docs/ARCHITECTURE.md
    "Why metrics aren't an enum") — new sensor payload fields shouldn't
    require a schema migration to record.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    well_id: uuid.UUID
    device_id: Optional[uuid.UUID] = None
    metric_code: str = Field(min_length=1, max_length=32)
    value: float
    unit: str
    recorded_at: datetime
    ingested_at: datetime = Field(default_factory=_utcnow)


class MetricThreshold(BaseModel):
    """Config, not a reading: the warning/critical bar for a given metric,
    used by SensorReadingService to decide when a well flips to LEAKING.
    """

    metric_code: str
    unit: str
    warning_value: Optional[float] = None
    critical_value: Optional[float] = None


class IngestionRun(BaseModel):
    """Audit record for one ingestion pass, so "when did we last sync
    Tarrant County" and "did the last statewide sync actually finish" are
    answerable without grepping logs.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    source: DataSourceType
    scope_description: str = Field(description='e.g. "county:Tarrant" or "api:43934308"')
    status: IngestionRunStatus = IngestionRunStatus.RUNNING
    started_at: datetime = Field(default_factory=_utcnow)
    completed_at: Optional[datetime] = None
    records_fetched: int = 0
    records_upserted: int = 0
    error_message: Optional[str] = None


class WellSearchQuery(BaseModel):
    """Structures a search/browse request against WellRepository.search —
    this is the shape a future API's `GET /wells` query params map onto.
    All filters are optional and AND-combined.
    """

    bbox: Optional[tuple[float, float, float, float]] = Field(
        default=None, description="(min_lon, min_lat, max_lon, max_lat)"
    )
    county_fips: Optional[str] = None
    county_name: Optional[str] = None
    operator_name: Optional[str] = None
    regulatory_statuses: Optional[list[RegulatoryStatus]] = None
    operational_statuses: Optional[list[OperationalStatus]] = None
    monitored_only: bool = False
    orphaned_only: bool = False
    text: Optional[str] = Field(
        default=None, description="Matches API number, well number, or lease name"
    )
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


class WellSearchResult(BaseModel):
    wells: list[Well]
    total_count: int
    limit: int
    offset: int
