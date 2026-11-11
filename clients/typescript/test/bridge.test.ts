import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { actedUntil, Bridge, CHECKIN_MS, FileCursorStore, MemoryCursorStore, type Result } from "../src/bridge.js";
import { Client } from "../src/client.js";
import { entry, fakeApi, item } from "./fake.js";

const manifest = { name: "my-bridge", title: "My system" };
const swap = { verb: "swap", mode: "suggest", to: "b1" };
const approved = { action: { ...swap, mode: "approve" }, action_status: "approved" };
const entries = [
  entry("20300101T000001#run_1", "my-bridge", [item("p1"), item("p2", { action: swap })]),
  entry("20300101T000002#run_2", "my-bridge", [
    item("s1", approved),
    item("s2", { action_status: "pending" }),
    item("s3", { action_status: "rejected" }),
    item("s4", { action_status: "mystery" }),
  ]),
];

// The same approved action sent again in a later run, as the API does.
const again = entry("20300101T000003#run_3", "my-bridge", [item("s1", approved)]);

class Recorder extends Bridge {
  acted: string[] = [];
  failOnce = false;
  apply(items: Result[]) {
    return { items: items.length };
  }
  act(item: Result) {
    this.acted.push(item.entity.id);
    if (this.failOnce) {
      this.failOnce = false;
      throw new Error("the system went down after acting");
    }
    return true;
  }
  log() {}
}

// These replay the whole feed unless told otherwise; a first start from the head is tested on its own.
const bridge = (
  api: ReturnType<typeof fakeApi>,
  options: { store?: MemoryCursorStore | FileCursorStore; conformanceLog?: string; backfill?: boolean } = {},
) =>
  new Recorder({
    manifest,
    client: new Client({ apiKey: "k", baseUrl: "https://api.test", fetch: api.fetch, retryDelayMs: 0, log: () => {} }),
    store: options.store ?? new MemoryCursorStore(),
    conformanceLog: options.conformanceLog ?? "",
    pollMs: 1,
    backfill: options.backfill ?? true,
  });

const expired = { ...approved, action: { ...approved.action, deadline: "2020-01-01T00:00:00Z" } };

