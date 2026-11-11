// The Constellation Forecast plugin for Open MCT. Released as constellation-openmct.js, one file
// an Open MCT page loads without a rebuild (it sets window.ConstellationForecast):
//
//   <script src="constellation-openmct.js"></script>
//   openmct.install(ConstellationForecast({ source: 'api', token: '<live key with results:read>' }));
//
// It reads forecasts from one of two sources:
//
//   source: 'api'     the Constellation results feed (GET /predictions/feed) as the connection
//                     `consumer` (default "openmct"), with a live key (cos_live_...) whose only
//                     scope is results:read: it reads the feed and nothing else, so the browser
//                     can hold it. The API answers any origin on the feed, without cookies. Both
//                     need a Constellation API release that has them; until yours does, use
//                     the Yamcs source. Never put a predictions:read key in a page: anyone who
//                     opens the page can read it, and it can do more than read the feed.
//   source: 'yamcs'   (default) what the Constellation Yamcs bridge writes to Yamcs's
//                     "constellation" bucket, read from yamcsUrl (required), so it needs no
//                     Constellation key. It sends no credentials. Stock Yamcs answers no other
//                     origin, so either point yamcsUrl at a proxy on the Open MCT page's own
//                     origin, or let Yamcs answer it in etc/yamcs.yaml:
//
//                       services:
//                         - class: org.yamcs.http.HttpServer
//                           args:
//                             cors:
//                               allowOrigin: "https://<your Open MCT>"
//                               allowCredentials: false
//
// What it adds to the tree, under "Constellation Forecast":
//
//   Forecast overview         the Constellation Forecast view: every workflow, by category
//   <workflow>                that workflow's forecast view
//   <workflow> · passes       an Open MCT Plan: each forecast pass as an activity per antenna,
//                             for Time Strips beside your own telemetry
//   Forecast time strip       a Time Strip holding every workflow's plan
//
// The view is the shared Constellation Forecast view (bridges/shared/forecast-view.js), the
// same one the Yamcs page draws, so both show the same thing.
import { mountForecast, actionState, TITLES } from './forecast-view.js';

const NS = 'constellation';
const ROOT = { namespace: NS, key: 'root' };
// Plans are for things that happen over time; capacity is per day and has its own view. A
// workflow's kind is what its results are (the bridge names it, as the template that makes them).
const PLANNED = new Set(['pass-yield', 'modcod-plan', 'rain-fade-swap']);
const id = (key) => ({ namespace: NS, key });
// Records a page of a run, as the Python bridges read them, and the most pages read of one run.
const PAGE_RECORDS = 400;
const MAX_PAGES = 50;

/** Whether a CSS colour is dark enough that the view should use its dark theme. */
export function isDark(color) {
  const [r, g, b] = (color.match(/\d+(\.\d+)?/g) || ['255', '255', '255']).map(Number);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b < 128;
}

/** A run's results as an Open MCT Plan body: one group per antenna, one activity per pass. */
export function planBody(run) {
  const groups = {};
  for (const item of run.items || []) {
    if (item.entity?.type !== 'pass' || !item.until) continue;
    const antenna = item.entity.antenna_id;
    const ledger = item.explain?.expected_gb?.value;
    const gb = typeof ledger?.p50 === 'number' ? ledger : item.value;
    const look = {
      approved: { suffix: ' · swap approved', type: 'rain-fade-approved', color: '#d4351c', textColor: '#ffffff' },
      pending: { suffix: ' · awaiting approval', type: 'awaiting-approval', color: '#8e8e8a', textColor: '#ffffff' },
      advice: { suffix: ' · rain-fade risk', type: 'rain-fade-risk', color: '#fab219', textColor: '#1a1a19' },
    }[actionState(item)] || { suffix: '', type: 'forecast-pass', color: '#2a78d6', textColor: '#ffffff' };
    (groups[antenna] ||= []).push({
      name: `Forecast · ${item.entity.satellite_id}${typeof gb?.p50 === 'number' ? ` · ${gb.p50.toFixed(1)} GB` : ''}${look.suffix}`,
      start: Date.parse(item.target_at),
      end: Date.parse(item.until),
      type: look.type,
      color: look.color,
      textColor: look.textColor,
    });
  }
  return groups;
}

/** An OTLP AnyValue back to plain JSON. */
function plain(value) {
  if ('kvlistValue' in value) return Object.fromEntries((value.kvlistValue.values ?? []).map((kv) => [kv.key, plain(kv.value)]));
  if ('arrayValue' in value) return (value.arrayValue.values ?? []).map(plain);
  if ('intValue' in value) return Number(value.intValue);
  for (const key of ['doubleValue', 'boolValue', 'stringValue']) if (key in value) return value[key];
  return null;
}

/**
 * A load(name) over the Constellation results feed: the newest run of each workflow addressed to
 * `consumer`, as the objects the Yamcs bridge writes ("index.json", "workflow-<slug>.json").
 */
