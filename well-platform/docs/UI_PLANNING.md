# UI Planning — not building yet, but designing the backend for it

No UI code here. This is a planning document so `well-platform`'s
service/data layers don't accidentally make a reasonable future UI
harder to build — every "what will the UI need" question below already
shaped a decision in [`ARCHITECTURE.md`](ARCHITECTURE.md) (bbox search,
`monitored_only`/`orphaned_only` filters, `WellSearchQuery`'s shape, the
`DataSourceType.FIELD_CREW` provenance value, etc.).

## Two different UIs, not one

The domain model's split between "the full RRC inventory" and "wells we
actually monitor" (see `ARCHITECTURE.md` "Two status axes") maps onto two
genuinely different UI problems:

| | **Browse / report** | **Monitor** |
|---|---|---|
| Data volume | ~1.4M wells statewide | tens to low-thousands, growing slowly as Sigil Nodes deploy |
| Update frequency | Synced from RRC weekly/monthly (see ingestion cadence in `../texas-rrc-wells/README.md`) | Sensor readings arriving continuously/near-real-time |
| Primary users | Analysts doing site selection, reporting, compliance review | Control-room operators (this is "Sigil Watch"), field crews |
| Core interaction | Filter/search a huge static-ish dataset, pick candidates | Watch live status, react to alerts, log field visits |
| Precedent in this repo | — (net new) | `well-monitor/` hackathon prototype (topo map, status pills, pollutant sparklines) — inspiration, not a spec |

Same backend, same database, same `Well` entity — but they're different
screens with different performance and freshness requirements, and
probably different primary devices (control-center on desktop, field crew
on mobile). Worth planning as two flows from the start rather than one
screen trying to do both.

## The scale problem, and why it shapes the API now

1.4M+ wells can't be sent to a browser as 1.4M markers — not as DOM
elements, not really even as canvas points without real effort. This is
the single biggest constraint on how "browse the well map" ends up
working, and it's why `WellSearchQuery` already has a `bbox` filter and
`WellRepository.search` already does the `ST_Within(WellORM.geom, envelope)`
spatial query (see `ARCHITECTURE.md`) instead of "fetch everything and
filter client-side." The pattern this points to for an eventual API:

- **Viewport-driven fetching.** The map asks for "wells in this bbox at
  this zoom," not "all wells." Already supported at the repository layer;
  an API layer just needs to expose `bbox` + pagination as query params.
- **Cluster/aggregate at low zoom, individual pins only close in.** At
  state or county zoom, show counts or density (e.g. a choropleth by
  `regulatory_status` or a heatmap of orphaned-well density per county —
  both are `GROUP BY` queries the schema already supports), not points.
  Below some zoom threshold, switch to real markers via the bbox query.
- **Vector tiles as the likely eventual answer**, not paginated JSON, once
  the "browse" UI is real. PostGIS can generate Mapbox Vector Tiles
  directly (`ST_AsMVT`), served by a small tile server (`pg_tileserv` or
  `Martin` — both point at an existing PostGIS database with no data
  duplication) sitting next to the API rather than through it. This is a
  backend addition, not a UI concern, and doesn't block anything built so
  far — noting it here so "why does the map feel slow with naive
  pagination" has a known answer when it comes up.
- **The "monitor" UI doesn't have this problem** — its working set is
  small enough that "fetch all monitored wells with their latest reading"
  is just a query, not an optimization problem. Don't over-build tiling
  for a dataset that's currently in the hundreds.

## Map layering: basemap/tile provider

Candidates, weighed against Sigilint's own positioning
("leverage COTS hardware and **open source systems**" — `content/overview.md`)
and the field-monitoring use case (rural, often poor-connectivity well
sites, not urban navigation):

