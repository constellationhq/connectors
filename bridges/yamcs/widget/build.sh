#!/usr/bin/env bash
# Builds constellation-yamcs.jar: the Forecast page plugin, and the mdb loader that carries
# Constellation's parameters (mdb/constellation.xml, packed in here). Needs a JDK (21+) and
# Yamcs's jars to compile against, fetched from Maven Central for the Yamcs version given.
# usage: bridges/yamcs/widget/build.sh [out-dir] [yamcs-version]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
out="${1:-$here/build}"
version="${2:-5.14.0}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
for artifact in yamcs-core yamcs-xtce yamcs-web yamcs-api; do
  url="https://repo1.maven.org/maven2/org/yamcs/$artifact/$version/$artifact-$version.jar"
  if command -v curl >/dev/null; then curl -fsSL -o "$work/$artifact.jar" "$url"; else wget -qO "$work/$artifact.jar" "$url"; fi
done
mkdir -p "$work/classes" "$out"
javac --release 21 -cp "$work/yamcs-core.jar:$work/yamcs-xtce.jar:$work/yamcs-web.jar:$work/yamcs-api.jar" \
  -d "$work/classes" "$here"/src/space/constellation/yamcs/*.java
cp -R "$here/resources/." "$work/classes/"
cp "$here/mdb/constellation.xml" "$work/classes/space/constellation/yamcs/"
# The shared Constellation Forecast view, unless the caller already placed it.
[ -f "$work/classes/web/forecast-view.js" ] || cp "$here/../../shared/forecast-view.js" "$work/classes/web/"
jar --create --file "$out/constellation-yamcs.jar" -C "$work/classes" .
echo "built $out/constellation-yamcs.jar"
