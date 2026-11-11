# @constellationspace/client

The Constellation API client for Node 18+ and browsers, with no runtime dependencies.

```ts
import { Client, actionState } from "@constellationspace/client";

const client = new Client(); // CONSTELLATION_API_KEY, CONSTELLATION_API_URL
for await (const run of client.results({ consumer: "my-bridge" })) {
  for (const { items } of run.parts) for (const item of items) console.log(item.entity.id, actionState(item));
}
```

A connector for your own system (Node): subclass `Bridge` from `@constellationspace/client/bridge`,
implement `apply(items, settings, run)` (and `act(item, settings, run)` for approved actions) and
call `run()`. It checks in, pulls and joins runs, reports, keeps its cursor and the actions it
carried out (`CURSOR_FILE` and `CURSOR_FILE.acted`, or any `CursorStore`), so it resumes on restart
and acts on each approved action once, and writes the log `constellation conformance` reads when
`CONSTELLATION_CONFORMANCE_LOG` is set.

`npm run build`, `npm test`, `npm run typecheck`. `npm run generate` (and `npm run build`) writes
`src/generated/api.ts` (not committed) from `contract/openapi.json` at the repository root.
