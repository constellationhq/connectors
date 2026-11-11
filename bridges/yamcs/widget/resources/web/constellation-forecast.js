// The Constellation Forecast page in Yamcs. Yamcs mounts <constellation-yamcs> once (this plugin's
// initializer), which adds the nav item, and <constellation-forecast> at /ext/constellation-forecast.
// The page itself is the shared Constellation Forecast view (bridges/shared/forecast-view.js),
// reading what the Constellation bridge writes to the "constellation" bucket through the operator's
// own Yamcs session.
import { mountForecast } from './forecast-view.js';

const BUCKET = 'constellation';

customElements.define(
  'constellation-yamcs',
  class extends HTMLElement {
    set extensionService(service) {
      service.addNavItem('archive', { path: 'ext/constellation-forecast', label: 'Constellation Forecast', icon: 'insights' });
    }
  },
);

customElements.define(
  'constellation-forecast',
  class extends HTMLElement {
    connectedCallback() {
      this.view = mountForecast(this, { load: (name) => this.object(name) });
    }

    disconnectedCallback() {
      this.view?.destroy();
    }

    async object(name) {
      const client = this.extensionService?.yamcs?.yamcsClient;
      const path = `/storage/buckets/${BUCKET}/objects/${name}`;
      let response;
      try {
        response = client ? await client.doFetch(client.apiUrl + path) : await fetch('api' + path);
      } catch (err) {
        if (err?.statusCode === 404 || /404|not found/i.test(err?.message ?? '')) throw new Error('none yet');
        throw err;
      }
      if (!response.ok) throw new Error(response.status === 404 ? 'none yet' : `Yamcs answered ${response.status}`);
      return response.json();
    }
  },
);
