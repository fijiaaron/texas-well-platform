"""Persistence for MetricReading and MetricThreshold — the sensor
time-series side of the schema, separate from WellRepository since it's a
different access pattern (high-volume inserts, time-range reads) and a
different eventual scaling path (see docs/ARCHITECTURE.md TimescaleDB note).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from domain.models import MetricReading, MetricThreshold

from ..orm import MetricReadingORM, MetricThresholdORM


class MetricReadingRepository:
    def __init__(self, session: Session):
        self.session = session

    def insert_batch(self, readings: list[MetricReading]) -> int:
        if not readings:
            return 0
        rows = [
            {
                "id": r.id,
                "well_id": r.well_id,
                "device_id": r.device_id,
                "metric_code": r.metric_code,
                "value": r.value,
                "unit": r.unit,
                "recorded_at": r.recorded_at,
                "ingested_at": r.ingested_at,
            }
            for r in readings
        ]
        self.session.execute(MetricReadingORM.__table__.insert(), rows)
        return len(rows)

    def latest_for_well(
        self, well_id: uuid.UUID, metric_codes: list[str] | None = None
    ) -> list[MetricReading]:
        """One row per metric_code: the most recent reading of each. Powers
        a well's "current snapshot" view.
        """
        ranked = (
            select(
                MetricReadingORM,
                func.row_number()
                .over(
                    partition_by=MetricReadingORM.metric_code,
                    order_by=MetricReadingORM.recorded_at.desc(),
                )
                .label("rn"),
            )
            .where(MetricReadingORM.well_id == well_id)
        )
        if metric_codes:
            ranked = ranked.where(MetricReadingORM.metric_code.in_(metric_codes))

        subq = ranked.subquery()
        stmt = select(subq).where(subq.c.rn == 1)
        rows = self.session.execute(stmt).all()
        return [self._row_to_domain(r) for r in rows]

    def history(
        self,
        well_id: uuid.UUID,
        metric_code: str,
        since: datetime,
        until: datetime | None = None,
    ) -> list[MetricReading]:
        stmt = select(MetricReadingORM).where(
            MetricReadingORM.well_id == well_id,
            MetricReadingORM.metric_code == metric_code,
            MetricReadingORM.recorded_at >= since,
        )
        if until:
            stmt = stmt.where(MetricReadingORM.recorded_at <= until)
        stmt = stmt.order_by(MetricReadingORM.recorded_at.asc())
        rows = self.session.execute(stmt).scalars().all()
        return [self._to_domain(r) for r in rows]

    def wells_exceeding_threshold(
        self, metric_code: str, level: str = "critical"
    ) -> list[tuple[uuid.UUID, float]]:
        """Latest reading per well for `metric_code`, filtered to those over
        the configured warning/critical threshold. `level` is "warning" or
        "critical". Returns (well_id, value) pairs.
        """
        threshold = self.session.get(MetricThresholdORM, metric_code)
        if threshold is None:
            return []
        bar = threshold.critical_value if level == "critical" else threshold.warning_value
        if bar is None:
            return []

        ranked = (
            select(
                MetricReadingORM.well_id,
                MetricReadingORM.value,
                func.row_number()
                .over(
                    partition_by=MetricReadingORM.well_id,
                    order_by=MetricReadingORM.recorded_at.desc(),
                )
                .label("rn"),
            )
            .where(MetricReadingORM.metric_code == metric_code)
            .subquery()
        )
        stmt = select(ranked.c.well_id, ranked.c.value).where(
            ranked.c.rn == 1, ranked.c.value >= bar
        )
        return [(row.well_id, row.value) for row in self.session.execute(stmt).all()]

    def get_threshold(self, metric_code: str) -> MetricThreshold | None:
        row = self.session.get(MetricThresholdORM, metric_code)
        if row is None:
            return None
        return MetricThreshold(
            metric_code=row.metric_code,
            unit=row.unit,
            warning_value=row.warning_value,
            critical_value=row.critical_value,
        )

    def upsert_threshold(self, threshold: MetricThreshold) -> None:
        stmt = pg_insert(MetricThresholdORM).values(
            metric_code=threshold.metric_code,
            unit=threshold.unit,
            warning_value=threshold.warning_value,
            critical_value=threshold.critical_value,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["metric_code"],
            set_={
                "unit": stmt.excluded.unit,
                "warning_value": stmt.excluded.warning_value,
                "critical_value": stmt.excluded.critical_value,
            },
        )
        self.session.execute(stmt)

    # ---- translation ----------------------------------------------------

    def _to_domain(self, row: MetricReadingORM) -> MetricReading:
        return MetricReading(
            id=row.id,
            well_id=row.well_id,
            device_id=row.device_id,
            metric_code=row.metric_code,
            value=row.value,
            unit=row.unit,
            recorded_at=row.recorded_at,
            ingested_at=row.ingested_at,
        )

    def _row_to_domain(self, row) -> MetricReading:
        return MetricReading(
            id=row.id,
            well_id=row.well_id,
            device_id=row.device_id,
            metric_code=row.metric_code,
            value=row.value,
            unit=row.unit,
            recorded_at=row.recorded_at,
            ingested_at=row.ingested_at,
        )
