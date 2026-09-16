"""Persistence for IngestionRun. Small and split out on its own because it
has a real ordering dependency: a run row must exist before raw features
can reference it via `raw_well_features.ingestion_run_id` (see
RawIngestionService — create() is called before landing any rows, update()
in a finally block once the run finishes or fails).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from domain.models import IngestionRun

from ..orm import IngestionRunORM


class IngestionRunRepository:
    def __init__(self, session: Session):
        self.session = session

    def create(self, run: IngestionRun) -> None:
        self.session.add(
            IngestionRunORM(
                id=run.id,
                source=run.source.value,
                scope_description=run.scope_description,
                status=run.status.value,
                started_at=run.started_at,
                records_fetched=run.records_fetched,
                records_upserted=run.records_upserted,
            )
        )
        self.session.flush()  # so FK references to run.id are valid within the same transaction

    def update(self, run: IngestionRun) -> None:
        row = self.session.get(IngestionRunORM, run.id)
        if row is None:
            raise KeyError(f"no ingestion_runs row for {run.id} — was create() called first?")
        row.status = run.status.value
        row.completed_at = run.completed_at
        row.records_fetched = run.records_fetched
        row.records_upserted = run.records_upserted
        row.error_message = run.error_message
