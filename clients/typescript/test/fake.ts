// A stand-in workflow results API behind a mock fetch, shaped like the real one (see cli/mock).
import type { Result, RawResultsEntry } from "../src/types.js";

const anyValue = (v: unknown): Record<string, unknown> => {
  if (typeof v === "boolean") return { boolValue: v };
  if (typeof v === "number") return Number.isInteger(v) ? { intValue: String(v) } : { doubleValue: v };
  if (Array.isArray(v)) return { arrayValue: { values: v.map(anyValue) } };
  if (v && typeof v === "object")
    return { kvlistValue: { values: Object.entries(v).map(([key, x]) => ({ key, value: anyValue(x) })) } };
  return { stringValue: String(v) };
};
const attrs = (d: Record<string, unknown>) => Object.entries(d).map(([key, v]) => ({ key, value: anyValue(v) }));

export const item = (id: string, extra: Partial<Result> = {}): Result => ({
  entity: { type: "pass", id },
  target_at: "2030-01-01T01:00:00+00:00",
  value: { p10: 10.5, p50: 14.5, p90: 16.5 },
  ...extra,
});

export function entry(cursor: string, connection: string, items: Result[], schema = "constellation.item/v2"): RawResultsEntry {
  const run = cursor.split("#")[1];
  const output = { connection, mapping: { system: "custom", settings: { min_p10_gb: 5 } } };
  return {
    cursor,
    otlp: {
      resourceLogs: [
        {
          resource: {
            attributes: attrs({
              "constellation.workflow": "wf",
              "constellation.run_id": run,
              "constellation.connection": connection,
              "constellation.workflow_version": 1,
              "constellation.output": JSON.stringify(output),
            }),
          },
          scopeLogs: [
            {
              scope: { name: "constellation.workflows" },
              logRecords: items.map((i) => ({
                body: anyValue(i),
                attributes: attrs({ "constellation.schema": schema, "constellation.entity.id": i.entity.id }),
              })),
            },
          ],
        },
      ],
    },
  };
}

function paged(e: RawResultsEntry, size: number, page: number): RawResultsEntry {
  if (!e.otlp.resourceLogs.length) return e;
  const logs = e.otlp.resourceLogs[0]!;
  const scope = logs.scopeLogs![0]!;
  const records = scope.logRecords!;
  const more = (page + 1) * size < records.length ? page + 1 : undefined;
  return {
    cursor: e.cursor,
    otlp: { resourceLogs: [{ ...logs, scopeLogs: [{ ...scope, logRecords: records.slice(page * size, (page + 1) * size) }] }] },
    ...(more && { next_page: more }),
  };
}

export interface Call {
  method: string;
  url: URL;
  body: any;
  headers: Record<string, string>;
}

/**
 * fetch serving `entries` `pageSize` records a page, after first answering with `fail` statuses. It
 * answers from=latest unless `older` (an API before contract 1.5.0, which ignores it).
 */
export function fakeApi(entries: RawResultsEntry[], options: { pageSize?: number; fail?: number[]; older?: boolean } = {}) {
  const calls: Call[] = [];
  const fail = [...(options.fail ?? [])];
  const json = (status: number, body?: unknown) =>
    new Response(body === undefined ? null : JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  const fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = new URL(String(input));
    calls.push({ method: init?.method ?? "GET", url, body: init?.body ? JSON.parse(String(init.body)) : undefined, headers: init?.headers as Record<string, string> });
    const status = fail.shift();
    if (status) return json(status, { detail: "stand-in failure" });
    if (init?.method === "POST") return json(204);
    const q = url.searchParams;
    const size = q.has("page_records") ? (options.pageSize ?? Number(q.get("page_records"))) : 1e6;
    if (q.has("cursor")) {
      const e = entries.find((x) => x.cursor === q.get("cursor"));
      return e ? json(200, paged(e, size, Number(q.get("page") ?? 0))) : json(404, { detail: { error: "unknown_cursor" } });
    }
    if (q.get("from") === "latest" && !options.older) {
      // Where a first start reads from: just before the newest entry with resources, else the newest.
      let n = entries.length - 1;
      while (n >= 0 && !entries[n]!.otlp.resourceLogs.length) n--;
      const next = n < 0 ? (entries.at(-1)?.cursor ?? "") : (entries[n - 1]?.cursor ?? "");
      return json(200, { entries: [], next, oldest: null });
    }
    const after = q.get("after") ?? "";
    const found = entries.filter((e) => e.cursor > after).slice(0, Number(q.get("limit") ?? 20)).map((e) => paged(e, size, 0));
    return json(200, { entries: found, next: found.at(-1)?.cursor ?? after, oldest: null });
  };
  return { fetch: fetch as typeof globalThis.fetch, calls };
}
