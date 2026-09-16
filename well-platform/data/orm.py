"""SQLAlchemy 2.0 ORM models — the PostgreSQL/PostGIS persistence schema.

Deliberately not a 1:1 mirror of domain/models.py field-for-field (e.g.
County/Operator are denormalized onto Well here as plain columns rather than
foreign keys — see docs/ARCHITECTURE.md "Why counties/operators aren't
normalized tables"). Repositories in data/repositories/ own the translation
between this schema and the domain models.

To generate schema.sql from this file: `python3 scripts/generate_schema_sql.py`
"""

from __future__ import annotations

import uuid
from datetime import datetime

from geoalchemy2 import Geometry
from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class WellORM(Base):
    """One row per RRC API number. This is the big table — statewide,
    ~1.4M+ rows across all historical statuses (see ../texas-rrc-wells/README.md
    for the live counts pulled during research). Only a tiny fraction will
    ever have `operational_status` set.
    """

    __tablename__ = "wells"

    id: Mapped[uuid.UUID] = _uuid_pk()

    # Wider than a real RRC API number (max ~14 chars) to fit the synthetic
    # "<stub>#<objectid>" form used when RRC's own data has no well-specific
    # identifier for this row — see api_number_is_synthetic below and
    # docs/ARCHITECTURE.md "The blank-GIS_API5 problem."
    api_number: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    api_number_is_synthetic: Mapped[bool] = mapped_column(default=False)
    # OBJECTID of whichever raw RRC row was chosen as canonical for this well
    # (see domain/dedup.py WellCluster.canonical) — not necessarily the only
    # raw row that fed this well; see source_feature_count/identity_method.
    rrc_object_id: Mapped[int | None] = mapped_column(Integer)
    # How this well's identity was resolved (domain/dedup.py WellCluster.method:
    # "reliable_api" | "spatial" | "unclustered_no_location") and how many raw
    # RRC rows were merged into it — surfaced for auditability, e.g. "this well
    # came from clustering 3 unidentified legacy pins" is operationally
    # meaningful, not just a debugging detail.
    identity_method: Mapped[str] = mapped_column(String(24), default="reliable_api")
    source_feature_count: Mapped[int] = mapped_column(Integer, default=1)
    well_number: Mapped[str | None] = mapped_column(String(16))
    lease_name: Mapped[str | None] = mapped_column(String(128))

    # Both a geometry column (for spatial predicates/indexing) and plain
    # float columns (for cheap reads when a caller just wants lat/lon for
    # display and doesn't want to pay for ST_X/ST_Y extraction or a
    # geometry-aware client library). Kept in sync by the repository layer.
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    elevation_ft: Mapped[float | None] = mapped_column(Float)
    # spatial_index=False: GeoAlchemy2 would otherwise auto-create its own
    # GiST index (named idx_wells_geom) in addition to the explicitly named
    # one below — we only want one.
    geom = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=False), nullable=False
    )

    county_fips: Mapped[str | None] = mapped_column(String(3), index=True)
    county_name: Mapped[str | None] = mapped_column(String(64))
    operator_name: Mapped[str | None] = mapped_column(String(128), index=True)
    rrc_operator_number: Mapped[str | None] = mapped_column(String(16))

    regulatory_status: Mapped[str] = mapped_column(String(40), index=True)
    regulatory_status_raw: Mapped[str | None] = mapped_column(String(64))
    is_orphaned: Mapped[bool] = mapped_column(default=False, index=True)

    # No plain index here — the partial index in __table_args__ below covers
    # the actual query pattern (rows where this is set) without wasting
    # index space entering ~1.4M NULL rows for the common case.
    operational_status: Mapped[str | None] = mapped_column(String(20))

    location_reliability_code: Mapped[str | None] = mapped_column(String(4))
    location_source: Mapped[str | None] = mapped_column(String(128))

    data_source: Mapped[str] = mapped_column(String(32))
    source_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    sensor_devices: Mapped[list["SensorDeviceORM"]] = relationship(back_populates="well")
    metric_readings: Mapped[list["MetricReadingORM"]] = relationship(back_populates="well")

    __table_args__ = (
        Index("ix_wells_geom", "geom", postgresql_using="gist"),
        # Partial index: the monitoring-program query path ("give me my
        # instrumented wells") is common and only ever touches a tiny
        # fraction of this otherwise-huge table.
        Index(
            "ix_wells_operational_status_not_null",
            "operational_status",
            postgresql_where=(operational_status.isnot(None)),
        ),
    )


