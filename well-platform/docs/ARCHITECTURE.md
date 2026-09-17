# well-platform — Architecture

Backend for Sigilint's Texas well data: the full RRC regulatory well
inventory, plus the (much smaller) subset of wells Sigilint has actually
instrumented with a Sigil Node and is pulling sensor readings from. See
`content/overview.md` (POC-03) for the product context this serves —
identifying and monitoring abandoned/orphaned wells for methane/H2S/VOC
leaks, for safety and mineral-lessor liability reasons.

This doc covers the domain/data/service layers and the choices behind them.
For where the source data comes from and how it was found, see
[`../texas-rrc-wells/README.md`](../texas-rrc-wells/README.md) — that
research is the foundation `services/ingestion_service.py` is built on. For
how this backend should shape (without yet building) a UI, see
[`UI_PLANNING.md`](UI_PLANNING.md).

Everything described here has been run on a local
Postgres 17 + PostGIS 3.6 instance, the live RRC service, a full
county-scale ingestion-then-curation pass (Real County, TX — 182 raw RRC
rows landed, 181 canonical wells resolved, re-run twice to confirm
idempotency), and every repository/service method were exercised
end-to-end during development. This isn't a theoretical design doc.

## Layers

```
services/          <- business logic; what a future API calls
  well_service.py         search, browse, monitoring-candidate selection, enrollment
  sensor_service.py       record readings, threshold evaluation, LEAKING transitions
  ingestion_service.py    stage 1: fetch from RRC, land raw rows — no interpretation
  curation_service.py     stage 2: dedup/resolve identity, enrich, promote to `wells`

data/               <- persistence; PostgreSQL/PostGIS only
  orm.py                  SQLAlchemy 2.0 declarative models
  session.py              engine/session factory (DATABASE_URL)
  schema.sql              generated DDL reference (scripts/generate_schema_sql.py)
  repositories/
    raw_feature_repository.py   raw_well_features landing-zone reads/writes
    well_repository.py          Well <-> ORM translation, search, upsert
    reading_repository.py       MetricReading/MetricThreshold <-> ORM, time-series
    ingestion_run_repository.py IngestionRun persistence (audit trail)

domain/             <- framework-agnostic models; no SQLAlchemy import here
  models.py               Well, GeoPoint, County, Operator, SensorDevice,
                           MetricReading, MetricThreshold, IngestionRun,
                           WellSearchQuery/Result — pydantic, used as-is by
                           services and (eventually) API request/response bodies
  enums.py                RegulatoryStatus, OperationalStatus, DataSourceType, ...
  status_mapping.py       RRC raw status -> RegulatoryStatus, enrollment defaults
  dedup.py                well-identity resolution — the dedup rule itself,
                           shared verbatim with ../texas-rrc-wells/dedup_analysis.py
```

A caller never skips a layer: services depend on repositories, repositories
depend on the ORM, nothing outside `data/` imports SQLAlchemy or writes SQL.
An API layer, when it's built, is a thin adapter on top of `services/` —
`domain/models.py` classes are already pydantic, so they can generally be
returned directly as API response bodies without a second schema.

## Why PostgreSQL + PostGIS, not MongoDB

Both were considered, as asked. PostgreSQL/PostGIS won on every axis this
data actually needs:

- **The data is inherently geospatial and needs to stay fast at scale.**
  RRC's own inventory is 1.4M+ wells statewide (see the status-count query
  in `../texas-rrc-wells/README.md`). "Every well in this county" or "every
  well in this map viewport" has to hit a spatial index, not a full scan.
  PostGIS's GiST indexes and `ST_Within`/`ST_Intersects`/`ST_DWithin` are
  mature, standard, and exactly this problem. MongoDB has `2dsphere`
  indexes and does support this reasonably well now — it's not disqualifying
  on its own — but it's the one dimension where Mongo is competitive rather
  than behind.
