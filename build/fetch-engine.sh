#!/usr/bin/env bash
# Fetches Mozilla's translation engine (Bergamot, on its Marian fork) at a
# pinned commit, with only the submodules a CPU inference build needs, and
# applies this repo's patches.
#
# Usage: build/fetch-engine.sh [dest]   (default: ./engine)
set -euo pipefail

# mozilla/translations, inference/ subtree. Bump deliberately: patches in
# patches/ are written against this commit.
ENGINE_REPO=https://github.com/mozilla/translations.git
ENGINE_COMMIT=69455acaecbe8650cdba988dbcf7c10ca20e7c48

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:-$ROOT/engine}"

if [[ ! -d "$DEST/.git" ]]; then
  git clone --quiet --filter=blob:none --no-checkout "$ENGINE_REPO" "$DEST"
fi
git -C "$DEST" sparse-checkout set inference
git -C "$DEST" checkout --quiet "$ENGINE_COMMIT"

# Only what a CPU build uses. Not fbgemm/nccl/onnxjs (GPU, server, wasm),
# not emsdk, and nothing from the training pipeline.
git -C "$DEST" submodule update --init --depth 1 \
  inference/3rd_party/ssplit-cpp \
  inference/marian-fork/src/3rd_party/sentencepiece \
  inference/marian-fork/src/3rd_party/intgemm \
  inference/marian-fork/src/3rd_party/ruy \
  inference/marian-fork/src/3rd_party/simd_utils
git -C "$DEST/inference/marian-fork/src/3rd_party/ruy" \
  submodule update --init --depth 1 third_party/cpuinfo

# patches/<where>-<what>.patch, applied inside that repo: `marian-*` to
# mozilla/translations itself (marian-fork/ isn't a submodule), others to
# the submodule they name. Idempotent: an applied patch is skipped.
apply() {
  local dir="$1" patch="$2"
  if git -C "$dir" apply --reverse --check "$patch" 2>/dev/null; then
    echo "already applied: $(basename "$patch")"
  else
    git -C "$dir" apply "$patch"
    echo "applied: $(basename "$patch")"
  fi
}
apply "$DEST" "$ROOT/patches/marian-TargetArch-arm64.patch"
apply "$DEST" "$ROOT/patches/marian-ExceptionWithCallStack-vla.patch"
apply "$DEST/inference/marian-fork/src/3rd_party/sentencepiece" \
  "$ROOT/patches/sentencepiece-clang20-kAnyType.patch"

echo "engine ready: $DEST/inference ($(git -C "$DEST" rev-parse --short HEAD))"
