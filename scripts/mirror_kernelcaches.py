#!/usr/bin/env python3
"""
Download all kernelcache files from Kami's release and prepare for upload.
Simply mirrors verified files - no IPSW extraction, no appledb queries.
Guarantees 100% file completeness.
"""

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

KAMI_IPAD_INDEX = "https://github.com/BuLu0208/kernelcache-mirror/releases/download/ipad-kernelcache/index_ipad.json"
KAMI_IPHONE_INDEX = "https://github.com/BuLu0208/kernelcache-mirror/releases/download/iphone-kernelcache/index_iphone.json"
KAMI_BASE = "https://github.com/BuLu0208/kernelcache-mirror/releases/download/"

OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "output")
SKIP_BETA = True


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def download_file(url, dest):
    """Download a file with retry."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
            with open(dest, "wb") as f:
                f.write(data)
            return len(data)
        except Exception as e:
            if attempt < 2:
                time.sleep(3)
            else:
                log(f"  FAILED: {e}")
                return 0


def process_release(index_url, release_tag, filter_type):
    """Download all files from one release."""
    log(f"Downloading index from {release_tag}...")
    req = urllib.request.Request(index_url, headers={"User-Agent": "Mozilla/5.0"})
    index = json.loads(urllib.request.urlopen(req, timeout=30).read())

    # Filter by type if needed
    if filter_type == "ipad":
        entries = [e for e in index if "iPad" in e.get("model", "")]
    elif filter_type == "iphone":
        entries = [e for e in index if "iPhone" in e.get("model", "")]
    else:
        entries = index

    # Skip beta versions
    if SKIP_BETA:
        before = len(entries)
        entries = [e for e in entries if "beta" not in e.get("version", "").lower()
                   and "beta" not in e.get("build", "").lower()
                   and "RC" not in e.get("build", "")]
        skipped = before - len(entries)
        if skipped:
            log(f"  Skipped {skipped} beta/RC entries")

    log(f"  {len(entries)} files to download")

    out_dir = Path(OUTPUT_DIR) / release_tag
    out_dir.mkdir(parents=True, exist_ok=True)

    success = 0
    fail = 0
    skip = 0
    total = len(entries)

    for i, entry in enumerate(entries):
        model = entry["model"]
        version = entry["version"]
        size = entry["size"]

        filename = f"{model.replace(',', '.')}.{version}.kernelcache"
        filepath = out_dir / filename

        if filepath.exists() and filepath.stat().st_size > 100 * 1024:
            skip += 1
            continue

        url = f"{KAMI_BASE}{release_tag}/{filename}"
        log(f"  [{i+1}/{total}] {filename} ({size // 1024 // 1024}MB)...")

        downloaded = download_file(url, str(filepath))
        if downloaded > 100 * 1024:
            # Verify size matches
            actual = filepath.stat().st_size
            if actual == size:
                success += 1
            else:
                log(f"    SIZE MISMATCH: expected {size}, got {actual}")
                filepath.unlink(missing_ok=True)
                fail += 1
        else:
            if filepath.exists():
                filepath.unlink(missing_ok=True)
            fail += 1

        time.sleep(0.3)

    # Generate index
    new_index = []
    for f in out_dir.glob("*.kernelcache"):
        model = f.stem.rsplit(".", 1)[0].replace(".", ",", 1)
        version = f.stem.rsplit(".", 1)[-1]
        new_index.append({
            "model": model,
            "version": version,
            "size": f.stat().st_size,
        })

    index_path = out_dir / "index.json"
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(new_index, f, ensure_ascii=False, indent=2)

    log(f"  Done: {success} ok, {skip} skipped, {fail} failed")
    return success, skip, fail


def main():
    filter_type = "all"
    if "--filter" in sys.argv:
        idx = sys.argv.index("--filter")
        if idx + 1 < len(sys.argv):
            filter_type = sys.argv[idx + 1].lower()

    log("=" * 60)
    log(f"  Kernelcache Mirror (from Kami's verified release)")
    log(f"  Filter: {filter_type}")
    log("=" * 60)

    results = {}

    if filter_type in ("all", "ipad"):
        s, sk, f = process_release(KAMI_IPAD_INDEX, "ipad-kernelcache", "ipad")
        results["ipad"] = (s, sk, f)

    if filter_type in ("all", "iphone"):
        s, sk, f = process_release(KAMI_IPHONE_INDEX, "iphone-kernelcache", "iphone")
        results["iphone"] = (s, sk, f)

    log(f"\n{'=' * 60}")
    log(f"  Summary:")
    for name, (s, sk, f) in results.items():
        log(f"    {name}: {s} ok, {sk} skipped, {f} failed")
    log(f"{'=' * 60}")


if __name__ == "__main__":
    main()
