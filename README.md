# texas-well-platform

Sigilint's Texas Railroad Commission (RRC) well data work: where the data
comes from, how to pull it, and a backend architecture (domain/data/service
layers) for ingesting, deduplicating, and serving it. Built to support
POC-03 (abandoned-well methane/H2S/VOC monitoring) — see
[`well-platform/content-overview` context in `docs/ARCHITECTURE.md`](well-platform/docs/ARCHITECTURE.md).

## Layout

- **[`texas-rrc-wells/`](texas-rrc-wells/)** — research and access guide for
  RRC's public well data: where to find it, how to query it (no API key
  required), a single-well lookup example, a bulk county-download example,
  and a standalone CSV-based well-deduplication analysis script.
- **[`well-platform/`](well-platform/)** — the backend: a PostgreSQL/PostGIS
  domain+data+service-layer architecture for ingesting RRC data through a
  two-stage raw-then-curate pipeline, resolving well identity (RRC's own
  identifiers turn out to be unreliable for a meaningful subset of
  records — see `well-platform/docs/ARCHITECTURE.md`), and serving search/
  monitoring-candidate/sensor-reading queries. No UI yet — see
  `well-platform/docs/UI_PLANNING.md` for how the backend was shaped with
  future UI/map/mobile needs in mind.

Start with `well-platform/docs/ARCHITECTURE.md` — it explains the design
choices (and the real bugs found and fixed while building this) end to end.
