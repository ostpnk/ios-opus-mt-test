#!/usr/bin/env python3
"""How much a bilingual dictionary built from Wiktionary would cover, per
language pair — measured before building any, to decide which pairs to
publish.

Sources: every Wiktionary edition kaikki.org extracts (wiktextract): the
English one and ~20 others. Each edition E gives pairs three ways:
  - glosses: a foreign word explained in E's language — word (L) -> E
    (the English edition's Italian "pala": "shovel, trowel", ...);
  - tables: a native word's translation table — E -> T, grouped by sense
    (German "Schaufel", sense 1: it "pala", "badile");
  - inverted tables: the same table read backwards — T -> E ("pala" ->
    "Schaufel"), with E's sense as the meaning.

Coverage is what an app would see: of a language's most frequent words
(hermitdave/FrequencyWords, from OpenSubtitles), the share a pair can
look up — the word itself, or its lemma when it's an inflected form
("sono" -> "essere"), matched case-insensitively.

Writes OUT/coverage.json (per pair: per-source and merged headword counts
and top-1k/top-10k coverage) and prints the pairs with the best merged
coverage.

Usage:
  python3 -I catalog/dict_coverage.py --out OUT --languages-from CATALOG_URL \\
      [--editions en,de,it] [--cache DIR]
"""

import argparse
import email.utils
import gzip
import json
import shutil
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

EDITIONS = ["en", "cs", "de", "el", "es", "fr", "id", "it", "ja", "ko", "ku",
            "ms", "nl", "pl", "pt", "ru", "th", "tr", "vi", "zh"]
FREQUENCY = "https://raw.githubusercontent.com/hermitdave/FrequencyWords/master/content/2018"
FREQUENCY_LISTING = "https://api.github.com/repos/hermitdave/FrequencyWords/contents/content/2018"


def dump_url(edition):
    if edition == "en":
        return "https://kaikki.org/dictionary/raw-wiktextract-data.jsonl.gz"
    return f"https://kaikki.org/{edition}wiktionary/raw-wiktextract-data.jsonl.gz"


def fetch(url, dest):
    """Downloads url to dest once; the server's Last-Modified date, if any,
    is kept beside it (dump_date)."""
    if dest.exists():
        return dest
    partial = dest.with_suffix(dest.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "translate-engine coverage"})
    with urllib.request.urlopen(request, timeout=600) as response, open(partial, "wb") as out:
        shutil.copyfileobj(response, out, length=1 << 20)
        modified = response.headers.get("Last-Modified")
    if modified:
        date = email.utils.parsedate_to_datetime(modified).strftime("%Y-%m-%d")
        dest.with_name(dest.name + ".date").write_text(date)
    partial.rename(dest)
    return dest


def dump_date(dest):
    """The date fetch recorded for dest (the dump's own, not the build's)."""
    dated = dest.with_name(dest.name + ".date")
    return dated.read_text().strip() if dated.exists() else None


def base(tag):
    return tag.split("-")[0].lower()


FREQUENCY_ALIASES = {"zh": "zh_cn", "nb": "no", "sh": "sr"}


def frequency_codes(languages):
    """Language -> FrequencyWords' code, for the languages it has lists for."""
    request = urllib.request.Request(FREQUENCY_LISTING, headers={"User-Agent": "coverage"})
    with urllib.request.urlopen(request, timeout=60) as response:
        available = {item["name"] for item in json.load(response)}
    codes = {}
    for lang in languages:
        code = lang if lang in available else FREQUENCY_ALIASES.get(lang)
        if code in available:
            codes[lang] = code
    return codes


def frequency_file(code, cache, names):
    for name in names:
        try:
            return fetch(f"{FREQUENCY}/{code}/{code}_{name}.txt", cache / f"freq-{code}_{name}.txt")
        except urllib.error.HTTPError:
            continue
    return None


def frequency_words(path):
    """A list's words, lowercased, most frequent first, each once."""
    seen = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if parts and parts[0].lower() not in seen:
            seen.add(parts[0].lower())
            yield parts[0].lower()


def frequency_lists(languages, cache, top):
    """The top words per language, lowercased, most frequent first."""
    lists = {}
    for lang, code in sorted(frequency_codes(languages).items()):
        path = frequency_file(code, cache, ("50k", "full"))
        if path is None:
            continue
        words = []
        for word in frequency_words(path):
            words.append(word)
            if len(words) == top:
                break
        lists[lang] = words
    return lists


def frequency_vocabulary(code, cache):
    """Every word of the language's full list (film subtitles, ~0.5-2.5M
    distinct words), lowercased; None if it has none."""
    path = frequency_file(code, cache, ("full",))
    return None if path is None else set(frequency_words(path))


