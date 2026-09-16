#!/usr/bin/env python3
"""
Fetches raw RRC well-location rows for a county, writes them to CSV
verbatim, then runs our own identity-resolution/dedup analysis on top and
writes a second, deduplicated CSV plus a summary report.

Why this exists: RRC's own `API` field is NOT a reliable unique well
identifier for every record (see README.md "Caveat on the API field"). A
meaningful fraction of older records share a bare district/county stub
across dozens of genuinely different wells, while genuinely-the-same-well
duplicate pins under a *real* API number can legitimately be up to ~2km
apart (surface vs. bottomhole, or permit-estimated vs. actual location) —
so naive "same API = same well" or "close together = same well" rules are
both wrong on their own. This script demonstrates the hybrid rule that
actually holds up against live data (see ../well-platform/domain/dedup.py
for the full rationale and the same logic used in the database pipeline):

  - RRC's API number is trusted as-is ONLY when RRC's own GIS_API5
    (well-specific suffix) is populated.
  - Otherwise, identity is resolved by clustering nearby raw rows
    ourselves (default: within 50m — well under the ~67m closest distinct
    -well separation observed empirically in this same data).

This script and the database curation pipeline
(../well-platform/services/curation_service.py) share the exact same
dedup function — this is the "run it on a CSV to see what it does" view of
the same logic that runs for real in Postgres.

Usage:
    python3 dedup_analysis.py --county Tarrant
    python3 dedup_analysis.py --county Real --radius-m 100
"""

import argparse
import csv
import json
import os
import sys
import time

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "well-platform"))
from domain.dedup import RawFeature, resolve_well_identities  # noqa: E402

BASE = "https://gis.rrc.texas.gov/server/rest/services/rrc_public/RRC_Public_Viewer_Srvs/MapServer"
WELL_LOCATIONS_URL = f"{BASE}/1/query"
COUNTIES_URL = f"{BASE}/29/query"
PAGE_SIZE = 1000

HEADERS = {"User-Agent": "texas-rrc-wells-dedup-analysis/1.0"}

RAW_FIELDS = [
    "OBJECTID", "API", "GIS_API5", "GIS_WELL_NUMBER", "SYMNUM",
    "GIS_SYMBOL_DESCRIPTION", "RELIAB", "GIS_LOCATION_SOURCE",
    "GIS_LAT83", "GIS_LONG83",
]


def get_county_polygon(county_name: str) -> dict:
    resp = requests.get(COUNTIES_URL, params={
        "where": f"UPPER(COUNTY_NAME) = '{county_name.upper()}'",
        "outFields": "COUNTY_NAME,FIPS", "returnGeometry": "true", "outSR": 4326, "f": "json",
    }, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    features = resp.json().get("features", [])
    if not features:
        raise ValueError(f"No RRC county found matching '{county_name}'")
    return features[0]


def fetch_raw_rows(polygon_geometry: dict, delay: float = 0.25) -> list[dict]:
    geometry_param = json.dumps({"rings": polygon_geometry["rings"], "spatialReference": {"wkid": 4326}})
    rows, offset = [], 0
    while True:
        resp = requests.post(WELL_LOCATIONS_URL, data={
            "where": "1=1", "geometry": geometry_param, "geometryType": "esriGeometryPolygon",
            "spatialRel": "esriSpatialRelIntersects", "inSR": 4326,
            "outFields": ",".join(RAW_FIELDS), "returnGeometry": "false",
            "resultRecordCount": PAGE_SIZE, "resultOffset": offset, "f": "json",
        }, headers=HEADERS, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        features = data.get("features", [])
        if not features:
            break
        rows.extend(f["attributes"] for f in features)
        offset += len(features)
        if not data.get("exceededTransferLimit"):
            break
        time.sleep(delay)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--county", default="Tarrant")
    parser.add_argument("--radius-m", type=float, default=50.0,
                         help="Spatial cluster radius for rows with no reliable API (default 50m)")
    parser.add_argument("--out-prefix", default=None, help="Output file prefix (default: <county>)")
    args = parser.parse_args()

    prefix = args.out_prefix or args.county.lower()

    print(f"Fetching raw RRC well rows for {args.county} County...", file=sys.stderr)
    county_feature = get_county_polygon(args.county)
    raw_rows = fetch_raw_rows(county_feature["geometry"])
    print(f"Fetched {len(raw_rows)} raw rows.", file=sys.stderr)

    raw_csv_path = f"{prefix}_raw.csv"
    with open(raw_csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=RAW_FIELDS)
        writer.writeheader()
        writer.writerows(raw_rows)
    print(f"Wrote raw data to {raw_csv_path}", file=sys.stderr)

    features = [
        RawFeature(
            rrc_object_id=r["OBJECTID"], raw_api=r["API"], gis_api5=r["GIS_API5"],
            latitude=r["GIS_LAT83"], longitude=r["GIS_LONG83"], symnum=r["SYMNUM"],
            symbol_description=r["GIS_SYMBOL_DESCRIPTION"], reliab=r["RELIAB"],
            location_source=r["GIS_LOCATION_SOURCE"], well_number=r["GIS_WELL_NUMBER"],
        )
        for r in raw_rows
    ]
    clusters = resolve_well_identities(features, spatial_radius_m=args.radius_m)

    dedup_csv_path = f"{prefix}_deduped.csv"
    with open(dedup_csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "canonical_api_number", "identity_method", "source_feature_count",
            "canonical_status", "canonical_object_id", "latitude", "longitude",
            "member_object_ids",
        ])
        writer.writeheader()
        for c in clusters:
            canon = c.canonical
            is_reliable = c.method == "reliable_api"
            api_number = (
                canon.raw_api.strip() if is_reliable
                else f"{canon.raw_api.strip()}#{c.cluster_key.split(':')[-1]}"
            )
            writer.writerow({
                "canonical_api_number": api_number,
                "identity_method": c.method,
                "source_feature_count": len(c.members),
                "canonical_status": canon.symbol_description,
                "canonical_object_id": canon.rrc_object_id,
                "latitude": canon.latitude,
                "longitude": canon.longitude,
                "member_object_ids": ";".join(str(m.rrc_object_id) for m in c.members),
            })
    print(f"Wrote deduplicated wells to {dedup_csv_path}", file=sys.stderr)

    by_method = {}
    for c in clusters:
        by_method.setdefault(c.method, []).append(c)

    print("\n--- Dedup analysis report ---")
    print(f"County: {args.county}")
    print(f"Raw rows fetched: {len(raw_rows)}")
    print(f"Canonical wells resolved: {len(clusters)}")
    print(f"Reduction: {len(raw_rows) - len(clusters)} rows merged into existing well identities")
    print()
    for method, cs in sorted(by_method.items()):
        sizes = [len(c.members) for c in cs]
        multi = sum(1 for s in sizes if s > 1)
        print(f"  {method}: {len(cs)} wells "
              f"(from {sum(sizes)} raw rows, {multi} required merging >1 row)")

    multi_source = [c for c in clusters if c.is_multi_source]
    if multi_source:
        print(f"\nExample multi-source clusters (same well, multiple RRC pins):")
        for c in multi_source[:5]:
            statuses = [m.symbol_description for m in c.members]
            print(f"  {c.canonical.raw_api.strip()!r} ({c.method}): {len(c.members)} rows, "
                  f"statuses={statuses}, chosen canonical={c.canonical.symbol_description!r}")


if __name__ == "__main__":
    main()
