import type { components } from "./generated/api.js";

// Request bodies come from the contract's openapi.json (`npm run generate`). The workflow
// results response is untyped there, so its shapes are written here from the contract's
// workflow-result.schema.json and the results route's documented OTLP envelope.
export type Schemas = components["schemas"];
export type CheckIn = Schemas["CheckIn"];
export type Delivery = Schemas["Delivery"];

/**
 * One result of a run for a connection, item shape constellation.item/v2
 * (the contract's workflow-result.schema.json). severity, action.verb, action.mode and
 * action_status are open strings: handle the known values and do not reject a new one.
 */
export interface Result {
  entity: {
    type: string;
    id: string;
    link_id?: string;
    satellite_id?: string;
    ground_station_id?: string;
    antenna_id?: string;
    source?: string;
    /** A pass's ground track, when known. */
    track?: Record<string, unknown>[];
    [key: string]: unknown;
  };
  target_at: string;
  until?: string;
  value: { p10?: number | null; p50?: number | null; p90?: number | null; [key: string]: unknown };
  explain?: Record<string, Record<string, unknown>>;
  /** A workflow's item carries workflow_version, a lane's model_release_id; never both. */
  workflow_version?: number;
  model_release_id?: string;
  /** What a lane result's value measures (snr_db, rx_power_dbm) and its unit (dB, dBm). */
  quantity?: string;
  unit?: string;
  series?: { t: string; value: unknown }[];
  /** Known values: info, warning, critical; read any other as info. */
  severity?: "info" | "warning" | "critical" | (string & {});
  /** A sentence for a person: the swap to make, or why there is none. */
  note?: string;
  /** Present on advice and approved items; a held item carries only action_status. */
  action?: ResultAction | null;
  /** Known values: pending, approved, rejected. Only approved may be acted on. */
  action_status?: "pending" | "approved" | "rejected" | (string & {}) | null;
  /** The action's own id, the same in every run that carries it. */
  action_id?: string;
  /** The id of the decision on the action, once approved or rejected: act once per decision_id. */
  decision_id?: string;
  [key: string]: unknown;
}

export interface ResultAction {
  /** Known values: reroute, swap. Skip a verb you do not know. */
  verb: "reroute" | "swap" | (string & {});
  /** Known values: suggest, approve. */
  mode: "suggest" | "approve" | (string & {});
  from?: string;
  to?: string | null;
  to_antenna_id?: string | null;
  to_at?: string | null;
  deadline?: string;
  gain_gb_p10?: number;
  [key: string]: unknown;
}

export interface OtlpKeyValue {
  key: string;
  value: Record<string, unknown>;
}

export interface OtlpLogs {
  resource?: { attributes?: OtlpKeyValue[] };
  scopeLogs?: {
    scope?: { name?: string };
    logRecords?: { body: Record<string, unknown>; attributes?: OtlpKeyValue[] }[];
  }[];
}

/** An entry of GET /predictions/feed: one finished run's items as an OTLP log request. */
export interface RawResultsEntry {
  cursor: string;
  otlp: { resourceLogs: OtlpLogs[] };
  next_page?: number;
}

export interface ResultsPage {
  entries: RawResultsEntry[];
  next: string;
  oldest?: string | null;
}

export interface RunInfo {
  workflow: string | undefined;
  workflow_version: number | undefined;
  run_id: string | undefined;
  window_from: string | undefined;
  window_to: string | undefined;
}

/** One feed output of a run for this connection: its items, its settings and the run. */
export interface ResultsPart {
  resource: Record<string, unknown>;
  items: Result[];
  settings: Record<string, unknown>;
  run: RunInfo;
}

/** A run, every page joined, as the parts this reader reads. */
export interface ResultsEntry {
  cursor: string;
  parts: ResultsPart[];
}

/** A custom bridge's manifest (connector.json): the API's ConnectorManifest plus its version. */
export type Manifest = Schemas["ConnectorManifest"] & { version?: string };
