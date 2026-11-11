import { appendFile, mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { dirname } from "node:path";
import { Client, loopback, origin, site, type ConstellationError } from "./client.js";
import { actionState } from "./results.js";
import type { Result, Manifest, RunInfo } from "./types.js";

/**
 * Where a bridge keeps the last cursor it applied and the approvals it already carried out (by
 * actionKey, each with when it may be forgotten: actedUntil), so neither a restart nor a later run
 * that sends an approval again acts twice. Keep both in your system if it can hold them.
 */
export interface CursorStore {
  load(): Promise<string>;
  save(cursor: string): Promise<void>;
  /** actionKey to actedUntil ("" for never). */
  acted(): Promise<Map<string, string>>;
  saveActed(acted: Map<string, string>): Promise<void>;
}

export class MemoryCursorStore implements CursorStore {
  constructor(
    public cursor = "",
    public actedKeys = new Map<string, string>(),
  ) {}
  async load() {
    return this.cursor;
  }
  async save(cursor: string) {
    this.cursor = cursor;
  }
  async acted() {
    return new Map(this.actedKeys);
  }
  async saveActed(acted: Map<string, string>) {
    this.actedKeys = new Map(acted);
  }
}

/**
 * Keeps the cursor in the file `path`, and the approvals carried out beside it (`path`.acted). With
 * `apiUrl`, the API it reads is kept beside them too (`path`.api), and a cursor read from another API
 * (the mock) is refused, never resumed against this one.
 */
export class FileCursorStore implements CursorStore {
  constructor(
    readonly path: string,
    readonly apiUrl?: string,
  ) {}
  async load() {
    const there = (await readFile(`${this.path}.api`, "utf8").catch(() => "")).trim();
    const here = this.apiUrl && origin(this.apiUrl);
    if (here && there && there !== here)
      throw Object.assign(
        new Error(`${this.path} is where this bridge read ${there} up to, and it now reads ${here}. Delete ${this.path}* to start afresh on ${here}, or set CURSOR_FILE to another file`),
        { fatal: true },
      );
    try {
      return (await readFile(this.path, "utf8")).trim();
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return "";
      throw error;
    }
  }
  async save(cursor: string) {
    await mkdir(dirname(this.path), { recursive: true });
    if (this.apiUrl) await writeFile(`${this.path}.api`, origin(this.apiUrl));
    await writeFile(`${this.path}.tmp`, cursor);
    await rename(`${this.path}.tmp`, this.path);
  }
  /** Kept beside the cursor, a line per key: the key, a tab, actedUntil; as the Python and Go bridges keep it. */
  async acted() {
    let text = "";
    try {
      text = await readFile(`${this.path}.acted`, "utf8");
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
    const acted = new Map<string, string>();
    for (const line of text.split("\n")) {
      const tab = line.indexOf("\t");
      if (line) acted.set(tab < 0 ? line : line.slice(0, tab), tab < 0 ? "" : line.slice(tab + 1));
    }
    return acted;
  }
  async saveActed(acted: Map<string, string>) {
    await mkdir(dirname(this.path), { recursive: true });
    const lines = [...acted].map(([key, until]) => `${key}\t${until}\n`).join("");
    await writeFile(`${this.path}.acted.tmp`, lines);
    await rename(`${this.path}.acted.tmp`, `${this.path}.acted`);
  }
}

/**
 * Names the decision to carry out an action: its decision_id, which a standing approval keeps in
 * every run and a new approval of the same action changes. Without one, its content, as
 * Constellation keys an action: the same verb on the same entity to the same target.
 */
export function actionKey(item: Result): string {
  if (typeof item.decision_id === "string" && item.decision_id) return item.decision_id;
  return `${item.entity.id}|${item.action?.verb ?? ""}|${item.action?.to ?? ""}`;
}

/** How long an acted key is kept once its action is over: past it, no run sends the action again. */
export const KEEP_ACTED_MS = 7 * 24 * 3600 * 1000;
/** How often a bridge checks in again; one that failed is tried again at the next poll. */
export const CHECKIN_MS = 5 * 60 * 1000;

/**
 * When an acted key may be forgotten: the latest of the item's time, its end (a pass's end), its
 * action's cancel deadline and now, plus KEEP_ACTED_MS, so a key is never dropped as soon as it is
 * written; "" (kept for good) when none of the item's is a time.
 */
export function actedUntil(item: Result): string {
  const times = [item.target_at, item.until, item.action?.deadline].map((t) => Date.parse(t ?? "")).filter((t) => !Number.isNaN(t));
  return times.length ? new Date(Math.max(...times, Date.now()) + KEEP_ACTED_MS).toISOString() : "";
}

/**
 * Why an item cannot be read (no entity with a string id and type, or a time that is not one), or
 * "" for a well-formed one; as the Python and Go bridges judge it.
 */
function malformed(item: unknown): string {
  const i = item as Partial<Result> | null;
  if (typeof i?.entity?.id !== "string" || typeof i.entity.type !== "string") return "no entity with an id and a type";
  const times: [string, unknown][] = [["target_at", i.target_at], ["until", i.until ?? undefined], ["action.deadline", i.action?.deadline ?? undefined]];
  for (const [name, t] of times) {
    if (name !== "target_at" && t === undefined) continue;
    if (typeof t !== "string" || Number.isNaN(Date.parse(t))) return `${name} is not a time`;
  }
  return "";
}

/** Whether the item's action is past its cancel deadline: closed, never to be carried out. */
function pastDeadline(item: Result): boolean {
  const deadline = Date.parse(item.action?.deadline ?? "");
  return !Number.isNaN(deadline) && deadline <= Date.now();
}

/**
 * Where a first start reads from: just before the newest run with something for `consumer`, so it
 * applies that run and none of the older history. The API answers it without reading a run.
 */
async function startCursor(client: Client, consumer: string): Promise<string> {
  const page = await client.resultsPage({ from: "latest", consumer, limit: 20, pageRecords: 1 });
  if (page.entries.length)
    throw new Error("the API ignored GET /predictions/feed?from=latest, so a first start cannot find the newest run: this bridge needs an API serving contract 1.5.0 or later");
  return page.next;
}

/** Drops the keys whose actedUntil has passed; says whether it dropped any. */
function prune(acted: Map<string, string>): boolean {
  const over = [...acted].filter(([, until]) => until && Date.parse(until) < Date.now());
  for (const [key] of over) acted.delete(key);
  return over.length > 0;
}

export interface BridgeOptions {
  manifest: Manifest;
  client?: Client;
  /** Defaults to a FileCursorStore at CURSOR_FILE, or ./cursor.txt, for the client's API. */
  store?: CursorStore;
  /** Defaults to POLL_SECONDS (15). */
  pollMs?: number;
  /** JSON-lines log of every item applied; defaults to CONSTELLATION_CONFORMANCE_LOG, off when unset. */
  conformanceLog?: string;
  /**
   * With no saved cursor, apply the whole history the feed holds instead of starting at its newest
   * run. Defaults to BACKFILL=1.
   */
  backfill?: boolean;
  log?: (message: string) => void;
}

/**
 * A Constellation connector for your own system: subclass it, implement apply() (and act() for
 * approved actions), call run(). Checking in, pulling and joining runs, reporting, cursor
 * persistence, acting on each approved action exactly once and never dying on a bad poll are done
 * here; a refused key or plan (401/403) stops run() with a ConstellationError, and a cursor it cannot
 * save stops it too.
 */
export abstract class Bridge {
  readonly manifest: Manifest;
  readonly client: Client;
  readonly store: CursorStore;
  readonly pollMs: number;
  readonly conformanceLog: string | undefined;
  private checkedIn = false;
  private nextCheckIn = 0;
  private readonly backfill: boolean;
  private started = false;
  private readonly unreported = new Map<string, { counts: Record<string, number>; acted: boolean[]; items: Result[] }[]>();
  private actedKeys: Map<string, string> | undefined;

  constructor(options: BridgeOptions) {
    this.manifest = options.manifest;
    this.log = options.log ?? this.log;
    this.client = options.client ?? new Client({ log: (m) => this.log(m) });
    this.store = options.store ?? new FileCursorStore(process.env.CURSOR_FILE ?? "cursor.txt", this.client.baseUrl);
    this.pollMs = options.pollMs ?? Number(process.env.POLL_SECONDS ?? 15) * 1000;
    this.conformanceLog = options.conformanceLog ?? (process.env.CONSTELLATION_CONFORMANCE_LOG || undefined);
    this.backfill = options.backfill ?? process.env.BACKFILL === "1";
  }

  /**
   * Write one run's items into your system; return what you wrote, e.g. {passes: 12}. Throw when
   * your system is unreachable: the run is retried. `run` is the whole answer for its window, so
   * withdraw what you wrote earlier for the same workflow inside the window that `items` no
   * longer carries. Carry out approved actions in act(), not here.
   */
  abstract apply(
    items: Result[],
    settings: Record<string, unknown>,
    run: RunInfo,
  ): Promise<Record<string, number>> | Record<string, number>;

  /**
   * Carry out an approved action (cancel the pass, book the backup); return true once done.
   * Called after apply(), once per approval, never for any other state, and never again for the
   * same approval (actionKey) in a later run or after a restart. Throw when your system is
   * failing: the run is retried, without acting again on what was done. The default does nothing.
   */
  act(
    _item: Result,
    _settings: Record<string, unknown>,
    _run: RunInfo,
  ): Promise<boolean> | boolean {
    return false;
  }

  log(message: string): void {
    console.log(`${this.manifest.name}: ${message}`);
  }

  /** Applies every run with results after the saved cursor; returns how many it applied. */
  async poll(): Promise<number> {
    const { name, title } = this.manifest;
    // Again every CHECKIN_MS: the platform reads a connection that stopped checking in as stopped.
    if (Date.now() >= this.nextCheckIn) {
      await this.client.checkIn(this.manifest);
      this.nextCheckIn = Date.now() + CHECKIN_MS;
      if (!this.checkedIn) this.log("checked in; approve it on Integrations if it is new or its manifest changed");
      this.checkedIn = true;
    }
    let after = await this.store.load();
    if (!this.started && !after && !this.backfill) {
      after = await startCursor(this.client, name);
      if (after) await this.keep(() => this.store.save(after));
      this.log(`first start: reading after ${after || "the start"}`);
    }
    this.started = true;
    let applied = 0;
    for await (const entry of this.client.results({ after, consumer: name })) {
      if (after && entry.cursor <= after) {
        // A feed replaying from an older cursor (a failover): applied before, never again.
        this.log(`skipped ${entry.cursor}: already applied`);
        continue;
      }
      // What each part wrote, kept until the run is reported: a failed report is retried without
      // writing or acting again.
      const done = this.unreported.get(entry.cursor) ?? [];
      this.unreported.set(entry.cursor, done);
      for (const [n, part] of entry.parts.entries()) {
        if (n === done.length) {
          // One malformed item is logged, skipped and counted: never worth its run and the runs after it.
          const items = part.items.filter((item, i) => {
            const why = malformed(item);
            if (why) this.log(`skipped item ${i} of run ${part.run.run_id}: it is malformed (${why})`);
            return !why;
          });
          const counts = { ...(await this.apply(items, part.settings, part.run)) };
          if (items.length < part.items.length) counts.malformed = part.items.length - items.length;
          const acted: boolean[] = [];
          for (const item of items)
            acted.push(actionState(item) === "approved" && (await this.carryOut(item, part.settings, part.run)));
          // So the platform can show that an approval was carried out.
          if (acted.some(Boolean)) counts.acted = acted.filter(Boolean).length;
          done.push({ counts, acted, items });
        }
        const { counts, acted, items } = done[n]!;
        await this.client.report(name, entry.cursor, counts, title);
        await this.record(items, acted);
        this.log(`applied ${JSON.stringify(counts)}, ${entry.cursor}`);
      }
      this.unreported.delete(entry.cursor);
      // Only a run applied and reported (or holding nothing for this connector) moves the cursor.
      await this.keep(() => this.store.save(entry.cursor));
      after = entry.cursor;
      applied++;
    }
    return applied;
  }

  /**
   * Polls until `signal` aborts. A failed poll is logged and retried; a refusal, state it cannot
   * save, or no key (unless the API is the mock on this machine) is thrown.
   */
  async run(signal?: AbortSignal): Promise<void> {
    if (!this.client.hasKey && !loopback(this.client.baseUrl))
      throw new Error(`CONSTELLATION_API_KEY is not set: make a key with predictions:read on ${site(this.client.baseUrl)}/keys and set it`);
    this.log(`${this.client.baseUrl} as ${this.manifest.name}, from cursor ${(await this.store.load()) || (this.backfill ? "the start" : "its newest run")}`);
    while (!signal?.aborted) {
      try {
        await this.poll();
      } catch (error) {
        // Checked by shape, not instanceof: the CommonJS build gives each entry its own copy of the class.
        if ((error as Partial<ConstellationError>)?.refused || (error as { fatal?: boolean })?.fatal) throw error;
        this.log(error instanceof Error ? `${error.name}: ${error.message}` : String(error));
      }
      await new Promise((resolve) => setTimeout(resolve, this.pollMs));
    }
  }

  /** Carries out an approved action unless it was carried out before or is past its cancel deadline; says whether it did. */
  private async carryOut(item: Result, settings: Record<string, unknown>, run: RunInfo): Promise<boolean> {
    if (pastDeadline(item)) {
      this.log(`not acting on ${item.entity.id}: its cancel deadline has passed`);
      return false;
    }
    if (!this.actedKeys) {
      this.actedKeys = await this.store.acted();
      if (prune(this.actedKeys)) await this.store.saveActed(this.actedKeys);
    }
    const key = actionKey(item);
    if (this.actedKeys.has(key) || !(await this.act(item, settings, run))) return false;
    this.actedKeys.set(key, actedUntil(item));
    prune(this.actedKeys);
    await this.keep(() => this.store.saveActed(this.actedKeys!));
    return true;
  }

  /**
   * Saves the cursor or the approvals carried out. A bridge that cannot would repeat its work after
   * every poll or restart, so a failure to stops run().
   */
  private async keep(save: () => Promise<void>): Promise<void> {
    try {
      await save();
    } catch (error) {
      const why = error instanceof Error ? error.message : String(error);
      throw Object.assign(new Error(`cannot keep its state, so it would repeat its work: ${why} (make CURSOR_FILE's folder writable)`), { fatal: true });
    }
  }

  /** One line per item applied, for `constellation conformance`. */
  private async record(items: Result[], acted: boolean[]): Promise<void> {
    if (!this.conformanceLog) return;
    const lines = items.map((item, i) =>
      JSON.stringify({
        item_id: item.entity.id,
        state: actionState(item) ?? "forecast",
        acted: acted[i],
        verb: item.action?.verb ?? null,
      }),
    );
    if (lines.length) await appendFile(this.conformanceLog, lines.join("\n") + "\n");
  }
}

export { actionState, type ActionState } from "./results.js";
export type { Result, Manifest, RunInfo } from "./types.js";
