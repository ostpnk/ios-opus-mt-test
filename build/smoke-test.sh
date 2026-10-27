#!/usr/bin/env bash
# Tests a build (out/<platform>) with Mozilla's Italian<->English models,
# downloaded from Mozilla's registry and checked against the SHA-256 the
# registry publishes for each model file:
#   1. test_capi: libbergamot's C interface — batch translation, HTML,
#      pivoting it->en->it, and that bad input is an error, not a crash.
#   2. translator-cli: one sentence per call (each call loads the model),
#      then all in one call.
#
# Usage: build/smoke-test.sh <out-dir> [runner-prefix...]
#   runner-prefix: how to execute binaries, e.g. for the iOS simulator:
#   build/smoke-test.sh out/ios-sim xcrun simctl spawn booted
set -euo pipefail

OUTDIR="$(cd "${1:?usage: smoke-test.sh <out-dir> [runner-prefix...]}" && pwd)"; shift
RUN=("$@")
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SENTENCES="$ROOT/test/sentences-it.txt"

# Mozilla's base-memory models, as listed in its registry
# (storage.googleapis.com/.../db/models.json). Only the model file carries a
# hash there; vocab and shortlist don't.
BUCKET=https://storage.googleapis.com/moz-fx-translations-data--303e-prod-translations-data/models

sha256() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1; }
now_ms() { perl -MTime::HiRes=time -e 'printf "%d", time * 1000'; }

# fetch <pair> <registry dir> <model sha256>: models/<pair>/ with a config.yml
# whose paths are relative to it.
fetch() {
  local pair="$1" dir="$2" sha="$3" st="${1/-/}"
  local dest="$ROOT/models/$pair"
  mkdir -p "$dest"
  for f in "model.$st.intgemm.alphas.bin" "vocab.$st.spm" "lex.50.50.$st.s2t.bin"; do
    if [[ ! -f "$dest/$f" ]]; then
      curl -sSfL "$BUCKET/$pair/$dir/exported/$f.gz" | gunzip > "$dest/$f.part"
      mv "$dest/$f.part" "$dest/$f"
    fi
  done
  local actual; actual=$(sha256 "$dest/model.$st.intgemm.alphas.bin")
  if [[ "$actual" != "$sha" ]]; then echo "$pair: model checksum mismatch: $actual" >&2; exit 1; fi
  cat > "$dest/config.yml" <<EOF
models:
  - model.$st.intgemm.alphas.bin
vocabs:
  - vocab.$st.spm
  - vocab.$st.spm
shortlist:
  - lex.50.50.$st.s2t.bin
  - false
beam-size: 1
normalize: 1.0
word-penalty: 0
max-length-break: 128
mini-batch-words: 1024
workspace: 128
max-length-factor: 2.0
skip-cost: true
quiet: true
quiet-translation: true
gemm-precision: int8shiftAlphaAll
alignment: soft
EOF
}
fetch it-en retrain_hr_d1nPDwVeSD6t-HdtWA4xNg 21b70978ce2f3b4da7a06b5de86a09abe3acd30b9eee1b2ebb3582b9bad790bf
fetch en-it retrain_hr_fix_names_GPSr_u8PSmC9fOx1ZDCHoA 248f47568788ecc351da7e5e07064d4153b4f71e011364ae2c931ffeec4d1cc2

# ${RUN[@]+...}: bash 3.2 treats an empty array as unset under `set -u`.
run() { ${RUN[@]+"${RUN[@]}"} "$@"; }

echo "== test_capi"
run "$OUTDIR/bin/test_capi" "$ROOT/models/it-en" "$SENTENCES" "$ROOT/models/en-it"

echo "== translator-cli"
cli() { run "$OUTDIR/bin/translator-cli" --model-config-paths "$ROOT/models/it-en/config.yml" --cpu-threads 1; }
failures=0
while IFS= read -r line; do
  [[ -z "$line" ]] && continue
  s=$(now_ms)
  if out=$(printf '%s' "$line" | cli 2>&1); then rc=0; else rc=$?; fi
  e=$(now_ms)
  printf '%5d ms  %s\n          -> %s\n' "$((e - s))" "$line" "$out"
  if [[ $rc -ne 0 || -z "${out//[[:space:]]/}" ]]; then
    echo "          FAILED (rc=$rc)"; failures=$((failures + 1))
  fi
done < "$SENTENCES"
s=$(now_ms)
cli < "$SENTENCES" > /dev/null
e=$(now_ms)
echo "all sentences in one call: $((e - s)) ms"

if [[ $failures -gt 0 ]]; then echo "$failures sentence(s) failed" >&2; exit 1; fi
