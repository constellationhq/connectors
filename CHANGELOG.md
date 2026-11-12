# Changelog

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
