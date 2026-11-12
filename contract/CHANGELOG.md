# Changelog

The published contract's versions, each the release contract-v<version>. Nobody edits a version
here: on a push to main the publish computes it from the latest release (an addition is a minor
bump, a removal DEPRECATIONS.md lists a major one, any other change a patch) and the bundle's
CHANGELOG.md is this file with each later release's computed changes added above the newest
entry.

## 2.0.0

- added `enum mcp-tools.json/get_predictions/inputSchema/properties/model_family "demand"`
- added `enum mcp-tools.json/get_predictions/inputSchema/properties/model_family "rfi"`
- added `enum mcp-tools.json/get_predictions/inputSchema/properties/model_family "snr"`
- added `enum mcp-tools.json/get_predictions/inputSchema/properties/model_family "ssa"`
- added `enum mcp-tools.json/get_predictions/inputSchema/properties/model_family "weather-attenuation"`
- added `enum mcp-tools.json/get_telemetry/inputSchema/properties/agg "last"`
- added `enum mcp-tools.json/get_telemetry/inputSchema/properties/agg "max"`
- added `enum mcp-tools.json/get_telemetry/inputSchema/properties/agg "mean"`
- added `enum mcp-tools.json/get_telemetry/inputSchema/properties/agg "min"`
- added `enum mcp-tools.json/get_telemetry/inputSchema/properties/interval "15m"`
- added `enum mcp-tools.json/get_telemetry/inputSchema/properties/interval "15s"`
- added `enum mcp-tools.json/get_telemetry/inputSchema/properties/interval "1h"`
- added `enum mcp-tools.json/get_telemetry/inputSchema/properties/interval "1m"`
- added `enum mcp-tools.json/get_telemetry/inputSchema/properties/interval "30s"`
- added `enum mcp-tools.json/get_telemetry/inputSchema/properties/interval "5m"`
- added `enum mcp-tools.json/send_telemetry/inputSchema/properties/records/items/properties/dataset/anyOf/0 "audit"`
- added `enum mcp-tools.json/send_telemetry/inputSchema/properties/records/items/properties/dataset/anyOf/0 "live"`
- added `enum mcp-tools.json/send_telemetry/inputSchema/properties/records/items/properties/dataset/anyOf/0 "sim"`
- added `field mcp-tools.json/get_predictions/inputSchema/properties/target_at`
- added `field mcp-tools.json/get_telemetry/inputSchema/properties/agg`
- added `field mcp-tools.json/get_telemetry/inputSchema/properties/end`
- added `field mcp-tools.json/get_telemetry/inputSchema/properties/field`
- added `field mcp-tools.json/get_telemetry/inputSchema/properties/ground_antenna_id`
- added `field mcp-tools.json/get_telemetry/inputSchema/properties/ground_station_id`
- added `field mcp-tools.json/get_telemetry/inputSchema/properties/interval`
- added `field mcp-tools.json/get_telemetry/inputSchema/properties/satellite_id`
- added `field mcp-tools.json/get_telemetry/inputSchema/properties/start`
- added `field mcp-tools.json/send_telemetry/inputSchema/properties/records`
- added `field mcp-tools.json/send_telemetry/inputSchema/properties/records/items/properties/contact_id`
- added `field mcp-tools.json/send_telemetry/inputSchema/properties/records/items/properties/dataset`
- added `field mcp-tools.json/send_telemetry/inputSchema/properties/records/items/properties/fields`
- added `field mcp-tools.json/send_telemetry/inputSchema/properties/records/items/properties/ground_antenna_id`
- added `field mcp-tools.json/send_telemetry/inputSchema/properties/records/items/properties/ground_station_id`
- added `field mcp-tools.json/send_telemetry/inputSchema/properties/records/items/properties/radio_id`
- added `field mcp-tools.json/send_telemetry/inputSchema/properties/records/items/properties/satellite_id`
- added `field mcp-tools.json/send_telemetry/inputSchema/properties/records/items/properties/time`
- added `tool get_telemetry`
- added `tool send_telemetry`
- removed `enum mcp-tools.json/get_telemetry_v2/inputSchema/properties/agg "last"`
- removed `enum mcp-tools.json/get_telemetry_v2/inputSchema/properties/agg "max"`
- removed `enum mcp-tools.json/get_telemetry_v2/inputSchema/properties/agg "mean"`
- removed `enum mcp-tools.json/get_telemetry_v2/inputSchema/properties/agg "min"`
- removed `enum mcp-tools.json/get_telemetry_v2/inputSchema/properties/interval "15m"`
- removed `enum mcp-tools.json/get_telemetry_v2/inputSchema/properties/interval "15s"`
- removed `enum mcp-tools.json/get_telemetry_v2/inputSchema/properties/interval "1h"`
- removed `enum mcp-tools.json/get_telemetry_v2/inputSchema/properties/interval "1m"`
- removed `enum mcp-tools.json/get_telemetry_v2/inputSchema/properties/interval "30s"`
- removed `enum mcp-tools.json/get_telemetry_v2/inputSchema/properties/interval "5m"`
- removed `enum mcp-tools.json/send_telemetry_v2/inputSchema/properties/records/items/properties/dataset/anyOf/0 "audit"`
- removed `enum mcp-tools.json/send_telemetry_v2/inputSchema/properties/records/items/properties/dataset/anyOf/0 "live"`
- removed `enum mcp-tools.json/send_telemetry_v2/inputSchema/properties/records/items/properties/dataset/anyOf/0 "sim"`
- removed `field mcp-tools.json/get_fleet/inputSchema/properties/as_of_utc`
- removed `field mcp-tools.json/get_fleet/inputSchema/properties/freshness_seconds`
- removed `field mcp-tools.json/get_fleet/inputSchema/properties/ground_antenna_id`
- removed `field mcp-tools.json/get_fleet/inputSchema/properties/ground_station_id`
- removed `field mcp-tools.json/get_fleet/inputSchema/properties/satellite_id`
- removed `field mcp-tools.json/get_telemetry_v2/inputSchema/properties/agg`
- removed `field mcp-tools.json/get_telemetry_v2/inputSchema/properties/end`
- removed `field mcp-tools.json/get_telemetry_v2/inputSchema/properties/field`
- removed `field mcp-tools.json/get_telemetry_v2/inputSchema/properties/ground_antenna_id`
- removed `field mcp-tools.json/get_telemetry_v2/inputSchema/properties/ground_station_id`
- removed `field mcp-tools.json/get_telemetry_v2/inputSchema/properties/interval`
- removed `field mcp-tools.json/get_telemetry_v2/inputSchema/properties/satellite_id`
- removed `field mcp-tools.json/get_telemetry_v2/inputSchema/properties/start`
- removed `field mcp-tools.json/send_telemetry_v2/inputSchema/properties/records`
- removed `field mcp-tools.json/send_telemetry_v2/inputSchema/properties/records/items/properties/contact_id`
- removed `field mcp-tools.json/send_telemetry_v2/inputSchema/properties/records/items/properties/dataset`
- removed `field mcp-tools.json/send_telemetry_v2/inputSchema/properties/records/items/properties/fields`
- removed `field mcp-tools.json/send_telemetry_v2/inputSchema/properties/records/items/properties/ground_antenna_id`
- removed `field mcp-tools.json/send_telemetry_v2/inputSchema/properties/records/items/properties/ground_station_id`
- removed `field mcp-tools.json/send_telemetry_v2/inputSchema/properties/records/items/properties/radio_id`
- removed `field mcp-tools.json/send_telemetry_v2/inputSchema/properties/records/items/properties/satellite_id`
- removed `field mcp-tools.json/send_telemetry_v2/inputSchema/properties/records/items/properties/time`
- removed `tool get_fleet`
- removed `tool get_telemetry_v2`
- removed `tool send_telemetry_v2`
-  "get_fleet": {
-    "description": "The fleet as declared and as observed, from GET /topology: config is the latest network config ({version, document}; null when there is none or the key lacks config:read), and state is the newest value of each field per ground antenna and satellite reported within freshness_seconds (default 300, at most 3600). An empty state means nothing has reported in the window. Requires the topology:read scope.",
-    "inputSchema": {
-      "additionalProperties": false,
-      "properties": {
-        "as_of_utc": {
-          "format": "date-time",
-          "type": "string"
-        },
-        "freshness_seconds": {
-          "maximum": 3600,
-          "minimum": 1,
-          "type": "number"
-        },
-        "ground_antenna_id": {
-          "description": "Only these ground_antenna_ids.",
-          "items": {
-            "type": "string"
-          },
-          "type": "array"
-        },
-        "ground_station_id": {
-          "description": "Only these ground_station_ids.",
-          "items": {
-            "type": "string"
-          },
-          "type": "array"
-        },
-        "satellite_id": {
-          "description": "Only these satellite_ids.",
-          "items": {
-            "type": "string"
-          },
-          "type": "array"
-        }
-      },
-      "type": "object"
-    }
-  },
-    "description": "Forecasts from Constellation's models. For snr (the default family), ask by instant: pass at (a future UTC instant) with a satellite_id, a ground_station_id or ground_antenna_id, or both sides, and no link_ids. Each link up at that instant comes back with its snr_db_p50 in dB, named by ground_station_id, ground_antenna_id and satellite_id; optical links (3000 GHz or more) return rx_power_dbm_p50, received power in dBm under clear sky. A link that is not up at that instant is listed in entity_errors (below_min_elevation), so pick a time in a pass. link_ids is the other snr form, for links sending telemetry now: ground antenna ids from the caller's network config (see get_topology), forecast a few minutes past their latest telemetry. A link without telemetry from the last minute answers no_telemetry, so use at for a link that is not in a pass right now. Up to 100 ids per call, of which at most 25 without a stored forecast are computed in one call. Entities that cannot be answered are listed in entity_errors with a code: no_telemetry or insufficient_telemetry means the input telemetry is missing or stale, and prediction_unavailable means the id was declined for now (the message says why, e.g. a per-call limit or a busy fill) and a later retry can answer it. Retry only the declined ids, after a short pause, not in a loop. weather-attenuation takes no link_ids: it prices the slant-path attenuation in dB of the links of the caller's network config at one future instant, at, for a ground_station_id, a satellite_id or both. With both, every antenna at the station is priced against the satellite; with one, only the links in view and on a common band. Each item names its ground_station_id, ground_antenna_id and satellite_id. Requires the predictions:read scope.",
-          "description": "weather-attenuation (required there), or snr: the future UTC instant to forecast the links at.",
-          "description": "snr with at: ground antenna ids from the network config; not with ground_station_id.",
-          "description": "weather-attenuation, or snr with at: a ground station id from the network config.",
-          "description": "snr from live telemetry: ground antenna ids sending telemetry now. Not taken by weather-attenuation.",
-          "description": "snr (default) for link SNR, or weather-attenuation for a link's attenuation at one instant.",
-          "description": "weather-attenuation, or snr with at: a satellite id from the network config.",
-  "get_telemetry_v2": {
-    "description": "Link telemetry written with send_telemetry_v2 between start and end, one record per time and id set, oldest first. Set interval and agg to aggregate numeric fields into windows; a raw read spans at most an hour. Requires the topology:read scope.",
-    "description": "The newest value of each field per ground antenna and satellite reported within freshness_seconds (default 300, at most 3600). An empty list means nothing has reported in the window. Requires the topology:read scope.",
-  "send_telemetry_v2": {

## 1.6.4

-            "description": "Malformed request body (not valid JSON)."
-            "description": "Malformed request body (not valid JSON)."
-            "description": "Malformed request body (not valid JSON)."
-            "description": "Malformed request body (not valid JSON)."
-            "description": "Malformed request body (not valid JSON)."
-            "description": "Malformed request body (not valid JSON)."
-            "description": "Malformed request body (not valid JSON)."
-            "description": "Malformed request body (not valid JSON)."
-            "description": "Malformed request body (not valid JSON)."
-            "description": "Malformed request body (not valid JSON)."
-            "description": "Malformed request body (not valid JSON)."
-            "description": "Malformed request body (not valid JSON)."
-            "description": "Malformed request body (not valid JSON)."
-            "description": "Malformed request body (not valid JSON)."
-            "description": "Malformed request body (not valid JSON)."
-            "description": "Malformed request body (not valid JSON)."
-            "description": "Malformed request body (not valid JSON)."
-            "description": "Malformed request body (not valid JSON)."
-            "description": "Malformed request body (not valid JSON)."
-            "description": "Malformed request body (not valid JSON)."
-            "description": "Malformed request body (not valid JSON)."
-            "description": "Malformed request body (not valid JSON)."
-            "description": "Malformed request body (not valid JSON)."
-            "description": "Malformed request body (not valid JSON)."
-            "description": "Malformed request body (not valid JSON)."
-        "description": "Load the demo fleet into the caller's account: demo_network_config.json (5 ground\nstations, 6 antennas and 5 sun-synchronous Earth observation satellites) as its network\nconfig, then the telemetry of its passes in the last two hours, then the demo workflows\nwith a first run of each. Safe to repeat: a config that holds the demo is kept, the\ntelemetry rewritten, and a workflow already saved under a demo slug left as it is.",
-            "description": "Malformed request body (not valid JSON)."
-            "description": "Malformed request body (not valid JSON)."
-    "description": "Load a ready-made demo fleet into the caller's account, so the other tools have data: 5 ground stations with 6 antennas and 5 sun-synchronous Earth observation satellites on X and Ka band (ids norad-<NORAD id>) in the network config, and the link telemetry of every pass in the last two hours at a 5 second cadence (es_n0_db, elevation_deg, range_m, frequency_ghz and the rfi noise channel: system_noise_temp_k, noise_diode_on and a contact_id per pass), from the live orbit catalog. The call merges the demo stations, antennas and satellites, with their RF terms, into the caller's network config with PATCH /topology/config, unless the config already holds a demo id (as on a reload), so no tenant entity is overwritten. The telemetry names the demo ids, so on a fresh account a key without config:write fails the call, and the error says so. The SNR is synthetic: a planted offset per link (a known answer for calibration, not a measured loss), plus a slow wander and noise. Each link's es_n0_db also drops by the rain attenuation its ground station stored for the last 30 minutes, scaled to the link's elevation. Older records are not faded, nor is a link with no stored terms; rain_note counts those links, or says the stored terms could not be read. The data ends at the present and does not advance; calling again rewrites the same points, so it is safe to repeat. The demo satellites are in low orbit, so a link is up only during its passes: ask snr with get_predictions and at, at a time in a pass (get_passes lists them). link_ids answers only while a pass is under way, right after this call. Requires the telemetry:write scope.",

## 1.6.3

-    "description": "Forecasts for named entities from Constellation's models; the ids go in link_ids whatever the family. The default family, snr, returns signal-to-noise forecasts in dB as a point value (snr_db_p50) per link and horizon; optical links (3000 GHz or more) return rx_power_dbm_p50, received power in dBm under clear sky; a link id is a ground antenna id from the caller's network config (see get_topology). To ask snr by instant, pass at (a future UTC instant) with a satellite_id, a ground_station_id or ground_antenna_id, or both sides, and no link_ids: each link up at that instant comes back with its snr_db_p50, named by ground_station_id, ground_antenna_id and satellite_id. Up to 100 ids per call, of which at most 25 without a stored forecast are computed in one call. Entities that cannot be answered are listed in entity_errors with a code: no_telemetry or insufficient_telemetry means the input telemetry is missing or stale, and prediction_unavailable means the id was declined for now (the message says why, e.g. a per-call limit or a busy fill) and a later retry can answer it. Retry only the declined ids, after a short pause, not in a loop. weather-attenuation takes no link_ids: it prices the slant-path attenuation in dB of the links of the caller's network config at one future instant, at, for a ground_station_id, a satellite_id or both. With both, every antenna at the station is priced against the satellite; with one, only the links in view and on a common band. Each item names its ground_station_id, ground_antenna_id and satellite_id. Requires the predictions:read scope.",
-          "description": "Ids to forecast: ground antenna ids for snr. Not taken by weather-attenuation.",
-    "description": "Load a ready-made demo fleet into the caller's account, so the other tools have data: 5 ground stations with 6 antennas and 5 sun-synchronous Earth observation satellites on X and Ka band (ids norad-<NORAD id>) in the network config, and the link telemetry of every pass in the last two hours at a 5 second cadence (es_n0_db, elevation_deg, range_m, frequency_ghz and the rfi noise channel: system_noise_temp_k, noise_diode_on and a contact_id per pass), from the live orbit catalog. The call merges the demo stations, antennas and satellites, with their RF terms, into the caller's network config with PATCH /topology/config, unless the config already holds a demo id (as on a reload), so no tenant entity is overwritten. The telemetry names the demo ids, so on a fresh account a key without config:write fails the call, and the error says so. The SNR is synthetic: a planted offset per link (a known answer for calibration, not a measured loss), plus a slow wander and noise. Each link's es_n0_db also drops by the rain attenuation its ground station stored for the last 30 minutes, scaled to the link's elevation. Older records are not faded, nor is a link with no stored terms; rain_note counts those links, or says the stored terms could not be read. The data ends at the present and does not advance; calling again rewrites the same points, so it is safe to repeat. SNR forecasts need telemetry from the last minute, so call load_demo_fleet again right before get_predictions. Requires the telemetry:write scope.",

## 1.6.2

-            "description": "Credential scopes do not permit predictions:read, or the plan does not include the model family (plan_required: demand needs learned_corrections)."

## 1.6.1

-    "description": "Load a ready-made demo fleet into the caller's account, so the other tools have data: 5 ground stations with 6 antennas and 5 sun-synchronous Earth observation satellites on X and Ka band (ids norad-<NORAD id>) in the network config, and the link telemetry of every pass in the last two hours at a 30 second cadence (es_n0_db, elevation_deg, range_m and frequency_ghz), from the live orbit catalog. The call merges the demo stations, antennas and satellites, with their RF terms, into the caller's network config with PATCH /topology/config, unless the config already holds a demo id (as on a reload), so no tenant entity is overwritten. The telemetry names the demo ids, so on a fresh account a key without config:write fails the call, and the error says so. The SNR is synthetic: a planted offset per link (a known answer for calibration, not a measured loss), plus a slow wander and noise. Each link's es_n0_db also drops by the rain attenuation its ground station stored for the last 30 minutes, scaled to the link's elevation. Older records are not faded, nor is a link with no stored terms; rain_note counts those links, or says the stored terms could not be read. The data ends at the present and does not advance; calling again rewrites the same points, so it is safe to repeat. SNR forecasts need telemetry from the last minute, so call load_demo_fleet again right before get_predictions. Requires the telemetry:write scope.",

## 1.6.0

- added `field mcp-tools.json/get_passes/inputSchema/properties/antenna_id`
- added `field mcp-tools.json/get_passes/inputSchema/properties/from`
- added `field mcp-tools.json/get_passes/inputSchema/properties/satellite_id`
- added `field mcp-tools.json/get_passes/inputSchema/properties/step_s`
- added `field mcp-tools.json/get_passes/inputSchema/properties/to`
- added `field mcp-tools.json/get_run/inputSchema/properties/antenna_id`
- added `field mcp-tools.json/get_run/inputSchema/properties/page`
- added `field mcp-tools.json/get_run/inputSchema/properties/page_records`
- added `field mcp-tools.json/get_run/inputSchema/properties/pass_id`
- added `field mcp-tools.json/get_run/inputSchema/properties/satellite_id`
- added `field mcp-tools.json/get_run/inputSchema/properties/station_id`
- added `field mcp-tools.json/get_run/inputSchema/properties/workflow`
- added `tool get_passes`
-    "description": "Approve an action a run holds (run_id and item_id from list_actions): it is published to the workflow's mission control connections as an instruction to carry out (a swap can cancel a booked pass) and cannot be rejected afterwards. Requires the act:approve scope.",
-    "description": "A run's status and, when done, every pass: pass_id, AOS, LOS, expected GB at p10/p50/p90, and the explain ledger naming each lane's value and source.",
-    "inputSchema": {
-      "additionalProperties": false,
-      "properties": {
-        }
-      },
-      "required": [
-        "run_id"
-      ],
-    "description": "The actions a workflow's current run holds for approval (a swap to book, a reroute), each pending, approved, rejected or expired, with the deadline to decide by, its action_id and, once decided, its decision_id. A run holds an action when its workflow has a policy step in approve mode.",
-    "description": "The caller's saved workflows, the latest version of each, as a name and a node graph. With templates true, the read-only templates instead (pass-yield, antenna-capacity, modcod-plan, rain-fade-swap, audit-ledger, and rfi-watch, marked available false where no rfi model is served), and lanes: each lane with whether a run prices it here and, where not, why; copy a template by saving its graph with put_workflow under your own slug.",
-    "description": "Save a workflow under a slug as a new version: a name and a graph of nodes ({id, kind, ref, params, position}) and edges ({id, from: {node, port}, to: {node, port}}). Kinds: input (passes or links: step_s, satellite_ids, antenna_ids), lane (snr, weather; demand, rfi and ssa run only once a model serves them), transform (sinr, capacity, success, expected_gb, sum_by, modcod_schedule, swap, ledger, lock_loss), condition (threshold: column, quantile, op, value; ports true and false), output (predictions, exactly one; feed; destination). Start from a template's graph (list_workflows with templates true). A graph with issues is refused with each one. Requires the workflows:write or config:write scope.",
-    "description": "Reject an action a run holds (run_id and item_id from list_actions): nothing is published and it cannot be decided again. Requires the act:approve scope.",
-    "description": "Start a run of a saved workflow over a UTC window (from and to) or at one time (at), optionally against a scenario_id. Returns a run_id for get_run. Every time carries a UTC offset. Requires the workflows:write scope.",
-    "description": "Write link telemetry keyed by the caller's network config: up to 1,000 records per call. Each record names a ground_station_id, one of its ground_antenna_ids and a satellite_id, as the network config has them. Antenna fields: azimuth_deg, elevation_deg, tracking_error_az_deg, tracking_error_el_deg, track_mode, range_m, range_rate_m_s. Carrier fields: carrier_locked, subcarrier_locked, symbol_locked, frame_locked, c_n0_dbhz, es_n0_db, rx_power_dbm, frequency_ghz, carrier_offset_hz, frames_delivered, frames_good, uplink_status, modcod, tx_power_dbm, ber, per, system_noise_temp_k, noise_power_dbm, noise_diode_on. A batch with one invalid record is rejected whole, naming its index, field and reason. Requires the telemetry:write scope.",

## 1.5.3

-            "description": "The account has a network config of its own (network_config_exists)."

## 1.5.2

-            "description": "Credential scopes do not permit predictions:read."
-            "description": "The prediction store is temporarily unreadable; retry later."
-            "description": "The key lacks the scope, is a test key (live_key_required), or the plan does not include the feature (plan_required names the plan that does)."

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
