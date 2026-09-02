#!/usr/bin/env bash
# Single source of truth for the pinned Surfer viewer-asset versions.
#
# WHY THIS FILE EXISTS
# The surver binary (server) and the Surfer WASM bundle (client) both embed a
# wellen version, and Surfer refuses to connect when the two differ: the user
# just sees a waveform that never loads. So the two artifacts must come from
# ONE upstream commit, and every script in the build chain must agree on which
# commit that is.
#
# Before this file, the commit was written down in four places
# (build_surver_static.sh, build_viewer_assets.sh, docker_build_all.sh and the
# crate-license report). On 2026-09-02 they drifted: docker_build_all.sh passed
# the release tag v0.7.0, which overrode the pinned commit and produced a
# wellen 0.20.5 surver against a 0.25.6 wasm. The version gate correctly
# rejected the pair, so nothing broken shipped, but the 2.17 bundle could not
# be built until the refs were realigned.
#
# RULE: pin the version HERE and nowhere else. Scripts source this file.
#
# HOW TO BUMP (all steps are mandatory)
#   1. Edit SURFER_REF / VIEWER_ASSETS_VERSION below.
#   2. Rebuild the surver side:  deploy/build_surver_static.sh
#   3. Refresh the wasm side from the SAME commit's CI pages_build artifact.
#   4. Repack:                   deploy/build_viewer_assets.sh <asset_dir>
#      The wellen gate in that script verifies the pair; it fails loudly if the
#      two sides disagree, which is exactly the mistake this file prevents.
#   5. Re-run the viewer demos/regression, then update
#      deploy/viewer-assets/PROVENANCE.md.
#
# Sourcing contract: this file only assigns variables, so it is safe to source
# from any script. Callers may pre-set either variable to override it.

# Upstream Surfer commit both artifacts are built from.
# Pin to a full commit SHA, never a branch or a release tag: tags move relative
# to the wasm snapshots published by CI, which is how the 2026-09-02 drift
# happened.
SURFER_REF="${SURFER_REF:-86eedfd0cda70fc0a61ab200ebf37aabf97c5cde}"

# Version of the wave-mcp-viewer-assets package. Tracks the wellen version of
# the pinned pair, which is what actually has to match at connect time.
VIEWER_ASSETS_VERSION="${VIEWER_ASSETS_VERSION:-0.25.6}"

# Expected wellen version of both artifacts. build_viewer_assets.sh compares
# the two binaries against each other and against this value, so a stale pin is
# caught even when surver and wasm happen to agree with each other.
VIEWER_WELLEN_VERSION="${VIEWER_WELLEN_VERSION:-0.25.6}"
