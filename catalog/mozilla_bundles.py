#!/usr/bin/env python3
"""Repackages Mozilla's released Firefox Translations models as one
deterministic .tar.gz per direction, with our own checksums, and writes the
catalog entries core_translate_model reads.

For each released pair in Mozilla's model registry, the phone-sized model
(a "Release Android" entry where there is one, else "Release" — never the
larger desktop-only builds): its model, vocabulary (or source and target
vocabularies) and lexical shortlist are downloaded, unpacked from .gz, the
model checked against the registry's SHA-256, and packed with the MPL-2.0
text and a SOURCE.md saying where each file came from.

Usage:
  python3 -I catalog/mozilla_bundles.py --out OUT --release-url URL [--only it-en,en-it]

Writes OUT/bundles/mozilla-<pair>.tar.gz, OUT/catalog.json (a list of
TranslationModelManifest entries pointing at URL/<bundle>), OUT/registry.json
(the registry snapshot used) and OUT/SHA256SUMS. Standard library only.
"""

import argparse
import gzip
import hashlib
import io
import json
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

REGISTRY = (
    "https://storage.googleapis.com/moz-fx-translations-data--303e-prod-"
    "translations-data/db/models.json"
)
MPL_TEXT = "https://www.mozilla.org/media/MPL/2.0/index.txt"

# Registry codes -> (BCP 47 tag, English name). The registry mostly uses
# ISO 639-1 codes; a few aren't BCP 47 as written.
LANGUAGES = {
    "af": ("af", "Afrikaans"), "ar": ("ar", "Arabic"), "az": ("az", "Azerbaijani"),
    "be": ("be", "Belarusian"), "bg": ("bg", "Bulgarian"), "bn": ("bn", "Bengali"),
    "bs": ("bs", "Bosnian"), "ca": ("ca", "Catalan"), "cs": ("cs", "Czech"),
    "da": ("da", "Danish"), "de": ("de", "German"), "el": ("el", "Greek"),
    "en": ("en", "English"), "es": ("es", "Spanish"), "et": ("et", "Estonian"),
    "eu": ("eu", "Basque"), "fa": ("fa", "Persian"), "fi": ("fi", "Finnish"),
    "fr": ("fr", "French"), "gl": ("gl", "Galician"), "gu": ("gu", "Gujarati"),
    "hbs": ("sh", "Serbo-Croatian"), "he": ("he", "Hebrew"), "hi": ("hi", "Hindi"),
    "hr": ("hr", "Croatian"), "hu": ("hu", "Hungarian"), "id": ("id", "Indonesian"),
    "is": ("is", "Icelandic"), "it": ("it", "Italian"), "ja": ("ja", "Japanese"),
    "kn": ("kn", "Kannada"), "ko": ("ko", "Korean"), "lt": ("lt", "Lithuanian"),
    "lv": ("lv", "Latvian"), "ml": ("ml", "Malayalam"), "mr": ("mr", "Marathi"),
    "ms": ("ms", "Malay"), "nb": ("nb", "Norwegian Bokmål"), "nl": ("nl", "Dutch"),
    "nn": ("nn", "Norwegian Nynorsk"), "no": ("no", "Norwegian"), "pl": ("pl", "Polish"),
    "pt": ("pt", "Portuguese"), "ro": ("ro", "Romanian"), "ru": ("ru", "Russian"),
    "sk": ("sk", "Slovak"), "sl": ("sl", "Slovenian"), "sq": ("sq", "Albanian"),
    "sr": ("sr", "Serbian"), "sv": ("sv", "Swedish"), "ta": ("ta", "Tamil"),
    "te": ("te", "Telugu"), "th": ("th", "Thai"), "tl": ("tl", "Tagalog"),
    "tr": ("tr", "Turkish"), "ug": ("ug", "Uyghur"), "uk": ("uk", "Ukrainian"),
    "ur": ("ur", "Urdu"), "vi": ("vi", "Vietnamese"), "zh": ("zh-Hans", "Chinese (Simplified)"),
    "zh_hant": ("zh-Hant", "Chinese (Traditional)"),
}


def fetch(url):
    with urllib.request.urlopen(url, timeout=300) as response:
        return response.read()


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def choose(entries):
    """The phone-sized released model of a pair, or None."""
    by_status = {e.get("releaseStatus"): e for e in entries}
    return by_status.get("Release Android") or by_status.get("Release")


