#!/bin/bash

# SPDX-License-Identifier: GPL-3.0-or-later

# Update the last published release of this module to the image under test,
# the same path an NS8 Software Center update takes.

set -Eeuo pipefail

cd "$(dirname "$0")"
IMAGE_URL="${2:?missing module image URL}"
BASELINE_IMAGE="$(python3 .github/scripts/previous-release "${IMAGE_URL%:*}")"
echo "Update scenario: ${BASELINE_IMAGE} -> ${IMAGE_URL}"
export BASELINE_IMAGE

exec bash ./test-module.sh \
    "${1:?missing leader node address}" \
    "${IMAGE_URL}" \
    update
