#!/usr/bin/env python3
"""CLI entry point for the two-stage ingestion pipeline against DATABASE_URL:
land raw RRC rows, then curate (dedupe/enrich) them into canonical wells.
See docs/ARCHITECTURE.md "Raw-then-curate ingestion."

Usage:
    DATABASE_URL=postgresql+psycopg://... python3 scripts/ingest.py --county Tarrant
    DATABASE_URL=postgresql+psycopg://... python3 scripts/ingest.py --api 43934308
    DATABASE_URL=postgresql+psycopg://... python3 scripts/ingest.py --county Tarrant --land-only
    DATABASE_URL=postgresql+psycopg://... python3 scripts/ingest.py --curate-only --county Tarrant
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.repositories.ingestion_run_repository import IngestionRunRepository
from data.repositories.raw_feature_repository import RawFeatureRepository
from data.repositories.well_repository import WellRepository
from data.session import session_scope
from services.curation_service import WellCurationService
from services.ingestion_service import RawIngestionService


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--county", help="Texas county name, e.g. Tarrant")
    group.add_argument("--api", help="Single RRC API number, e.g. 43934308")
    parser.add_argument(
        "--land-only", action="store_true", help="Only fetch+land raw rows, skip curation"
    )
    parser.add_argument(
        "--curate-only", action="store_true", help="Only curate already-landed raw rows for --county, skip fetching"
    )
    args = parser.parse_args()

    if args.curate_only and not args.county:
        parser.error("--curate-only requires --county")

    with session_scope() as session:
        raw_repo = RawFeatureRepository(session)
        well_repo = WellRepository(session)
        run_repo = IngestionRunRepository(session)

        if not args.curate_only:
            ingestion = RawIngestionService(raw_repo, run_repo)
            if args.county:
                run = ingestion.ingest_county(args.county)
            else:
                run = ingestion.ingest_single(args.api)
            print(
                f"landed: {run.status.value} fetched={run.records_fetched} "
                f"upserted={run.records_upserted} duration={(run.completed_at - run.started_at)}"
            )

        if args.land_only:
            return

        county_name = args.county
        curation = WellCurationService(raw_repo, well_repo)
        if county_name:
            summary = curation.promote_county(county_name)
        else:
            # No county given (a plain --api run): look up the county hint
            # the ingestion step just wrote, then curate that scope.
            from sqlalchemy import select
            from data.orm import RawWellFeatureORM

            hint = session.execute(
                select(RawWellFeatureORM.county_name_hint)
                .where(RawWellFeatureORM.raw_api == args.api)
                .limit(1)
            ).scalar_one_or_none()
            summary = curation.promote_county(hint) if hint else None

        if summary is None:
            print("curation: no county resolved, nothing to curate")
            return

        print(
            f"curated: raw_rows={summary.raw_rows_considered} wells_upserted={summary.wells_upserted} "
            f"by_method={summary.clusters_by_method} multi_source_clusters={summary.multi_source_clusters} "
            f"issues={len(summary.issues)}"
        )
        for issue in summary.issues[:10]:
            print(f"  issue: {issue.reason} (rrc_object_ids={issue.rrc_object_ids})")


if __name__ == "__main__":
    main()
