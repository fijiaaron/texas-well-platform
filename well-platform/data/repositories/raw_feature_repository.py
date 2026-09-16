"""Persistence for the raw landing zone (raw_well_features). Deliberately
lower-level than WellRepository/MetricReadingRepository: it speaks RRC's
raw attribute dicts and ORM rows directly rather than domain.Well, because
its only job is "land what RRC said, safely, always" — interpretation is
services/curation_service.py's job, not this repository's.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..orm import RawWellFeatureORM


class RawFeatureRepository:
    def __init__(self, session: Session):
        self.session = session

    def upsert_batch(
        self,
        rows: list[dict],
        source_layer: str,
        ingestion_run_id: uuid.UUID | None,
        county_name_hint: str | None,
        county_fips_hint: str | None = None,
    ) -> int:
        """`rows` are raw RRC attribute dicts (OBJECTID, API, GIS_API5, ...)
        as returned by RRCWellClient — landed close to verbatim. Upserts on
        (source_layer, rrc_object_id), RRC's own always-unique per-row key,
        so this can never fail the way api_number-keyed upserts could (see
        docs/ARCHITECTURE.md "The blank-GIS_API5 problem").
        """
        if not rows:
            return 0

        values = []
        for r in rows:
            values.append(
                {
                    "id": uuid.uuid4(),
                    "source_layer": source_layer,
                    "rrc_object_id": r["OBJECTID"],
                    "ingestion_run_id": ingestion_run_id,
                    "raw_api": r.get("API"),
                    "gis_api5": r.get("GIS_API5"),
                    "well_number": r.get("GIS_WELL_NUMBER"),
                    "symnum": r.get("SYMNUM"),
                    "symbol_description": r.get("GIS_SYMBOL_DESCRIPTION"),
                    "reliab": r.get("RELIAB"),
                    "location_source": r.get("GIS_LOCATION_SOURCE"),
                    "latitude": r.get("GIS_LAT83"),
                    "longitude": r.get("GIS_LONG83"),
                    "latitude_nad27": r.get("GIS_LAT27"),
                    "longitude_nad27": r.get("GIS_LONG27"),
                    "county_name_hint": county_name_hint,
                    "county_fips_hint": county_fips_hint,
                }
            )

        stmt = pg_insert(RawWellFeatureORM).values(values)
        update_cols = {
            c.name: getattr(stmt.excluded, c.name)
            for c in RawWellFeatureORM.__table__.columns
            if c.name not in ("id", "well_id", "promoted_at")
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=["source_layer", "rrc_object_id"], set_=update_cols
        )
        self.session.execute(stmt)
        return len(values)

    def mark_orphaned(self, source_layer: str, api_numbers: set[str]) -> None:
        if not api_numbers:
            return
        self.session.execute(
            update(RawWellFeatureORM)
            .where(
                RawWellFeatureORM.source_layer == source_layer,
                RawWellFeatureORM.raw_api.in_(api_numbers),
            )
            .values(is_orphaned_hint=True)
        )

    def get_for_county(
        self, county_name: str, source_layer: str = "well_locations", only_unpromoted: bool = False
    ) -> list[RawWellFeatureORM]:
        # Case-insensitive: RRC's own COUNTY_NAME comes back upper-cased
        # (landed verbatim as the hint), but callers naturally type
        # "Tarrant"/"Real" — same convention WellRepository.search and
        # RRCWellClient.get_county_polygon already use for this reason.
        stmt = select(RawWellFeatureORM).where(
            func.upper(RawWellFeatureORM.county_name_hint) == county_name.upper(),
            RawWellFeatureORM.source_layer == source_layer,
        )
        if only_unpromoted:
            stmt = stmt.where(RawWellFeatureORM.well_id.is_(None))
        return list(self.session.execute(stmt).scalars().all())

    def mark_promoted(self, raw_row_ids: list[uuid.UUID], well_id: uuid.UUID) -> None:
        if not raw_row_ids:
            return
        self.session.execute(
            update(RawWellFeatureORM)
            .where(RawWellFeatureORM.id.in_(raw_row_ids))
            .values(well_id=well_id, promoted_at=datetime.now(timezone.utc))
        )
