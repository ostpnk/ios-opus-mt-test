#!/usr/bin/env python3
"""Merges the Mozilla and OPUS-MT releases' catalog.json into the one
catalog core_translate_catalog bundles, ordered by compare.py's scores.

Order matters: where two models serve one direction, an app routes through
the earlier one unless its user chose the other. For each direction both
cover, OPUS-MT goes first only if its chrF, averaged over FLORES and Tatoeba,
beats Mozilla's by at least --margin; otherwise Mozilla's stays first — it's
a third of the download, half the memory and several times faster, so a tie
goes to it. Directions only one family covers keep their release order,
Mozilla's first, then OPUS-MT's — except that an OPUS-MT direct model
(fr-it) is left out when Mozilla's route through English (fr-en, en-it)
beats it by --margin: an installed direct model always wins the route, so
it would only make that direction worse.

Usage:
  python3 -I catalog/merge_catalogs.py --mozilla URL_OR_FILE --opus URL_OR_FILE \\
      --compare compare.json --out catalog.json [--margin 0.01]
"""

import argparse
import json
import urllib.request
from pathlib import Path


def load(source):
    if source.startswith("https://"):
        with urllib.request.urlopen(source, timeout=120) as response:
            return json.load(response)
    return json.loads(Path(source).read_text())


def mean_chrf(result):
    return (result["flores"]["chrF"] + result["tatoeba"]["chrF"]) / 2


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mozilla", required=True)
    parser.add_argument("--opus", required=True)
    parser.add_argument("--compare", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--margin", type=float, default=0.01)
    args = parser.parse_args()

    mozilla, opus = load(args.mozilla), load(args.opus)
    compared = {c["direction"]: c for c in json.loads(args.compare.read_text())}

    def direction(entry):
        return f"{entry['sourceLanguage']}-{entry['targetLanguage']}"

    opus_by_direction = {direction(e): e for e in opus}
    merged, used, report = [], set(), []
    for entry in mozilla:
        rival = opus_by_direction.get(direction(entry))
        if rival is None:
            merged.append(entry)
            continue
        result = compared.get(direction(entry))
        if result is None:
            raise SystemExit(f"{direction(entry)}: both families cover it, but "
                             "compare.json has no result — rerun compare.py")
        lead = mean_chrf(result["opus-mt"]) - mean_chrf(result["mozilla"])
        opus_first = lead >= args.margin
        merged += [rival, entry] if opus_first else [entry, rival]
        used.add(rival["id"])
        report.append(f"{direction(entry)}: {'opus-mt' if opus_first else 'mozilla'} "
                      f"first (OPUS-MT {lead:+.3f})")
    for entry in opus:
        if entry["id"] in used:
            continue
        result = compared.get(direction(entry))
        if result is not None and "mozilla-pivot" in result:
            lead = mean_chrf(result["opus-mt"]) - mean_chrf(result["mozilla-pivot"])
            if lead <= -args.margin:
                report.append(f"{direction(entry)}: opus-mt left out — the route "
                              f"through English is better (OPUS-MT {lead:+.3f})")
                continue
            report.append(f"{direction(entry)}: opus-mt direct kept "
                          f"(OPUS-MT {lead:+.3f} over the route through English)")
        merged.append(entry)

    args.out.write_text(json.dumps(merged, indent=1, ensure_ascii=False) + "\n")
    print("\n".join(report))
    opus_ids = {e["id"] for e in opus}
    kept = sum(1 for e in merged if e["id"] in opus_ids)
    print(f"{len(merged)} models ({len(mozilla)} Mozilla, {kept} of {len(opus)} OPUS-MT)")


if __name__ == "__main__":
    main()
