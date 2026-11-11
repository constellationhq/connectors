# constellation-space

Constellation for Python: install `constellation-space` (`pip install constellation-space`) and
import `constellation_space`:

- `constellation_space.client.Client`: the API with `x-api-key` auth, retries on 429 and 5xx, and
  workflow results read whole and in order.
- `constellation_space.results`: the workflow results protocol every bridge shares, including `action_state(item)`.
- `constellation_space.models`: typed shapes of results, actions and runs.
- `constellation_space.bridges.template.Bridge`: subclass it, write `apply(items, settings, run)`, and
  `act(item, settings, run)` for approved actions (each carried out once, across restarts).
- `constellation_space.bridges.yamcs` and `constellation_space.bridges.lattice`: the Yamcs and Lattice bridges,
  run with `python -m constellation_space.bridges.yamcs` or
  `python -m constellation_space.bridges.lattice` (extra `[lattice]`).

The package needs Python 3.11 or newer.

Settings come from the environment: `CONSTELLATION_API_URL` (default
`https://api.constellation.space`) and `CONSTELLATION_API_KEY`.
