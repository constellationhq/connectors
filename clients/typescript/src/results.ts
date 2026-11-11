import type { Result, ResultsPart, OtlpLogs, RawResultsEntry } from "./types.js";

/** The item shape this client reads; a part in another is skipped and logged, never guessed at. */
export const SCHEMA = "constellation.item/v2";
/** The most records the API sends in one page of a run; it pages a run with more. */
export const PAGE_RECORDS = 400;

export type ActionState = "advice" | "approved" | "pending" | "rejected" | null;

/**
 * What an item's action is to a bridge. "advice": suggested, never an instruction. "approved": a
 * person approved it, an instruction to carry out. "pending": held for approval. "rejected": the
 * original stands. null: no action. Any status other than approved or rejected counts as held,
 * so an item a bridge cannot read is never taken for a plain forecast.
 */
export function actionState(item: Pick<Result, "action" | "action_status">): ActionState {
  const status = item.action_status ?? null;
  if (status !== null && status !== "approved") return status === "rejected" ? "rejected" : "pending";
  const action = item.action;
  if (!action || Object.keys(action).length === 0) return status === null ? null : "pending";
  return status === "approved" ? "approved" : "advice";
}

/** An OTLP AnyValue back to plain JSON. */
export function plain(value: Record<string, any>): unknown {
  if ("kvlistValue" in value)
    return Object.fromEntries((value.kvlistValue.values ?? []).map((kv: any) => [kv.key, plain(kv.value)]));
  if ("arrayValue" in value) return (value.arrayValue.values ?? []).map(plain);
  if ("intValue" in value) return Number(value.intValue);
  for (const key of ["doubleValue", "boolValue", "stringValue"]) if (key in value) return value[key];
  return null;
}

const attributes = (kvs: { key: string; value: Record<string, unknown> }[] = []) =>
  Object.fromEntries(kvs.map((kv) => [kv.key, plain(kv.value)])) as Record<string, any>;

/**
 * An entry's parts: each resourceLogs whose constellation.connection is `consumer` (every one when
 * no consumer is given). A part in an item shape this client does not read is skipped and logged.
 */
export function unpack(entry: RawResultsEntry, consumer: string | undefined, log: (m: string) => void): ResultsPart[] {
  const parts: ResultsPart[] = [];
  for (const logs of entry.otlp.resourceLogs) {
    const resource = attributes(logs.resource?.attributes);
    if (consumer && resource["constellation.connection"] !== consumer) continue;
    const records = (logs.scopeLogs ?? []).flatMap((s) => s.logRecords ?? []);
    const newer = new Set(records.map((r) => attributes(r.attributes)["constellation.schema"]));
    newer.delete(SCHEMA);
    if (newer.size) {
      log(`skipped run ${resource["constellation.run_id"]}: its items are ${[...newer].join(", ")} and this client reads ${SCHEMA}`);
      continue;
    }
    const output = JSON.parse(resource["constellation.output"] || "{}");
    parts.push({
      resource,
      items: records.map((r) => plain(r.body) as Result),
      settings: output?.mapping?.settings ?? {},
      run: {
        workflow: resource["constellation.workflow"],
        workflow_version: resource["constellation.workflow_version"],
        run_id: resource["constellation.run_id"],
        window_from: resource["constellation.window.from"],
        window_to: resource["constellation.window.to"],
      },
    });
  }
  return parts;
}

/** `entry` with every page of its run joined in, each page's records added to their own resource. */
export async function whole(
  entry: RawResultsEntry,
  fetchPage: (cursor: string, page: number) => Promise<RawResultsEntry>,
): Promise<RawResultsEntry> {
  const resourceLogs: OtlpLogs[] = entry.otlp.resourceLogs.map((l) => ({
    ...l,
    scopeLogs: (l.scopeLogs ?? []).map((s) => ({ ...s, logRecords: [...(s.logRecords ?? [])] })),
  }));
  const byResource = new Map(resourceLogs.map((l) => [JSON.stringify(l.resource), l]));
  let next = entry.next_page;
  while (next) {
    const more = await fetchPage(entry.cursor, next);
    for (const logs of more.otlp.resourceLogs) {
      const key = JSON.stringify(logs.resource);
      const known = byResource.get(key)?.scopeLogs?.[0];
      if (known) known.logRecords!.push(...(logs.scopeLogs?.[0]?.logRecords ?? []));
      else {
        resourceLogs.push(logs);
        byResource.set(key, logs);
      }
    }
    next = more.next_page;
  }
  return { cursor: entry.cursor, otlp: { ...entry.otlp, resourceLogs } };
}