export function feedLoader({
  apiUrl = 'https://api.constellation.space',
  token,
  consumer = 'openmct',
  fetchFn = fetch,
  now = Date.now,
}) {
  const runs = new Map();
  let cursor = '';
  let reading;
  // A refused key stays refused until the page reloads with another, and a 429 (a rate limit,
  // auth_lockout, quota_exceeded) or 402 does not clear by asking again soon: every tab asking
  // would only keep the lockout going, so the loader stops asking.
  let refused;
  let limited;
  let pausedUntil = 0;
  let pause = 30_000;

  async function get(query) {
    if (refused) throw refused;
    if (now() < pausedUntil) throw limited;
    const url = `${apiUrl.replace(/\/+$/, '')}/predictions/feed?${new URLSearchParams(query)}`;
    const response = await fetchFn(url, { headers: { 'x-api-key': token }, credentials: 'omit' });
    if (response.ok) {
      pause = 30_000;
      return response.json();
    }
    const error = (await response.json().catch(() => null))?.detail?.error;
    const answer = `HTTP ${response.status}${error ? ` ${error}` : ''}`;
    if (response.status === 401 || response.status === 403) {
      refused = new Error(`Constellation refused the key (${answer}): put a live key with results:read in the page and reload it`);
      throw refused;
    }
    if (response.status === 429 || response.status === 402) {
      pause = Math.min(pause * 2, 900_000);
      pausedUntil = now() + pause;
      limited = new Error(`Constellation is not answering this key for now (${answer}); asking again in ${pause / 60_000} min`);
      throw limited;
    }
    throw new Error(`Constellation answered ${response.status}`);
  }

  function resourceOf(logs) {
    return Object.fromEntries((logs.resource?.attributes ?? []).map((kv) => [kv.key, plain(kv.value)]));
  }

  // One record of each entry finds each workflow's newest run (from=latest finds only the newest
  // run of all), and only those runs are read whole, so a tab never reads days of runs.
  async function read() {
    const newest = new Map();
    let before = cursor;
    for (;;) {
      // The API's most entries a page (its contract's maximum); more is refused with a 422.
      const page = await get({ after: before, limit: 20, consumer, page_records: 1 });
      if (!page.entries.length) break;
      for (const entry of page.entries) {
        for (const logs of entry.otlp.resourceLogs) {
          const resource = resourceOf(logs);
          if (resource['constellation.connection'] === consumer) newest.set(resource['constellation.workflow'] || 'workflow', before);
        }
        before = entry.cursor;
      }
    }
    for (const after of new Set(newest.values())) await readRun(after);
    cursor = before;
  }

  // The run just after `after`, every page of it joined: each page's records added to their own
  // resource. A run of more than MAX_PAGES pages is shown as an error instead of in part.
  async function readRun(after) {
    const [entry] = (await get({ after, limit: 1, consumer, page_records: PAGE_RECORDS })).entries;
    if (!entry) return;
    const resources = new Map(entry.otlp.resourceLogs.map((logs) => [JSON.stringify(logs.resource), logs]));
    let next = entry.next_page;
    for (let pages = 1; next && pages < MAX_PAGES; pages++) {
      const more = await get({ cursor: entry.cursor, page: next, consumer, page_records: PAGE_RECORDS });
      for (const logs of more.otlp.resourceLogs) {
        const key = JSON.stringify(logs.resource);
        if (!resources.has(key)) resources.set(key, logs);
        else resources.get(key).scopeLogs[0].logRecords.push(...logs.scopeLogs[0].logRecords);
      }
      next = more.next_page;
    }
    for (const logs of resources.values()) {
      const resource = resourceOf(logs);
      if (resource['constellation.connection'] !== consumer) continue;
      const output = JSON.parse(resource['constellation.output'] || '{}');
      const items = (logs.scopeLogs ?? []).flatMap((s) => s.logRecords ?? []).map((r) => plain(r.body));
      const workflow = resource['constellation.workflow'] || 'workflow';
      runs.set(workflow, {
        workflow,
        workflow_version: resource['constellation.workflow_version'],
        kind: resource['constellation.template'] || 'pass-yield',
        run_id: resource['constellation.run_id'],
        delivered_at: new Date().toISOString(),
        blocks: output.widget?.blocks,
        count: items.length,
        items: next ? [] : items,
        ...(next && { error: `run ${resource['constellation.run_id']} has more than ${MAX_PAGES * PAGE_RECORDS} results, more than this view reads` }),
      });
    }
  }

  return async function load(name) {
    await (reading ||= read().finally(() => (reading = undefined)));
    if (name === 'index.json') {
      const workflows = [...runs.values()].map(({ items, ...entry }) => entry);
      return { workflows: workflows.sort((a, b) => a.workflow.localeCompare(b.workflow)) };
    }
    const run = runs.get(name.replace(/^workflow-(.*)\.json$/, '$1'));
    if (!run) throw new Error('none yet');
    if (run.error) throw new Error(run.error);
    return run;
  };
}

