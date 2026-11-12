# Deprecations

What the published contract will drop, and the version that drops it. `make contract-check`
fails a change that removes anything a client can rely on unless it is listed here, and the
publish then makes that release a major one. List the removal version as the next major (the
latest contract-v* release's major plus one). Add the row when the item is deprecated, so
clients see it coming; remove the item when that version is due.

An item is named as the check names it:

- `endpoint GET /predictions/feed`
- `tool get_predictions` (an MCP tool)
- `family snr` (a model family in contracts/models/model_catalog.json)
- `field openapi.json/components/schemas/Name/properties/field`
- `enum workflow-result.schema.json/properties/severity "info"`
- `proto telemetry.proto/Message/field = 3`
- `file telemetry.proto`

| Item | Removed in | Use instead |
| --- | --- | --- |
| `family snr-v2` | 2.0.0 | `snr`, which serves the same Link SNR v2 release; the API still answers `model_family=snr-v2` as `snr` |
| `tool get_fleet` | 2.0.0 | the same in MCP tool `get_topology`, which now returns the config and state `get_fleet` did |
| `field mcp-tools.json/get_fleet/inputSchema/properties/as_of_utc` | 2.0.0 | the same in MCP tool `get_topology`, which now returns the config and state `get_fleet` did |
| `field mcp-tools.json/get_fleet/inputSchema/properties/freshness_seconds` | 2.0.0 | the same in MCP tool `get_topology`, which now returns the config and state `get_fleet` did |
| `field mcp-tools.json/get_fleet/inputSchema/properties/ground_antenna_id` | 2.0.0 | the same in MCP tool `get_topology`, which now returns the config and state `get_fleet` did |
| `field mcp-tools.json/get_fleet/inputSchema/properties/ground_station_id` | 2.0.0 | the same in MCP tool `get_topology`, which now returns the config and state `get_fleet` did |
| `field mcp-tools.json/get_fleet/inputSchema/properties/satellite_id` | 2.0.0 | the same in MCP tool `get_topology`, which now returns the config and state `get_fleet` did |
| `tool get_telemetry_v2` | 2.0.0 | the same in MCP tool `get_telemetry` |
| `enum mcp-tools.json/get_telemetry_v2/inputSchema/properties/agg "last"` | 2.0.0 | the same in MCP tool `get_telemetry` |
| `enum mcp-tools.json/get_telemetry_v2/inputSchema/properties/agg "max"` | 2.0.0 | the same in MCP tool `get_telemetry` |
| `enum mcp-tools.json/get_telemetry_v2/inputSchema/properties/agg "mean"` | 2.0.0 | the same in MCP tool `get_telemetry` |
| `enum mcp-tools.json/get_telemetry_v2/inputSchema/properties/agg "min"` | 2.0.0 | the same in MCP tool `get_telemetry` |
| `enum mcp-tools.json/get_telemetry_v2/inputSchema/properties/interval "15m"` | 2.0.0 | the same in MCP tool `get_telemetry` |
| `enum mcp-tools.json/get_telemetry_v2/inputSchema/properties/interval "15s"` | 2.0.0 | the same in MCP tool `get_telemetry` |
| `enum mcp-tools.json/get_telemetry_v2/inputSchema/properties/interval "1h"` | 2.0.0 | the same in MCP tool `get_telemetry` |
| `enum mcp-tools.json/get_telemetry_v2/inputSchema/properties/interval "1m"` | 2.0.0 | the same in MCP tool `get_telemetry` |
| `enum mcp-tools.json/get_telemetry_v2/inputSchema/properties/interval "30s"` | 2.0.0 | the same in MCP tool `get_telemetry` |
| `enum mcp-tools.json/get_telemetry_v2/inputSchema/properties/interval "5m"` | 2.0.0 | the same in MCP tool `get_telemetry` |
| `field mcp-tools.json/get_telemetry_v2/inputSchema/properties/agg` | 2.0.0 | the same in MCP tool `get_telemetry` |
| `field mcp-tools.json/get_telemetry_v2/inputSchema/properties/end` | 2.0.0 | the same in MCP tool `get_telemetry` |
| `field mcp-tools.json/get_telemetry_v2/inputSchema/properties/field` | 2.0.0 | the same in MCP tool `get_telemetry` |
| `field mcp-tools.json/get_telemetry_v2/inputSchema/properties/ground_antenna_id` | 2.0.0 | the same in MCP tool `get_telemetry` |
| `field mcp-tools.json/get_telemetry_v2/inputSchema/properties/ground_station_id` | 2.0.0 | the same in MCP tool `get_telemetry` |
| `field mcp-tools.json/get_telemetry_v2/inputSchema/properties/interval` | 2.0.0 | the same in MCP tool `get_telemetry` |
| `field mcp-tools.json/get_telemetry_v2/inputSchema/properties/satellite_id` | 2.0.0 | the same in MCP tool `get_telemetry` |
| `field mcp-tools.json/get_telemetry_v2/inputSchema/properties/start` | 2.0.0 | the same in MCP tool `get_telemetry` |
| `tool send_telemetry_v2` | 2.0.0 | the same in MCP tool `send_telemetry` |
| `enum mcp-tools.json/send_telemetry_v2/inputSchema/properties/records/items/properties/dataset/anyOf/0 "audit"` | 2.0.0 | the same in MCP tool `send_telemetry` |
| `enum mcp-tools.json/send_telemetry_v2/inputSchema/properties/records/items/properties/dataset/anyOf/0 "live"` | 2.0.0 | the same in MCP tool `send_telemetry` |
| `enum mcp-tools.json/send_telemetry_v2/inputSchema/properties/records/items/properties/dataset/anyOf/0 "sim"` | 2.0.0 | the same in MCP tool `send_telemetry` |
| `field mcp-tools.json/send_telemetry_v2/inputSchema/properties/records` | 2.0.0 | the same in MCP tool `send_telemetry` |
| `field mcp-tools.json/send_telemetry_v2/inputSchema/properties/records/items/properties/contact_id` | 2.0.0 | the same in MCP tool `send_telemetry` |
| `field mcp-tools.json/send_telemetry_v2/inputSchema/properties/records/items/properties/dataset` | 2.0.0 | the same in MCP tool `send_telemetry` |
| `field mcp-tools.json/send_telemetry_v2/inputSchema/properties/records/items/properties/fields` | 2.0.0 | the same in MCP tool `send_telemetry` |
| `field mcp-tools.json/send_telemetry_v2/inputSchema/properties/records/items/properties/ground_antenna_id` | 2.0.0 | the same in MCP tool `send_telemetry` |
| `field mcp-tools.json/send_telemetry_v2/inputSchema/properties/records/items/properties/ground_station_id` | 2.0.0 | the same in MCP tool `send_telemetry` |
| `field mcp-tools.json/send_telemetry_v2/inputSchema/properties/records/items/properties/radio_id` | 2.0.0 | the same in MCP tool `send_telemetry` |
| `field mcp-tools.json/send_telemetry_v2/inputSchema/properties/records/items/properties/satellite_id` | 2.0.0 | the same in MCP tool `send_telemetry` |
| `field mcp-tools.json/send_telemetry_v2/inputSchema/properties/records/items/properties/time` | 2.0.0 | the same in MCP tool `send_telemetry` |

The check sees tool inputs, not responses, so this change is noted here: MCP tool `get_topology` now returns GET /topology's `{config, state, declined}` object where it returned /v2/topology's bare list.
