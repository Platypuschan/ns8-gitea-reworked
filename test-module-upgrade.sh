#!/bin/bash

# SPDX-License-Identifier: GPL-3.0-or-later

# Update the newest released catalog version below CATALOG_VERSION to the
# image under test, the same path an NS8 Software Center update takes.

set -Eeuo pipefail

cd "$(dirname "$0")"
IMAGE_URL="${2:?missing module image URL}"
image="${IMAGE_URL%:*}"

previous="$(python3 - "${image}" <<'PY'
import importlib.machinery
import importlib.util
import json
import sys
import urllib.parse
import urllib.request

loader = importlib.machinery.SourceFileLoader(
    "check_catalog_version", ".github/scripts/check-catalog-version"
)
check = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(check)
image = sys.argv[1]
if not image.startswith("ghcr.io/"):
    sys.exit(f"Expected a ghcr.io image: {image}")
name = image.removeprefix("ghcr.io/")
query = urllib.parse.urlencode({"scope": f"repository:{name}:pull", "service": "ghcr.io"})
with urllib.request.urlopen(f"https://ghcr.io/token?{query}", timeout=30) as response:
    token = json.load(response)["token"]
request = urllib.request.Request(
    f"https://ghcr.io/v2/{name}/tags/list?n=1000",
    headers={"Authorization": f"Bearer {token}"},
)
with urllib.request.urlopen(request, timeout=30) as response:
    tags = json.load(response).get("tags") or []

with open("CATALOG_VERSION", encoding="utf-8") as stream:
    current = check.parse_version(stream.read())
released = []
for tag in tags:
    try:
        key = check.parse_version(tag)
    except check.VersionError:
        continue
    # Stable releases only: those are what the catalog offers by default.
    if key[3] == 1 and key < current:
        released.append((key, tag))
if not released:
    sys.exit("No released catalog version below CATALOG_VERSION")
print(max(released)[1])
PY
)"

echo "Upgrade scenario: ${image}:${previous} -> ${IMAGE_URL}"
BASELINE_IMAGE="${image}:${previous}" exec bash ./test-module.sh \
    "${1:?missing leader node address}" \
    "${IMAGE_URL}" \
    upgrade