| Option | Fit |
|---|---|
| **Google Maps JS API** | Best-known basemap, but: per-load billing at any real traffic, and its ToS explicitly restricts caching/offline storage of tiles — a real problem for a field-crew mobile app used at well sites with no signal. Sits oddly next to an "open source" positioning. |
| **MapLibre GL JS + vector tiles** (self-hosted via `pg_tileserv`/`Martin`, or a provider like MapTiler/Protomaps for the basemap layer) | Open source, no per-load billing at self-hosted scale, tiles can be bundled/cached for offline field use, and — because it's vector not raster — well/status data can be styled as just another data layer in the same renderer rather than an overlay bolted onto someone else's map. Matches the brand positioning. This is the recommendation. |
| **Esri (ArcGIS API for JS / ArcGIS Online)** | Worth naming because **RRC's own GIS Viewer is Esri** (see `../texas-rrc-wells/README.md`) — there's a real argument for using Esri's topo/imagery basemaps specifically for interop/familiarity with RRC's own maps, or for an internal analyst tool (see QGIS below) even if the customer-facing app doesn't run on Esri. Licensing cost is the tradeoff. |
| **Plain OpenStreetMap raster tiles (Leaflet)** | Cheapest, simplest, zero licensing friction — but raster tiles for a 1.4M-point dataset means the clustering problem above has to be solved entirely client-side with less flexibility than vector tiles give. Fine for an early internal prototype; probably not the long-term choice. |

`well-monitor/topo-map.jsx` (the hackathon prototype) hand-draws a
synthetic topographic field rather than using a real basemap at all —
fine for a demo, but real well coordinates need a real basemap/terrain
layer once this is real data. Topographic/terrain context (elevation,
drainage, access roads) is genuinely relevant for a field crew reaching a
remote wellsite, which is a point in favor of a basemap provider that
offers real terrain tiles (MapTiler and Esri both do; plain OSM doesn't).

## GIS software (internal tooling, not the product)

Separate from the customer-facing map: analysts doing county-level site
selection or one-off spatial analysis (e.g. "which orphaned wells are
within a mile of a school or livestock operation" — directly relevant to
the safety framing in `content/overview.md`) will likely want a desktop
GIS tool against this same database, not a bespoke UI for every ad hoc
question:

- **QGIS** (free, open source, connects directly to PostGIS) is the
  natural fit here — an analyst can point it at the `wells` table via its
  native PostGIS connector and do arbitrary spatial queries/exports (to
  shapefile, GeoJSON, CSV) without any custom tooling being built for
  them. Worth documenting a standard PostGIS connection recipe once a real
  environment exists.
- **QGIS also directly consumes RRC's own ArcGIS REST layers** (the ones
  documented in `../texas-rrc-wells/README.md`) as a live source, useful
  for cross-checking our ingested data against RRC's current live state
  without writing a script.

This is a "give analysts QGIS + read access to Postgres" recommendation,
not something to build — flagging it so it doesn't get reinvented as a
custom internal admin UI later.

## Search & browse UX (backend-shaped, not built)

Everything below maps directly onto a `WellSearchQuery` field or a
`WellService` method that already exists, which is the point — the API
layer, when built, should mostly be argument parsing on top of what's
here rather than new query logic:

- **Filter panel**: county, district, operator, regulatory status,
  monitored-only, orphaned-only — all already fields on `WellSearchQuery` /
  columns with indexes on `WellORM` (`ARCHITECTURE.md` "Data layer notes").
- **Text search**: API number / well number / lease name —
  `WellSearchQuery.text`, already implemented as an `ILIKE` match in
  `WellRepository.search`.
- **List + map dual view**: same `search()` call backs both; a list view
  is just the same query without the bbox filter tied to a map viewport.
- **"Candidate wells" view**: a dedicated screen for the site-selection
  workflow (`WellService.list_monitoring_candidates`) — this is arguably
  the single highest-value screen for the actual POC-03 program, distinct
  from general browsing, and should probably exist before a general map
  browser does.
- **Well detail view**: merges RRC regulatory history (`regulatory_status`,
  `regulatory_status_raw`, location provenance) with, if monitored, current
  sensor snapshot + history — this is what `well-monitor/info-panel.jsx`
  prototypes visually (status pill, location grid, pollutant bars +
  sparklines) and is a reasonable visual reference for that specific
  screen even though its data is fabricated.

## Monitoring / control-center UX ("Sigil Watch")

- **Needs near-real-time updates**, not just request/response — a
  WebSocket or SSE channel pushing new readings/alerts to an open control
  center view, rather than polling. `SensorReadingService.record_readings`
  already returns the `ThresholdAlert` list at the moment a reading comes
  in, which is the natural hook point for pushing an alert out.
  `well-monitor`'s "MESH ACTIVE" status indicator in `info-panel.jsx`
  gestures at this same live-connection expectation.