def deterministic_tar_gz(path, root_name, files):
    """files: [(name inside root, bytes)], packed sorted, with fixed
    metadata, so the same inputs always give the same archive bytes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        def add(name, data=None):
            info = tarfile.TarInfo(name)
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            if data is None:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            else:
                info.size = len(data)
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(data))

        add(root_name)
        for name, data in sorted(files):
            add(f"{root_name}/{name}", data)
    with open(path, "wb") as out:
        with gzip.GzipFile(fileobj=out, mode="wb", mtime=0, filename="") as gz:
            gz.write(buf.getvalue())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--release-url", required=True)
    parser.add_argument("--only", help="comma-separated pairs, e.g. it-en,en-it")
    args = parser.parse_args()

    registry_bytes = fetch(REGISTRY)
    registry = json.loads(registry_bytes)
    base = registry["baseUrl"]
    mpl = fetch(MPL_TEXT)

    out = args.out
    bundles = out / "bundles"
    if out.exists():
        shutil.rmtree(out)
    bundles.mkdir(parents=True)
    (out / "registry.json").write_bytes(registry_bytes)

    wanted = set(args.only.split(",")) if args.only else None
    catalog = []
    for pair in sorted(registry["models"]):
        if wanted is not None and pair not in wanted:
            continue
        entry = choose(registry["models"][pair])
        if entry is None:
            continue
        src, trg = entry["sourceLanguage"], entry["targetLanguage"]
        if src not in LANGUAGES or trg not in LANGUAGES:
            print(f"skipping {pair}: unknown language code", file=sys.stderr)
            continue

        files = {}
        sources = []
        for key, spec in sorted(entry["files"].items()):
            url = f"{base}/{spec['path']}"
            data = gzip.decompress(fetch(url))
            name = spec["path"].rsplit("/", 1)[1].removesuffix(".gz")
            expected = spec.get("uncompressedHash")
            if expected and sha256(data) != expected:
                sys.exit(f"{pair}: {name} does not match the registry's SHA-256")
            files[key] = (name, data)
            sources.append(
                f"- `{name}` ({key}): {url}"
                + (f", registry SHA-256 {expected}" if expected else "")
            )

        bundle_id = f"mozilla-{pair}"
        source_md = (
            f"# {bundle_id}\n\n"
            f"Mozilla's Firefox Translations model for {pair} "
            f"({entry['architecture']}, release status "
            f"\"{entry.get('releaseStatus')}\"), unmodified: each file is the "
            "registry's .gz, unpacked.\n\n"
            f"Registry: {REGISTRY} (generated {registry.get('generated')}).\n\n"
            + "\n".join(sources)
            + "\n\nLicensed under the Mozilla Public License 2.0 (LICENSE); the "
            "training pipeline is https://github.com/mozilla/translations.\n"
        ).encode()

        packed = [(name, data) for name, data in files.values()]
        packed += [("LICENSE", mpl), ("SOURCE.md", source_md)]
        archive = bundles / f"{bundle_id}.tar.gz"
        deterministic_tar_gz(archive, bundle_id, packed)
        archive_bytes = archive.read_bytes()

        vocabs = (
            [files["srcVocab"][0], files["trgVocab"][0]]
            if "srcVocab" in files
            else [files["vocab"][0]]
        )
        metrics = entry.get("metrics", {}).get("flores200-plus", {})
        (src_tag, src_name), (trg_tag, trg_name) = LANGUAGES[src], LANGUAGES[trg]
        manifest = {
            "id": bundle_id,
            "name": f"{src_name} → {trg_name} (Mozilla)",
            "sourceLanguage": src_tag,
            "targetLanguage": trg_tag,
            "engineId": "bergamot",
            "family": "mozilla",
            "license": "MPL-2.0",
            "commercialUse": True,
        }
        if "comet22" in metrics:
            manifest["quality"] = {"metric": "comet22", "score": metrics["comet22"]}
        manifest.update({
            "downloadUrl": f"{args.release_url}/{archive.name}",
            "expectedDownloadBytes": len(archive_bytes),
            "archiveSha256": sha256(archive_bytes),
            "archiveFormat": "tarGz",
            "modelRoot": bundle_id,
            "files": [
                {"relativePath": f"{bundle_id}/{name}", "sha256": sha256(data)}
                for name, data in sorted(packed)
            ],
            "engineConfig": {
                "model": files["model"][0],
                "vocabs": vocabs,
                "shortlist": files["lexicalShortlist"][0],
            },
        })
        catalog.append(manifest)
        print(f"{bundle_id}: {len(archive_bytes) / 1e6:.1f} MB", flush=True)

    (out / "catalog.json").write_text(json.dumps(catalog, indent=1, ensure_ascii=False) + "\n")
    sums = [
        f"{sha256(p.read_bytes())}  {p.name}"
        for p in sorted(bundles.iterdir())
    ] + [f"{sha256((out / 'catalog.json').read_bytes())}  catalog.json"]
    (out / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    print(f"{len(catalog)} bundles", flush=True)


if __name__ == "__main__":
    main()
