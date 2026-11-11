"""A Constellation connector for your own system: fill in connector.json, apply() and act().

    pip install constellation-space
    CONSTELLATION_API_KEY=<a key with predictions:read> python3 bridge.py

What the base class does for you, and the rules apply() follows, are in
constellation_space.bridges.template.
"""

from pathlib import Path

from constellation_space.bridges.template import Bridge


class MyBridge(Bridge):
    def apply(self, items, settings, run):
        """Write one run's items into your system; return what you wrote."""
        raise NotImplementedError("write the items into your system here")

    def act(self, item, settings, run):
        """Carry out one approved action in your system; return True once done."""
        return False


if __name__ == "__main__":
    MyBridge(Path(__file__).with_name("connector.json")).run_forever()
