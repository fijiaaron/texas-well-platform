"""Stage two of ingestion: turns landed raw_well_features rows into
canonical Well records. This is where identity resolution (domain/dedup.py),
sanity checking, and enrichment happen — deliberately separated from
fetching/landing (services/ingestion_service.py) so a bad RRC surprise
(like the blank-GIS_API5 problem) can be fixed and *replayed* against
already-landed data without re-hitting RRC's server at all.

See docs/ARCHITECTURE.md "Raw-then-curate ingestion" for the rationale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from pydantic import ValidationError

from domain.dedup import RawFeature, WellCluster, resolve_well_identities
from domain.enums import DataSourceType
from domain.models import County, GeoPoint, Well
from domain.status_mapping import normalize_rrc_status

from data.orm import RawWellFeatureORM
from data.repositories.raw_feature_repository import RawFeatureRepository
from data.repositories.well_repository import WellRepository


@dataclass
class CurationIssue:
    rrc_object_ids: list[int]
    reason: str


@dataclass
class CurationSummary:
    county_name: str
    raw_rows_considered: int = 0
    wells_upserted: int = 0
    clusters_by_method: dict[str, int] = field(default_factory=dict)
    multi_source_clusters: int = 0
    issues: list[CurationIssue] = field(default_factory=list)


class WellCurationService:
    def __init__(
        self,
        raw_feature_repository: RawFeatureRepository,
        well_repository: WellRepository,
        spatial_radius_m: float = 50.0,
    ):
        self.raw_feature_repository = raw_feature_repository
        self.well_repository = well_repository
        self.spatial_radius_m = spatial_radius_m

    def promote_county(self, county_name: str, only_unpromoted: bool = False) -> CurationSummary:
        """Re-curating the full county (the default — `only_unpromoted=False`)
        rather than just new rows is intentional: it's idempotent (upsert
        keyed on api_number) and means a dedup/enrichment logic fix applies
        retroactively to everything already landed, not just new fetches.
        Use `only_unpromoted=True` only as a cheaper incremental path once
        you're confident the curation logic itself hasn't changed.
        """
        raw_rows = self.raw_feature_repository.get_for_county(
            county_name, source_layer="well_locations", only_unpromoted=only_unpromoted
        )
        summary = CurationSummary(county_name=county_name, raw_rows_considered=len(raw_rows))
        if not raw_rows:
            return summary

        by_object_id: dict[int, RawWellFeatureORM] = {r.rrc_object_id: r for r in raw_rows}
        features = [_to_raw_feature(r) for r in raw_rows]

        clusters = resolve_well_identities(features, spatial_radius_m=self.spatial_radius_m)

        wells: list[Well] = []
        cluster_by_api_number: dict[str, WellCluster] = {}

        for cluster in clusters:
            summary.clusters_by_method[cluster.method] = (
                summary.clusters_by_method.get(cluster.method, 0) + 1
            )
            if cluster.is_multi_source:
                summary.multi_source_clusters += 1

            well = _well_from_cluster(cluster, county_name, by_object_id, summary.issues)
            if well is None:
                continue
            wells.append(well)
            cluster_by_api_number[well.api_number] = cluster

        upsert_summary = self.well_repository.upsert_many(wells)
        summary.wells_upserted = upsert_summary.total

        for api_number, well_id in upsert_summary.ids_by_api_number.items():
            cluster = cluster_by_api_number.get(api_number)
            if cluster is None:
                continue
            raw_row_ids = [
                by_object_id[m.rrc_object_id].id
                for m in cluster.members
                if m.rrc_object_id in by_object_id
            ]
            self.raw_feature_repository.mark_promoted(raw_row_ids, well_id)

        return summary


def _to_raw_feature(row: RawWellFeatureORM) -> RawFeature:
    return RawFeature(
        rrc_object_id=row.rrc_object_id,
        raw_api=row.raw_api,
        gis_api5=row.gis_api5,
        latitude=row.latitude or row.latitude_nad27,
        longitude=row.longitude or row.longitude_nad27,
        symnum=row.symnum,
        symbol_description=row.symbol_description,
        reliab=row.reliab,
        location_source=row.location_source,
        well_number=row.well_number,
    )


def _well_from_cluster(
    cluster: WellCluster,
    county_name: str,
    by_object_id: dict[int, RawWellFeatureORM],
    issues: list[CurationIssue],
) -> Well | None:
    canonical = cluster.canonical
    canonical_row = by_object_id.get(canonical.rrc_object_id)

    if canonical.latitude is None or canonical.longitude is None:
        issues.append(
            CurationIssue(
                rrc_object_ids=[m.rrc_object_id for m in cluster.members],
                reason="no usable coordinates on the canonical row — well not promoted",
            )
        )
        return None

    is_reliable = cluster.method == "reliable_api"
    api_number = (
        canonical.raw_api.strip()
        if is_reliable
        else f"{canonical.raw_api.strip()}#{cluster.cluster_key.split(':')[-1]}"
    )
    is_orphaned = any(
        (by_object_id.get(m.rrc_object_id) and by_object_id[m.rrc_object_id].is_orphaned_hint)
        for m in cluster.members
    )
    county_fips = canonical_row.county_fips_hint if canonical_row else None

    try:
        return Well(
            api_number=api_number,
            api_number_is_synthetic=not is_reliable,
            rrc_object_id=canonical.rrc_object_id,
            identity_method=cluster.method,
            source_feature_count=len(cluster.members),
            well_number=canonical.well_number,
            location=GeoPoint(latitude=canonical.latitude, longitude=canonical.longitude),
            county=County(fips=county_fips, name=county_name) if county_name else None,
            regulatory_status=normalize_rrc_status(canonical.symnum, canonical.symbol_description),
            regulatory_status_raw=canonical.symbol_description,
            is_orphaned=is_orphaned,
            location_reliability_code=canonical.reliab,
            location_source=canonical.location_source,
            data_source=DataSourceType.RRC_ARCGIS_REST,
            source_synced_at=datetime.now(timezone.utc),
        )
    except ValidationError as exc:
        issues.append(
            CurationIssue(
                rrc_object_ids=[m.rrc_object_id for m in cluster.members],
                reason=f"failed domain validation: {exc}",
            )
        )
        return None
