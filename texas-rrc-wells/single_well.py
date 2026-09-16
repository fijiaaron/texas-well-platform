#!/usr/bin/env python3
"""
Look up a single Texas oil/gas well by API number using the RRC's public
ArcGIS REST service (no API key required).

Usage:
    python3 single_well.py 43934308
    python3 single_well.py 439 --exact-only   # narrow, still API prefix match
"""

import argparse
import sys

import requests

BASE = "https://gis.rrc.texas.gov/server/rest/services/rrc_public/RRC_Public_Viewer_Srvs/MapServer"
WELL_LOCATIONS_LAYER = f"{BASE}/1/query"

HEADERS = {
    "User-Agent": "texas-rrc-wells-example/1.0 (single_well.py; contact: set-your-own-contact-here)"
}

FIELDS = [
    "API",
    "GIS_WELL_NUMBER",
    "GIS_SYMBOL_DESCRIPTION",
    "RELIAB",
    "GIS_LOCATION_SOURCE",
    "GIS_LAT83",
    "GIS_LONG83",
]

WELLBORE_QUERY_URL = "https://webapps2.rrc.texas.gov/EWA/wellboreQueryAction.do"


def lookup_well(api_number: str):
    """Query RRC's Well Locations layer for a given (possibly partial) API number."""
    params = {
        "where": f"API = '{api_number}'",
        "outFields": ",".join(FIELDS),
        "returnGeometry": "true",
        "outSR": 4326,
        "f": "json",
    }
    resp = requests.get(WELL_LOCATIONS_LAYER, params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if "error" in data:
        raise RuntimeError(f"RRC service error: {data['error']}")

    return data.get("features", [])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("api_number", help="RRC API well number, e.g. 43934308")
    args = parser.parse_args()

    features = lookup_well(args.api_number)

    if not features:
        print(f"No well found for API number '{args.api_number}'.", file=sys.stderr)
        sys.exit(1)

    for f in features:
        attrs = f["attributes"]
        geom = f.get("geometry", {})
        print("-" * 50)
        print(f"API number:      {attrs.get('API')}")
        print(f"Well number:     {attrs.get('GIS_WELL_NUMBER')}")
        print(f"Status:          {attrs.get('GIS_SYMBOL_DESCRIPTION')}")
        print(f"Location source: {attrs.get('GIS_LOCATION_SOURCE')}")
        print(f"Reliability:     {attrs.get('RELIAB')}")
        lat = attrs.get("GIS_LAT83") or (geom.get("y") if geom else None)
        lon = attrs.get("GIS_LONG83") or (geom.get("x") if geom else None)
        print(f"Coordinates:     {lat}, {lon}  (NAD83)")

    print("-" * 50)
    print(
        "For the full regulatory case file (drilling permit, completion, "
        "plugging report) on this well, search the API number by hand at:"
    )
    print("  https://www.rrc.texas.gov/oil-and-gas/research-and-statistics/"
          "obtaining-commission-records/oil-and-gas-well-records-online/")
    print("or in the Wellbore Query app:")
    print(f"  {WELLBORE_QUERY_URL}")


if __name__ == "__main__":
    main()