class SensorDeviceORM(Base):
    __tablename__ = "sensor_devices"

    id: Mapped[uuid.UUID] = _uuid_pk()
    well_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wells.id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str] = mapped_column(String(64), unique=True)
    device_type: Mapped[str] = mapped_column(String(32), default="sigil_node")
    status: Mapped[str] = mapped_column(String(16), default="active")
    installed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    well: Mapped["WellORM"] = relationship(back_populates="sensor_devices")


class MetricReadingORM(Base):
    """Narrow (EAV-style) time-series table: one row per (well, metric,
    time). See docs/ARCHITECTURE.md "Why metrics aren't an enum" for why
    this is narrow rather than wide fixed columns per pollutant, and for
    the TimescaleDB upgrade path if/when ingest volume warrants it.
    """

    __tablename__ = "metric_readings"

    id: Mapped[uuid.UUID] = _uuid_pk()
    well_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wells.id", ondelete="CASCADE"))
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sensor_devices.id", ondelete="SET NULL")
    )
    metric_code: Mapped[str] = mapped_column(String(32))
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(16))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    well: Mapped["WellORM"] = relationship(back_populates="metric_readings")

    __table_args__ = (
        # The dominant query pattern is "latest N readings for well X,
        # metric Y" and "history for well X, metric Y, time range" — this
        # composite index covers both.
        Index("ix_metric_readings_well_metric_time", "well_id", "metric_code", "recorded_at"),
    )


class MetricThresholdORM(Base):
    __tablename__ = "metric_thresholds"

    metric_code: Mapped[str] = mapped_column(String(32), primary_key=True)
    unit: Mapped[str] = mapped_column(String(16))
    warning_value: Mapped[float | None] = mapped_column(Float)
    critical_value: Mapped[float | None] = mapped_column(Float)


class IngestionRunORM(Base):
    __tablename__ = "ingestion_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    source: Mapped[str] = mapped_column(String(32))
    scope_description: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(16), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    records_fetched: Mapped[int] = mapped_column(Integer, default=0)
    records_upserted: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)


class RawWellFeatureORM(Base):
    """Landing zone: one row per RRC GIS feature ever fetched, as close to
    verbatim as the field mapping allows. Never interprets, merges, or
    drops anything — see docs/ARCHITECTURE.md "Raw-then-curate ingestion."

    Keyed on (source_layer, rrc_object_id) — RRC's own per-row feature ID,
    which is always genuinely unique (unlike `API`, see
    domain/dedup.py/"The blank-GIS_API5 problem") — so landing raw data can
    never fail on a conflict regardless of how messy RRC's own identifiers
    are. All dedup/identity-resolution logic lives downstream, in
    services/curation_service.py, entirely replayable from this table
    without re-hitting RRC.
    """

    __tablename__ = "raw_well_features"

    id: Mapped[uuid.UUID] = _uuid_pk()

    source_layer: Mapped[str] = mapped_column(String(20))  # "well_locations" | "orphan_wells"
    rrc_object_id: Mapped[int] = mapped_column(Integer)
    ingestion_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ingestion_runs.id", ondelete="SET NULL")
    )
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    raw_api: Mapped[str] = mapped_column(String(20))
    gis_api5: Mapped[str | None] = mapped_column(String(8))
    well_number: Mapped[str | None] = mapped_column(String(16))
    symnum: Mapped[int | None] = mapped_column(Integer)
    symbol_description: Mapped[str | None] = mapped_column(String(64))
    reliab: Mapped[str | None] = mapped_column(String(4))
    location_source: Mapped[str | None] = mapped_column(String(128))

    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    latitude_nad27: Mapped[float | None] = mapped_column(Float)
    longitude_nad27: Mapped[float | None] = mapped_column(Float)

    # The layer itself carries no county attribute (see
    # ../texas-rrc-wells/README.md §2) — this is the county we queried
    # under at fetch time, a hint for curation, not asserted RRC truth.
    county_name_hint: Mapped[str | None] = mapped_column(String(64), index=True)
    county_fips_hint: Mapped[str | None] = mapped_column(String(3))
    is_orphaned_hint: Mapped[bool] = mapped_column(default=False)

    # Set once curation resolves this row into a well. NULL means
    # "landed but not yet curated" — the query that drives
    # WellCurationService's backlog.
    well_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("wells.id", ondelete="SET NULL"))
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "source_layer", "rrc_object_id", name="uq_raw_well_features_source_objectid"
        ),
        Index("ix_raw_well_features_well_id", "well_id"),
        Index(
            "ix_raw_well_features_unpromoted",
            "county_name_hint",
            postgresql_where=(well_id.is_(None)),
        ),
    )