export default function ConstellationForecast({
  source = 'yamcs',
  yamcsUrl,
  bucket = 'constellation',
  apiUrl,
  token,
  consumer,
  refreshMs = 15000,
} = {}) {
  if (source !== 'api' && !yamcsUrl) throw new Error('ConstellationForecast: the Yamcs source needs yamcsUrl, where this page reaches Yamcs');
  const base = yamcsUrl?.replace(/\/?$/, '/');

  const load =
    source === 'api'
      ? feedLoader({ apiUrl, token, consumer })
      : async function load(name) {
          const response = await fetch(`${base}api/storage/buckets/${bucket}/objects/${encodeURIComponent(name)}`);
          if (!response.ok) throw new Error(response.status === 404 ? 'none yet' : `Yamcs answered ${response.status}`);
          return response.json();
        };

  async function workflows() {
    try {
      return (await load('index.json')).workflows || [];
    } catch {
      return [];
    }
  }

  return function install(openmct) {
    for (const [type, plugin] of [
      ['plan', 'Plan'],
      ['time-strip', 'Timeline'],
    ]) {
      if (!openmct.types.listKeys().includes(type) && openmct.plugins[plugin]) openmct.install(openmct.plugins[plugin]());
    }

    openmct.types.addType('constellation.forecast', {
      name: 'Constellation Forecast',
      description: 'Forecasts from Constellation workflows: predicted passes and recommendations, not scheduled contacts.',
      cssClass: 'icon-telemetry',
      creatable: false,
    });
    openmct.types.addType('constellation.workflow', {
      name: 'Constellation workflow',
      description: 'One Constellation workflow’s latest forecast.',
      cssClass: 'icon-telemetry',
      creatable: false,
    });

    openmct.objects.addRoot(ROOT, openmct.priority.HIGH);
    openmct.objects.addProvider(NS, {
      async get(identifier) {
        const key = identifier.key;
        if (key === 'root') {
          const all = await workflows();
          return {
            identifier,
            name: 'Constellation Forecast',
            type: 'folder',
            location: 'ROOT',
            composition: [
              id('overview'),
              id('strip'),
              ...all.flatMap((w) => [id(`workflow:${w.workflow}`), ...(PLANNED.has(w.kind) ? [id(`plan:${w.workflow}`)] : [])]),
            ],
          };
        }
        if (key === 'overview') return { identifier, name: 'Forecast overview', type: 'constellation.forecast', location: 'constellation:root' };
        if (key === 'strip') {
          const all = (await workflows()).filter((w) => PLANNED.has(w.kind));
          return {
            identifier,
            name: 'Forecast time strip',
            type: 'time-strip',
            location: 'constellation:root',
            composition: all.map((w) => id(`plan:${w.workflow}`)),
            configuration: { useIndependentTime: false, containers: [], swimlaneVisibility: {} },
          };
        }
        const [kind, slug] = key.split(':');
        const entry = (await workflows()).find((w) => w.workflow === slug);
        const title = `${TITLES[entry?.kind] ?? slug}${entry ? ` · ${slug}` : ''}`;
        if (kind === 'workflow') return { identifier, name: title, type: 'constellation.workflow', workflow: slug, location: 'constellation:root' };
        if (kind === 'plan') {
          let body = {};
          try {
            body = planBody(await load(`workflow-${slug}.json`));
          } catch {
            /* no run yet: an empty plan */
          }
          return {
            identifier,
            name: `${title} · passes`,
            type: 'plan',
            location: 'constellation:root',
            selectFile: { name: `${slug}-forecast.json`, body: JSON.stringify(body) },
            // Every lane shown up front: Open MCT's Plan view otherwise writes this into the
            // object, which a read-only provider refuses, and draws nothing.
            configuration: { clipActivityNames: false, swimlaneVisibility: Object.fromEntries(Object.keys(body).map((lane) => [lane, true])) },
          };
        }
        return undefined;
      },
    });

    openmct.objectViews.addProvider({
      key: 'constellation.forecast-view',
      name: 'Forecast',
      cssClass: 'icon-telemetry',
      canView: (object) => object.type === 'constellation.forecast' || object.type === 'constellation.workflow',
      view(object) {
        let mounted;
        // A workflow's own object opens on that workflow; the overview shows them all.
        const scoped = object.workflow
          ? async (name) => {
              const json = await load(name);
              if (name === 'index.json') json.workflows = (json.workflows || []).filter((w) => w.workflow === object.workflow);
              return json;
            }
          : load;
        return {
          show(element) {
            element.style.overflow = 'auto';
            // Open MCT's themes (Espresso, Snow) show which by their text: light text, dark theme.
            const dark = !isDark(getComputedStyle(element).color);
            mounted = mountForecast(element, { load: scoped, refreshMs, theme: dark ? 'dark' : 'light' });
          },
          destroy() {
            mounted?.destroy();
          },
        };
      },
      priority: () => 1,
    });
  };
}