- **The data is relational, not document-shaped.** Well -> Operator,
  Well -> County, Well -> SensorDevice -> MetricReading is a small number of
  well-defined foreign-key relationships with genuine join needs ("wells
  operated by X, currently leaking, in county Y"). That's what SQL is for.
  Modeling the same thing in Mongo means either denormalizing into deep
  nested well documents (dropping the ability to change what "sensor
  device" or "operator" data captures independently later, and blowing
  document size limits at the well-with-readings-embedded scale) or
  reaching for `$lookup` aggregation pipelines that are worse SQL than SQL.
- **Regulatory/reporting data wants a fixed, enforced schema.** This feeds
  liability and safety reporting — "how many wells in Tarrant County are
  RRC-orphaned and not yet monitored" needs to be an exact, auditable
  count every time, not dependent on every document happening to have the
  same shape. Postgres enforces that at write time; Mongo enforces it by
  convention, if you remember to.
- **Sensor readings are a time series, and Postgres has a credible scaling
  path for that already** — starting as a plain indexed table (what's
  built here) and upgrading to the TimescaleDB extension (still Postgres,
  same SQL, same ORM) if/when Sigil Node volume warrants hypertables and
  retention policies, rather than a second database to keep in sync.
- **MongoDB's actual strength — schema-less, highly variable documents —
  isn't what this data is.** It would have been the right call if sensor
  payloads were wildly heterogeneous blobs we mostly store and rarely
  query structurally. They're not: a fixed (metric_code, value, unit,
  timestamp) shape per reading, queried by well/metric/time-range
  constantly. Where we do want schema flexibility (raw ingestion payloads,
  ad hoc device metadata), Postgres's native `JSONB` gets that without a
  second database — not used yet, but available if a real need shows up.

**Bottom line:** one database, not two. Splitting operational (Postgres)
and sensor (Mongo) data across two systems was considered and rejected —
the join between "which wells are leaking" and "what county/operator is
that well in" is a core query, not an edge case, and keeping it as one SQL
join beats keeping two databases consistent for a marginal, unproven
document-flexibility benefit.

## Domain model choices

### Two status axes, not one

`RegulatoryStatus` (RRC's classification — Active Oil Well, Plugged Gas
Well, Dry Hole, ...) and `OperationalStatus` (Sigilint's own monitoring
state — Active, Inspection, Leaking, Offline, ...) are separate enums on
purpose. RRC's status is regulatory truth we ingest and never write to.
Ours is what our monitoring program is actually doing at a well, and is
`None` for the ~99%+ of wells we've never touched. Collapsing these into
one field would mean either inventing fake RRC statuses for our own
concepts ("leaking" isn't an RRC term) or losing the distinction between
"RRC says dry hole" and "we've confirmed it's not leaking." Kept apart,
`Well.is_monitoring_candidate` (in `domain/models.py`) can cleanly express
the actual business rule: *unmonitored AND (RRC-orphaned OR RRC status is
plugged/abandoned/dry)* — that's the exact query behind
`WellService.list_monitoring_candidates`, i.e. the site-selection workflow
for where to put the next Sigil Node.

### Why metrics aren't an enum

`MetricReading.metric_code` is a free-form string (with a `MetricThreshold`
config row per known code), not a closed `Pollutant` enum. The
`well-monitor` hackathon prototype hardcodes five pollutants
(CH4/H2S/BTEX/VOC/CO2); this schema deliberately doesn't inherit that as a
constraint (per direction during this build — the prototype is inspiration,
not a spec) because real Sigil Nodes will report things that aren't
pollutants at all — battery level, signal strength, pump rate, pressure,
temperature — and a closed enum means a schema migration every time a new
sensor field ships. A narrow (well, metric, time) table is also the
natural shape for a future TimescaleDB hypertable, and is what
`MetricReadingRepository.latest_for_well` / `.history` /
`.wells_exceeding_threshold` are built against.

### Why `wells` denormalizes county/operator instead of foreign-keying them

`WellORM` stores `county_name`/`county_fips`/`operator_name` directly
rather than `county_id`/`operator_id` foreign keys into separate tables.
This is a deliberate tradeoff, not an oversight: RRC is the source of
truth for both (there's no "add a county" workflow of our own — see RRC's
own Counties layer in `../texas-rrc-wells/README.md` §2), the values are
attached per-well at ingestion time with no independent lifecycle, and a
join for every well read (which will be the majority of all reads) to
fetch a name that never changes trades real query cost for a normalization
benefit this data doesn't need. If reference-quality operator data (RRC's
operator number, address, contacts) becomes a real requirement later —
plausible, since RRC does publish it separately — that's an additive
`operators` table and a backfill, not a redesign.

