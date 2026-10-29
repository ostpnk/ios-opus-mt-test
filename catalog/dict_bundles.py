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
    meta(key, value)            source, target, sources, fold (JSON), schema
    entries(key, word, pos, senses)  primary key (key, word, pos), WITHOUT
      ROWID: stored sorted by it, so no separate index (a quarter smaller)
      key: the word lowercased and folded (accents off; see `fold` in meta,
           which lists exactly the per-character mapping used, so a reader
           folds a query the same way).
      senses: JSON list, each one of
        {"g": [gloss, ...], "tags": [...]}            (glosses, in tgt)
        {"l": label, "t": [{"w": word, "tags": [...]}, ...]}
                                                       (translations into tgt;
                                                        label in either language)
  LICENSE (CC BY-SA 4.0), SOURCE.md.
Per source language (forms-<lang>.tar.gz, folder forms-<lang>/):
  forms.sqlite: meta; forms(key, form, lemma, tags), primary key (key,
  form, lemma), WITHOUT ROWID — inflected form -> lemma, one row per
  (form, lemma), tags a JSON list of its readings (each
  a list of tags; [[]] when the source gave none), kept only for lemmas
  some dictionary from that language has, and only for forms in use: ones
  whose folded key is a word of the language's full FrequencyWords list
  (film subtitles). Wiktionary's tables generate every form a lemma has —
  25M for Finnish (368 MB), 1.7M for Italian, most never written (86% of
  Italian's aren't once among 800k distinct subtitle words) — and the
  filter keeps those people meet (Italian 23 -> 3.3 MB). Every form is
  kept for a language with no list, for one whose list isn't made of the
  words Wiktionary lists (UNSEGMENTED), and for a form in a script the
  list barely uses (Serbian's is 94% Latin; its Cyrillic forms stay).

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
from dict_coverage import (EDITIONS, base, dump_date, dump_url, fetch, frequency_codes,  # noqa: E402
                           frequency_lists, frequency_vocabulary)
from mozilla_bundles import deterministic_tar_gz, sha256  # noqa: E402

SERBO_CROATIAN = {"sh": ["bs", "hr", "sr"]}
# Languages whose subtitle lists can't filter forms: written without spaces,
# so a segmenter cut the words (Japanese 食べ|ます), or with particles
# attached to the word (Korean).
UNSEGMENTED = {"ja", "ko", "th", "zh"}
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

    def subset(self, chars):
        """The part of the table these (lowercased) characters use."""
        return {c: f for c, f in self.table.items() if c in chars}


def script_of(word):
    """The Unicode script of a word's first letter (LATIN, CYRILLIC, ...)."""
    for c in word:
        if c.isalpha():
            return unicodedata.name(c, "").split(" ")[0]
    return ""


def list_scripts(words):
    """The scripts at least a tenth of a frequency list's words are in."""
    counts = {}
    for w in words:
        script = script_of(w)
        counts[script] = counts.get(script, 0) + 1
    return {s for s, n in counts.items() if n >= len(words) / 10}


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
    # to every part of speech on it (Italian "essere" noun and verb alike,
    # the noun first). Such a table is kept once, with no part of speech,
    # since it can't be told which one it belongs to: the rows of the last
    # table wait in `pending` until the next entry shows whether it repeats.
    pending = None  # [word, signature, rows, shared]

    def settle():
        nonlocal pending
        if pending is not None:
            _, _, table_rows, shared = pending
            rows.extend((r[0], r[1], r[2], "", *r[4:]) if shared else r for r in table_rows)
            pending = None

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
            if (signature is not None and pending is not None
                    and pending[:2] == [word, signature]):
                pending[3] = True
                table = []
            else:
                settle()
            table_rows = []
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
                        table_rows.append((f"{tgt}-{edition}", fold(translated), translated, pos,
                                     json.dumps(inverted, ensure_ascii=False),
                                     f"{edition}:inverted", translated.lower()))
                for (tgt, label), items in groups.items():
                    data = {"l": label, "t": items} if label else {"t": items}
                    table_rows.append((f"{edition}-{tgt}", fold(word), word, pos,
                                       json.dumps(data, ensure_ascii=False),
                                       f"{edition}:tables", lower))
            if table_rows:
                pending = [word, signature, table_rows, False]

            if len(rows) + len(forms) > 200_000:
                flush()
    settle()
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
from kaikki.org's wiktextract extraction{dated}: {sources}.

Wiktionary content is by the Wiktionary contributors and licensed under
Creative Commons Attribution-ShareAlike 4.0 (LICENSE); this file is a
derivative of it and is licensed under the same terms. Credit: "Wiktionary
contributors (wiktionary.org), via kaikki.org (Tatu Ylonen, Wiktextract:
Wiktionary as Machine-Readable Structured Data, LREC 2022)".
"""


def dated(dumps, editions):
    """' (dumps: en 2026-10-25, it 2026-10-28)' for the editions used."""
    known = [f"{e} {dumps[e]}" for e in sorted(editions) if dumps.get(e)]
    return f" (dumps: {', '.join(known)})" if known else ""


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
    freq_codes = frequency_codes(languages)

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
    # Each edition's dump date (kaikki.org's Last-Modified), for SOURCE.md:
    # the build date would change every bundle on every rebuild of the same
    # data, so republishing would upload them all again.
    dumps = {}
    for edition in editions:
        path = fetch(dump_url(edition), args.work / f"{edition}wiktionary.jsonl.gz")
        dumps[edition] = dump_date(path)
        count = stage(conn, edition, path, languages, fold)
        print(f"{edition}: {count} entries staged", flush=True)
        if not args.keep_dumps:
            path.unlink()
    print("indexing", flush=True)
    conn.executescript("""
        CREATE INDEX staged_direction ON staged(direction, key, word, pos);
        CREATE INDEX staged_forms_lang ON staged_forms(lang, key, form, lemma, tags);
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
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
            CREATE TABLE entries(key TEXT NOT NULL, word TEXT NOT NULL,
                                 pos TEXT NOT NULL, senses TEXT NOT NULL,
                                 PRIMARY KEY (key, word, pos)) WITHOUT ROWID;
        """)
        keys, chars, sources, current, batch = set(), set(), set(), None, []
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
            chars.update(lower)
            sources.add(source)
        emit()
        db.executemany("INSERT INTO entries VALUES (?,?,?,?)", rows)
        meta = {"source": src, "target": tgt, "sources": json.dumps(sorted(sources)),
                "fold": json.dumps(fold.subset(chars), ensure_ascii=False, sort_keys=True),
                "schema": "1"}
        db.executemany("INSERT INTO meta VALUES (?,?)", sorted(meta.items()))
        db.commit()
        db.execute("VACUUM")
        db.close()
        headword_keys.setdefault(src, set()).update(keys)
        bundle_id = f"dict-{src}-{tgt}"
        source_md = SOURCE_MD.format(
            id=bundle_id, what=f"{NAMES.get(src, src)} → {NAMES.get(tgt, tgt)} dictionary",
            dated=dated(dumps, {s.split(":")[0] for s in sources}),
            sources=", ".join(sorted(sources)))
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
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
            CREATE TABLE forms(key TEXT NOT NULL, form TEXT NOT NULL,
                               lemma TEXT NOT NULL, tags TEXT NOT NULL,
                               PRIMARY KEY (key, form, lemma)) WITHOUT ROWID;
        """)
        lemma_keys = headword_keys[src]
        frequent = {fold(w) for w in freq.get(src, [])}
        vocabulary = (frequency_vocabulary(freq_codes[src], args.work)
                      if src in freq_codes and src not in UNSEGMENTED else None)
        in_use = None if vocabulary is None else {fold(w) for w in vocabulary}
        scripts = None if vocabulary is None else list_scripts(vocabulary)
        del vocabulary
        dropped = 0
        # One row per (form, lemma), with every reading of it: German
        # "schöne" is several cases and genders of "schön". Rows stream in
        # (key, form, lemma) order, so a group is adjacent and nothing but
        # the current group is held in memory (German has millions).
        # Untagged readings are dropped from a group that has tagged ones.
        mapping, chars, batch, count = {}, set(), [], 0
        group, readings, skipped = None, [], None

        def emit():
            nonlocal count
            if group is None:
                return
            tagged = [r for r in readings if r]
            batch.append((*group, json.dumps(tagged or [[]], ensure_ascii=False)))
            count += 1
            if len(batch) >= 100_000:
                db.executemany("INSERT INTO forms VALUES (?,?,?,?)", batch)
                batch.clear()

        for key, form, lemma, tags in conn.execute(
                "SELECT key, form, lemma, tags FROM staged_forms WHERE lang = ? "
                "ORDER BY key, form, lemma, tags", (src,)):
            if (key, form, lemma) != group:
                if fold(lemma) not in lemma_keys:
                    continue
                if (in_use is not None and key not in in_use
                        and script_of(form) in scripts):
                    if (key, form, lemma) != skipped:
                        skipped = (key, form, lemma)
                        dropped += 1
                    continue
                emit()
                group, readings = (key, form, lemma), []
                chars.update(form.lower())
                if key in frequent:
                    mapping.setdefault(key, set()).add(fold(lemma))
            reading = json.loads(tags)
            if reading not in readings:
                readings.append(reading)
        emit()
        if batch:
            db.executemany("INSERT INTO forms VALUES (?,?,?,?)", batch)
        if not count:
            db.close()
            continue
        meta = {"language": src, "schema": "1",
                "fold": json.dumps(fold.subset(chars), ensure_ascii=False, sort_keys=True)}
        db.executemany("INSERT INTO meta VALUES (?,?)", sorted(meta.items()))
        db.commit()
        db.execute("VACUUM")
        db.close()
        bundle_id = f"forms-{src}"
        source_md = SOURCE_MD.format(
            id=bundle_id, what=f"{NAMES.get(src, src)} inflected forms",
            dated=dated(dumps, dumps), sources="forms and form-of senses")
        forms_by_lang[src] = {
            "id": bundle_id, "mapping": mapping, "forms": count,
            "packed": pack(args.out, bundle_id, db_path, source_md, license_text,
                           args.release_url)}
        print(f"  {bundle_id}: {count} forms"
              f"{'' if in_use is None else f' ({dropped} not in use dropped)'}, "
              f"{forms_by_lang[src]['packed']['expectedDownloadBytes'] / 1e6:.1f} MB", flush=True)

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
