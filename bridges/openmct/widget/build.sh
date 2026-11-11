#!/usr/bin/env bash
# Builds constellation-openmct.js: the plugin and the shared view in one file an Open MCT page
# loads with a <script> tag, without a rebuild; it sets window.ConstellationForecast. Needs
# esbuild, which npm ci in clients/typescript installs.
# usage: bridges/openmct/widget/build.sh [out-dir]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
out="$(mkdir -p "${1:-$here/build}" && cd "${1:-$here/build}" && pwd)"
esbuild="$here/../../../clients/typescript/node_modules/.bin/esbuild"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
# Side by side, as the plugin imports the view (and as the ES module zip ships them).
cp "$here/constellation-forecast.js" "$here/../../shared/forecast-view.js" "$work/"
echo "import ConstellationForecast from './constellation-forecast.js'; window.ConstellationForecast = ConstellationForecast;" > "$work/entry.js"
(cd "$work" && "$esbuild" entry.js --bundle --format=iife --target=es2020 --log-level=warning --outfile="$out/constellation-openmct.js")
echo "built $out/constellation-openmct.js"