- **Alert/leak history**, not just current state — since
  `SensorReadingService` deliberately doesn't auto-clear `LEAKING` (see
  `ARCHITECTURE.md`), the UI needs a place to show *why* a well is still
  flagged and let an operator acknowledge/clear it
  (`WellService.update_operational_status`), which is itself an audit-worthy
  action worth its own history rather than a silent field overwrite.
- **Reporting/export**: the actual business driver (safety + mineral-lessor
  liability, per `content/overview.md`) implies a periodic or on-demand
  report — "here is the monitoring history for well X over the last year"
  — as a PDF or structured export, not just a live dashboard. Not designed
  yet; flagging that `MetricReadingRepository.history` is the query this
  would be built on.

## Mobile / field-crew app

A separate, probably higher-priority-than-it-looks surface: someone
physically visiting a well site (to install a Sigil Node, to do the
`INSPECTION` operational-status work already modeled, or to confirm an
alert) needs:

- **Offline-capable maps** — exactly the case against Google Maps above;
  vector tiles can be pre-downloaded for a work area, raster OSM tiles can
  be cached too but less flexibly.
- **"Nearest candidate wells to me"** — a radius/bbox query around GPS
  position, which is the same spatial query machinery already built for
  the browse map, just centered on the device instead of a map viewport.
- **Field data entry that becomes ingestion, not a side channel** — a crew
  logging "checked well X, no visible leak, node installed" should land in
  the same `metric_readings`/`operational_status` data the sensors feed,
  tagged with `DataSourceType.FIELD_CREW` (already in `domain/enums.py`
  for exactly this) rather than a separate mobile-app-only data store that
  has to be reconciled later.
- **Sync-on-reconnect**, given rural well sites frequently have no signal —
  the mobile app queues writes locally and pushes them through the same
  `SensorReadingService`/`WellService` calls once connectivity returns;
  nothing about the service layer as built assumes synchronous,
  always-connected callers.

## Related products / prior art

Worth tracking who else operates in this space, so UI/positioning
decisions are made knowingly rather than accidentally reinventing or
colliding with an existing product. Add an entry here whenever one
surfaces — this is expected to grow.

- **[texas-drilling.com](https://texas-drilling.com)** — a commercial
  subscription SaaS ($39.99–$89.99/mo across three tiers) for searching
  Texas wells/leases/operators/permits/production, sourcing from RRC under
  the Texas Public Information Act (the same public data we use). Offers
  "Map Based Searching" as a named paid feature in every tier — confirming
  the "browse wells on a map" problem is real and already monetized by
  someone — but the map itself is gated behind login, so its
  implementation (basemap/library) wasn't inspectable without subscribing.
  No public API for developers. Its Tarrant County well count (7,334, per
  its public county page) landed within 0.4% of ours (7,305, independently
  derived from RRC's live ArcGIS service — see `ARCHITECTURE.md`), a
  useful external cross-check that our ingestion is in the right ballpark.
  **Not a competitive overlap in the differentiated sense**: it's a
  research/lookup tool for landmen and investors, with no sensor
  integration, no real-time monitoring, and no exposed data layer — the
  actual product here (instrumented-well leak monitoring, an eventual open
  API, the raw-then-curate data pipeline) doesn't compete with it
  directly. Relevant mainly as a UX reference for the "browse/report" half
  of the two-UI split above, and as a reminder that a paywalled,
  closed-map product is one direction we could differentiate against
  (e.g. an open map/API where texas-drilling.com is closed).

## Open questions for when UI work actually starts

- Auth/role model — analyst (read/search) vs field crew (write
  inspections/readings for their assigned wells) vs admin (enroll/
  decommission wells) isn't designed anywhere yet. The service layer's
  method boundaries (`WellService`, `SensorReadingService`) are a
  reasonable place to hang authorization checks when this is designed, but
  nothing here assumes a particular auth scheme yet.
- Whether "Sigil Watch" (control center) and the RRC browse/report tool
  are one app with two modes or two separate apps sharing this backend —
  leaning toward one app, two modes, given they share the same `Well`
  entity and mostly differ in filters/refresh-rate, but not decided.
- Native mobile (React Native/Flutter) vs a responsive web app usable in
  the field — offline map caching and GPS access are easier natively, but
  worth revisiting once real field-crew workflows are specced in detail.
