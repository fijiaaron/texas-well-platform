#!/usr/bin/env python3
"""
Bulk-download every oil/gas well RRC has a location for within a given Texas
county, using RRC's public ArcGIS REST service (no API key required).

How it works:
  1. Query RRC's own "Counties" layer (id 29) for the county's polygon.
  2. Use that polygon as a spatial filter against the "Well Locations" layer
     (id 1), paginating past the 1000-record-per-request cap with
     `resultOffset` until every page has been fetched.
  3. Write results to CSV (or print a status-count summary).

Usage:
    python3 bulk_county.py --county Tarrant
    python3 bulk_county.py --county Tarrant --out tarrant_wells.csv
    python3 bulk_county.py --county Tarrant --status-summary-only
"""

import argparse
import csv
import json
import sys
import time

import requests

BASE = "https://gis.rrc.texas.gov/server/rest/services/rrc_public/RRC_Public_Viewer_Srvs/MapServer"
COUNTIES_LAYER = f"{BASE}/29/query"
WELL_LOCATIONS_LAYER = f"{BASE}/1/query"

HEADERS = {
    "User-Agent": "texas-rrc-wells-example/1.0 (bulk_county.py; contact: set-your-own-contact-here)"
}

PAGE_SIZE = 1000  # RRC's published maxRecordCount for this layer

WELL_FIELDS = [
    "API",
    "GIS_WELL_NUMBER",
    "GIS_SYMBOL_DESCRIPTION",
    "GIS_LAT83",
    "GIS_LONG83",
]


def get_county_polygon(county_name: str) -> dict:
    """Fetch a county's polygon geometry from RRC's own Counties layer."""
    params = {
        "where": f"UPPER(COUNTY_NAME) = '{county_name.upper()}'",
        "outFields": "COUNTY_NAME,FIPS",
        "returnGeometry": "true",
        "outSR": 4326,
        "f": "json",
    }
    resp = requests.get(COUNTIES_LAYER, params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    features = data.get("features", [])
    if not features:
        raise ValueError(f"No county found matching '{county_name}'")

    return features[0]["geometry"]


def fetch_wells_in_polygon(polygon: dict, delay: float = 0.25):
    """Yield every well feature intersecting the given polygon, paginating
    through RRC's 1000-record-per-request cap."""
    geometry_param = json.dumps(
        {"rings": polygon["rings"], "spatialReference": {"wkid": 4326}}
    )

    offset = 0
    while True:
        payload = {
            "where": "1=1",
            "geometry": geometry_param,
            "geometryType": "esriGeometryPolygon",
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": 4326,
            "outFields": ",".join(WELL_FIELDS),
            "returnGeometry": "true",
            "outSR": 4326,
            "resultRecordCount": PAGE_SIZE,
            "resultOffset": offset,
            "f": "json",
        }
        # POST, not GET: county polygons can have thousands of vertices,
        # which overflows a GET URL's length limit.
        resp = requests.post(
            WELL_LOCATIONS_LAYER, data=payload, headers=HEADERS, timeout=60
        )
        resp.raise_for_status()
        data = resp.json()

        if "error" in data:
            raise RuntimeError(f"RRC service error: {data['error']}")

        features = data.get("features", [])
        if not features:
            break

        for f in features:
            yield f

        offset += len(features)

        if not data.get("exceededTransferLimit"):
            break

        time.sleep(delay)  # be polite to a shared public server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--county", default="Tarrant", help="Texas county name (default: Tarrant)")
    parser.add_argument("--out", default=None, help="CSV output path (default: <county>_wells.csv)")
    parser.add_argument("--delay", type=float, default=0.25, help="Seconds between paginated requests")
    parser.add_argument(
        "--status-summary-only",
        action="store_true",
        help="Print well-status counts instead of writing a CSV",
    )
    args = parser.parse_args()

    print(f"Looking up '{args.county}' County boundary...", file=sys.stderr)
    polygon = get_county_polygon(args.county)

    print(f"Fetching wells within {args.county} County (paginated, {args.delay}s between pages)...",
          file=sys.stderr)

    status_counts = {}
    rows = []
    for feature in fetch_wells_in_polygon(polygon, delay=args.delay):
        attrs = feature["attributes"]
        geom = feature.get("geometry") or {}
        status = attrs.get("GIS_SYMBOL_DESCRIPTION") or "Unknown"
        status_counts[status] = status_counts.get(status, 0) + 1
        rows.append(
            {
                "api_number": attrs.get("API"),
                "well_number": attrs.get("GIS_WELL_NUMBER"),
                "status": status,
                "latitude": attrs.get("GIS_LAT83") or geom.get("y"),
                "longitude": attrs.get("GIS_LONG83") or geom.get("x"),
            }
        )

    print(f"Fetched {len(rows)} wells.", file=sys.stderr)
    print("\nStatus breakdown:", file=sys.stderr)
    for status, count in sorted(status_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>6}  {status}", file=sys.stderr)

    if args.status_summary_only:
        return

    out_path = args.out or f"{args.county.lower()}_wells.csv"
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["api_number", "well_number", "status", "latitude", "longitude"]
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nWrote {len(rows)} rows to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
