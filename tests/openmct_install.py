"""Stock Open MCT with constellation-openmct.js added by a <script> tag, in Chromium, reading the
stub feed (stub_api.py) directly: the Constellation Forecast folder is in the tree, the overview
draws the workflow, and its plan holds the forecast pass. Run by install.yml.

    python tests/openmct_install.py http://127.0.0.1:8081/index.html
"""

import sys
import time

from playwright.sync_api import expect, sync_playwright

url = sys.argv[1]
with sync_playwright() as p:
    page = p.chromium.launch().new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url)
    expect(page.get_by_text("Constellation Forecast").first).to_be_visible(timeout=60_000)
    page.goto(f"{url}#/browse/constellation:root/constellation:overview")
    expect(page.get_by_text("Pass yield").first).to_be_visible(timeout=30_000)
    # The pass is an hour ahead: a time window over the next three hours shows it.
    now = int(time.time() * 1000)
    window = f"tc.mode=fixed&tc.timeSystem=utc&tc.startBound={now}&tc.endBound={now + 3 * 3600_000}"
    page.goto(f"{url}#/browse/constellation:root/constellation:plan:pass-yield?{window}")
    expect(page.get_by_text("Forecast · sat-1 · 10.0 GB").first).to_be_attached(timeout=30_000)
    assert not errors, errors
print("Open MCT shows the Constellation forecast")
