import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { describe, expect, it, vi } from "vitest";
import { actionState, Client, ConstellationError } from "../src/index.js";
import { origin, site } from "../src/client.js";
import { entry, fakeApi, item } from "./fake.js";

const client = (fetch: typeof globalThis.fetch, log: (m: string) => void = () => {}) =>
  new Client({ apiKey: "k", baseUrl: "https://api.test/", fetch, retryDelayMs: 0, log });

const collect = async <T>(it: AsyncIterable<T>) => {
  const out: T[] = [];
  for await (const x of it) out.push(x);
  return out;
};

describe("actionState, identical to constellation_space.results.action_state", () => {
  const swap = { verb: "swap" };
  it.each([
    [{}, null],
    [{ action: swap }, "advice"],
    [{ action: swap, action_status: "approved" }, "approved"],
    [{ action_status: "approved" }, "pending"],
    [{ action: {}, action_status: "approved" }, "pending"],
    [{ action: {} }, null],
    [{ action_status: "pending" }, "pending"],
    [{ action: swap, action_status: "pending" }, "pending"],
    [{ action_status: "rejected" }, "rejected"],
    [{ action: swap, action_status: "rejected" }, "rejected"],
    [{ action_status: "escalated" }, "pending"],
    [{ action: swap, action_status: "escalated" }, "pending"],
    [{ action_status: "" }, "pending"],
    [{ action: null, action_status: null }, null],
  ])("%j is %s", (fields, state) => {
    expect(actionState(fields as never)).toBe(state);
  });
});

