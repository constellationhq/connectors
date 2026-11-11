import { PAGE_RECORDS, unpack, whole } from "./results.js";
import type { CheckIn, Delivery, ResultsEntry, ResultsPage, Manifest, RawResultsEntry } from "./types.js";

export const DEFAULT_API_URL = "https://api.constellation.space";

/**
 * The Constellation site that serves `apiUrl`, where its keys and billing pages are: api.<domain>
 * is platform.<domain>, <env>.api.<domain> is <env>-platform.<domain>. Any other API is told the
 * public site.
 */
export function site(apiUrl: string | undefined): string {
  let host = "";
  try {
    host = new URL(apiUrl ?? "").hostname;
  } catch {}
  if (host.startsWith("api.")) return `https://platform.${host.slice(4)}`;
  const at = host.indexOf(".api.");
  if (at > 0) return `https://${host.slice(0, at)}-platform.${host.slice(at + 5)}`;
  return "https://platform.constellation.space";
}

/** Whether `url` names this machine: localhost or a loopback address. */
export function loopback(url: string): boolean {
  let host = "";
  try {
    host = new URL(url).hostname.replace(/^\[|\]$/g, "");
  } catch {}
  return host === "localhost" || host === "::1" || /^127\.\d+\.\d+\.\d+$/.test(host);
}

/** `url`'s origin, any name for this machine (localhost, 127.0.0.1, ::1) as localhost: two URLs with one origin are the same API. */
export function origin(url: string): string {
  const u = new URL(url);
  if (loopback(url)) u.hostname = "localhost";
  return u.origin;
}

/** Whether an API key may go to `url`: over https, or plain http to this machine. */
export function keySafe(url: string): boolean {
  let scheme = "";
  try {
    scheme = new URL(url).protocol;
  } catch {}
  return scheme === "https:" || (scheme === "http:" && loopback(url));
}

export interface ClientOptions {
  /** Sent as x-api-key, never over plain http to another machine. Defaults to CONSTELLATION_API_KEY in Node. */
  apiKey?: string;
  /** Defaults to CONSTELLATION_API_URL in Node, else https://api.constellation.space. */
  baseUrl?: string;
  /** Retries after a 429, a 5xx or a network failure, with exponential backoff. */
  retries?: number;
  retryDelayMs?: number;
  /** How long one attempt may take, answer read included, before it counts as a network failure (60 s). */
  timeoutMs?: number;
  fetch?: typeof fetch;
  log?: (message: string) => void;
}

const env = (name: string): string | undefined =>
  (globalThis as { process?: { env: Record<string, string | undefined> } }).process?.env[name];

/** The longest Retry-After honored: a longer one is waited this long, then asked again. */
export const MAX_RETRY_AFTER_S = 60;

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** A refused or failed API call, carrying the API's error body. */
export class ConstellationError extends Error {
  /** The API's machine error, e.g. plan_required, insufficient_scope, unknown_cursor. */
  readonly code: string | undefined;
  /** The error object: the body's `detail` when it is an object, else the body. */
  readonly detail: Record<string, any>;

  constructor(
    readonly status: number,
    readonly body: unknown,
    apiUrl?: string,
  ) {
    const b = body as Record<string, any> | null;
    const inner = b && typeof b === "object" ? (typeof b.detail === "object" && b.detail ? b.detail : b) : {};
    super(describe(status, inner, b, site(apiUrl)));
    this.name = "ConstellationError";
    this.detail = inner;
    this.code = typeof inner.error === "string" ? inner.error : undefined;
  }

  /** 401 or 403: neither clears by asking again, so a bridge stops on one. */
  get refused(): boolean {
    return this.status === 401 || this.status === 403;
  }
}

function describe(status: number, d: Record<string, any>, body: unknown, home: string): string {
  const fix = `make a key with predictions:read on ${home}/keys and set it as CONSTELLATION_API_KEY`;
  if (status === 401) return `Constellation does not accept the API key (HTTP 401; wrong, revoked or expired): ${fix}`;
  if (status === 403) {
    if (d.error === "plan_required")
      return `this account's plan does not include ${d.feature} (HTTP 403 plan_required); the ${d.plan} plan does. Upgrade on ${home}/billing`;
    if (d.error === "insufficient_scope") return `the API key lacks ${d.required} (HTTP 403): ${fix}`;
    if (d.error === "live_key_required") return `the API key is a test key; this takes a live one (HTTP 403): ${fix}`;
    return `Constellation refused the API key (HTTP 403 ${d.error ?? "forbidden"}): ${fix}`;
  }
  return `Constellation answered ${status}: ${JSON.stringify(body)?.slice(0, 300)}`;
}

export class Client {
  readonly baseUrl: string;
  private readonly apiKey: string;
  private readonly retries: number;
  private readonly retryDelayMs: number;
  private readonly timeoutMs: number;
  private readonly fetchFn: typeof fetch;
  readonly log: (message: string) => void;

