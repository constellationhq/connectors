# openc3-cosmos-constellation

Constellation workflow forecasts in OpenC3 COSMOS. The plugin adds a `CONSTELLATION` target, a
Constellation Forecast tool, and a bridge microservice that pulls your workflow results from the
Constellation API (outbound HTTPS) and injects them as packets:

| Packet | What it holds |
| --- | --- |
| `NEXT_PASS` | The next predicted pass and its expected GB (p10, p50, p90) |
| `NEXT_24H` | Predicted passes and expected GB over the next 24 hours |
| `SWAP` | The soonest rain-fade swap to decide, with minutes left |
| `CAPACITY` | One day's capacity across the antennas, and the tightest antenna |
| `MODCOD` | The next pass's MODCOD plan and its lowest planned SINR |
| `RUN` | The workflow run the values came from |

Every value is a forecast, not telemetry measured from a spacecraft or a ground station. Needs
COSMOS 7.0.0 or later.

## Install

1. **Admin > Secrets**: add `CONSTELLATION_API_KEY`, a Constellation API key with
   `predictions:read`. The bridge reads it at each poll, so it can be added or changed after
   installing.
2. **Admin > Plugins**: install `openc3-cosmos-constellation`, and set `constellation_api_url`
   (`https://api.constellation.space` by default) and `consumer` (`cosmos-bridge` by default; set
   another name if you run more than one COSMOS).

   From a shell, in your COSMOS project folder with the gem and a `variables.json` copied into it:

       ./openc3.sh cli load openc3-cosmos-constellation-<version>.gem --variables variables.json

   with `{"constellation_api_url": "https://api.constellation.space"}` in `variables.json`.
3. In Constellation, send a workflow to the OpenC3 COSMOS output with the same connection name as
   `consumer`.

The bridge sends the key only over HTTPS, or plain HTTP to this machine.

Full guide: https://platform.constellation.space/docs/connectors/cosmos

## License

Apache-2.0. Source: https://github.com/constellationhq/connectors
