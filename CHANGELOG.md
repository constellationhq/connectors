# Changelog

## v0.8.5 (2026-11-24)

v0.8.4 was never published to PyPI, npm, RubyGems or Maven (only its ghcr images were pushed); its
changes ship here, with the wording fixes below.

### For operators

- Bridges and `constellation conformance` say an approved action is past its decide-by time, not
  its "cancel deadline". A ground network's own cancel-by time on an offer or a held booking keeps
  its name.
- Forecast view (Yamcs, COSMOS, Open MCT): the rain-fade swap chart shows what the advice adds to
  each at-risk pass (the GB expected in the rain, and that plus `gain_gb`), not the pass's p10
  against its p50, which are equal on a forward forecast.
- Rain-fade swaps read as advice everywhere: "Add a backup pass" (keep this one) or "Move to the
  backup", with the gain over doing nothing (`gain_gb`) and why, and a decide-by time. Never
  "cancel": Constellation books and cancels nothing. The loss is the note's "Rain costs N% of its
  dry X GB", else the GB expected in the rain, never a p10 that equals the p50.
- Yamcs: a timeline item for each open swap's backup pass, AOS to LOS with its expected GB; the
  pass's own item names the backup pass, its AOS and LOS and the decision.
- Yamcs: no duplicate events after a bridge restart. The last event per item is kept in
  `STATE_DIR` (`yamcs-bridge-sent.json`), so a republished run writes nothing Yamcs already holds.
- Yamcs: `compose.yaml` and `.env.example` show how to join the Yamcs container's Docker network.
- COSMOS: `SWAP` gains `ADVICE`, `BACKUP_LOS`, `BACKUP_GB` and `GAIN_GB` (appended, so earlier
  items keep their place). `KEEPS_GB_P10` is deprecated and off the screen; `DEADLINE` is
  "Decide by"; `EXPECTED_GB_P50` is the GB expected in the rain, not "GB if dry".
- Lattice: a ReservePass task on a swap's backup pass is decided from the swap's own action (its
  AOS, LOS and expected GB), with no pass-yield feed needed. Only approved advice to move
  (`action: move`) denies the original; advice to add keeps it taskable.
- Yamcs, COSMOS and Lattice report each approved action's outcome in the delivery report
  (`actions`: acted, or refused when it arrived past its decide-by time). An API from before
  outcomes still gets the delivery, sent again without them.
- Open MCT: the plugin installs Open MCT 4.3.1's `PlanLayout`, or `Plan` in versions that name it
  so; the passes object was "Unknown Type" in stock 4.3.1.

### For developers

- The `act` scaffolds (`constellation new python|go|ts`) and the SDK docs no longer say "book the
  backup pass, cancel the original". A swap is advice: `add` a backup pass and keep this one, or
  `move` to the backup; the scaffolds show where your own booking calls go for each.
- An approved action is "advice to act on", not "an instruction to carry out".
- Built against contract 2.8.1 (was 2.1.0), which adds the advisory swap fields and per-action outcomes.
- Python (`constellation_space.models`) and TypeScript (`types.ts`) declare the advisory swap
  fields on an action, all optional: `action` (`add` or `move`; read any other value as `add`),
  `reason` (`adds_capacity`, `overlap`, `paid_booking` or `original_empty`), the backup pass's
  `to_until`, `to_duration_s` and `to_gb`, and `gain_gb`, the GB the advice adds over doing
  nothing at the p50.
- `gain_gb_p10` is deprecated: use `gain_gb`. It stays declared on both models for now.

## v0.8.3 (2026-11-20)

- Forecast view (Yamcs, COSMOS, Open MCT): a MODCOD plan's row says how many times the MODCOD
  changes over the pass ("3 changes"), counted as the COSMOS `MODCOD CHANGES` item and the
  Lattice bridge count them: the steps after the one at AOS. It said "N MODCODs" and counted
  every step, so a plan that stepped up and back down read "10 MODCODs".
- Forecast view: the ledger shows a probability or share as a percent (`success 97%`), as the
  lock-loss risk already was, not rounded to one decimal (`1.0 probability`).
- Forecast view: chart labels show GB at one decimal everywhere (`164.0`, `96.0`) and counts as
  whole numbers (the MODCOD mix).
- Forecast view: the Passes tab opens on pass yield when a MODCOD plan is there too, whatever
  order the workflows are listed in.
- COSMOS: `MODCOD` shows the plan of the next pass to start, as `NEXT_PASS` does, not a pass
  already under way, and reads `NONE` with no plan ahead. `PLANNED_PASSES` counts the passes
  ahead with a plan.
- COSMOS: `SWAP MINUTES_TO_DEADLINE` says when it turns yellow (30 minutes or less) and red (15
  or less). COSMOS limits include the value at the limit, so 15.0 is red. The limits are
  unchanged.

## v0.8.2 (2026-11-20)

- COSMOS: a new OpenC3 Store cover image (`public/store_img.png`).
- Plainer wording across the connectors: the Forecast view header drops its chip and says
  "Predictions from Constellation workflows.", and the COSMOS screen title, packet and gem
  descriptions, Open MCT, Yamcs and Lattice texts drop the repeated "not measured telemetry" and
  "not a scheduled contact" lines.

## v0.8.1 (2026-11-19)

- The COSMOS plugin is ready for the OpenC3 Store: the gem carries the store title, keywords,
  cover image (`public/store_img.png`), access type and minimum COSMOS version (7.0.0), the
  Apache-2.0 LICENSE, and the COSMOS guide as its homepage.
