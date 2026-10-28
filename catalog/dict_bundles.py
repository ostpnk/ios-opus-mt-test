#!/usr/bin/env python3
"""Builds bilingual dictionaries from Wiktionary — one SQLite file per
direction with data, plus one inflected-forms pack per source language —
and the catalog entries core_dictionary reads.

Sources (as dict_coverage.py measures them): every Wiktionary edition
kaikki.org extracts with wiktextract. An edition E gives:
  - glosses: a word of language L explained in E (L -> E), sense by sense;
  - tables: a word of E's translation table (E -> T), grouped by sense;
  - inverted tables: the same table read backwards (T -> E), the E word
    as the translation, under its sense label.
Serbo-Croatian ("sh") entries also serve Bosnian, Croatian and Serbian.

Per direction (dict-<src>-<tgt>.tar.gz, folder dict-<src>-<tgt>/):
  dict.sqlite:
    meta(key, value)            source, target, sources, fold (JSON)
    entries(key, word, pos, senses)
      key: the word lowercased and folded (accents off; see `fold` in meta,
           which lists exactly the per-character mapping used, so a reader
           folds a query the same way); indexed.
      senses: JSON list, each one of
        {"g": [gloss, ...], "tags": [...]}            (glosses, in tgt)
        {"l": label, "t": [{"w": word, "tags": [...]}, ...]}
                                                       (translations into tgt;
                                                        label in either language)
  LICENSE (CC BY-SA 4.0), SOURCE.md.
Per source language (forms-<lang>.tar.gz, folder forms-<lang>/):
  forms.sqlite: meta; forms(key, form, lemma, tags) — inflected form ->
  lemma, kept only for lemmas some dictionary from that language has.

The catalog carries each direction's coverage: the share of the source
language's 1,000 and 10,000 most frequent words (FrequencyWords) that
lookup finds, directly or through the forms pack.

Usage:
  python3 -I catalog/dict_bundles.py --out OUT --work DIR --release-url URL \\
      --languages-from CATALOG_URL [--editions en,de,it] [--keep-dumps]
"""

import argparse
import gzip
import json
import sqlite3
import sys
import unicodedata
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from dict_coverage import EDITIONS, base, dump_url, fetch, frequency_lists  # noqa: E402
from mozilla_bundles import deterministic_tar_gz, sha256  # noqa: E402

SERBO_CROATIAN = {"sh": ["bs", "hr", "sr"]}
NAMES = {}  # filled from the translation catalog: language tag -> English name

# wiktextract tags that describe inflection tables, not a form.
TABLE_TAGS = {"table-tags", "inflection-template", "class", "romanization",
              "canonical", "error-unknown-tag"}
SKIP_SENSE_TAGS = {"form-of", "alt-of"}

LICENSE_URL = "https://creativecommons.org/licenses/by-sa/4.0/legalcode.txt"


def fold_char(c):
    decomposed = unicodedata.normalize("NFKD", c)
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


class Folder:
    """Lowercases, then maps each character through fold_char — recording
    every mapping that changes a character, so readers can replay it."""

    def __init__(self):
        self.table = {}

    def __call__(self, text):
        out = []
        for c in text.lower():
            f = self.table.get(c)
            if f is None:
                f = fold_char(c)
                if f != c:
                    self.table[c] = f
                else:
                    f = c
            out.append(f)
        return "".join(out)

    def subset(self, words):
        """The part of the table these (already lowercased) words use."""
        used = {c for w in words for c in w}
        return {c: f for c, f in self.table.items() if c in used}


def targets_of(code):
    return SERBO_CROATIAN.get(code, [code])