describe("Bridge", () => {
  it("checks in again once CHECKIN_MS has passed, so the platform sees it alive", async () => {
    const api = fakeApi([]);
    const b = bridge(api);
    let now = 1_000_000;
    const real = Date.now;
    Date.now = () => now;
    try {
      await b.poll();
      await b.poll();
      now += CHECKIN_MS + 1;
      await b.poll();
    } finally {
      Date.now = real;
    }
    const checkins = api.calls.filter((c) => c.url.pathname === "/predictions/feed/checkin");
    expect(checkins).toHaveLength(2);
  });

  it("checks in once, applies and reports each run, and saves the cursor", async () => {
    const api = fakeApi(entries);
    const store = new MemoryCursorStore();
    const b = bridge(api, { store });
    expect(await b.poll()).toBe(2);
    expect(await b.poll()).toBe(0);
    const posts = api.calls.filter((c) => c.method === "POST");
    expect(posts.map((c) => c.url.pathname)).toEqual([
      "/predictions/feed/checkin",
      "/predictions/feed/deliveries",
      "/predictions/feed/deliveries",
    ]);
    expect(posts[0]!.body).toMatchObject({ consumer: "my-bridge", system: "custom", manifest });
    expect(posts[2]!.body).toEqual({ consumer: "my-bridge", run_id: "run_2", cursor: entries[1]!.cursor, applied: { items: 4, acted: 1 }, target: "My system" });
    expect(store.cursor).toBe(entries[1]!.cursor);
    expect(b.acted).toEqual(["s1"]);
  });

  it("resumes from the saved cursor after a restart without acting twice", async () => {
    const path = join(mkdtempSync(join(tmpdir(), "bridge-")), "state", "cursor.txt");
    const first = bridge(fakeApi(entries.slice(0, 1)), { store: new FileCursorStore(path) });
    await first.poll();
    expect(readFileSync(path, "utf8")).toBe(entries[0]!.cursor);

    const api = fakeApi(entries);
    const second = bridge(api, { store: new FileCursorStore(path) });
    expect(await second.poll()).toBe(1);
    expect(second.acted).toEqual(["s1"]);
    const third = bridge(api, { store: new FileCursorStore(path) });
    expect(await third.poll()).toBe(0);
    expect(third.acted).toEqual([]);
  });

  it("acts on an approved action sent again in a later run only once", async () => {
    const store = new MemoryCursorStore();
    const b = bridge(fakeApi([...entries, again]), { store });
    expect(await b.poll()).toBe(3);
    expect(b.acted).toEqual(["s1"]);
    expect([...store.actedKeys]).toEqual([["s1|swap|b1", "2030-01-08T01:00:00.000Z"]]);
  });

  it("does not act again after a restart on an action it carried out before", async () => {
    const path = join(mkdtempSync(join(tmpdir(), "bridge-")), "cursor.txt");
    await bridge(fakeApi(entries), { store: new FileCursorStore(path) }).poll();
    expect(readFileSync(`${path}.acted`, "utf8")).toBe("s1|swap|b1\t2030-01-08T01:00:00.000Z\n");
    const restarted = bridge(fakeApi([...entries, again]), { store: new FileCursorStore(path) });
    expect(await restarted.poll()).toBe(1);
    expect(restarted.acted).toEqual([]);
  });

  it("acts on a standing approval once, across runs and a restart, by its decision_id", async () => {
    const path = join(mkdtempSync(join(tmpdir(), "bridge-")), "cursor.txt");
    const standing = (cursor: string) => entry(cursor, "my-bridge", [item("s1", { ...approved, decision_id: "d1" })]);
    const first = bridge(fakeApi([standing("20300101T000001#run_1"), standing("20300101T000002#run_2")]), { store: new FileCursorStore(path) });
    await first.poll();
    expect(first.acted).toEqual(["s1"]);
    const restarted = bridge(fakeApi([standing("20300101T000003#run_3")]), { store: new FileCursorStore(path) });
    await restarted.poll();
    expect(restarted.acted).toEqual([]);
    expect(readFileSync(`${path}.acted`, "utf8")).toBe("d1\t2030-01-08T01:00:00.000Z\n");
  });

  it("acts again on a new approval of the same action", async () => {
    const decided = (cursor: string, decision: string) => entry(cursor, "my-bridge", [item("s1", { ...approved, decision_id: decision })]);
    const b = bridge(fakeApi([decided("20300101T000001#run_1", "d1"), decided("20300101T000002#run_2", "d2")]));
    await b.poll();
    expect(b.acted).toEqual(["s1", "s1"]);
  });

  it("forgets an approval a week after its action is over and keeps a live one", async () => {
    const store = new MemoryCursorStore("", new Map([["old", "2020-01-08T00:00:00.000Z"], ["live", "2099-01-08T00:00:00.000Z"], ["kept", ""]]));
    await bridge(fakeApi(entries), { store }).poll();
    expect([...store.actedKeys.keys()]).toEqual(["live", "kept", "s1|swap|b1"]);
  });

  it("keeps an approval until the latest of its pass end and cancel deadline, plus a week", () => {
    const late = item("s1", { ...approved, until: "2030-01-01T02:00:00Z", action: { ...approved.action, deadline: "2030-01-02T00:00:00Z" } });
    expect(actedUntil(late)).toBe("2030-01-09T00:00:00.000Z");
    expect(actedUntil(item("s1", { target_at: "soon" }))).toBe("");
  });

  it("retries an action whose act() threw, and the run with it", async () => {
    const store = new MemoryCursorStore();
    const b = bridge(fakeApi(entries), { store });
    b.failOnce = true;
    await expect(b.poll()).rejects.toThrow("went down");
    expect(store.cursor).toBe(entries[0]!.cursor);
    await b.poll();
    expect(b.acted).toEqual(["s1", "s1"]);
    expect(store.cursor).toBe(entries[1]!.cursor);
  });

  it("does not move the cursor past a run whose apply failed", async () => {
    class Failing extends Recorder {
      apply(): never {
        throw new Error("system down");
      }
    }
    const store = new MemoryCursorStore();
    const api = fakeApi(entries);
    const b = new Failing({ manifest, store, conformanceLog: "", backfill: true, client: new Client({ apiKey: "k", baseUrl: "https://api.test", fetch: api.fetch }) });
    await expect(b.poll()).rejects.toThrow("system down");
    expect(store.cursor).toBe("");
    expect(api.calls.some((c) => c.url.pathname.endsWith("/deliveries"))).toBe(false);
  });

  it("stops run() on a refused key but keeps polling after other failures", async () => {
    const api = fakeApi(entries, { fail: [500, 500, 500, 500, 500, 401] });
    await expect(bridge(api).run()).rejects.toMatchObject({ status: 401, refused: true });
    expect(api.calls).toHaveLength(6);
  });

  it("stops run() at once with no key, unless the API is the mock on this machine", async () => {
    const api = fakeApi([]);
    const keyless = (baseUrl: string) => new Recorder({ manifest, store: new MemoryCursorStore(), conformanceLog: "", pollMs: 1, client: new Client({ apiKey: "", baseUrl, fetch: api.fetch }) });
    await expect(keyless("https://api.test").run()).rejects.toThrow("CONSTELLATION_API_KEY is not set: make a key with predictions:read on https://platform.test/keys");
    expect(api.calls).toEqual([]);
    const stop = new AbortController();
    const running = keyless("http://localhost:7070").run(stop.signal);
    await new Promise((resolve) => setTimeout(resolve, 20));
    stop.abort();
    await running;
    expect(api.calls.length).toBeGreaterThan(0);
  });

  it("never resumes a cursor read from another API (the mock)", async () => {
    const path = join(mkdtempSync(join(tmpdir(), "bridge-")), "cursor.txt");
    await bridge(fakeApi(entries), { store: new FileCursorStore(path, "http://localhost:7070") }).poll();
    await expect(bridge(fakeApi(entries), { store: new FileCursorStore(path, "https://api.test") }).run()).rejects.toThrow(
      `${path} is where this bridge read http://localhost:7070 up to, and it now reads https://api.test`,
    );
    expect(await new FileCursorStore(path, "HTTP://LOCALHOST:7070/").load()).toBe(entries[1]!.cursor);
    process.env.CURSOR_FILE = path;
    const byDefault = new Recorder({ manifest, client: new Client({ apiKey: "k", baseUrl: "https://api.test", fetch: fakeApi([]).fetch }) });
    delete process.env.CURSOR_FILE;
    await expect(byDefault.store.load()).rejects.toThrow("it now reads https://api.test");
  });

  it.each(["cursor", "acted"])("stops run() when it cannot save its %s, instead of repeating its work", async (what) => {
    class Unwritable extends MemoryCursorStore {
      async save(cursor: string) {
        if (what === "cursor") throw new Error("EACCES: permission denied, open 'cursor.txt.tmp'");
        await super.save(cursor);
      }
      async saveActed(acted: Map<string, string>) {
        if (what === "acted") throw new Error("EACCES: permission denied, open 'cursor.txt.acted.tmp'");
        await super.saveActed(acted);
      }
    }
    const api = fakeApi(entries);
    await expect(bridge(api, { store: new Unwritable() }).run()).rejects.toThrow(/cannot keep its state.*EACCES/);
    expect(api.calls.filter((c) => c.url.pathname.endsWith("/checkin"))).toHaveLength(1);
  });

  it("starts at the newest run on a first start and never acts on the older decisions", async () => {
    const feed = fakeApi([
      entry("20300101T000001#run_1", "my-bridge", [item("old", { ...approved, decision_id: "d1" })]),
      entry("20300101T000002#run_2", "my-bridge", [item("closed", { ...expired, decision_id: "d2" }), item("p1")]),
    ]);
    const store = new MemoryCursorStore();
    for (let n = 0; n < 3; n++) {
      const b = bridge(feed, { store, backfill: false });
      await b.poll();
      expect(b.acted).toEqual([]);
    }
    expect(store.cursor).toBe("20300101T000002#run_2");
  });

  it("asks once where a first start reads from", async () => {
    const api = fakeApi([
      entry("20300101T000001#run_1", "my-bridge", [item("a")]),
      entry("20300101T000002#run_2", "my-bridge", [item("b")]),
      { cursor: "20300101T000003#run_3", otlp: { resourceLogs: [] } },
    ]);
    const store = new MemoryCursorStore();
    await bridge(api, { store, backfill: false }).poll();
    expect(store.cursor).toBe("20300101T000003#run_3");
    const [first, ...rest] = api.calls.filter((c) => c.method === "GET");
    expect(first!.url.searchParams.get("from")).toBe("latest");
    expect(rest.every((c) => (c.url.searchParams.get("after") ?? "") >= "20300101T000001#run_1")).toBe(true);
  });

  it("fails a first start plainly against an API that ignores from=latest", async () => {
    const older = fakeApi([entry("20300101T000001#run_1", "my-bridge", [item("a")])], { older: true });
    const store = new MemoryCursorStore();
    await expect(bridge(older, { store, backfill: false }).poll()).rejects.toThrow(/from=latest.*contract 1\.5\.0 or later/);
    expect(older.calls.filter((c) => c.method === "GET")).toHaveLength(1);
    expect(store.cursor).toBe("");
  });

  it("never acts on an action past its cancel deadline, even replaying the history", async () => {
    const b = bridge(fakeApi([entry("20300101T000001#run_1", "my-bridge", [item("closed", expired)])]));
    await b.poll();
    expect(b.acted).toEqual([]);
  });

  it("acts at most once on an approval of a long past pass, however often it is sent", async () => {
    const old = item("old", { ...approved, target_at: "2020-01-01T01:00:00Z", decision_id: "d-old" });
    const feed: ReturnType<typeof entry>[] = [];
    const b = bridge(fakeApi(feed));
    for (let n = 1; n <= 3; n++) {
      feed.push(entry(`2030010${n}T000000#run_${n}`, "my-bridge", [old]));
      await b.poll();
    }
    expect(b.acted).toEqual(["old"]);
  });

  it("retries a failed report without applying or acting again", async () => {
    const api = fakeApi(entries.slice(1), { fail: [0, 0, 503, 503, 503, 503, 503] });
    class Counting extends Recorder {
      applies = 0;
      apply(items: Result[]) {
        this.applies++;
        return super.apply(items);
      }
    }
    const b = new Counting({ manifest, store: new MemoryCursorStore(), conformanceLog: "", backfill: true,
      client: new Client({ apiKey: "k", baseUrl: "https://api.test", fetch: api.fetch, retryDelayMs: 0, retries: 4, log: () => {} }) });
    await expect(b.poll()).rejects.toMatchObject({ status: 503 });
    expect(await b.poll()).toBe(1);
    expect(b.applies).toBe(1);
    expect(b.acted).toEqual(["s1"]);
  });

  it("never applies again a run a replaying feed delivers again", async () => {
    const store = new MemoryCursorStore();
    await bridge(fakeApi(entries), { store }).poll();
    const api = fakeApi([...entries, again]);
    const replaying = { ...api, fetch: (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      if (url.searchParams.get("after") === entries[1]!.cursor && !replayed) {
        replayed = true;
        url.searchParams.set("after", "");
      }
      return api.fetch(url, init);
    }) as typeof fetch };
    let replayed = false;
    const b = bridge(replaying, { store });
    expect(await b.poll()).toBe(1);
    expect(b.acted).toEqual([]);
  });

  it("skips and counts a malformed item, and the run and the runs after it go on", async () => {
    const api = fakeApi([
      entry("20300101T000001#run_1", "my-bridge", [item("bad", { target_at: "soon" }), item("good")]),
      entry("20300101T000002#run_2", "my-bridge", [item("next")]),
    ]);
    const store = new MemoryCursorStore();
    expect(await bridge(api, { store }).poll()).toBe(2);
    const reports = api.calls.filter((c) => c.url.pathname.endsWith("/deliveries")).map((c) => c.body.applied);
    expect(reports).toEqual([{ items: 1, malformed: 1 }, { items: 1 }]);
    expect(store.cursor).toBe("20300101T000002#run_2");
  });

  it("writes the conformance log, one line per item applied", async () => {
    const log = join(mkdtempSync(join(tmpdir(), "bridge-")), "actions.jsonl");
    await bridge(fakeApi([...entries, again]), { conformanceLog: log }).poll();
    const lines = readFileSync(log, "utf8").trimEnd().split("\n").map((l) => JSON.parse(l));
    expect(lines).toEqual([
      { item_id: "p1", state: "forecast", acted: false, verb: null },
      { item_id: "p2", state: "advice", acted: false, verb: "swap" },
      { item_id: "s1", state: "approved", acted: true, verb: "swap" },
      { item_id: "s2", state: "pending", acted: false, verb: null },
      { item_id: "s3", state: "rejected", acted: false, verb: null },
      { item_id: "s4", state: "pending", acted: false, verb: null },
      { item_id: "s1", state: "approved", acted: false, verb: "swap" },
    ]);
  });
});