- CI installs the COSMOS gem into stock COSMOS Core 7.5.1 and 7.0.0 with the COSMOS CLI and checks that
  the bridge fills the `CONSTELLATION` packets, the screen is listed and the Forecast tool is
  served.
- COSMOS: a `CONSTELLATION FORECAST` Telemetry Viewer screen shows every packet.
- COSMOS: the bridge logs to COSMOS's log messages (CmdTlmServer), says what to do with no key or
  no runs yet, says an API outage once and keeps the forecast it holds fresh meanwhile, and
  while a refused key or a plan or rate hold-off waits too, so the minutes left to a swap and the
  next pass keep moving.
- COSMOS: a `consumer` the API would refuse (it takes lowercase letters, digits and hyphens) and
  a plain-http API URL to another host are each said once at start, and the bridge waits rather
  than exit and be respawned by COSMOS over and over.
- COSMOS: `NEXT_PASS` is the next AOS, as the Forecast tool shows it, not a pass already under
  way. A packet is injected only once a run of its kind has arrived, so a kind not sent shows no
  value rather than `NONE` and 0. `NEXT_PASS`, `CAPACITY` and `MODCOD` read `NONE` when a run of
  their kind leaves nothing to show, rather than keep the last run's values. `SWAP PENDING`
  counts a held swap only until the API would expire it (its cancel deadline, else its pass's
  start). Floats show at the precision they are sent, and the summed GB items (`NEXT_24H` and
  `CAPACITY`) are 64-bit so a large sum keeps its third decimal. A string longer than its item
  (an id the API allows) is cut to fit, where COSMOS would drop the whole packet. `RUN` and its
  screen box say they are the latest run received, of any kind, and `MODCOD MIN_SINR_DB` that it
  is the lowest SINR at a MODCOD change.
- COSMOS: the Forecast tool says no forecasts yet, not "COSMOS answered 500", before the first
  run on COSMOS before 7.5, whose storage API answers 500 for a file not written yet.
- Python client and bridges: a call carrying the API key follows no redirect, so the key never
  goes to another host or over plain http.
- COSMOS: a bridge repointed at another API starts afresh rather than at the old API's cursor.
- COSMOS: the Forecast tool takes COSMOS's title bar and theme colours; the install dialog says
  where the API key goes.
- Forecast view (Yamcs, COSMOS, Open MCT): the GB chart's axis reaches the highest hour; a swap
  run whose swaps are all held for approval no longer reads as no risk; held approvals are counted
  once, not again in the section header; and the header says nothing shown is measured
  telemetry.
- Built against API contract 2.1.0, as v0.8.0.

## v0.8.0 (2026-11-18)

- Built against API contract 2.1.0 (was 1.6.0). Contract 2.0.0 is a major release: it removed
  three MCP tools, each replaced by one that already exists or is added in the same contract:
  `get_fleet` by `get_topology`, `get_telemetry_v2` by `get_telemetry`, and `send_telemetry_v2`
  by `send_telemetry`. An MCP client calling the old names must move to the new ones.
- The API gains `DELETE /topology/demo-fleet`, and `get_predictions` takes `target_at` and the
  model families `demand`, `rfi`, `snr`, `ssa` and `weather-attenuation`.
- The Python and TypeScript result types take the GSaaS booking fields: `Action.to_offer`,
  `Action.from_held` and `Entity.booking`, all optional.
- The bridges' protocol is unchanged, so 0.7.x bridges keep working.

## v0.7.1 (2026-11-12)

- Built against API contract 1.6.0 (was 1.5.1). The MCP tools gained `get_passes` and paged
  `get_run`; the bridges' protocol is unchanged, so 0.7.0 bridges keep working.
- npm publishes with trusted publishing (OIDC) and provenance, with no token.
- The Open MCT plugin's header no longer says to wait for an API release before using the API
  source.
- README and CHANGELOG name the contract each release was built against.

## v0.7.0 (2026-11-12)

The first public release. Every package, image and plugin is 0.7.0, built against API contract
1.5.1.

- Bridges for Yamcs (a plugin jar with the Forecast page and `/ConstellationForecast`
  parameters, and a bridge image), OpenC3 COSMOS (a plugin gem with a target, a Forecast tool and
  a bridge microservice), Anduril Lattice (a bridge image publishing antennas and passes and
  answering ReservePass tasks) and Open MCT (a one-file plugin reading the results feed or Yamcs).
- `constellation-space` on PyPI and `@constellationspace/client` on npm: API clients, the
  workflow results protocol, and a bridge base class that checks in, keeps its cursor, starts at
  the newest run, applies each run once and acts on each approval exactly once.
- The `constellation` CLI: `login`, `results tail`, `mock`, `conformance`, `bridge new` (Python,
  TypeScript and Go scaffolds), `bridge run` and `verify`; and its Go `results` package for Go
  bridges.
- A first start asks the API where to read from (`GET /predictions/feed?from=latest`) and stops
  with a plain error against an API that does not answer it: the bridges need contract 1.5.0 or
  later.
- Releases also publish the Yamcs jar to Maven Central (`space.constellation:constellation-yamcs`),
  the COSMOS gem to RubyGems and the bridge images to `ghcr.io/constellationhq`.
- Every release is signed keyless with Sigstore, with an SBOM per artifact, `SHA256SUMS`, and a
  GitHub build provenance attestation; `constellation verify` checks a download.
