"""Persistence + query boundary for Well. Owns all translation between
domain.models.Well (what the rest of the app works with) and orm.WellORM
(what's actually in Postgres), and is the only place PostGIS-specific SQL
(ST_MakeEnvelope, ST_Within, ...) should appear.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from geoalchemy2.shape import to_shape
from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from domain.enums import DataSourceType, OperationalStatus, RegulatoryStatus
from domain.models import County, GeoPoint, Operator, Well, WellSearchQuery, WellSearchResult

from ..orm import WellORM


@dataclass
class UpsertSummary:
    inserted: int
    updated: int
    # DB-assigned id per api_number, from the upsert's RETURNING clause —
    # for a row that already existed, this is the *pre-existing* row's id,
    # not the fresh uuid4() the caller's Well object happened to carry (ON
    # CONFLICT DO UPDATE never touches id). Callers that need to link
    # something else to the resulting well (see
    # services/curation_service.py linking raw rows via well_id) must use
    # this, not well.id, or they'll link against an id nothing in the
    # database actually has.
    ids_by_api_number: dict[str, uuid.UUID] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.inserted + self.updated


_CANDIDATE_STATUSES = [s for s in RegulatoryStatus if s.is_plugged_or_abandoned]


class WellRepository:
    def __init__(self, session: Session):
        self.session = session

    # ---- reads --------------------------------------------------------

    def search_monitoring_candidates(
        self, county_name: str | None = None, limit: int = 100, offset: int = 0
    ) -> WellSearchResult:
        """Wells not currently in the monitoring program (`operational_status`
        unset) whose RRC status makes them the kind of well the Texas
        abandoned-well program cares about: RRC's own Orphan Wells layer, or
        RegulatoryStatus.is_plugged_or_abandoned. This is an OR condition on
        top of an AND, which WellSearchQuery's flat filter list can't express
        — hence its own method rather than a WellSearchQuery combination.
        """
        candidate_cond = or_(
            WellORM.is_orphaned.is_(True),
            WellORM.regulatory_status.in_([s.value for s in _CANDIDATE_STATUSES]),
        )
        conditions = [WellORM.operational_status.is_(None), candidate_cond]
        if county_name:
            conditions.append(func.upper(WellORM.county_name) == county_name.upper())

        base_stmt = select(WellORM).where(and_(*conditions))
        total = self.session.execute(
            select(func.count()).select_from(base_stmt.subquery())
        ).scalar_one()
        page_stmt = base_stmt.order_by(WellORM.api_number).limit(limit).offset(offset)
        rows = self.session.execute(page_stmt).scalars().all()

        return WellSearchResult(
            wells=[self._to_domain(r) for r in rows], total_count=total, limit=limit, offset=offset
        )

    def get_by_api_number(self, api_number: str) -> Well | None:
        row = self.session.execute(
            select(WellORM).where(WellORM.api_number == api_number)
        ).scalar_one_or_none()
        return self._to_domain(row) if row else None

    def get_by_id(self, well_id: uuid.UUID) -> Well | None:
        row = self.session.get(WellORM, well_id)
        return self._to_domain(row) if row else None

    def search(self, query: WellSearchQuery) -> WellSearchResult:
        conditions = []

        if query.bbox:
            min_lon, min_lat, max_lon, max_lat = query.bbox
            envelope = func.ST_MakeEnvelope(min_lon, min_lat, max_lon, max_lat, 4326)
            conditions.append(func.ST_Within(WellORM.geom, envelope))

        if query.county_fips:
            conditions.append(WellORM.county_fips == query.county_fips)
        if query.county_name:
            conditions.append(func.upper(WellORM.county_name) == query.county_name.upper())
        if query.operator_name:
            conditions.append(WellORM.operator_name.ilike(f"%{query.operator_name}%"))
        if query.regulatory_statuses:
            conditions.append(
                WellORM.regulatory_status.in_([s.value for s in query.regulatory_statuses])
            )
        if query.operational_statuses:
            conditions.append(
                WellORM.operational_status.in_([s.value for s in query.operational_statuses])
            )
        if query.monitored_only:
            conditions.append(WellORM.operational_status.isnot(None))
        if query.orphaned_only:
            conditions.append(WellORM.is_orphaned.is_(True))
        if query.text:
            like = f"%{query.text}%"
            conditions.append(
                or_(
                    WellORM.api_number.ilike(like),
                    WellORM.well_number.ilike(like),
                    WellORM.lease_name.ilike(like),
                )
            )

        base_stmt = select(WellORM)
        if conditions:
            base_stmt = base_stmt.where(and_(*conditions))

        total = self.session.execute(
            select(func.count()).select_from(base_stmt.subquery())
        ).scalar_one()

        page_stmt = base_stmt.order_by(WellORM.api_number).limit(query.limit).offset(query.offset)
        rows = self.session.execute(page_stmt).scalars().all()

        return WellSearchResult(
            wells=[self._to_domain(r) for r in rows],
            total_count=total,
            limit=query.limit,
            offset=query.offset,
        )

    # ---- writes ---------------------------------------------------------

    def upsert_many(self, wells: list[Well]) -> UpsertSummary:
        """Bulk upsert keyed on api_number — the standard ingestion path.
        A single INSERT ... ON CONFLICT statement per batch rather than N
        round trips; callers (ingestion_service) are expected to batch in
        the low thousands (matches RRC's own 1000-record page size).
        """
        if not wells:
            return UpsertSummary(inserted=0, updated=0)

        # RRC's own GIS export can legitimately carry more than one location
        # row under the same real API number — e.g. a "Permitted Location"
        # pin and a later "Dry Hole" pin for the same wellbore, RRC updating
        # its own record over time. That's a genuine same-well merge, and a
        # single multi-row ON CONFLICT statement can't update the same
        # target row twice, so dedupe within the batch first, keeping the
        # last (i.e. most-recently-fetched-from-RRC) record. Rows where RRC
        # never assigned a well-specific identifier at all get a synthetic,
        # per-row-unique api_number upstream (ingestion_service.py) rather
        # than reaching this dedupe as false collisions — see
        # docs/ARCHITECTURE.md "The blank-GIS_API5 problem."
        deduped: dict[str, Well] = {}
        for well in wells:
            deduped[well.api_number] = well
        wells = list(deduped.values())

        existing_apis = set(
            self.session.execute(
                select(WellORM.api_number).where(
                    WellORM.api_number.in_([w.api_number for w in wells])
                )
            )
            .scalars()
            .all()
        )

        rows = [self._to_row_values(w) for w in wells]
        stmt = pg_insert(WellORM).values(rows)
        update_cols = {
            c.name: getattr(stmt.excluded, c.name)
            for c in WellORM.__table__.columns
            if c.name not in ("id", "created_at")
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=["api_number"], set_=update_cols
        ).returning(WellORM.id, WellORM.api_number)
        result = self.session.execute(stmt)
        ids_by_api_number = {api_number: row_id for row_id, api_number in result.all()}

        inserted = sum(1 for w in wells if w.api_number not in existing_apis)
        return UpsertSummary(
            inserted=inserted, updated=len(wells) - inserted, ids_by_api_number=ids_by_api_number
        )

    def set_operational_status(
        self, well_id: uuid.UUID, status: OperationalStatus | None
    ) -> None:
        row = self.session.get(WellORM, well_id)
        if row is None:
            raise KeyError(f"no well with id {well_id}")
        row.operational_status = status.value if status else None

    # ---- translation ----------------------------------------------------

    def _to_row_values(self, well: Well) -> dict:
        point_wkt = f"SRID=4326;POINT({well.location.longitude} {well.location.latitude})"
        return {
            "id": well.id,
            "api_number": well.api_number,
            "api_number_is_synthetic": well.api_number_is_synthetic,
            "rrc_object_id": well.rrc_object_id,
            "identity_method": well.identity_method,
            "source_feature_count": well.source_feature_count,
            "well_number": well.well_number,
            "lease_name": well.lease_name,
            "latitude": well.location.latitude,
            "longitude": well.location.longitude,
            "elevation_ft": well.location.elevation_ft,
            "geom": point_wkt,
            "county_fips": well.county.fips if well.county else None,
            "county_name": well.county.name if well.county else None,
            "operator_name": well.operator.name if well.operator else None,
            "rrc_operator_number": well.operator.rrc_operator_number if well.operator else None,
            "regulatory_status": well.regulatory_status.value,
            "regulatory_status_raw": well.regulatory_status_raw,
            "is_orphaned": well.is_orphaned,
            "operational_status": (
                well.operational_status.value if well.operational_status else None
            ),
            "location_reliability_code": well.location_reliability_code,
            "location_source": well.location_source,
            "data_source": well.data_source.value,
            "source_synced_at": well.source_synced_at,
            "updated_at": datetime.now(timezone.utc),
        }

    def _to_domain(self, row: WellORM) -> Well:
        county = (
            County(fips=row.county_fips, name=row.county_name)
            if row.county_fips or row.county_name
            else None
        )
        operator = (
            Operator(rrc_operator_number=row.rrc_operator_number, name=row.operator_name)
            if row.operator_name
            else None
        )
        point = to_shape(row.geom)
        return Well(
            id=row.id,
            api_number=row.api_number,
            api_number_is_synthetic=row.api_number_is_synthetic,
            rrc_object_id=row.rrc_object_id,
            identity_method=row.identity_method,
            source_feature_count=row.source_feature_count,
            well_number=row.well_number,
            lease_name=row.lease_name,
            location=GeoPoint(
                latitude=row.latitude if row.latitude is not None else point.y,
                longitude=row.longitude if row.longitude is not None else point.x,
                elevation_ft=row.elevation_ft,
            ),
            county=county,
            operator=operator,
            regulatory_status=RegulatoryStatus(row.regulatory_status),
            regulatory_status_raw=row.regulatory_status_raw,
            is_orphaned=row.is_orphaned,
            operational_status=(
                OperationalStatus(row.operational_status) if row.operational_status else None
            ),
            location_reliability_code=row.location_reliability_code,
            location_source=row.location_source,
            data_source=DataSourceType(row.data_source),
            source_synced_at=row.source_synced_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
