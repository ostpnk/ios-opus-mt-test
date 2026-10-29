#!/usr/bin/env python3
"""Publishes dict_bundles.py's output across releases <tag>-p1, <tag>-p2, ...
(parts.json says which bundles go in which; every part also gets
catalog.json and SHA256SUMS). Resumable: a file already on its release with
the same SHA-256 (GitHub's asset digest) is skipped, a different one
replaced, so rerunning after a failure uploads only what's missing or
changed. Uploads are paced and retried with backoff, since GitHub's
secondary rate limit stopped a run after ~1,500 quick uploads.

Usage (in the build's out directory, GH_TOKEN set):
  python3 -I publish_parts.py --repo OWNER/REPO --tag TAG --target SHA
"""

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

NOTES = ("Bilingual dictionaries and inflected-forms packs built from Wiktionary via "
         "kaikki.org (wiktextract), CC BY-SA 4.0 — credit the Wiktionary contributors. "
         "catalog.json (in every part) lists all parts' files for core_dictionary.")
PAUSE = 2  # seconds between uploads
BACKOFF = [60, 120, 240, 480, 600, 600, 600, 600]


def gh(*args, check=True):
    return subprocess.run(["gh", *args], check=check, capture_output=True, text=True)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def retried(what, *args):
    for wait in [*BACKOFF, None]:
        result = gh(*args, check=False)
        if result.returncode == 0:
            return result
        print(f"  {what} failed: {result.stderr.strip()[:200]}", flush=True)
        if wait is None:
            sys.exit(f"giving up on {what}")
        time.sleep(wait)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--target", required=True)
    args = parser.parse_args()

    parts = json.loads(Path("parts.json").read_text())
    for part, files in parts.items():
        tag = f"{args.tag}-p{part}"
        if gh("release", "view", tag, "--repo", args.repo, check=False).returncode != 0:
            retried(f"creating {tag}", "release", "create", tag, "--repo", args.repo,
                    "--target", args.target,
                    "--title", f"Dictionaries {args.tag} (part {part})", "--notes", NOTES)
        release_id = retried(f"reading {tag}", "api", f"repos/{args.repo}/releases/tags/{tag}",
                             "--jq", ".id").stdout.strip()
        listed = retried(f"listing {tag}", "api", "--paginate",
                         f"repos/{args.repo}/releases/{release_id}/assets?per_page=100",
                         "--jq", '.[] | "\\(.name) \\(.state) \\(.digest)"').stdout
        existing = {}
        for line in listed.splitlines():
            name, state, digest = line.split(" ")
            if state == "uploaded":
                existing[name] = digest
        paths = [Path("bundles") / f for f in files] + [Path("catalog.json"), Path("SHA256SUMS")]
        skipped = uploaded = 0
        for path in paths:
            if existing.get(path.name) == f"sha256:{sha256(path)}":
                skipped += 1
                continue
            retried(f"uploading {path.name}", "release", "upload", tag, str(path),
                    "--repo", args.repo, "--clobber")
            uploaded += 1
            time.sleep(PAUSE)
        print(f"{tag}: {uploaded} uploaded, {skipped} already there", flush=True)


if __name__ == "__main__":
    main()
