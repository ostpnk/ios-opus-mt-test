#!/usr/bin/env python3
"""Compares the published Mozilla and OPUS-MT models where both cover a
direction: the same sentences through each, via libbergamot (catalog/bt.py,
the interface apps use), scored with chrF against human references. Where
only OPUS-MT has a direct model (fr-it), compares it with Mozilla's route
through English (fr-en, then en-it) — what an app would use without it.

Two test sets, since neither alone says how a model does in an app:
  - FLORES-200 devtest (Meta, CC BY-SA 4.0): Wikipedia-style sentences,
    from neither project's training data;
  - Tatoeba: short everyday sentences — the release test file of the OPUS-MT
    model's own Tatoeba release (held out from its training).

Writes OUT/compare.json: per direction, both models' (or the direct model's
and the pivot route's) chrF on each set and their translation time. Nothing is published; core_translate_catalog orders
its entries from this.

Usage:
  python3 -I catalog/compare.py --out OUT --library out/linux/lib/libbergamot.so \\
      --mozilla-release URL --opus-release URL [--only it-en,en-it] [--cache DIR]
"""

import argparse
import json
import sys
import tarfile
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bt import Bergamot  # noqa: E402
from opus_bundles import LANGUAGES, TATOEBA, chrf, fetch, read_test  # noqa: E402

FLORES_URL = "https://dl.fbaipublicfiles.com/nllb/flores200_dataset.tar.gz"
FLORES = {
    "en": "eng_Latn", "it": "ita_Latn", "de": "deu_Latn", "fr": "fra_Latn",
    "es": "spa_Latn", "pt": "por_Latn", "nl": "nld_Latn", "pl": "pol_Latn",
    "ro": "ron_Latn", "sv": "swe_Latn", "da": "dan_Latn", "cs": "ces_Latn",
    "el": "ell_Grek", "hu": "hun_Latn", "fi": "fin_Latn",
}
BCP47_TO_TATOEBA = {bcp: iso3 for iso3, (bcp, _) in LANGUAGES.items()}


def config_yaml(engine_config):
    """The Bergamot config core_translate_bergamot generates from a catalog
    entry's engineConfig (BergamotModelConfig.toYaml)."""
    q = json.dumps
    vocabs = engine_config["vocabs"]
    lines = ["models:", f"  - {q(engine_config['model'])}",
             "vocabs:", f"  - {q(vocabs[0])}", f"  - {q(vocabs[-1])}"]
    if engine_config.get("shortlist"):
        lines += ["shortlist:", f"  - {q(engine_config['shortlist'])}", "  - false"]
    lines += [
        f"beam-size: {engine_config.get('beamSize', 1)}", "normalize: 1.0",
        "word-penalty: 0", "max-length-break: 128", "mini-batch-words: 1024",
        "workspace: 128", "max-length-factor: 2.0", "skip-cost: true",
        "quiet: true", "quiet-translation: true",
        f"gemm-precision: {q(engine_config.get('gemmPrecision', 'int8shiftAlphaAll'))}",
        "alignment: soft", "",
    ]
    return "\n".join(lines)


def load_catalog(release_url, cache, name):
    return json.loads(fetch(f"{release_url}/catalog.json", cache / name).read_text())


def unpack(entry, cache, into):
    archive = fetch(entry["downloadUrl"], cache / f"{entry['id']}.tar.gz")
    with tarfile.open(archive) as tar:
        tar.extractall(into, filter="data")
    return into / entry["modelRoot"]