### Both `geom` and plain `latitude`/`longitude` columns on `wells`

`geom` (PostGIS `Point`, SRID 4326) is what spatial queries filter on and
what carries the GiST index. Plain float columns exist alongside it so
"give me lat/lon for map display" doesn't require every caller to unpack
WKB/EWKT or every read query to carry `ST_X`/`ST_Y`. Kept in sync by
`WellRepository` on every write — see `_to_row_values`.

## Raw-then-curate ingestion

Ingestion is two stages, not one, on purpose — this replaced an earlier
single-stage design (RRC -> map straight to `Well` -> upsert) after that
design's dedup logic turned out to be silently dropping real wells (see
"How we got here," below, for the full story of why trusting RRC's own
identifiers directly doesn't work).

```
RRC ArcGIS REST API
      |
      v
RawIngestionService.ingest_county() / .ingest_single()   <- stage 1: land, don't interpret
      |
      v
raw_well_features   (Postgres table — landing zone)
      |
      v
WellCurationService.promote_county()                      <- stage 2: dedup, sanity-check, enrich
      |
      v
wells   (Postgres table — curated, queryable)
```

**Stage 1 (`services/ingestion_service.py`, `RawIngestionService`)** talks
to RRC and lands what it gets back into `raw_well_features`, close to
verbatim, keyed on `(source_layer, rrc_object_id)` — RRC's own per-row
feature ID, which unlike `API` is always genuinely unique. This means
landing data **can never fail on a conflict or lose a row**, no matter how
messy RRC's own identifiers turn out to be — the exact failure mode that
broke the original single-stage design (see below). It does no
normalization, no status mapping, no identity resolution. Every raw row
RRC has ever returned for a scope is preserved, full stop.

**Stage 2 (`services/curation_service.py`, `WellCurationService`)** reads
raw rows back out — from the database, not from RRC — and does the actual
work: resolves which rows are really the same well
(`domain/dedup.py::resolve_well_identities`, see "Well identity resolution"
below), normalizes status (`status_mapping.normalize_rrc_status`), runs
domain validation (a `GeoPoint` outside Texas's bounding box, for instance,
rejects the row instead of silently corrupting a location), and upserts
canonical `Well` records via `WellRepository`. It links every raw row it
consumed back to the well it produced (`raw_well_features.well_id`), so
"which raw RRC rows fed into this well" is always answerable.

**Why this split matters in practice, not just in theory:** dedup/identity
logic is exactly the kind of thing that turns out to be wrong on first
attempt against messy real-world government data (it was, here — see
below). With raw data landed separately, fixing that logic means re-running
curation against already-landed data — no RRC re-fetch, no risk of losing
data while iterating, and `promote_county` is written to be safely
re-run any number of times (upsert-keyed, idempotent — confirmed by running
it twice against the same raw data and getting identical results both
times). A single-stage pipeline can't offer that: fixing a mapping bug
means re-fetching from RRC and hoping nothing was silently dropped on the
first pass, which is exactly what happened.

### Well identity resolution (`domain/dedup.py`)

RRC's `API` field is not a reliable unique well identifier on its own —
first discovered as an `ON CONFLICT` crash, then investigated properly by
pulling live data and measuring it, not guessing. Two things were checked
against real RRC data before landing on the rule below, both counter to
the obvious first guess:

1. **Does a shared API number mean "same location"?** No. RRC's layer
   carries a second field, `GIS_API5` (the well-specific suffix). When it's
   populated, `API` is genuinely reliable — but rows sharing one can still
   be **up to ~2.3km apart** (23 confirmed cases in Tarrant County alone),
   almost certainly permit-estimated vs. actual-drilled location, or
   surface vs. bottomhole for a horizontal well. Distance cannot be used to
   validate or override a reliable API match.