def stage(conn, edition, path, languages, fold):
    rows, forms = [], []

    def flush():
        conn.executemany("INSERT INTO staged VALUES (?,?,?,?,?,?,?)", rows)
        conn.executemany("INSERT INTO staged_forms VALUES (?,?,?,?,?)", forms)
        rows.clear()
        forms.clear()

    count = 0
    # Outside the English edition, a page's translation table is attached
    # to every part of speech on it ("pala" noun and verb alike): a table
    # seen for the same word just before is skipped, keeping it under the
    # first part of speech only.
    last_table = (None, None)
    with gzip.open(path, "rt", encoding="utf-8") as lines:
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            word, code = entry.get("word"), entry.get("lang_code")
            if not word or not code:
                continue
            langs = [l for l in targets_of(code) if l in languages]
            if not langs:
                continue
            count += 1
            pos = entry.get("pos") or ""
            senses = entry.get("senses") or []
            lower = word.lower()

            # Inflected forms -> lemma, for the forms packs.
            for form in entry.get("forms") or []:
                f, tags = form.get("form") or "", form.get("tags") or []
                if (not f or f == word or " " in f or TABLE_TAGS & set(tags)
                        or f.startswith("-") or f == "—"):
                    continue
                for lang in langs:
                    forms.append((lang, fold(f), f, word, json.dumps(tags)))
            for sense in senses:
                for target in sense.get("form_of") or []:
                    if target.get("word"):
                        tags = [t for t in sense.get("tags") or [] if t not in SKIP_SENSE_TAGS]
                        for lang in langs:
                            forms.append((lang, fold(word), word, target["word"], json.dumps(tags)))

            # Glosses: this word explained in the edition's language.
            if code != edition and edition in languages:
                for sense in senses:
                    glosses = sense.get("glosses")
                    tags = sense.get("tags") or []
                    if not glosses or sense.get("form_of") or "form-of" in tags:
                        continue
                    data = {"g": glosses}
                    kept = [t for t in tags if t not in SKIP_SENSE_TAGS]
                    if kept:
                        data["tags"] = kept
                    for lang in langs:
                        rows.append((f"{lang}-{edition}", fold(word), word, pos,
                                     json.dumps(data, ensure_ascii=False),
                                     f"{edition}:glosses", lower))

            # Tables: this edition's own word, translated; and inverted.
            table = entry.get("translations") or []
            signature = json.dumps(table, sort_keys=True) if table else None
            if signature is not None and last_table == (word, signature):
                table = []
            elif signature is not None:
                last_table = (word, signature)
            if code == edition:
                groups = {}
                for t in table:
                    target, translated = t.get("lang_code") or t.get("code"), t.get("word")
                    if not target or not translated:
                        continue
                    for tgt in targets_of(target):
                        if tgt not in languages or tgt == edition:
                            continue
                        label = t.get("sense") or ""
                        item = {"w": translated}
                        if t.get("tags"):
                            item["tags"] = t["tags"]
                        groups.setdefault((tgt, label), []).append(item)
                        inverted = {"l": label, "t": [{"w": word}]}
                        rows.append((f"{tgt}-{edition}", fold(translated), translated, pos,
                                     json.dumps(inverted, ensure_ascii=False),
                                     f"{edition}:inverted", translated.lower()))
                for (tgt, label), items in groups.items():
                    data = {"l": label, "t": items} if label else {"t": items}
                    rows.append((f"{edition}-{tgt}", fold(word), word, pos,
                                 json.dumps(data, ensure_ascii=False),
                                 f"{edition}:tables", lower))

            if len(rows) + len(forms) > 200_000:
                flush()
    flush()
    conn.commit()
    return count


def merge_senses(raw):
    """Senses of one (word, pos) from every source: identical ones once,
    translation groups with the same label combined."""
    out, by_label, seen = [], {}, set()
    for data in raw:
        if "t" in data:
            label = data.get("l", "")
            group = by_label.get(label)
            if group is None:
                group = {"l": label, "t": []} if label else {"t": []}
                by_label[label] = group
                out.append(group)
            for item in data["t"]:
                if item["w"] not in {i["w"] for i in group["t"]}:
                    group["t"].append(item)
        else:
            key = json.dumps(data, sort_keys=True, ensure_ascii=False)
            if key not in seen:
                seen.add(key)
                out.append(data)
    # Glosses first: they're the explained meanings; translation groups after.
    return [s for s in out if "g" in s] + [s for s in out if "t" in s]


def coverage(words, forms, freq):
    """words: folded headword keys; forms: folded form -> folded lemmas."""
    if not freq:
        return None
    found = {1000: 0, len(freq): 0}
    for i, w in enumerate(freq):
        if w in words or any(l in words for l in forms.get(w, ())):
            for n in found:
                if i < n:
                    found[n] += 1
    return {f"top{n}": round(c / min(n, len(freq)), 3) for n, c in found.items()}


def pack(out_dir, bundle_id, db_path, source_md, license_text, release_url):
    files = [(db_path.name, db_path.read_bytes()), ("LICENSE", license_text),
             ("SOURCE.md", source_md.encode())]
    path = out_dir / "bundles" / f"{bundle_id}.tar.gz"
    deterministic_tar_gz(path, bundle_id, files)
    data = path.read_bytes()
    return {
        "downloadUrl": f"{release_url}/{path.name}",
        "expectedDownloadBytes": len(data),
        "archiveSha256": sha256(data),
        "archiveFormat": "tarGz",
        "modelRoot": bundle_id,
        "files": [{"relativePath": f"{bundle_id}/{name}", "sha256": sha256(blob)}
                  for name, blob in sorted(files)],
    }


