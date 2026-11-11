# Changelog

The published contract's versions, each the release contract-v<version>. Nobody edits a version
here: on a push to main the publish computes it from the latest release (an addition is a minor
bump, a removal DEPRECATIONS.md lists a major one, any other change a patch) and the bundle's
CHANGELOG.md is this file with each later release's computed changes added above the newest
entry.

## 1.5.1

-            "description": "A conflict: no finished run yet, a changed manifest, or a run that finished or is already delivering when asked to cancel."

## 1.5.0

- added `field mcp-tools.json/put_workflow/inputSchema/properties/demo`
- added `field openapi.json/components/schemas/WorkflowDefinition/properties/demo`
-        "description": "A pass log of the past week for SAMPLE_SATELLITES, one row inside each pass an\naudit-ledger run of that week finds (the same sightings, on a run's step): delivered, short,\nfailed, missed, and some passes left unlogged. Only for an account whose network config holds\nthose satellites as the demo's (demo_network.demo_entities).",
-        "description": "Load the demo fleet into the caller's account: 7 ground stations, 22 antennas, 6\ngeostationary satellites and NOAA-20 in low orbit as its network config, then two hours of\nthe geostationary links' telemetry ending now. Safe to repeat: a config that holds the demo is kept and the telemetry rewritten.",
-            "description": "The demo network config is in place. `network_config` says whether it was merged now or already held; `telemetry` counts the records posted, or is the refusal of POST /telemetry (its status and body: 403 insufficient_scope for a key without telemetry:write, 429 or 503 while telemetry cannot be written)."
-            "description": "The key lacks config:write, or the plan does not allow 6 satellites."
-    "description": "Load a ready-made demo fleet into the caller's account, so the other tools have data: 7 ground stations with 22 antennas, 6 geostationary satellites and NOAA-20 in low orbit (ids norad-<NORAD id>) in the network config, and two hours of link telemetry per geostationary antenna at a 30 second cadence (es_n0_db, elevation_deg, range_m and frequency_ghz). The call merges the demo stations, antennas and satellites, with their RF terms, into the caller's network config with PATCH /topology/config, unless the config already holds a demo id (as on a reload), so no tenant entity is overwritten. The telemetry names the demo ids, so on a fresh account a key without config:write fails the call, and the error says so. The SNR is synthetic: a planted offset per link (a known answer for calibration, not a measured loss), plus a slow wander and noise. Each link's es_n0_db also drops by the rain attenuation its ground station stored for the last 30 minutes, scaled to the link's elevation. Older records are not faded, nor is a link with no stored terms; rain_note counts those links, or says the stored terms could not be read. The data ends at the present and does not advance; calling again rewrites the same points, so it is safe to repeat. SNR forecasts need telemetry from the last minute, so call load_demo_fleet again right before get_predictions. Requires the telemetry:write scope.",

## 1.4.0

- added `field workflow-result.schema.json/properties/note`

## 1.3.0

- added `endpoint GET /predictions/actions/decisions`
- added `endpoint GET /predictions/contract-minimum`
- added `endpoint GET /predictions/runs`
- added `endpoint PUT /predictions/contract-minimum`
- added `field openapi.json/components/schemas/ContractMinimum/properties/min_gb`

## 1.2.2

-        "description": "Finished runs' items as OTLP log requests, oldest first, after a cursor. A plugin inside a\ncustomer's mission control polls this from their side and keeps the last cursor it applied;\nnaming itself (consumer) lets the platform show when it last pulled, and gets it only the\nresources of feed outputs to its connection: a run's entry repeats its items per output.\n\nA bridge that pages (`page_records`) gets an entry with more records than that in pages:\nthe first here, with `next_page`, and the rest from `cursor=<the entry>&page=<n>`, which it\njoins before applying the run. A bridge that does not page always gets whole entries.\n\n`oldest` is the oldest entry the feed still holds (null when it holds none): entries expire\nFEED_TTL after they were published, so a bridge whose cursor sorts before `oldest` may have\nmissed entries that expired while it was offline.",

## 1.2.1

-        "description": "A pass log of the past week for SAMPLE_SATELLITES, one row inside each pass an\naudit-ledger run of that week finds (the same sightings, on a run's step): delivered, short,\nfailed, missed, and some passes left unlogged. Only for an account whose network config holds\nthose satellites as the demo defines them.",
-        "description": "Every pass between from and to in the latest network config, computed from the fleet's\nelement sets and antenna masks and sampled every step_s from `from`, as a run samples it. Each\npass carries the pass_id a run's item for it carries. Below the satellites feature it covers\nat most FREE_SATELLITES, as a test run does.",

## 1.2.0

- added `endpoint DELETE /predictions/views/{view_id}`
- added `endpoint GET /predictions/views`
- added `endpoint POST /predictions/views`
- added `endpoint PUT /predictions/views/{view_id}`
- added `field openapi.json/components/schemas/ViewBody/properties/default`
- added `field openapi.json/components/schemas/ViewBody/properties/name`
- added `field openapi.json/components/schemas/ViewBody/properties/run_id`
- added `field openapi.json/components/schemas/ViewBody/properties/to`

## 1.1.0

- added `field workflow-result.schema.json/properties/quantity`
- added `field workflow-result.schema.json/properties/unit`
-        "description": "The fleet as declared (the latest network config, as /topology/config holds it) and as\nobserved (the newest value of each field per antenna and satellite in the window), with the\nentities of the context layers `layers` names in `state`: each station's rain and fade\n(contacts/context). `declined` names a layer that has none and why, e.g. ssa while no model\nserves it. `config`\nis null when there is no network config, or when the key may not read it (no config:read,\nconfig:write or workflows:write): topology:read alone still reads the observed state and the\nlayers.",

## 1.0.0

- First published contract: openapi.json (the routes a customer calls with their own key),
  workflow-result.schema.json (constellation.item/v2), telemetry.proto
  (constellation.ingest.v2) and mcp-tools.json.
