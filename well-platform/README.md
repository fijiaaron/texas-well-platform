# well-platform

Backend domain/data/service layers for Sigilint's Texas well data: the full
RRC (Railroad Commission of Texas) regulatory well inventory, plus the
subset of wells Sigilint has instrumented with sensors ("Sigil Nodes") and
is monitoring for methane/H2S/VOC leaks — see `content/overview.md` (POC-03)
for the product context.

No UI here yet, by design — see `docs/UI_PLANNING.md` for how the backend
was shaped with future UI/map/mobile needs in mind without building them.

## Layout

```
domain/       framework-agnostic models (pydantic) + enums + RRC status mapping + dedup.py
data/         PostgreSQL/PostGIS persistence: SQLAlchemy ORM + repositories
              (raw landing table + curated `wells` — see ARCHITECTURE.md)
services/     business logic: raw ingestion, curation/dedup, well search/enrollment, sensor readings
scripts/      runnable entry points (ingest.py, generate_schema_sql.py)
docs/         ARCHITECTURE.md (layers + choices), UI_PLANNING.md (future UI needs)
```

Ingestion is two stages — land raw RRC data untouched, then curate
(dedup/enrich/promote) it into queryable wells — not one. See
`docs/ARCHITECTURE.md` "Raw-then-curate ingestion" for why; it's not
incidental, a real bug in a single-stage version is what drove the split.

Also see [`../texas-rrc-wells/README.md`](../texas-rrc-wells/README.md) —
the research this is built on: where RRC's data lives, how it was found,
the raw single-well/bulk-county examples this project's
`services/ingestion_service.py` generalizes into a reusable client, and
[`dedup_analysis.py`](../texas-rrc-wells/dedup_analysis.py), a standalone
CSV version of this project's well-identity-resolution logic
(`domain/dedup.py`, shared verbatim) for inspecting the dedup rule without
standing up Postgres.

## Quick start

```bash
pip install -r requirements.txt

# needs a Postgres server with the postgis extension available
export DATABASE_URL=postgresql+psycopg://user:pass@host:5432/well_platform
psql "$DATABASE_URL" -f data/schema.sql          # or: Base.metadata.create_all(...)

# land raw RRC data, then dedup/curate it into queryable wells
python3 scripts/ingest.py --county Tarrant
python3 scripts/ingest.py --api 43934308
```

Read `docs/ARCHITECTURE.md` first — it explains the PostgreSQL-vs-MongoDB
decision, the two-stage raw-then-curate ingestion design (and the real bug
that drove it), the domain model's two separate status axes (RRC's
regulatory status vs. our own monitoring status), why sensor metrics
aren't a closed enum, and what's deliberately not built yet (API framework
wiring, migrations tooling, operator/lease enrichment).

Every piece of this — schema, repositories, services, the ingestion
pipeline — was run against a real local PostgreSQL 17 + PostGIS 3.6
instance and the live RRC service during development, not just written and
left untested.