describe("Client", () => {
  it("sends the key as x-api-key and defaults from the environment", async () => {
    process.env.CONSTELLATION_API_KEY = " env-key ";
    process.env.CONSTELLATION_API_URL = "https://env.test/";
    const api = fakeApi([]);
    const c = new Client({ fetch: api.fetch });
    await c.resultsPage();
    expect(c.baseUrl).toBe("https://env.test");
    expect(api.calls[0]!.url.origin).toBe("https://env.test");
    expect(api.calls[0]!.headers["x-api-key"]).toBe("env-key");
    delete process.env.CONSTELLATION_API_KEY;
    delete process.env.CONSTELLATION_API_URL;
  });

  it.each([
    ["http://localhost:7070", true],
    ["http://127.0.0.1:7070", true],
    ["http://[::1]:7070", true],
    ["https://api.test", true],
    ["http://10.0.0.5:7070", false],
    ["http://example.com", false],
    ["ftp://x.test", false],
  ])("sends the key to %s: %s", async (baseUrl, sent) => {
    const api = fakeApi([]);
    const c = new Client({ apiKey: "k", baseUrl, fetch: api.fetch, retries: 0 });
    if (sent) await c.resultsPage();
    else await expect(c.resultsPage()).rejects.toThrow(/not sending the API key/);
    expect(api.calls.map((call) => call.headers["x-api-key"])).toEqual(sent ? ["k"] : []);
  });

  it.each(["http://localhost:7070", "http://127.0.0.1:7070/", "HTTP://[::1]:7070"])("counts %s as this machine's origin", (url) => {
    expect(origin(url)).toBe("http://localhost:7070");
  });

  it("sends no x-api-key header without a key", async () => {
    const api = fakeApi([]);
    await new Client({ apiKey: "", baseUrl: "http://example.com", fetch: api.fetch }).resultsPage();
    expect("x-api-key" in api.calls[0]!.headers).toBe(false);
  });

  it("retries 429 and 5xx, then succeeds", async () => {
    const api = fakeApi([], { fail: [429, 503, 500] });
    const page = await client(api.fetch).resultsPage();
    expect(page.entries).toEqual([]);
    expect(api.calls).toHaveLength(4);
  });

  it("gives up after its retries with the last error", async () => {
    const api = fakeApi([], { fail: [503, 503, 503] });
    const c = new Client({ apiKey: "k", baseUrl: "https://api.test", fetch: api.fetch, retries: 2, retryDelayMs: 0 });
    await expect(c.resultsPage()).rejects.toMatchObject({ status: 503 });
    expect(api.calls).toHaveLength(3);
  });

  it("gives up on an attempt that gets no answer within timeoutMs, and retries it", async () => {
    let asked = 0;
    const server = createServer(() => void asked++).listen(0, "127.0.0.1");
    await new Promise((resolve) => server.once("listening", resolve));
    const { port } = server.address() as AddressInfo;
    const c = new Client({ apiKey: "k", baseUrl: `http://127.0.0.1:${port}`, timeoutMs: 100, retries: 1, retryDelayMs: 0 });
    await expect(c.resultsPage()).rejects.toMatchObject({ name: "TimeoutError" });
    expect(asked).toBe(2);
    server.closeAllConnections();
    server.close();
  });

  it("waits at most 60 s for a Retry-After, and says so", async () => {
    vi.useFakeTimers();
    try {
      let n = 0;
      const fetch = (async () =>
        n++ === 0
          ? new Response("{}", { status: 503, headers: { "retry-after": "86400" } })
          : new Response(JSON.stringify({ entries: [], next: "" }), { status: 200 })) as typeof globalThis.fetch;
      const logs: string[] = [];
      const page = client(fetch, (m) => logs.push(m)).resultsPage();
      await vi.advanceTimersByTimeAsync(60_000);
      await expect(page).resolves.toMatchObject({ entries: [] });
      expect(logs).toEqual(["answered 503; retrying in 60 s (Retry-After)"]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("logs a cursor older than the oldest entry the feed holds", async () => {
    const fetch = (async () => new Response(JSON.stringify({ entries: [], next: "", oldest: "5#e" }))) as typeof globalThis.fetch;
    const logs: string[] = [];
    for (const after of ["1#a", "6#f"]) for await (const _ of client(fetch, (m) => logs.push(m)).results({ after }));
    expect(logs).toEqual([expect.stringContaining("no longer holds cursor 1#a")]);
  });

  it("retries a network failure", async () => {
    let n = 0;
    const fetch = (async () => {
      if (n++ === 0) throw new TypeError("fetch failed");
      return new Response(JSON.stringify({ entries: [], next: "" }), { status: 200 });
    }) as typeof globalThis.fetch;
    await expect(client(fetch).resultsPage()).resolves.toMatchObject({ entries: [] });
  });

  it("does not retry a 4xx and carries the API's error body", async () => {
    let calls = 0;
    const body = { error: "plan_required", plan: "enterprise", feature: "output:lattice" };
    const fetch = (async () => {
      calls++;
      return new Response(JSON.stringify({ detail: body }), { status: 403 });
    }) as typeof globalThis.fetch;
    const error = await client(fetch).resultsPage().catch((e) => e);
    expect(error).toBeInstanceOf(ConstellationError);
    expect(calls).toBe(1);
    expect(error).toMatchObject({ status: 403, code: "plan_required", refused: true, detail: body });
    expect(error.message).toContain("enterprise plan");
  });

  it("links the keys and billing pages of the API it calls", async () => {
    const fetch = (async () => new Response("", { status: 401 })) as typeof globalThis.fetch;
    const dev = new Client({ apiKey: "k", baseUrl: "https://dev.api.constellation.space", fetch, retries: 0 });
    const error = await dev.request("GET", "/x").catch((e) => e);
    expect(error.message).toContain("https://dev-platform.constellation.space/keys");
    expect(site("https://api.constellation.space")).toBe("https://platform.constellation.space");
    expect(site("https://stage.api.constellation.space")).toBe("https://stage-platform.constellation.space");
    expect(site("http://127.0.0.1:7070")).toBe("https://platform.constellation.space");
  });

  it("reads an error body that is not wrapped in detail", async () => {
    const fetch = (async () =>
      new Response(JSON.stringify({ error: "plan_required", plan: "standard", feature: "webhooks" }), {
        status: 403,
      })) as typeof globalThis.fetch;
    await expect(client(fetch).request("GET", "/x")).rejects.toMatchObject({ code: "plan_required", detail: { plan: "standard" } });
  });
});

describe("results", () => {
  const entries = [
    entry("20300101T000001#run_1", "me", [item("a"), item("b"), item("c")]),
    entry("20300101T000002#run_2", "me", [item("d")], "constellation.item/v9"),
    entry("20300101T000003#run_3", "other", [item("e")]),
    entry("20300101T000004#run_4", "me", [item("f", { action: { verb: "swap", mode: "approve" }, action_status: "approved" })]),
  ];

  it("follows the cursor and joins every page of a run", async () => {
    // A page size of 1 makes every run with several items arrive in pages.
    const api = fakeApi(entries, { pageSize: 1 });
    const logs: string[] = [];
    const got = await collect(client(api.fetch, (m) => logs.push(m)).results({ consumer: "me" }));
    expect(got.map((e) => e.cursor)).toEqual(entries.map((e) => e.cursor));
    expect(got[0]!.parts[0]!.items.map((i) => i.entity.id)).toEqual(["a", "b", "c"]);
    expect(got[0]!.parts[0]!.settings).toEqual({ min_p10_gb: 5 });
    expect(got[0]!.parts[0]!.run).toMatchObject({ workflow: "wf", run_id: "run_1", workflow_version: 1 });
    // a newer item shape is skipped and logged; another connection's part is left alone
    expect(got[1]!.parts).toEqual([]);
    expect(logs.join()).toContain("constellation.item/v9");
    expect(got[2]!.parts).toEqual([]);
    expect(actionState(got[3]!.parts[0]!.items[0]!)).toBe("approved");
    const afters = api.calls.filter((c) => !c.url.searchParams.has("cursor")).map((c) => c.url.searchParams.get("after"));
    expect(afters).toEqual(["", ...entries.map((e) => e.cursor)]);
  });

  it("starts after the given cursor", async () => {
    const api = fakeApi(entries);
    const got = await collect(client(api.fetch).results({ after: entries[2]!.cursor }));
    expect(got.map((e) => e.cursor)).toEqual([entries[3]!.cursor]);
  });

  it("keeps polling with pollMs until aborted", async () => {
    const api = fakeApi([]);
    const abort = new AbortController();
    const done = collect(client(api.fetch).results({ pollMs: 1, signal: abort.signal }));
    await new Promise((r) => setTimeout(r, 30));
    abort.abort();
    expect(await done).toEqual([]);
    expect(api.calls.length).toBeGreaterThan(1);
  });
});