2. **When `GIS_API5` is blank, is location a safe fallback?** Yes, and more
   than that — one blank-`GIS_API5` stub (`API = "385"`) in Real County
   covered **29** distinct wells sharing nothing but an incomplete stub,
   different `OBJECTID`s, coordinates, and statuses. But checked whether
   *genuinely duplicate* unreliable-API pins ever cluster tightly: across
   every blank-`GIS_API5` row in Real County, the closest two distinct
   wells were **67m apart**, and no pair was under 50m. So a small-radius
   spatial cluster is a safe, principled way to build our own identity
   here — not a guess, a threshold picked with real headroom under the
   observed floor.

The resulting rule (`resolve_well_identities`):

- **`GIS_API5` populated** → trust RRC's `API` directly. Rows sharing it
  are the same wellbore recorded at different points in its life; merge
  them regardless of distance.
- **`GIS_API5` blank** → RRC gives no reliable identifier at all. Cluster
  by spatial proximity ourselves (default 50m radius, grid-bucketed
  union-find for county-scale batches; the database path could move to
  PostGIS `ST_ClusterDBSCAN` if a single all-Texas batch ever needs it, but
  per-county batches don't). Each resulting `Well.api_number` is
  synthesized as `"<RRC stub>#<anchor OBJECTID>"` and flagged
  `api_number_is_synthetic = True`, so nobody mistakes it for a real RRC
  API number. `Well.identity_method` (`"reliable_api"` | `"spatial"` |
  `"unclustered_no_location"`) and `Well.source_feature_count` record how
  each well's identity was actually resolved and from how many raw rows —
  surfaced, not buried, because "this well's identity came from clustering
  3 unlabeled legacy pins" is operationally meaningful (worth a lower
  location-confidence treatment in a future UI, for instance), not just a
  debugging detail.

This exact function is shared, unmodified, between the database curation
path and `../texas-rrc-wells/dedup_analysis.py` — a standalone script that
fetches a county's raw rows to CSV and runs the same dedup logic outside
the database, for anyone who wants to inspect or validate the rule without
standing up Postgres. Both paths will always agree, by construction.

### How we got here (worth keeping — it's why the design looks like this)

The first version of this pipeline was single-stage: fetch from RRC, map
straight to `Well`, upsert keyed on `api_number`. It broke immediately —
`ON CONFLICT DO UPDATE command cannot affect row a second time` — because
of the blank-`GIS_API5` problem above. The first fix (dedupe within a
batch, keep the last row seen) stopped the crash but was **silently
wrong**: it collapsed every row sharing a stub `API` into one record,
destroying real distinct wells. Caught only because the RRC-reported count
for Real County (80, via `returnCountOnly`) didn't match what the API
actually returned (182 rows) — a red flag that led to actually measuring
distances instead of trusting the fields at face value, which produced the
two findings above and the current two-stage design.

**The concrete cost of getting this wrong wasn't cosmetic**: under the
naive fix, `WellService.list_monitoring_candidates` for Real County
returned 96 candidate wells. After the real fix, it returns **146** — 50
real, legitimate candidate wells for the monitoring program that the naive
version was making invisible by silently merging them away, not just
double-counting them. For a program whose whole point is finding
abandoned/orphaned wells to instrument, that's the kind of bug that costs
real sites, not just a wrong number in a report.

## Data layer notes

- **`raw_well_features` is keyed on `(source_layer, rrc_object_id)`** — see
  "Raw-then-curate ingestion" above. This upsert can never fail on a
  conflict regardless of how RRC's own identifiers behave, which is the
  entire reason the landing stage exists as a separate table.
- **`wells` is still keyed on `api_number`** (real or synthesized — see
  above), via `INSERT ... ON CONFLICT DO UPDATE ... RETURNING id,
  api_number`. The `RETURNING` matters: for a well that already existed,
  `ON CONFLICT DO UPDATE` never touches `id`, so the DB's actual id can
  differ from the fresh `uuid4()` a newly-constructed `Well` object
  happens to carry. `WellCurationService` uses the returned id (not
  `well.id`) to link raw rows via `raw_well_features.well_id` — using the
  wrong one would silently point provenance links at ids nothing in the
  database has.