def score(bt, entries, directories, sets):
    """Translates every set through [entries] in order — one model, or two
    for a route through English."""
    models = [bt.load_model(config_yaml(e["engineConfig"]), str(d))
              for e, d in zip(entries, directories)]
    try:
        result = {}
        for name, (sources, references) in sets.items():
            start = time.monotonic()
            hypotheses = bt.translate(models[0], sources,
                                      pivot=models[1] if len(models) > 1 else None)
            seconds = time.monotonic() - start
            result[name] = {"chrF": round(chrf(hypotheses, references), 4),
                            "seconds": round(seconds, 2),
                            "example": [sources[0], hypotheses[0], references[0]]}
        return result
    finally:
        for model in models:
            bt.free_model(model)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--library", required=True)
    parser.add_argument("--mozilla-release", required=True)
    parser.add_argument("--opus-release", required=True)
    parser.add_argument("--opus-list", type=Path,
                        default=Path(__file__).parent / "opus_directions.txt")
    parser.add_argument("--only")
    parser.add_argument("--cache", type=Path, default=Path("compare-cache"))
    parser.add_argument("--flores-sentences", type=int, default=500)
    parser.add_argument("--tatoeba-sentences", type=int, default=300)
    args = parser.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    args.out.mkdir(parents=True, exist_ok=True)

    mozilla = load_catalog(args.mozilla_release, args.cache, "mozilla-catalog.json")
    opus = load_catalog(args.opus_release, args.cache, "opus-catalog.json")
    releases = {}
    for line in args.opus_list.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            pair, release = line.split()
            releases[pair] = release

    by_pair = {(e["sourceLanguage"], e["targetLanguage"]): e for e in mozilla}
    # (direction, [mozilla entries: one, or two through English], opus entry)
    pairs = []
    for o in opus:
        src, trg = o["sourceLanguage"], o["targetLanguage"]
        if (src, trg) in by_pair:
            pairs.append((f"{src}-{trg}", [by_pair[(src, trg)]], o))
        elif (src, "en") in by_pair and ("en", trg) in by_pair:
            pairs.append((f"{src}-{trg}", [by_pair[(src, "en")], by_pair[("en", trg)]], o))
    if args.only:
        wanted = set(args.only.split(","))
        pairs = [p for p in pairs if p[0] in wanted]
    print(f"{len(pairs)} directions to compare", flush=True)

    flores_tar = fetch(FLORES_URL, args.cache / "flores200_dataset.tar.gz")
    flores = {}
    with tarfile.open(flores_tar) as tar:
        for member in tar.getmembers():
            path = Path(member.name)
            if path.parent.name == "devtest" and path.suffix == ".devtest":
                code = path.stem
                if code in FLORES.values():
                    flores[code] = tar.extractfile(member).read().decode().splitlines()

    results = []
    with Bergamot(args.library) as bt:
        for direction, moz, op in pairs:
            src, trg = op["sourceLanguage"], op["targetLanguage"]
            mozilla_name = "mozilla" if len(moz) == 1 else "mozilla-pivot"
            print(f"{direction}: {'+'.join(m['id'] for m in moz)} vs {op['id']}", flush=True)
            n = args.flores_sentences
            sets = {"flores": (flores[FLORES[src]][:n], flores[FLORES[trg]][:n])}
            pair3 = f"{BCP47_TO_TATOEBA[src]}-{BCP47_TO_TATOEBA[trg]}"
            test = fetch(f"{TATOEBA}/{pair3}/{releases[pair3]}.test.txt",
                         args.cache / f"{pair3}-{releases[pair3]}.test.txt")
            triples = read_test(test.read_text(encoding="utf-8"), args.tatoeba_sentences)
            sets["tatoeba"] = ([t[0] for t in triples], [t[1] for t in triples])

            entry = {"direction": direction, "sentences": {k: len(v[0]) for k, v in sets.items()}}
            for family, entries in [(mozilla_name, moz), ("opus-mt", [op])]:
                with tempfile.TemporaryDirectory() as tmp:
                    directories = [unpack(e, args.cache, Path(tmp)) for e in entries]
                    entry[family] = {"id": "+".join(e["id"] for e in entries),
                                     **score(bt, entries, directories, sets)}
                print(f"  {family}: " + ", ".join(
                    f"{s} chrF {entry[family][s]['chrF']:.3f} ({entry[family][s]['seconds']} s)"
                    for s in sets), flush=True)
            results.append(entry)

    (args.out / "compare.json").write_text(
        json.dumps(results, indent=1, ensure_ascii=False) + "\n")
    print(f"compared {len(results)} directions", flush=True)


if __name__ == "__main__":
    main()
