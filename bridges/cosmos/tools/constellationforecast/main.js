// The Constellation Forecast tool in COSMOS. COSMOS loads a tool's INLINE_URL with SystemJS and
// runs it as a single-spa app, so this file is System.register by hand: no framework, no build.
// The view is the shared Constellation Forecast view (bridges/shared/forecast-view.js, copied
// in beside this file when the gem is built), the same one Yamcs and Open MCT draw.
//
//   /tools/constellationforecast          every workflow, by category
//   /tools/constellationforecast/<slug>   that workflow alone
//
// It reads the documents the plugin's bridge writes to COSMOS's config bucket
// (<scope>/constellation/index.json and workflow-<slug>.json) through COSMOS's storage API, with
// the operator's own COSMOS token: the browser never sees a Constellation key.
System.register([], function (_export) {
  'use strict';
  return {
    execute: function () {
      const BASE = '/tools/constellationforecast';
      let view = null;
      let mountForecast = null;
      let nav = null;
      let body = null;

      async function load(name) {
        const scope = window.openc3Scope || 'DEFAULT';
        // COSMOS Core's auth.js: refreshes (or sends to login) and leaves the token here.
        if (window.OpenC3Auth) await window.OpenC3Auth.updateToken(window.OpenC3Auth.defaultMinValidity);
        const key = encodeURIComponent(`${scope}/constellation/${name}`);
        const response = await fetch(`/openc3-api/storage/download_file/${key}?bucket=OPENC3_CONFIG_BUCKET&scope=${scope}`, {
          headers: { Authorization: localStorage.openc3Token || '' },
        });
        if (response.status === 401) {
          // A token COSMOS no longer knows (its sessions were reset) still looks valid here: drop
          // it and sign in again, coming back to this page.
          delete localStorage.openc3Token;
          location.assign(`/login?redirect=${encodeURIComponent(location.pathname)}`);
          throw new Error('Your COSMOS sign-in expired: signing in again');
        }
        if (!response.ok) throw new Error(response.status === 404 ? 'none yet' : `COSMOS answered ${response.status}`);
        const bytes = Uint8Array.from(atob((await response.json()).contents), (c) => c.charCodeAt(0));
        return JSON.parse(new TextDecoder().decode(bytes));
      }

      const slugOf = () => decodeURIComponent(location.pathname.slice(BASE.length).split('/')[1] || '');
      const link = (href, label, current) =>
        `<a href="${href}" data-nav${current ? ' aria-current="page"' : ''}>${label.replace(/[&<>"']/g, '')}</a>`;

      function drawNav(workflows, slug) {
        nav.innerHTML = [link(BASE, 'Overview', !slug), ...workflows.map((w) => link(`${BASE}/${encodeURIComponent(w.workflow)}`, w.workflow, w.workflow === slug))].join('');
      }

      function show() {
        const slug = slugOf();
        view?.destroy();
        drawNav([], slug);
        view = mountForecast(body, {
          theme: 'dark',
          // A workflow's page shows that workflow alone; the nav always lists them all.
          load: async (name) => {
            const doc = await load(name);
            if (name === 'index.json') {
              drawNav(doc.workflows || [], slug);
              if (slug) doc.workflows = (doc.workflows || []).filter((w) => w.workflow === slug);
            }
            return doc;
          },
        });
      }

      function follow(e) {
        const a = e.target.closest('a[data-nav]');
        if (!a) return;
        e.preventDefault();
        history.pushState(null, '', a.getAttribute('href'));
        show();
      }

      _export({
        load,
        bootstrap: async () => {},
        mount: async () => {
          ({ mountForecast } = await import(`${BASE}/forecast-view.js`));
          const root = document.getElementById('openc3-tool');
          root.innerHTML = `<style>
.cfc { min-height:100%; background:#1a1a19; }
.cfc-nav { display:flex; gap:4px; flex-wrap:wrap; padding:12px 24px 0; font:13px Roboto, system-ui, sans-serif; }
.cfc-nav a { color:#a3a29c; text-decoration:none; padding:4px 10px; border:1px solid rgba(255,255,255,.12); border-radius:6px; }
.cfc-nav a[aria-current] { color:#e8e8e6; border-color:#e8e8e6; }
</style><div class="cfc"><nav class="cfc-nav" aria-label="Constellation Forecast"></nav><div></div></div>`;
          [nav, body] = [root.querySelector('nav'), root.querySelector('nav + div')];
          nav.addEventListener('click', follow);
          window.addEventListener('popstate', show);
          show();
        },
        unmount: async () => {
          window.removeEventListener('popstate', show);
          view?.destroy();
          view = null;
          document.getElementById('openc3-tool').innerHTML = '';
        },
      });
    },
  };
});