def translation_code(t):
    return t.get("lang_code") or t.get("code")


def is_form_of(sense):
    return bool(sense.get("form_of")) or "form-of" in (sense.get("tags") or [])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--languages-from", required=True,
                        help="a catalog.json (URL); its source/target languages")
    parser.add_argument("--editions", default=",".join(EDITIONS))
    parser.add_argument("--cache", type=Path, default=Path("dict-cache"))
    parser.add_argument("--top", type=int, default=10000)
    args = parser.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    args.out.mkdir(parents=True, exist_ok=True)

    with urllib.request.urlopen(args.languages_from, timeout=120) as response:
        catalog = json.load(response)
    languages = {base(e[k]) for e in catalog for k in ("sourceLanguage", "targetLanguage")}
    print(f"{len(languages)} languages: {' '.join(sorted(languages))}", flush=True)
    freq = frequency_lists(languages, args.cache, args.top)
    print(f"frequency lists for {len(freq)}: {' '.join(sorted(freq))}", flush=True)
    wanted = {lang: set(words) for lang, words in freq.items()}

    # heads[(src, tgt)][source] = lowercased headwords with a meaning in tgt
    heads = defaultdict(lambda: defaultdict(set))
    meanings = defaultdict(lambda: defaultdict(int))
    # lemmas[lang][frequent word] = lemmas it's an inflected form of
    lemmas = defaultdict(lambda: defaultdict(set))

    for edition in args.editions.split(","):
        path = fetch(dump_url(edition), args.cache / f"{edition}wiktionary.jsonl.gz")
        entries = 0
        with gzip.open(path, "rt", encoding="utf-8") as lines:
            for line in lines:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                word, lang = entry.get("word"), entry.get("lang_code")
                if not word or not lang or lang not in languages:
                    continue
                entries += 1
                key = word.lower()
                senses = entry.get("senses") or []

                # Inflected forms of frequent words, both ways round.
                if lang in wanted:
                    for form in entry.get("forms") or []:
                        f = (form.get("form") or "").lower()
                        if f in wanted[lang] and f != key:
                            lemmas[lang][f].add(key)
                    if key in wanted[lang]:
                        for sense in senses:
                            for target in sense.get("form_of") or []:
                                if target.get("word"):
                                    lemmas[lang][key].add(target["word"].lower())

                if lang != edition and edition in languages:
                    real = [s for s in senses if s.get("glosses") and not is_form_of(s)]
                    if real:
                        heads[(lang, edition)][f"{edition}:glosses"].add(key)
                        meanings[(lang, edition)][f"{edition}:glosses"] += len(real)
                if lang == edition:
                    for t in entry.get("translations") or []:
                        target, translated = translation_code(t), t.get("word")
                        if target not in languages or target == edition or not translated:
                            continue
                        heads[(edition, target)][f"{edition}:tables"].add(key)
                        meanings[(edition, target)][f"{edition}:tables"] += 1
                        heads[(target, edition)][f"{edition}:inverted"].add(translated.lower())
                        meanings[(target, edition)][f"{edition}:inverted"] += 1
        print(f"{edition}: {entries} entries in our languages", flush=True)

    def coverage(src, words):
        if src not in freq:
            return None
        found = {n: 0 for n in (1000, len(freq[src]))}
        for i, w in enumerate(freq[src]):
            if w in words or any(lemma in words for lemma in lemmas[src].get(w, ())):
                for n in found:
                    if i < n:
                        found[n] += 1
        return {f"top{n}": round(c / min(n, len(freq[src])), 3) for n, c in found.items()}

    results = []
    for (src, tgt), by_source in heads.items():
        merged = set().union(*by_source.values())
        results.append({
            "pair": f"{src}-{tgt}",
            "headwords": len(merged),
            "coverage": coverage(src, merged),
            "sources": {
                name: {"headwords": len(words), "meanings": meanings[(src, tgt)][name],
                       "coverage": coverage(src, words)}
                for name, words in sorted(by_source.items())
            },
        })
    results.sort(key=lambda r: -((r["coverage"] or {}).get("top1000", 0)))
    (args.out / "coverage.json").write_text(json.dumps(results, indent=1) + "\n")

    print(f"\n{len(results)} pairs with any data. Best merged coverage of the "
          "1,000 most frequent source words:")
    for r in results[:80]:
        c = r["coverage"] or {}
        print(f"  {r['pair']:8} top1k {c.get('top1000', '-'):>5}  top10k "
              f"{c.get('top10000', '-'):>5}  {r['headwords']:>8} headwords  "
              f"via {', '.join(r['sources'])}")


if __name__ == "__main__":
    main()