- **`operational_status` gets a partial index**, not a plain one — nearly
  every one of ~1.4M rows has it `NULL` (not monitored), so a full B-tree
  over that column would mostly index rows nobody queries for. The partial
  index (`WHERE operational_status IS NOT NULL`) covers the actual "my
  monitored wells" query pattern cheaply.
- **Bulk writes are batched (~500/upsert call)**, matching the pagination
  size ingestion already receives from RRC (1000/page, split into two
  upsert batches) rather than one call per well or one call per 1000-row
  RRC page as a single giant statement.

## Service layer notes

- `RawIngestionService` owns talking to RRC (`RRCWellClient`, same
  endpoints/pagination approach as `../texas-rrc-wells/bulk_county.py` and
  `single_well.py`, refactored into a reusable client) and landing raw rows
  — see "Raw-then-curate ingestion" above for why it stops there rather
  than writing to `wells` directly. It records an `IngestionRun` per pass
  (persisted immediately via `IngestionRunRepository.create`, before any
  raw rows are landed, since they FK-reference it — updated with final
  status/counts in a `finally` block either way) — so "did last night's
  sync finish" is a real query, not a log grep.
- `WellCurationService.promote_county` is stage two: reads landed raw rows,
  resolves identity (`domain/dedup.py`), and upserts canonical wells. Safe
  to re-run at any time — re-curating already-curated data is how a fixed
  dedup rule gets applied retroactively.
- `WellService` is the read/browse/enrollment surface: `search`,
  `list_monitoring_candidates`, `enroll_well_for_monitoring`,
  `update_operational_status`, `decommission_well`. `enroll_...` picks a
  sensible default `OperationalStatus` from the well's `RegulatoryStatus`
  (`status_mapping.DEFAULT_OPERATIONAL_STATUS_ON_ENROLLMENT`) unless one is
  given explicitly.
- `SensorReadingService` records readings and is the **only** place a
  reading is allowed to change a well's `operational_status`
  automatically — a critical threshold breach flips a well to `LEAKING`.
  It deliberately does *not* auto-clear `LEAKING` back to `ACTIVE` on a
  later normal reading; clearing an alert is a human/field-crew action via
  `WellService.update_operational_status`, so a transient sensor dip can't
  quietly wave off a real leak.

## Running it

```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql+psycopg://user:pass@host:5432/well_platform

# one-time: create tables (or apply data/schema.sql directly with psql)
python3 -c "from data.session import get_engine; from data.orm import Base; Base.metadata.create_all(get_engine())"

# land + curate in one go (the normal path)
python3 scripts/ingest.py --county Tarrant
python3 scripts/ingest.py --api 43934308

# or split the stages -- e.g. to land now and curate later, or to
# re-curate already-landed data after a dedup-logic fix without
# re-fetching from RRC at all
python3 scripts/ingest.py --county Tarrant --land-only
python3 scripts/ingest.py --county Tarrant --curate-only
```

Requires the `postgis` extension available on the target Postgres server
(`CREATE EXTENSION postgis;` — `data/schema.sql` does this for you).

## What's deliberately not here yet

- **No API framework wiring.** `services/` is written to be called
  directly by tests/scripts now and wrapped by a REST (FastAPI recommended
  — pydantic domain models pass through as request/response schemas with
  minimal glue) or other API layer later, per direction to hold off on the
  UI-facing layer.
- **No Alembic/migration tooling set up** — `data/schema.sql` and
  `Base.metadata.create_all` are enough for a from-scratch environment;
  add Alembic once there's a real deployed database with data in it that
  needs incremental migrations, rather than speculatively now.
- **No operator/lease-name enrichment from RRC's "Statewide API Data" bulk
  file.** The live ArcGIS layer used for ingestion doesn't carry
  operator/lease name (see `../texas-rrc-wells/README.md` §1's API-field
  caveat and §3); wiring up that secondary bulk-file join is future work,
  not required for location/status ingestion or for the monitoring-program
  use cases this was built for.