  constructor(options: ClientOptions = {}) {
    this.apiKey = (options.apiKey ?? env("CONSTELLATION_API_KEY") ?? "").trim();
    this.baseUrl = (options.baseUrl ?? env("CONSTELLATION_API_URL") ?? DEFAULT_API_URL).replace(/\/+$/, "");
    this.retries = options.retries ?? 4;
    this.retryDelayMs = options.retryDelayMs ?? 500;
    this.timeoutMs = options.timeoutMs ?? 60_000;
    this.fetchFn = options.fetch ?? globalThis.fetch.bind(globalThis);
    this.log = options.log ?? ((m) => console.warn(`constellation: ${m}`));
  }

  /** Whether it has an API key to send. */
  get hasKey(): boolean {
    return this.apiKey !== "";
  }

  /** Any API call: `query` values left undefined are dropped. Returns the parsed JSON body, or undefined. */
  async request<T = any>(
    method: string,
    path: string,
    options: { query?: Record<string, string | number | undefined>; body?: unknown } = {},
  ): Promise<T> {
    const query = Object.entries(options.query ?? {}).filter(([, v]) => v !== undefined) as [string, string][];
    const url = `${this.baseUrl}${path}${query.length ? `?${new URLSearchParams(query.map(([k, v]) => [k, String(v)]))}` : ""}`;
    if (this.apiKey && !keySafe(this.baseUrl))
      throw new Error(`not sending the API key to ${this.baseUrl}: it is not https (plain http only to localhost)`);
    const init: RequestInit = {
      method,
      headers: { ...(this.apiKey && { "x-api-key": this.apiKey }), "content-type": "application/json", accept: "application/json" },
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    };
    for (let attempt = 0; ; attempt++) {
      let response: Response;
      let text: string;
      try {
        response = await this.fetchFn(url, { ...init, signal: AbortSignal.timeout(this.timeoutMs) });
        text = await response.text();
      } catch (error) {
        if (attempt >= this.retries) throw error;
        await sleep(this.retryDelayMs * 2 ** attempt);
        continue;
      }
      const body = text ? parse(text) : undefined;
      if (response.ok) return body as T;
      const retryable = response.status === 429 || response.status >= 500;
      if (!retryable || attempt >= this.retries) throw new ConstellationError(response.status, body, this.baseUrl);
      const after = Math.min(Number(response.headers.get("retry-after")), MAX_RETRY_AFTER_S);
      if (after > 0) this.log(`answered ${response.status}; retrying in ${after} s (Retry-After)`);
      await sleep(after > 0 ? after * 1000 : this.retryDelayMs * 2 ** attempt);
    }
  }

  /**
   * One page of workflow results after `after`: whole entries, or first pages when `pageRecords` is set.
   * `from: "latest"` asks where a first start reads from instead: no entries, and `next` just before
   * the newest run with something for `consumer`.
   */
  resultsPage(
    options: { after?: string; consumer?: string; limit?: number; pageRecords?: number; from?: "latest" } = {},
  ): Promise<ResultsPage> {
    return this.request("GET", "/predictions/feed", {
      query: {
        from: options.from,
        after: options.after ?? "",
        limit: options.limit,
        consumer: options.consumer,
        page_records: options.pageRecords,
      },
    });
  }

  /**
   * Runs with results after `after`, oldest first, each joined whole and unpacked; a cursor older than
   * the oldest entry the feed holds is logged (the runs between expired unread). Ends when the API
   * has no more, unless `pollMs` is set: then it waits that long and asks again, for as long as the
   * caller iterates or until `signal` aborts.
   */
  async *results(
    options: { after?: string; consumer?: string; pollMs?: number; signal?: AbortSignal } = {},
  ): AsyncGenerator<ResultsEntry> {
    const { consumer } = options;
    let after = options.after ?? "";
    while (!options.signal?.aborted) {
      const page = await this.resultsPage({ after, consumer, limit: 1, pageRecords: PAGE_RECORDS });
      if (after && page.oldest && after < page.oldest)
        this.log(`the feed no longer holds cursor ${after}: any runs after it and before the oldest it holds, ${page.oldest}, expired unread; reading on from there`);
      for (const entry of page.entries) {
        const full = await whole(entry, (cursor, n) =>
          this.request<RawResultsEntry>("GET", "/predictions/feed", {
            query: { cursor, page: n, page_records: PAGE_RECORDS, consumer },
          }),
        );
        yield { cursor: entry.cursor, parts: unpack(full, consumer, this.log) };
        after = entry.cursor;
      }
      if (page.entries.length) continue;
      if (options.pollMs === undefined) return;
      await sleep(options.pollMs);
    }
  }

  /** Registers a custom bridge's manifest; it waits for approval on Integrations. */
  checkIn(manifest: Manifest): Promise<void> {
    const { name, title, description, settings } = manifest;
    const body: CheckIn = {
      consumer: name,
      system: "custom",
      manifest: { name, title, ...(description !== undefined && { description }), ...(settings && { settings }) },
      bridge_version: manifest.version ?? "0.1.0",
      target: title,
    };
    return this.request("POST", "/predictions/feed/checkin", { body });
  }

  /** Tells Constellation what the run at `cursor` became in the system, shown in Constellation. */
  report(consumer: string, cursor: string, applied: Record<string, number>, target: string): Promise<void> {
    const body: Delivery = { consumer, run_id: cursor.slice(cursor.indexOf("#") + 1), cursor, applied, target };
    return this.request("POST", "/predictions/feed/deliveries", { body });
  }
}

function parse(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}
