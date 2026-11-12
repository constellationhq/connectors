# Constellation connectors

Bring Constellation's forecasts and approved actions into your own systems: ready-made bridges
for four mission control systems, the Python and TypeScript clients, the `constellation` CLI, and
the API contract they all follow. Guides:
[platform.constellation.space/docs/connectors](https://platform.constellation.space/docs/connectors).

## Bridges

Each takes one download from the [latest release](https://github.com/constellationhq/connectors/releases)
(or one image) and one Constellation API key.

| System | Install | Guide |
| --- | --- | --- |
| Yamcs | `constellation-yamcs.jar` into Yamcs's `lib/ext/`, then, from `yamcs-constellation-<version>.tar.gz`, `cp .env.example .env`, fill it in and `docker compose up -d` | [Yamcs](https://platform.constellation.space/docs/connectors/yamcs) |
| OpenC3 COSMOS | Admin > Plugins: `openc3-cosmos-constellation-<version>.gem` | [COSMOS](https://platform.constellation.space/docs/connectors/cosmos) |
| Anduril Lattice | In [`bridges/lattice/deploy/`](bridges/lattice/deploy): `cp .env.example .env`, fill it in, `docker compose up -d` | [Lattice](https://platform.constellation.space/docs/connectors/lattice) |
| Open MCT | `constellation-openmct.js` next to Open MCT's `index.html` | [Open MCT](https://platform.constellation.space/docs/connectors/open-mct) |

## Your own bridge, in any language

A bridge is an HTTP client of three endpoints. [Receive
results](https://platform.constellation.space/docs/connectors/receive-results) documents them from
the contract in [`contract/`](contract), so you can write one with no code from here. The clients
and the CLI are the quicker path:

    pip install constellation-space                 # Python: constellation_space
    npm install @constellationspace/client          # TypeScript
    constellation bridge new my-bridge --lang python   # or ts, go
    constellation mock                              # a demo fleet on localhost:7070
    constellation conformance -- python bridge.py   # 21 checks against the mock
    constellation results tail                      # print runs as they arrive

[`examples/`](examples) holds three, each run in CI against the mock:

- `python-custom-bridge/`: a bridge on the Python `Bridge` base class.
- `go-custom-bridge/`: a bridge on the CLI's Go `results` package (its `go.mod` replaces it with `../../cli`).
- `typescript-results-dashboard/`: a live table of passes and actions from `@constellationspace/client`.

## Layout

| Path | What it is |
| --- | --- |
| `bridges/yamcs/` | `widget/`: the Forecast page jar (`widget/build.sh`, JDK 21+); `deploy/`: the bridge's compose, Dockerfile and `.env.example` |
| `bridges/lattice/` | `deploy/`: the bridge's compose, Dockerfile and `.env.example`; `proto/`: the ReservePass task definition |
| `bridges/cosmos/` | The COSMOS plugin's root (`rake build VERSION=x` builds the gem) |
| `bridges/openmct/` | `widget/`: the plugin (`widget/build.sh` bundles it into one file) |
| `bridges/shared/` | The Forecast view the Yamcs page, the COSMOS tool and the Open MCT plugin all draw with |
| `clients/python/` | `constellation-space`: client, results protocol, bridge base, and the Yamcs and Lattice bridges |
| `clients/typescript/` | `@constellationspace/client`: client and bridge base |
| `cli/` | The `constellation` CLI and the Go `results` package |
| `contract/`, `contract.lock` | The API contract, pinned per file; `make contract` checks it |

## Compatibility

What each release (one tag, `vX.Y.Z`, for everything) is built and tested against. The contract
is the one `contract.lock` pins; the Fleet Agent is released on its own `agent-vX.Y.Z` tags.

| Release | Contract | Fleet Agent | Yamcs (jar built and tested against) | Python | Node | Go | Open MCT (tested against) | COSMOS | Lattice |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| v0.7.0 | 1.5.1 | agent-v0.1.0 | 5.14.0, 5.13.7 | >= 3.11 | >= 18 (client), >= 22.6 (TypeScript bridge scaffold) | >= 1.22 | 4.3.1 | not tested in COSMOS | stub only |

CI runs on every pull request and release, against a stub Constellation API: the jar in stock
Yamcs for each version listed with the Yamcs bridge writing into it; stock Open MCT with the
plugin in headless Chromium; the COSMOS gem built and its bridge unit tested, not installed into
a COSMOS; the Lattice bridge against a stub of the Lattice routes it calls. Images are built for
linux/amd64 and linux/arm64, every test runs on linux/amd64.

## Verify a release

Every release is signed keyless with Sigstore by this repository's release workflow, and every
file carries a build provenance attestation:

    constellation verify <download folder>     # SHA256SUMS, its signature, then each file
    gh attestation verify <file> -R constellationhq/connectors
    cosign verify ghcr.io/constellationhq/lattice-bridge:<version> \
      --certificate-identity-regexp '^https://github\.com/constellationhq/connectors/\.github/workflows/release\.yml@refs/tags/v' \
      --certificate-oidc-issuer https://token.actions.githubusercontent.com

`CHANGELOG.md` lists what each release changed.

## Contributing

`contract/` changes only through `make contract-update VERSION=X.Y.Z`. Within the hour of a new
contract, the contract-bump workflow opens one PR, `contract: bump to X.Y.Z`; merge it by hand
once its CI is green. `make contract-test` runs everything that reads `contract/`, and the
contract-nightly workflow runs it against the newest contract and checks the dev API's feed,
keeping one issue open while either fails.

## Security

Report a vulnerability to security@constellation.space, never in a public issue.

## License

[Apache License 2.0](LICENSE). Yamcs, OpenC3 COSMOS, Open MCT and Anduril Lattice are their
owners' products, under their own licenses.
