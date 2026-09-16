"""Business logic for sensor/metric readings: recording them, reading them
back, and deciding when a reading means a well should flip to LEAKING.

Threshold breach -> OperationalStatus.LEAKING is intentionally the one place
a *reading* is allowed to change a well's operational status automatically
(everything else in well_service.py is an explicit human/ingestion action).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from domain.enums import OperationalStatus
from domain.models import MetricReading

from data.repositories.reading_repository import MetricReadingRepository
from data.repositories.well_repository import WellRepository


@dataclass
class ThresholdAlert:
    well_id: uuid.UUID
    metric_code: str
    value: float
    level: Literal["warning", "critical"]


class SensorReadingService:
    def __init__(
        self,
        reading_repository: MetricReadingRepository,
        well_repository: WellRepository,
    ):
        self.reading_repository = reading_repository
        self.well_repository = well_repository

    def record_readings(
        self,
        well_id: uuid.UUID,
        readings: list[MetricReading],
        device_id: uuid.UUID | None = None,
    ) -> list[ThresholdAlert]:
        """Inserts a batch of readings (all for the same well; `device_id`
        is applied to any reading that doesn't already carry one — e.g. a
        Sigil Node reporting multiple pollutants in one payload) and
        evaluates them against configured thresholds. A critical breach
        flips the well to LEAKING; a resolved reading does NOT
        auto-clear it back to ACTIVE — that's a human/field-crew call
        (see well_service.update_operational_status), on purpose, so a
        transient sensor dip can't quietly wave off a real alert.
        """
        for r in readings:
            if r.device_id is None:
                r.device_id = device_id

        self.reading_repository.insert_batch(readings)

        alerts = self._evaluate_thresholds(readings)
        if any(a.level == "critical" for a in alerts):
            well = self.well_repository.get_by_id(well_id)
            if well and well.is_monitored and well.operational_status != OperationalStatus.LEAKING:
                self.well_repository.set_operational_status(well_id, OperationalStatus.LEAKING)

        return alerts

    def get_latest_snapshot(
        self, well_id: uuid.UUID, metric_codes: list[str] | None = None
    ) -> dict[str, MetricReading]:
        readings = self.reading_repository.latest_for_well(well_id, metric_codes)
        return {r.metric_code: r for r in readings}

    def get_history(
        self, well_id: uuid.UUID, metric_code: str, since: datetime, until: datetime | None = None
    ) -> list[MetricReading]:
        return self.reading_repository.history(well_id, metric_code, since, until)

    def _evaluate_thresholds(self, readings: list[MetricReading]) -> list[ThresholdAlert]:
        alerts: list[ThresholdAlert] = []
        for r in readings:
            threshold = self.reading_repository.get_threshold(r.metric_code)
            if threshold is None:
                continue
            if threshold.critical_value is not None and r.value >= threshold.critical_value:
                alerts.append(
                    ThresholdAlert(well_id=r.well_id, metric_code=r.metric_code, value=r.value, level="critical")
                )
            elif threshold.warning_value is not None and r.value >= threshold.warning_value:
                alerts.append(
                    ThresholdAlert(well_id=r.well_id, metric_code=r.metric_code, value=r.value, level="warning")
                )
        return alerts
