"""Pulls well data from the Texas RRC ArcGIS REST service (see
../texas-rrc-wells/README.md for how that endpoint was found and how it
behaves) and lands it — verbatim, unmodified, undeduplicated — in the raw
staging table via RawFeatureRepository. This is stage one of the two-stage
"raw-then-curate" ingestion pipeline (see docs/ARCHITECTURE.md); stage two,
turning raw rows into canonical Well records, is
services/curation_service.py.

RRCWellClient is a thin, dependency-light HTTP client — the same endpoints
and pagination pattern as texas-rrc-wells/bulk_county.py and single_well.py,
refactored into something a service can call repeatedly rather than a
one-shot script. RawIngestionService is the orchestration: fetch, land,
record an IngestionRun. It does not interpret RRC's data at all — no
status normalization, no identity resolution — on purpose, so a fetch can
never be lossy or crash on a data surprise like RRC's non-unique API stubs.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import requests

from domain.enums import DataSourceType, IngestionRunStatus
from domain.models import IngestionRun

from data.repositories.ingestion_run_repository import IngestionRunRepository
from data.repositories.raw_feature_repository import RawFeatureRepository

BASE_URL = "https://gis.rrc.texas.gov/server/rest/services/rrc_public/RRC_Public_Viewer_Srvs/MapServer"
WELL_LOCATIONS_URL = f"{BASE_URL}/1/query"
ORPHAN_WELLS_URL = f"{BASE_URL}/2/query"
COUNTIES_URL = f"{BASE_URL}/29/query"

PAGE_SIZE = 1000  # RRC's published maxRecordCount for these layers
WELL_FIELDS = (
    "OBJECTID,API,GIS_API5,GIS_WELL_NUMBER,SYMNUM,GIS_SYMBOL_DESCRIPTION,"
    "RELIAB,GIS_LOCATION_SOURCE,GIS_LAT83,GIS_LONG83,GIS_LAT27,GIS_LONG27"
)

HEADERS = {
    "User-Agent": "sigilint-well-platform/1.0 (ingestion_service.py; contact: set-your-own-contact-here)"
}


class RRCWellClient:
    """Talks to RRC's public ArcGIS REST service. No API key (there isn't
    one — see texas-rrc-wells/README.md). Paginates sequentially with a
    delay between pages per the "be a good citizen" guidance in that doc —
    this is a shared government server, not a dedicated API product.
    """

    def __init__(self, session: requests.Session | None = None, delay: float = 0.25):
        self.session = session or requests.Session()
        self.delay = delay

    def get_county_polygon(self, county_name: str) -> dict:
        resp = self.session.get(
            COUNTIES_URL,
            params={
                "where": f"UPPER(COUNTY_NAME) = '{county_name.upper()}'",
                "outFields": "COUNTY_NAME,FIPS",
                "returnGeometry": "true",
                "outSR": 4326,
                "f": "json",
            },
            headers=HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        features = resp.json().get("features", [])
        if not features:
            raise ValueError(f"No RRC county found matching '{county_name}'")
        return features[0]

    def resolve_county_for_point(self, lat: float, lon: float) -> dict | None:
        resp = self.session.get(
            COUNTIES_URL,
            params={
                "where": "1=1",
                "geometry": json.dumps({"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}),
                "geometryType": "esriGeometryPoint",
                "spatialRel": "esriSpatialRelIntersects",
                "inSR": 4326,
                "outFields": "COUNTY_NAME,FIPS",
                "returnGeometry": "false",
                "f": "json",
            },
            headers=HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        features = resp.json().get("features", [])
        return features[0]["attributes"] if features else None

    def iter_wells_in_polygon(self, polygon_geometry: dict):
        geometry_param = json.dumps(
            {"rings": polygon_geometry["rings"], "spatialReference": {"wkid": 4326}}
        )
        offset = 0
        while True:
            payload = {
                "where": "1=1",
                "geometry": geometry_param,
                "geometryType": "esriGeometryPolygon",
                "spatialRel": "esriSpatialRelIntersects",
                "inSR": 4326,
                "outFields": WELL_FIELDS,
                "returnGeometry": "false",
                "resultRecordCount": PAGE_SIZE,
                "resultOffset": offset,
                "f": "json",
            }
            resp = self.session.post(WELL_LOCATIONS_URL, data=payload, headers=HEADERS, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                raise RuntimeError(f"RRC service error: {data['error']}")

            features = data.get("features", [])
            if not features:
                break
            for feature in features:
                yield feature["attributes"]

            offset += len(features)
            if not data.get("exceededTransferLimit"):
                break
            time.sleep(self.delay)

    def get_orphan_api_numbers_in_polygon(self, polygon_geometry: dict) -> set[str]:
        geometry_param = json.dumps(
            {"rings": polygon_geometry["rings"], "spatialReference": {"wkid": 4326}}
        )
        apis: set[str] = set()
        offset = 0
        while True:
            payload = {
                "where": "1=1",
                "geometry": geometry_param,
                "geometryType": "esriGeometryPolygon",
                "spatialRel": "esriSpatialRelIntersects",
                "inSR": 4326,
                "outFields": "API",
                "returnGeometry": "false",
                "resultRecordCount": PAGE_SIZE,
                "resultOffset": offset,
                "f": "json",
            }
            resp = self.session.post(ORPHAN_WELLS_URL, data=payload, headers=HEADERS, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            features = data.get("features", [])
            if not features:
                break
            for feature in features:
                api = feature["attributes"].get("API")
                if api:
                    apis.add(api)
            offset += len(features)
            if not data.get("exceededTransferLimit"):
                break
            time.sleep(self.delay)
        return apis

    def get_all_by_api(self, api_number: str) -> list[dict]:
        """All raw rows matching this API string — plural on purpose. A
        stub (blank-GIS_API5) API can match more than one real RRC row;
        the old version of this method took features[0] and silently
        dropped the rest. Landing every match and letting curation resolve
        identity is what fixes that.
        """
        resp = self.session.get(
            WELL_LOCATIONS_URL,
            params={
                "where": f"API = '{api_number}'",
                "outFields": WELL_FIELDS,
                "returnGeometry": "false",
                "f": "json",
            },
            headers=HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        return [f["attributes"] for f in resp.json().get("features", [])]

    def get_orphan_api_numbers(self, api_number: str) -> set[str]:
        resp = self.session.get(
            ORPHAN_WELLS_URL,
            params={"where": f"API = '{api_number}'", "outFields": "API", "f": "json"},
            headers=HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        return {f["attributes"]["API"] for f in resp.json().get("features", [])}


class RawIngestionService:
    """Fetches from RRC and lands it in raw_well_features — nothing more.
    No deduplication, no identity resolution, no writes to `wells`; that's
    services/curation_service.py's job, deliberately kept separate (see
    docs/ARCHITECTURE.md "Raw-then-curate ingestion"). This service can't
    corrupt curated data because it never touches it.

    `raw_feature_repository` is expected to be bound to an already-open
    session / unit of work (see data/session.py session_scope) that the
    caller commits.
    """

    def __init__(
        self,
        raw_feature_repository: RawFeatureRepository,
        ingestion_run_repository: IngestionRunRepository,
        client: RRCWellClient | None = None,
    ):
        self.raw_feature_repository = raw_feature_repository
        self.ingestion_run_repository = ingestion_run_repository
        self.client = client or RRCWellClient()

    def ingest_county(self, county_name: str, batch_size: int = 500) -> IngestionRun:
        run = IngestionRun(
            source=DataSourceType.RRC_ARCGIS_REST, scope_description=f"county:{county_name}"
        )
        self.ingestion_run_repository.create(run)
        try:
            county_feature = self.client.get_county_polygon(county_name)
            canonical_county_name = county_feature["attributes"]["COUNTY_NAME"]
            county_fips = county_feature["attributes"]["FIPS"]

            orphan_apis = self.client.get_orphan_api_numbers_in_polygon(county_feature["geometry"])

            batch: list[dict] = []
            for attrs in self.client.iter_wells_in_polygon(county_feature["geometry"]):
                if not attrs.get("API"):
                    continue  # RRC row with no identifier at all -- nothing to land
                run.records_fetched += 1
                batch.append(attrs)
                if len(batch) >= batch_size:
                    run.records_upserted += self.raw_feature_repository.upsert_batch(
                        batch, "well_locations", run.id, canonical_county_name, county_fips
                    )
                    batch = []
            if batch:
                run.records_upserted += self.raw_feature_repository.upsert_batch(
                    batch, "well_locations", run.id, canonical_county_name, county_fips
                )

            self.raw_feature_repository.mark_orphaned("well_locations", orphan_apis)

            run.status = IngestionRunStatus.SUCCEEDED
        except Exception as exc:
            run.status = IngestionRunStatus.FAILED
            run.error_message = str(exc)
            raise
        finally:
            run.completed_at = datetime.now(timezone.utc)
            self.ingestion_run_repository.update(run)
        return run

    def ingest_single(self, api_number: str) -> IngestionRun:
        """Lands every raw row matching this API (see
        RRCWellClient.get_all_by_api — can be more than one for a stub
        API), with county resolved once via the first row's location.
        Does not promote to `wells`; call
        WellCurationService.promote_county afterward (or
        WellCurationService.promote_raw_rows directly on the returned
        run's rows) to get a queryable Well.
        """
        run = IngestionRun(
            source=DataSourceType.RRC_ARCGIS_REST, scope_description=f"api:{api_number}"
        )
        self.ingestion_run_repository.create(run)
        try:
            rows = self.client.get_all_by_api(api_number)
            if not rows:
                raise ValueError(f"No RRC well found for API number '{api_number}'")
            run.records_fetched = len(rows)

            first = rows[0]
            lat = first.get("GIS_LAT83") or first.get("GIS_LAT27")
            lon = first.get("GIS_LONG83") or first.get("GIS_LONG27")
            county_attrs = self.client.resolve_county_for_point(lat, lon) if lat and lon else None
            county_name = county_attrs["COUNTY_NAME"] if county_attrs else None
            county_fips = county_attrs["FIPS"] if county_attrs else None

            run.records_upserted = self.raw_feature_repository.upsert_batch(
                rows, "well_locations", run.id, county_name, county_fips
            )
            orphan_apis = self.client.get_orphan_api_numbers(api_number)
            self.raw_feature_repository.mark_orphaned("well_locations", orphan_apis)

            run.status = IngestionRunStatus.SUCCEEDED
        except Exception as exc:
            run.status = IngestionRunStatus.FAILED
            run.error_message = str(exc)
            raise
        finally:
            run.completed_at = datetime.now(timezone.utc)
            self.ingestion_run_repository.update(run)
        return run
