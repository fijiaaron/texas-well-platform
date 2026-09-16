"""Well-identity resolution: turns raw RRC GIS rows into canonical well
clusters using our own rules, not a blind trust of RRC's `API` field.

Empirically grounded (queried live against RRC, see docs/ARCHITECTURE.md
"The blank-GIS_API5 problem" and "Why identity isn't purely spatial
either"), not guessed:

- When RRC's `GIS_API5` (well-specific suffix) is populated, `API` is a
  real, reliable per-well identifier — rows sharing one are the same
  wellbore recorded at different points in its life (e.g. "Permitted
  Location" later replaced by "Gas Well"). Confirmed these pairs can be
  up to ~2.3km apart (Tarrant County) — almost certainly permit-estimated
  vs. actual-drilled location, or surface vs. bottomhole for a horizontal
  well — so **distance must NOT be used to second-guess a reliable API
  match**. RRC's own identifier wins here.
- When `GIS_API5` is blank, `API` is a bare district/county stub RRC's
  legacy "hardcopy map" digitization shares across unrelated wells (one
  stub covered 29 distinct wells in one county, confirmed). RRC gives us
  no reliable grouping signal at all for these rows. We build our own:
  spatial proximity. Confirmed empirically that genuinely distinct wells
  in this subset are never closer than ~67m apart (closest observed pair,
  Real County) — so a small-radius spatial cluster (default 50m, safely
  under that floor) is a defensible proxy for "this is really the same
  pin recorded more than once," without a real risk of merging two
  actually-different unidentified wells.

This module is framework/DB-agnostic — used identically by the
Postgres/PostGIS curation path (services/curation_service.py, using real
ST_ClusterDBSCAN for the spatial pass) and by the standalone CSV analysis
script (../texas-rrc-wells/dedup_analysis.py, using the pure-Python
grid-bucketed union-find below) so both paths apply the exact same rule.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable

from .status_mapping import normalize_rrc_status

# Status precedence for picking a canonical status among raw rows RRC
# recorded for the same well over time. RRC's GIS layer carries no
# per-row timestamp, so we can't know which pin is literally most recent —
# this instead prefers a definitive lifecycle outcome (drilled-and-plugged,
# drilled-and-dry, canceled) over a merely-proposed one (permitted), and an
# active/operational state over either. A heuristic, documented as such —
# not a claim of certainty.
_STATUS_PRECEDENCE = {
    "plugged": 0,
    "dry_hole": 0,
    "canceled_or_abandoned_location": 0,
    "orphaned": 0,
    "active_oil": 1,
    "active_gas": 1,
    "active_oil_gas": 1,
    "shut_in": 1,
    "injection_or_disposal": 1,
    "storage": 1,
    "observation": 1,
    "water_supply": 1,
    "brine_mining": 1,
    "geothermal": 1,
    "service": 1,
    "core_test": 1,
    "other": 2,
    "permitted": 3,  # merely proposed -- lowest precedence once anything else exists
}

DEFAULT_SPATIAL_CLUSTER_RADIUS_M = 50.0  # see module docstring: safely under the observed ~67m floor


@dataclass
class RawFeature:
    """One raw RRC GIS row — the shared input shape for both the CSV
    analysis path and the DB curation path.
    """

    rrc_object_id: int
    raw_api: str
    gis_api5: str | None
    latitude: float | None
    longitude: float | None
    symnum: int | None
    symbol_description: str | None
    reliab: str | None = None
    location_source: str | None = None
    well_number: str | None = None


@dataclass
class WellCluster:
    """One resolved well identity: the raw rows judged to be the same
    wellbore, plus the chosen canonical representative row.
    """

    cluster_key: str
    method: str  # "reliable_api" | "spatial" | "unclustered_no_location"
    members: list[RawFeature] = field(default_factory=list)

    @property
    def canonical(self) -> RawFeature:
        def sort_key(f: RawFeature):
            # Route through the same RRC-status normalizer used at ingestion
            # (domain/status_mapping.py) rather than matching raw description
            # text directly — SYMNUM is the stable key, and this keeps
            # precedence keyed on our one normalized vocabulary instead of a
            # second, easy-to-drift copy of RRC's raw strings.
            status = normalize_rrc_status(f.symnum, f.symbol_description)
            precedence = _STATUS_PRECEDENCE.get(status.value, 2)
            return (precedence, -(f.rrc_object_id or 0))

        # lower precedence number = more definitive outcome; ties broken by
        # preferring the higher OBJECTID (RRC assigns these roughly in
        # ingestion order, so this is a weak "more recently digitized" proxy)
        return min(self.members, key=sort_key)

    @property
    def is_multi_source(self) -> bool:
        return len(self.members) > 1


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _is_reliable(feature: RawFeature) -> bool:
    return bool((feature.gis_api5 or "").strip())


def _spatial_cluster(
    features: list[RawFeature], radius_m: float
) -> list[list[RawFeature]]:
    """Grid-bucketed union-find clustering for features with no reliable
    API. O(n) expected rather than O(n^2): bucket by a grid cell sized to
    the radius, then only compare each point against points in its own and
    neighboring cells. Good enough for county-scale batches (low thousands);
    the Postgres path uses real ST_ClusterDBSCAN instead of re-implementing
    this at full statewide scale.
    """
    located = [f for f in features if f.latitude is not None and f.longitude is not None]
    unlocated = [f for f in features if f.latitude is None or f.longitude is None]

    # ~radius_m in degrees latitude; longitude cell widened generously to
    # stay conservative near higher latitudes (not a concern for Texas, but
    # cheap to get right).
    cell_deg = max(radius_m / 111_320.0, 1e-6)

    buckets: dict[tuple[int, int], list[int]] = {}
    for idx, f in enumerate(located):
        cell = (int(f.latitude / cell_deg), int(f.longitude / cell_deg))
        buckets.setdefault(cell, []).append(idx)

    parent = list(range(len(located)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for (cx, cy), idxs in buckets.items():
        neighbor_idxs: list[int] = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                neighbor_idxs.extend(buckets.get((cx + dx, cy + dy), []))
        for i in idxs:
            for j in neighbor_idxs:
                if j <= i:
                    continue
                if _haversine_m(
                    located[i].latitude, located[i].longitude, located[j].latitude, located[j].longitude
                ) <= radius_m:
                    union(i, j)

    groups: dict[int, list[RawFeature]] = {}
    for idx, f in enumerate(located):
        groups.setdefault(find(idx), []).append(f)

    clusters = list(groups.values())
    # features with no usable coordinates at all can't be clustered by
    # location; each is its own singleton cluster rather than being dropped
    clusters.extend([f] for f in unlocated)
    return clusters


def resolve_well_identities(
    features: Iterable[RawFeature], spatial_radius_m: float = DEFAULT_SPATIAL_CLUSTER_RADIUS_M
) -> list[WellCluster]:
    """The single dedup entry point both the CSV analysis script and the DB
    curation service call. Returns one WellCluster per resolved well.
    """
    features = list(features)
    reliable = [f for f in features if _is_reliable(f)]
    unreliable = [f for f in features if not _is_reliable(f)]

    clusters: list[WellCluster] = []

    by_api: dict[str, list[RawFeature]] = {}
    for f in reliable:
        by_api.setdefault(f.raw_api.strip(), []).append(f)
    for api, members in by_api.items():
        clusters.append(WellCluster(cluster_key=f"api:{api}", method="reliable_api", members=members))

    for group in _spatial_cluster(unreliable, spatial_radius_m):
        # Keyed by the MINIMUM rrc_object_id in the group, not just
        # whichever member happens to come first — group order depends on
        # dict/bucket iteration order, which isn't guaranteed stable across
        # repeated runs (e.g. after a re-fetch changes row ordering from
        # Postgres). min() is deterministic regardless of input order, which
        # matters for idempotent re-curation: the same physical cluster
        # should resolve to the same key every time it's promoted.
        anchor_id = min(f.rrc_object_id for f in group)
        if group[0].latitude is None:
            key = f"objectid:{anchor_id}"
            method = "unclustered_no_location"
        else:
            key = f"spatial:{anchor_id}"
            method = "spatial"
        clusters.append(WellCluster(cluster_key=key, method=method, members=group))

    return clusters
