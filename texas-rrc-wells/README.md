# Texas Railroad Commission (RRC) Oil & Gas Well Data — Access Guide

The Railroad Commission of Texas (RRC) regulates oil and gas wells statewide and
publishes well location/status data for free, **with no API key or account
required**. This doc covers where the data lives, what's structured vs.
scanned-document-only, and includes two working Python examples.

## TL;DR

| Need | Use |
|---|---|
| Single well: location + status | RRC ArcGIS REST API (`gis.rrc.texas.gov`) |
| Bulk wells for a county/region | Same REST API, spatial query + pagination |
| Full regulatory case file (permit, completion, plugging report) for one well | "Oil and Gas Well Records Online" (scanned PDFs, browser only) |
| Interactive search by operator/lease/field/API# | "Wellbore Query" web app (HTML, not a scriptable API) |
| Statewide flat-file dumps (all wells, all permits) | "Data Sets Available for Download" page (shapefile/DBF/legacy EBCDIC) |

No registration, login, or API key is needed for any of these. That's notable —
most state regulators gate this behind a portal account; Texas RRC does not.

---

## 1. The data source: RRC's ArcGIS REST service

The **Public GIS Viewer** (https://gis.rrc.texas.gov/GISViewer/) is a browser
map application, but it's just a thin UI over a public Esri ArcGIS Server
instance. That server has a normal ArcGIS REST API you can call directly:

```
https://gis.rrc.texas.gov/server/rest/services/rrc_public/RRC_Public_Viewer_Srvs/MapServer
```

This isn't officially "documented" by RRC as a developer API (there's no
published API reference or key-signup page) — it was found by inspecting what
the GIS Viewer itself calls. It's a standard ArcGIS Server, so it follows the
[standard Esri REST query spec](https://developers.arcgis.com/rest/services-reference/enterprise/query-map-service-layer/),
which is well documented independent of RRC.

Relevant layers on that MapServer:

| Layer ID | Name | Contents |
|---|---|---|
| `0` | Well Number | Label layer (cartographic only) |
| `1` | **Well Locations** | Every well RRC has a location for — API number, well number, status category, lat/long (NAD27 and NAD83) |
| `2` | **Orphan Wells** | Subset flagged as orphaned (no responsible operator) — API number + geometry only |
| `9` | Horiz/Dir Surface Locations | Surface locations for directional/horizontal wells |
| `10` | Horizontal/Directional Lines | Surface-to-bottomhole trace lines |
| `13` | Pipelines | Regulated pipeline data |

Layer 1 fields:

| Field | Meaning |
|---|---|
| `API` | API well number (RRC's internal short form — see caveat below) |
| `GIS_WELL_NUMBER` | Well number/suffix |
| `SYMNUM` | Numeric status/type code |
| `GIS_SYMBOL_DESCRIPTION` | Human-readable status (see table below) |
| `RELIAB` | Location reliability code |
| `GIS_LOCATION_SOURCE` | How the location was derived (hardcopy map, mainframe distances, GPS, etc.) |
| `GIS_LAT83` / `GIS_LONG83` | Coordinates, NAD83 (use these) |
| `GIS_LAT27` / `GIS_LONG27` | Coordinates, NAD27 (legacy) |

**Caveat on the `API` field — this is more than a display-width quirk.**
`API` is a display-shortened version of the real 14-digit API number, and
its width is inconsistent across records — but for a meaningful chunk of
older records, it's worse than that: it isn't unique at all. There's a
second field, `GIS_API5`, RRC's well-specific 5-digit suffix; when
`GIS_API5` is blank, `API` is just a bare district/county-level stub with
no well-specific portion, shared across every wellbore RRC's legacy
"Commission's hardcopy map" digitization never gave a distinct ID.
Confirmed directly against the live service: one such stub in Real
County (`API = "385"`, `GIS_API5` blank on every row) covers **29**
different RRC records — different `OBJECTID`s, different coordinates,
different statuses.

**And don't assume location is a safe substitute either** — rows sharing a
*real, reliable* API number (`GIS_API5` populated) can legitimately be up
to **~2.3km apart** (23 confirmed cases in Tarrant County), almost
certainly permit-estimated vs. actual-drilled location, or surface vs.
bottomhole for a horizontal well. So neither "same `API`" nor "close
together" is a safe rule on its own — do not treat `API` alone as a
unique well key, and do not use proximity to second-guess a reliable
`API` match.

The rule that actually holds up against live data — trust `API` directly
when `GIS_API5` is populated (regardless of distance between its rows);
fall back to spatial clustering only when `GIS_API5` is blank (safe
because the closest two genuinely distinct blank-`GIS_API5` wells observed
were 67m apart, well outside a 50m cluster radius) — is implemented once
in [`../well-platform/domain/dedup.py`](../well-platform/domain/dedup.py)
and used identically by two things:

- **[`dedup_analysis.py`](dedup_analysis.py)** in this directory — a
  standalone script, no database required: `python3 dedup_analysis.py
  --county Tarrant` fetches raw rows, writes them to CSV, runs the dedup
  rule, and writes a second deduplicated CSV plus a summary report.
- `well-platform`'s two-stage database ingestion pipeline (raw landing
  table -> curation pass) — see
  [`../well-platform/docs/ARCHITECTURE.md`](../well-platform/docs/ARCHITECTURE.md)
  "Raw-then-curate ingestion" and "Well identity resolution" for the full
  writeup, including how a naive first attempt at this got it wrong.

For an authoritative, full-length API number tied to operator/lease/county
names on the records that do have one, cross-reference against the
"Statewide API Data" bulk file (§3) or the Wellbore Query app (§4).

### Well status taxonomy (`GIS_SYMBOL_DESCRIPTION`)

Pulled directly from the service (`groupBy` query, statewide counts as of
2026-09-16). This is effectively RRC's "active / plugged / abandoned / etc."
classification:

- **Active-ish**: Oil Well, Gas Well, Oil/Gas Well, Shut-In Oil, Shut-In Gas
- **Abandoned/plugged**: Plugged Oil Well, Plugged Gas Well, Plugged Oil/Gas,
  Canceled / Abandoned Location, Plugged Storage, Plugged Brine Mining
- **Other statuses**: Permitted Location, Dry Hole, Injection/Disposal (and
  variants), Core Test, Storage, Water Supply, Observation, Brine Mining,
  Geothermal, Horizontal Drainhole, Service

Plus a **separate "Orphan Wells" layer (id `2`)** — RRC's specific regulatory
term for wells whose operator is no longer responsible (bond forfeited,
operator defunct, etc.), which is a stricter/narrower category than "plugged"
or "abandoned" above.

### Access characteristics (verified by calling it directly)

- No API key, no token, no rate-limit header returned.
- `maxRecordCount: 1000` — any query matching more than 1000 features is
  truncated (`"exceededTransferLimit": true`) and must be paginated with
  `resultOffset`.
- Supports `f=json`, `f=geojson`, `f=pbf`.
- Supports both attribute filters (`where=`) and spatial filters
  (`geometry=` + `geometryType=` + `spatialRel=`) — this is what makes a
  "give me every well in Tarrant County" query possible without RRC
  publishing a county-name field on the layer itself (it doesn't have one).

---

## 2. Narrowing by location — no metadata database needed

The `Well Locations` layer itself has no `COUNTY`, `DISTRICT`, or `FIELD`
attribute field, so you can't just do `where=COUNTY='Tarrant'` against it
directly. But you don't need to build your own reference/lookup tables for
this — **the same MapServer already publishes the boundary layers you'd
otherwise have to source separately**:

| Layer ID | Name | Key field |
|---|---|---|
| `29` | Counties | `COUNTY_NAME`, `FIPS` |
| `31` | Districts | `DISTRICT` (RRC's own oil & gas district numbers, ~8 across the state) |
| `20` | Subdivisions | subdivision polygons |
| `24` | Surveys | original land-grant survey boundaries (fine-grained, mainly for legal descriptions, not typically what you want) |

So "every well in Tarrant County" is a two-step, both against `gis.rrc.texas.gov`
and nothing else:

1. Query layer `29` with `where=COUNTY_NAME='TARRANT'` → get the county polygon.
2. Query layer `1` (Well Locations) with that polygon as a spatial filter
   (`geometry=` + `geometryType=esriGeometryPolygon` + `spatialRel=esriSpatialRelIntersects`).

Same pattern works for an RRC **district** (layer `31`, field `DISTRICT`) if
you want a multi-county regulatory region instead of one county, or for any
custom area (city limits — layer `28`; or your own bounding box/radius).

**What isn't available this way: field name/number.** RRC's oil & gas
"fields" (geologic reservoirs, tens of thousands of them, e.g. "SPRABERRY
(TREND AREA)") aren't published as a boundary layer in this service — a well's
field is a per-record attribute, not something with a queryable shape here.
To filter/join by field, operator, or lease name you need the **Statewide API
Data** bulk file (§3) or the interactive **Wellbore Query** app (§4), which do
carry those fields directly. In short: for pure *geographic* narrowing
(county/district/city/custom area), everything you need is already in RRC's
own service — no separate metadata database to build. For narrowing by
*regulatory* metadata (operator, field, lease), you're joining against RRC's
own reference file rather than maintaining your own.

---

## 2a. Being a good citizen of a shared public server

There's no published rate limit, API terms-of-service page, or `robots.txt`
disallow rule for `gis.rrc.texas.gov` (checked directly — the service returns
a plain 404 for `/robots.txt`, and `rrc.texas.gov/robots.txt` is empty, i.e.
no restrictions stated either way). That means there's no documented ceiling
to bump into — and also no official guarantee, so treat it as a shared state
server you don't want to lean on hard:

- **Page results, don't parallelize them.** The examples below paginate
  sequentially with a small delay (`--delay`, default 0.25s) between
  requests rather than firing concurrent requests.
- **Filter server-side, not client-side.** Use `where=`/`geometry=` to get
  only the rows you need instead of pulling everything and filtering
  locally — this is both faster for you and lighter on RRC's server.
- **Cache results locally.** The well/location data doesn't change
  minute-to-minute; re-run against a saved CSV/GeoJSON instead of
  re-querying for repeated analysis.
- **For a full-state or recurring/scheduled job, prefer the bulk flat-file
  downloads (§3) over the live query API.** Those are designed for bulk
  consumption (updated on a fixed schedule) rather than a live service meant
  for the interactive map viewer's traffic pattern.
- **Set a descriptive `User-Agent`** identifying your script/contact, which
  both examples do below — this is a standard courtesy for unauthenticated
  public APIs.

---

## 3. Bulk flat-file downloads (statewide dumps)

For full statewide extracts rather than API queries, RRC publishes flat files
here, free, no login:

**https://www.rrc.texas.gov/resource-center/research/data-sets-available-for-download/**

Notable ones for well status/location work:

- **Well Layers by County** — ArcView shapefiles, pre-split by county, updated
  twice weekly. Closest thing to "give me Tarrant County as a file" without
  calling an API.
- **Statewide API Data** — ASCII or dBase (.dbf), updated twice weekly. Maps
  API numbers to operator/lease/county names properly (fixes the truncation
  caveat above).
- **Full Wellbore Query Data / Statewide Oil & Gas Well Database** — the full
  wellbore history, in **EBCDIC** or ASCII fixed-width mainframe format
  (updated weekly/monthly). Needs RRC's published column-layout manual to
  parse; not casually loadable with `pandas.read_csv`.
- **Drilling Permit Master** — every permit application since 1976, ASCII,
  monthly.

These download links go through RRC's file-transfer portal
(`mft.rrc.texas.gov`, a GoAnywhere MFT instance). In testing, the link
returns an HTML landing page rather than the file directly — so it's easy to
click through in a browser, but not a one-line `curl -O`; scripting it would
need to parse that landing page for the actual asset link. For that reason,
the bulk example below (§5) uses the REST API + pagination instead, which
*is* directly scriptable end-to-end.

---

## 4. Full regulatory case file for one well (PDF only, not bulk)

The GIS layer above only gives you location + a status category. The actual
regulatory documents (Form W-1 drilling permit, W-2 completion report, W-3
plugging report, etc.) are scanned images, browsable one lease/well at a time:

- **Oil and Gas Well Records Online**: https://www.rrc.texas.gov/oil-and-gas/research-and-statistics/obtaining-commission-records/oil-and-gas-well-records-online/
  — search by API number or lease, records from 1981–present as PDF (older /
  oversized ones as TIFF). Requires a PDF viewer; no bulk export.
- **Wellbore Query** app: `https://webapps2.rrc.texas.gov/EWA/` — a
  server-rendered JSP search form (search by API number, operator, lease,
  county, field, district, permit number). Good for interactive
  spot-checking a specific well's full drilling/completion/plugging history.
  It's a legacy session-based web app, not a REST/JSON API — I tried driving
  it with a direct query-string API-number parameter and it just re-rendered
  the blank search form, so it isn't reliably scriptable without a real
  browser session. Use it by hand, or automate with browser automation if
  truly needed — not covered here.

---

## 5. Examples

Both scripts are plain `requests`, no API key, no auth. Install once:

```bash
pip install requests
```

- `single_well.py` — look up one well by (partial or full) API number.
- `bulk_county.py` — download every well RRC has for a given Texas county
  (defaults to Tarrant County), paginating past the 1000-record cap, with
  status counts and CSV output.

Run:

```bash
python3 single_well.py 43934308
python3 bulk_county.py --county Tarrant --out tarrant_wells.csv
```