SOURCE_MD = """# {id}

{what}, built from Wiktionary by translate-engine's catalog/dict_bundles.py
from kaikki.org's wiktextract extraction{dated} — {sources}.

Wiktionary content is by the Wiktionary contributors and licensed under
Creative Commons Attribution-ShareAlike 4.0 (LICENSE); this file is a
derivative of it and is licensed under the same terms. Credit: "Wiktionary
contributors (wiktionary.org), via kaikki.org (Tatu Ylonen, Wiktextract:
Wiktionary as Machine-Readable Structured Data, LREC 2022)".
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--release-url", required=True,
                        help="where bundles are downloaded from; {part} is replaced "
                             "by the release part (1, 2, ...)")
    parser.add_argument("--languages-from", required=True)
    parser.add_argument("--editions", default=",".join(EDITIONS))
    parser.add_argument("--assets-per-release", type=int, default=990)
    parser.add_argument("--keep-dumps", action="store_true")
    parser.add_argument("--dated", default="")
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    (args.out / "bundles").mkdir(parents=True, exist_ok=True)

    with urllib.request.urlopen(args.languages_from, timeout=120) as response:
        catalog = json.load(response)
    languages = {base(e[k]) for e in catalog for k in ("sourceLanguage", "targetLanguage")}
    languages |= {"bs", "hr", "sr"}
    for e in catalog:
        for k, n in (("sourceLanguage", e["name"].split(" → ")[0]),
                     ("targetLanguage", e["name"].split(" → ")[1].split(" (")[0])):
            NAMES.setdefault(base(e[k]), n)
    NAMES.update({"bs": "Bosnian", "hr": "Croatian", "sr": "Serbian", "sh": "Serbo-Croatian"})
    print(f"{len(languages)} languages", flush=True)
    freq = frequency_lists(languages, args.work, 10000)

    fold = Folder()
    staging = args.work / "staging.sqlite"
    staging.unlink(missing_ok=True)
    conn = sqlite3.connect(staging)
    conn.executescript("""
        PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
        CREATE TABLE staged(direction, key, word, pos, data, source, lower);
        CREATE TABLE staged_forms(lang, key, form, lemma, tags);
    """)
    editions = args.editions.split(",")
    for edition in editions:
        path = fetch(dump_url(edition), args.work / f"{edition}wiktionary.jsonl.gz")
        count = stage(conn, edition, path, languages, fold)
        print(f"{edition}: {count} entries staged", flush=True)
        if not args.keep_dumps:
            path.unlink()
    print("indexing", flush=True)
    conn.executescript("""
        CREATE INDEX staged_direction ON staged(direction, key, word, pos);
        CREATE INDEX staged_forms_lang ON staged_forms(lang, key);
    """)

    license_text = fetch(LICENSE_URL, args.work / "cc-by-sa-4.0.txt").read_bytes()
    directions = [d for (d,) in conn.execute("SELECT DISTINCT direction FROM staged ORDER BY 1")]
    headword_keys = {}  # source language -> folded headwords across its directions
    built = []
    for direction in directions:
        src, tgt = direction.split("-")
        db_path = args.work / "dict.sqlite"
        db_path.unlink(missing_ok=True)
        db = sqlite3.connect(db_path)
        db.executescript("""
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE entries(key TEXT NOT NULL, word TEXT NOT NULL,
                                 pos TEXT NOT NULL, senses TEXT NOT NULL);
        """)
        keys, lowers, sources, current, batch = set(), set(), set(), None, []
        rows = []

        def emit():
            if current is not None:
                rows.append((*current, json.dumps(merge_senses(batch), ensure_ascii=False)))

        for key, word, pos, data, source, lower in conn.execute(
                "SELECT key, word, pos, data, source, lower FROM staged "
                "WHERE direction = ? ORDER BY key, word, pos", (direction,)):
            if (key, word, pos) != current:
                emit()
                current, batch = (key, word, pos), []
            batch.append(json.loads(data))
            keys.add(key)
            lowers.add(lower)
            sources.add(source)
        emit()
        db.executemany("INSERT INTO entries VALUES (?,?,?,?)", rows)
        db.execute("CREATE INDEX entries_key ON entries(key)")
        meta = {"source": src, "target": tgt, "sources": json.dumps(sorted(sources)),
                "fold": json.dumps(fold.subset(lowers), ensure_ascii=False, sort_keys=True),
                "schema": "1"}
        db.executemany("INSERT INTO meta VALUES (?,?)", sorted(meta.items()))
        db.commit()
        db.execute("VACUUM")
        db.close()
        headword_keys.setdefault(src, set()).update(keys)
        bundle_id = f"dict-{src}-{tgt}"
        source_md = SOURCE_MD.format(
            id=bundle_id, what=f"{NAMES.get(src, src)} → {NAMES.get(tgt, tgt)} dictionary",
            dated=f" ({args.dated})" if args.dated else "", sources=", ".join(sorted(sources)))
        packed = pack(args.out, bundle_id, db_path, source_md, license_text, args.release_url)
        built.append({"id": bundle_id, "src": src, "tgt": tgt, "keys": keys,
                      "headwords": len(rows), "sources": sorted(sources), "packed": packed})
        print(f"  {bundle_id}: {len(rows)} headwords, "
              f"{packed['expectedDownloadBytes'] / 1e6:.1f} MB", flush=True)

    # Forms packs: per source language, for lemmas some dictionary has.
    forms_by_lang = {}
    for src in sorted(headword_keys):
        db_path = args.work / "forms.sqlite"
        db_path.unlink(missing_ok=True)
        db = sqlite3.connect(db_path)
        db.executescript("""
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE forms(key TEXT NOT NULL, form TEXT NOT NULL,
                               lemma TEXT NOT NULL, tags TEXT NOT NULL);
        """)
        lemma_keys = headword_keys[src]
        frequent = {fold(w) for w in freq.get(src, [])}
        # Only what coverage needs: the frequent words' lemmas.
        mapping, seen, tagged, rows, lowers = {}, set(), set(), [], set()
        for key, form, lemma, tags in conn.execute(
                "SELECT key, form, lemma, tags FROM staged_forms WHERE lang = ? "
                "ORDER BY key, form, lemma, tags = '[]'",
                (src,)):
            if fold(lemma) not in lemma_keys or (key, form, lemma, tags) in seen:
                continue
            if tags == "[]" and (key, form, lemma) in tagged:
                continue  # Already there with its tags (sorted first).
            seen.add((key, form, lemma, tags))
            if tags != "[]":
                tagged.add((key, form, lemma))
            rows.append((key, form, lemma, tags))
            lowers.add(form.lower())
            if key in frequent:
                mapping.setdefault(key, set()).add(fold(lemma))
        if not rows:
            db.close()
            continue
        db.executemany("INSERT INTO forms VALUES (?,?,?,?)", rows)
        db.execute("CREATE INDEX forms_key ON forms(key)")
        meta = {"language": src, "schema": "1",
                "fold": json.dumps(fold.subset(lowers), ensure_ascii=False, sort_keys=True)}
        db.executemany("INSERT INTO meta VALUES (?,?)", sorted(meta.items()))
        db.commit()
        db.execute("VACUUM")
        db.close()
        bundle_id = f"forms-{src}"
        source_md = SOURCE_MD.format(
            id=bundle_id, what=f"{NAMES.get(src, src)} inflected forms",
            dated=f" ({args.dated})" if args.dated else "", sources="forms and form-of senses")
        forms_by_lang[src] = {
            "id": bundle_id, "mapping": mapping, "forms": len(rows),
            "packed": pack(args.out, bundle_id, db_path, source_md, license_text,
                           args.release_url)}
        print(f"  {bundle_id}: {len(rows)} forms", flush=True)

    # Catalog, with each bundle's release part filled into its URL.
    entries = []
    for forms in forms_by_lang.values():
        entries.append({
            "id": forms["id"], "kind": "forms", "name": f"{NAMES.get(forms['id'][6:], forms['id'][6:])} word forms",
            "sourceLanguage": forms["id"][6:], "forms": forms["forms"], **forms["packed"]})
    for b in built:
        forms = forms_by_lang.get(b["src"])
        entries.append({
            "id": b["id"], "kind": "dictionary",
            "name": f"{NAMES.get(b['src'], b['src'])} → {NAMES.get(b['tgt'], b['tgt'])}",
            "sourceLanguage": b["src"], "targetLanguage": b["tgt"],
            **({"formsId": forms["id"]} if forms else {}),
            "headwords": b["headwords"],
            "coverage": coverage(b["keys"], forms["mapping"] if forms else {},
                                 [fold(w) for w in freq.get(b["src"], [])]),
            "sources": b["sources"],
            **b["packed"]})
    for i, entry in enumerate(entries):
        entry["downloadUrl"] = entry["downloadUrl"].replace(
            "{part}", str(i // args.assets_per_release + 1))
        entry.update({"license": "CC-BY-SA-4.0", "commercialUse": True})
    (args.out / "catalog.json").write_text(
        json.dumps(entries, indent=1, ensure_ascii=False) + "\n")
    parts = {}
    for i, entry in enumerate(entries):
        parts.setdefault(i // args.assets_per_release + 1, []).append(
            Path(entry["downloadUrl"]).name)
    (args.out / "parts.json").write_text(json.dumps(parts, indent=1) + "\n")
    sums = [f"{sha256(p.read_bytes())}  {p.name}"
            for p in sorted((args.out / "bundles").iterdir())]
    (args.out / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    print(f"{len(built)} dictionaries, {len(forms_by_lang)} forms packs, "
          f"{len(parts)} release parts", flush=True)


if __name__ == "__main__":
    main()
