#!/bin/bash

#
# Copyright (C) 2023 Nethesis S.r.l.
# SPDX-License-Identifier: GPL-3.0-or-later
#

set -Eeuo pipefail

images=()
repobase="${REPOBASE:-ghcr.io/platypuschan}"
reponame="gitea"

# Keep runtime images pinned: upgrades are reviewed and tested through Renovate PRs.
postgres_image="docker.io/library/postgres:15.19-alpine3.23"
gitea_image="docker.gitea.com/gitea:1.27.3"
node_image="docker.io/library/node:24.20.0-slim"

container="$(buildah from scratch)"
nodebuilder=""

cleanup() {
    buildah rm -f "${container}" >/dev/null 2>&1 || true
    if [[ -n "${nodebuilder}" ]]; then
        buildah rm -f "${nodebuilder}" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

echo "Pulling the pinned Node.js build image..."
nodebuilder="$(buildah from -v "${PWD}:/usr/src:Z" "${node_image}")"

echo "Building static UI files..."
buildah run \
    --workingdir=/usr/src/ui \
    --env="NODE_OPTIONS=--openssl-legacy-provider" \
    "${nodebuilder}" \
    sh -c "corepack enable && yarn install --immutable && yarn build"

buildah add "${container}" imageroot /imageroot
buildah add "${container}" ui/dist /ui

buildah config --entrypoint=/ \
    --label="org.nethserver.authorizations=traefik@node:routeadm node:fwadm,portsadm" \
    --label="org.nethserver.tcp-ports-demand=2" \
    --label="org.nethserver.rootfull=0" \
    --label="org.nethserver.min-core=3.2.2" \
    --label="org.nethserver.images=${postgres_image} ${gitea_image}" \
    "${container}"

buildah commit "${container}" "${repobase}/${reponame}"
images+=("${repobase}/${reponame}")

if [[ -n "${CI:-}" ]]; then
    printf "images=%s\n" "${images[*],,}" >>"${GITHUB_OUTPUT}"
else
    printf "Publish the images with:\n\n"
    for image in "${images[@],,}"; do
        printf "  buildah push %s docker://%s:%s\n" "${image}" "${image}" "${IMAGETAG:-latest}"
    done
    printf "\n"
fi
