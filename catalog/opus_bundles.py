#!/usr/bin/env python3
"""Converts OPUS-MT models (Helsinki-NLP's Tatoeba releases) for Bergamot and
packs them as one deterministic .tar.gz per direction, with the catalog
entries core_translate_model reads.

Per direction in catalog/opus_directions.txt (a pinned Tatoeba release):
  1. download the model archive (cached in --cache);
  2. re-index it for Bergamot's SentencePiece numbering (opus_to_bergamot)
     and quantize it to int8 (marian-conv --gemm-type intgemm8);
  3. quality gate: translate the first --qa-sentences of Helsinki's own test
     file with the converted model through libbergamot (catalog/bt.py, the
     interface apps use) and compare chrF with the original model's
     translations of the same sentences — a conversion that drops more than
     --qa-tolerance fails the build instead of shipping;
  4. pack model.intgemm8.bin, source.spm, target.spm, the release's own
     LICENSE (CC BY 4.0) and README, and a SOURCE.md (attribution, origin,
     conversion, both chrF scores).

Usage:
  python3 -I catalog/opus_bundles.py --out OUT --release-url URL \\
      --library out/linux/lib/libbergamot.so --marian-conv out/linux/bin/marian-conv \\
      [--only ita-eng,eng-ita] [--cache DIR]

Writes OUT/bundles/opus-<src>-<trg>.tar.gz, OUT/catalog.json, OUT/qa.json
(every gate result) and OUT/SHA256SUMS. Needs numpy and pyyaml.
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bt import Bergamot  # noqa: E402
from mozilla_bundles import deterministic_tar_gz, sha256  # noqa: E402
from opus_to_bergamot import convert  # noqa: E402

TATOEBA = "https://object.pouta.csc.fi/Tatoeba-MT-models"

# ISO 639-3 (Tatoeba) -> (BCP 47, English name).
LANGUAGES = {
    "eng": ("en", "English"), "ita": ("it", "Italian"), "deu": ("de", "German"),
    "fra": ("fr", "French"), "spa": ("es", "Spanish"), "por": ("pt", "Portuguese"),
    "nld": ("nl", "Dutch"), "pol": ("pl", "Polish"), "ron": ("ro", "Romanian"),
    "swe": ("sv", "Swedish"), "dan": ("da", "Danish"), "ces": ("cs", "Czech"),
    "ell": ("el", "Greek"), "hun": ("hu", "Hungarian"), "fin": ("fi", "Finnish"),
}

# Bergamot settings OPUS-MT models translate with. Greedy (beam 1), like
# Mozilla's; int8shift because they're quantized without precomputed alphas.
ENGINE_CONFIG = {"gemmPrecision": "int8shift", "beamSize": 1}


def config_yaml(model, vocabs):
    return "\n".join([
        "models:", f"  - {json.dumps(model)}",
        "vocabs:", *[f"  - {json.dumps(v)}" for v in vocabs],
        "beam-size: 1", "normalize: 1.0", "word-penalty: 0",
        "max-length-break: 128", "mini-batch-words: 1024", "workspace: 128",
        "max-length-factor: 2.0", "skip-cost: true", "quiet: true",
        "quiet-translation: true", 'gemm-precision: "int8shift"', "",
    ])


def chrf(hypotheses, references, n=6, beta=2.0):
    """Corpus chrF on a 0-1 scale, as sacrebleu computes it by default:
    character n-grams 1..n with whitespace removed, matches and totals summed
    over the whole corpus per order, precision and recall averaged over the
    orders, then the F-beta score of those averages."""
    matches, hyp_total, ref_total = [0] * n, [0] * n, [0] * n
    for hyp, ref in zip(hypotheses, references):
        hyp, ref = hyp.replace(" ", ""), ref.replace(" ", "")
        for k in range(1, n + 1):
            h = Counter(hyp[i:i + k] for i in range(len(hyp) - k + 1))
            r = Counter(ref[i:i + k] for i in range(len(ref) - k + 1))
            matches[k - 1] += sum((h & r).values())
            hyp_total[k - 1] += sum(h.values())
            ref_total[k - 1] += sum(r.values())
    orders = [k for k in range(n) if hyp_total[k] and ref_total[k]]
    if not orders:
        return 0.0
    p = sum(matches[k] / hyp_total[k] for k in orders) / len(orders)
    r = sum(matches[k] / ref_total[k] for k in orders) / len(orders)
    if p + r == 0:
        return 0.0
    return (1 + beta**2) * p * r / (beta**2 * p + r)


def fetch(url, dest):
    if dest.exists():
        return dest
    partial = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=600) as response, open(partial, "wb") as out:
        shutil.copyfileobj(response, out, length=1 << 20)
    partial.rename(dest)
    return dest


def read_test(text, limit):
    """Helsinki's test file: blank-line-separated (source, reference, model
    output) triples."""
    triples = []
    for block in text.strip().split("\n\n"):
        lines = block.split("\n")
        if len(lines) == 3 and all(lines):
            triples.append(lines)
        if len(triples) == limit:
            break
    return triples


def tatoeba_chrf(readme, pair):
    match = re.search(rf"\|\s*Tatoeba-test\.{pair}\s*\|\s*[\d.]+\s*\|\s*([\d.]+)", readme)
    return float(match.group(1)) if match else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--release-url", required=True)
    parser.add_argument("--library", required=True)
    parser.add_argument("--marian-conv", required=True)
    parser.add_argument("--list", type=Path, default=Path(__file__).parent / "opus_directions.txt")
    parser.add_argument("--only")
    parser.add_argument("--cache", type=Path, default=Path("opus-cache"))
    parser.add_argument("--qa-sentences", type=int, default=300)
    parser.add_argument("--qa-tolerance", type=float, default=0.04)
    args = parser.parse_args()

    directions = []
    for line in args.list.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            pair, release = line.split()
            directions.append((pair, release))
    if args.only:
        wanted = set(args.only.split(","))
        directions = [d for d in directions if d[0] in wanted]

    out = args.out
    if out.exists():
        shutil.rmtree(out)
    (out / "bundles").mkdir(parents=True)
    args.cache.mkdir(parents=True, exist_ok=True)

    catalog, qa, failed = [], [], []
    with Bergamot(args.library) as bt:
        for pair, release in directions:
            src3, trg3 = pair.split("-")
            (src, src_name), (trg, trg_name) = LANGUAGES[src3], LANGUAGES[trg3]
            bundle_id = f"opus-{src}-{trg}"
            source_url = f"{TATOEBA}/{pair}/{release}.zip"
            print(f"{bundle_id}: {source_url}", flush=True)
            archive = fetch(source_url, args.cache / f"{pair}-{release}.zip")

            with tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                with zipfile.ZipFile(archive) as z:
                    names = z.namelist()
                    npz = next(n for n in names if n.endswith("best-perplexity.npz"))
                    vocab = next(n for n in names if n.endswith(".vocab.yml"))
                    for name in [npz, vocab, "source.spm", "target.spm", "LICENSE", "README.md"]:
                        z.extract(name, tmp)
                readme = (tmp / "README.md").read_text(encoding="utf-8")

                convert(tmp / npz, tmp / vocab, tmp / "source.spm", tmp / "target.spm",
                        tmp / "bergamot.npz", log=lambda m: print(f"  {m}", flush=True))
                subprocess.run(
                    [args.marian_conv, "-f", str(tmp / "bergamot.npz"),
                     "-t", str(tmp / "model.intgemm8.bin"), "--gemm-type", "intgemm8"],
                    check=True, capture_output=True,
                )

                # Quality gate.
                test = fetch(f"{TATOEBA}/{pair}/{release}.test.txt",
                             args.cache / f"{pair}-{release}.test.txt")
                triples = read_test(test.read_text(encoding="utf-8"), args.qa_sentences)
                model = bt.load_model(
                    config_yaml("model.intgemm8.bin", ["source.spm", "target.spm"]), str(tmp)
                )
                ours = bt.translate(model, [t[0] for t in triples])
                refs = [t[1] for t in triples]
                original = chrf([t[2] for t in triples], refs)
                converted = chrf(ours, refs)
                ok = converted >= original - args.qa_tolerance
                result = {
                    "id": bundle_id, "release": release, "sentences": len(triples),
                    "chrfOriginal": round(original, 4), "chrfConverted": round(converted, 4),
                    "passed": ok,
                }
                qa.append(result)
                print(f"  QA on {len(triples)} sentences: original {original:.3f}, "
                      f"converted {converted:.3f} -> {'pass' if ok else 'FAIL'}", flush=True)
                if not ok:
                    failed.append(bundle_id)
                    continue

                published = tatoeba_chrf(readme, pair)
                source_md = (
                    f"# {bundle_id}\n\n"
                    f"OPUS-MT model {pair} from Helsinki-NLP's Tatoeba Translation "
                    f"Challenge (Jörg Tiedemann and colleagues, University of Helsinki), "
                    f"release `{release}`: {source_url}\n\n"
                    "Licensed under Creative Commons Attribution 4.0 (LICENSE, as "
                    "released). Please credit Helsinki-NLP / OPUS-MT when you use it. "
                    "Cite: Tiedemann, J. (2020) \"The Tatoeba Translation Challenge\"; "
                    "Tiedemann & Thottingal (2020) \"OPUS-MT — Building open translation "
                    "services for the World\".\n\n"
                    "## Changes\n\n"
                    "Converted for Mozilla's Bergamot engine: the tied embedding was "
                    "re-indexed to SentencePiece's numbering (separate source and target "
                    "embeddings, output tied to the target one) and the model quantized to "
                    "int8 with Marian's marian-conv (`--gemm-type intgemm8`). Weights are "
                    "otherwise as released; source.spm, target.spm, LICENSE and README.md "
                    "are unmodified.\n\n"
                    "## Quality\n\n"
                    f"Published chrF on Tatoeba-test: {published}. Checked after "
                    f"conversion on the first {len(triples)} sentences of the release's "
                    f"own test file: chrF {original:.3f} for the original model's "
                    f"translations, {converted:.3f} for this one (greedy, int8).\n"
                ).encode()
                packed = [
                    (name, (tmp / name).read_bytes())
                    for name in ["model.intgemm8.bin", "source.spm", "target.spm",
                                 "LICENSE", "README.md"]
                ] + [("SOURCE.md", source_md)]

            path = out / "bundles" / f"{bundle_id}.tar.gz"
            deterministic_tar_gz(path, bundle_id, packed)
            data = path.read_bytes()
            entry = {
                "id": bundle_id,
                "name": f"{src_name} → {trg_name} (OPUS-MT)",
                "sourceLanguage": src,
                "targetLanguage": trg,
                "engineId": "bergamot",
                "family": "opus-mt",
                "license": "CC-BY-4.0",
                # CC BY allows commercial use; it requires attribution
                # (SOURCE.md says how).
                "commercialUse": True,
            }
            if published is not None:
                entry["quality"] = {"metric": "chrF-tatoeba", "score": published}
            entry.update({
                "downloadUrl": f"{args.release_url}/{path.name}",
                "expectedDownloadBytes": len(data),
                "archiveSha256": sha256(data),
                "archiveFormat": "tarGz",
                "modelRoot": bundle_id,
                "files": [
                    {"relativePath": f"{bundle_id}/{name}", "sha256": sha256(blob)}
                    for name, blob in sorted(packed)
                ],
                "engineConfig": {
                    "model": "model.intgemm8.bin",
                    "vocabs": ["source.spm", "target.spm"],
                    **ENGINE_CONFIG,
                },
            })
            catalog.append(entry)
            print(f"  {len(data) / 1e6:.1f} MB", flush=True)

    (out / "catalog.json").write_text(json.dumps(catalog, indent=1, ensure_ascii=False) + "\n")
    (out / "qa.json").write_text(json.dumps(qa, indent=1) + "\n")
    sums = [f"{sha256(p.read_bytes())}  {p.name}" for p in sorted((out / "bundles").iterdir())]
    sums += [f"{sha256((out / f).read_bytes())}  {f}" for f in ["catalog.json", "qa.json"]]
    (out / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    print(f"{len(catalog)} bundles, {len(failed)} failed the quality gate: {failed}", flush=True)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
