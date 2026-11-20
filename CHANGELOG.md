# Changelog

## v0.8.1 (2026-11-19)

- The COSMOS plugin is ready for the OpenC3 Store: the gem carries the store title, keywords,
  cover image (`public/store_img.png`), access type and minimum COSMOS version (7.0.0), the
  Apache-2.0 LICENSE, and the COSMOS guide as its homepage.
- CI installs the COSMOS gem into stock COSMOS Core 7.5.1 with the COSMOS CLI and checks that
  the bridge fills the `CONSTELLATION` packets, the screen is listed and the Forecast tool is
  served.
- COSMOS: a `CONSTELLATION FORECAST` Telemetry Viewer screen shows every packet.
- COSMOS: the bridge logs to COSMOS's log messages (CmdTlmServer), says what to do with no key or
  no runs yet, says an API outage once and keeps the forecast it holds fresh meanwhile.
- COSMOS: `NEXT_PASS` is the next AOS, as the Forecast tool shows it, not a pass already under
  way. `NEXT_PASS`, `CAPACITY` and `MODCOD` read `NONE` when a run leaves nothing to show,
  rather than keep the last run's values. `SWAP PENDING` counts only held swaps still before
  their deadline. Floats show at the precision they are sent.
- COSMOS: a bridge repointed at another API starts afresh rather than at the old API's cursor.
- COSMOS: the Forecast tool takes COSMOS's title bar and theme colours; the install dialog says
  where the API key goes.
- Forecast view (Yamcs, COSMOS, Open MCT): the GB chart's axis reaches the highest hour, and a
  swap run whose swaps are all held for approval no longer reads as no risk.
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
