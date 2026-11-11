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
