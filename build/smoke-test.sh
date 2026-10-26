#!/usr/bin/env bash
# Translates test/sentences-it.txt with a built translator-cli and Mozilla's
# Italian->English model, downloaded from Mozilla's registry and checked
# against the SHA-256 the registry publishes for it.
#
# Usage: build/smoke-test.sh <translator-cli> [runner-prefix...]
#   runner-prefix: how to execute the binary, e.g. for the iOS simulator:
#   build/smoke-test.sh out/ios-sim/bin/translator-cli xcrun simctl spawn booted
#
# Fails if the binary exits non-zero or returns an empty translation.
# Prints the time per sentence (each call loads the model) and for all
# sentences in one call.
set -euo pipefail

CLI="${1:?usage: smoke-test.sh <translator-cli> [runner-prefix...]}"; shift
CLI="$(cd "$(dirname "$CLI")" && pwd)/$(basename "$CLI")"
RUN=("$@")
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODELS="$ROOT/models/it-en"

# Mozilla's it->en base-memory model, as listed in its registry
# (storage.googleapis.com/.../db/models.json). Only the model file carries
# a hash there; vocab and shortlist don't.
BASE=https://storage.googleapis.com/moz-fx-translations-data--303e-prod-translations-data/models/it-en/retrain_hr_d1nPDwVeSD6t-HdtWA4xNg/exported
MODEL_SHA256=21b70978ce2f3b4da7a06b5de86a09abe3acd30b9eee1b2ebb3582b9bad790bf

sha256() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1; }
now_ms() { perl -MTime::HiRes=time -e 'printf "%d", time * 1000'; }

mkdir -p "$MODELS"
for f in model.iten.intgemm.alphas.bin vocab.iten.spm lex.50.50.iten.s2t.bin; do
  if [[ ! -f "$MODELS/$f" ]]; then
    curl -sSfL "$BASE/$f.gz" | gunzip > "$MODELS/$f.part"
    mv "$MODELS/$f.part" "$MODELS/$f"
  fi
done
actual=$(sha256 "$MODELS/model.iten.intgemm.alphas.bin")
if [[ "$actual" != "$MODEL_SHA256" ]]; then
  echo "model checksum mismatch: $actual" >&2; exit 1
fi

cat > "$MODELS/config.yml" <<EOF
models:
  - $MODELS/model.iten.intgemm.alphas.bin
vocabs:
  - $MODELS/vocab.iten.spm
  - $MODELS/vocab.iten.spm
shortlist:
  - $MODELS/lex.50.50.iten.s2t.bin
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

translate() {
  # ${RUN[@]+...}: bash 3.2 treats an empty array as unset under `set -u`.
  ${RUN[@]+"${RUN[@]}"} "$CLI" --model-config-paths "$MODELS/config.yml" --cpu-threads 1
}

failures=0
while IFS= read -r line; do
  [[ -z "$line" ]] && continue
  s=$(now_ms)
  if out=$(printf '%s' "$line" | translate 2>&1); then rc=0; else rc=$?; fi
  e=$(now_ms)
  printf '%5d ms  %s\n          -> %s\n' "$((e - s))" "$line" "$out"
  if [[ $rc -ne 0 || -z "${out//[[:space:]]/}" ]]; then
    echo "          FAILED (rc=$rc)"; failures=$((failures + 1))
  fi
done < "$ROOT/test/sentences-it.txt"

s=$(now_ms)
translate < "$ROOT/test/sentences-it.txt" > /dev/null
e=$(now_ms)
echo "all sentences in one call: $((e - s)) ms"

if [[ $failures -gt 0 ]]; then echo "$failures sentence(s) failed" >&2; exit 1; fi
