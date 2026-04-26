#!/usr/bin/env python3
"""
Add special version support to kernelcache releases.

iOS Rapid Security Response versions like 16.4.1(a) use the same kernelcache
as their base version. This script:
1. Finds special versions from appledb
2. Downloads the base version kernelcache from the existing release
3. Re-uploads it with the special version filename
4. Adds alias entries to the index

Usage:
  python3 add_special_aliases.py --type iphone
  python3 add_special_aliases.py --type ipad
"""

import json
import lzma
import os
import re
import sys
import time
import requests

API_BASE = "https://api.appledb.dev/ios/main.json.xz"
GITHUB_REPO = os.environ.get("GITHUB_REPO", "BuLu0208/kernelcache-mirror")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
BASE_RELEASE_URL = f"https://github.com/{GITHUB_REPO}/releases/download"


def model_to_filename(model):
    return model.replace(",", ".")


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def get_github_session():
    s = requests.Session()
    if GITHUB_TOKEN:
        s.headers["Authorization"] = f"token {GITHUB_TOKEN}"
    s.headers["User-Agent"] = "github-actions"
    return s


def get_release_assets(session, tag):
    """Get existing asset names in a release"""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/{tag}"
    r = session.get(url, timeout=30)
    if r.status_code == 404:
        return {}
    r.raise_for_status()
    assets = {}
    for a in r.json().get("assets", []):
        assets[a["name"]] = a["id"]
    return assets


def download_index(tag, index_name):
    """Download existing index from GitHub release"""
    url = f"{BASE_RELEASE_URL}/{tag}/{index_name}"
    try:
        r = requests.get(url, timeout=60, allow_redirects=True)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log(f"  Warning: Could not download {index_name}: {e}")
    return []


def upload_to_release(session, tag, file_path, asset_name):
    """Upload a file to a GitHub release"""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/{tag}"
    r = session.get(url, timeout=30)
    if r.status_code == 404:
        log(f"  Release {tag} not found!")
        return False
    release = r.json()
    release_id = release["id"]

    upload_url = f"https://uploads.github.com/repos/{GITHUB_REPO}/releases/{release_id}/assets"
    headers = {}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"
    headers["Content-Type"] = "application/octet-stream"

    size = os.path.getsize(file_path)
    log(f"  Uploading {asset_name} ({size / 1024.0 / 1024.0:.1f} MB)...")

    with open(file_path, "rb") as f:
        r = session.post(upload_url, params={"name": asset_name}, headers=headers, data=f, timeout=600)

    if r.status_code in (200, 201):
        log(f"  Uploaded: {asset_name}")
        return True
    else:
        log(f"  Upload failed ({r.status_code}): {r.text[:200]}")
        return False


def main():
    if "--type" not in sys.argv:
        print("Usage: python3 add_special_aliases.py --type <iphone|ipad>")
        sys.exit(1)

    idx = sys.argv.index("--type")
    device_type = sys.argv[idx + 1]
    if device_type not in ("iphone", "ipad"):
        print("Type must be 'iphone' or 'ipad'")
        sys.exit(1)

    tag = f"{device_type}-kernelcache"
    index_name = f"index_{device_type}.json"

    session = get_github_session()

    # Step 1: Check existing assets
    log(f"Checking existing assets in release {tag}...")
    existing_assets = get_release_assets(session, tag)
    log(f"Release has {len(existing_assets)} existing assets")

    # Step 2: Download existing index
    log(f"Downloading existing {index_name}...")
    existing_index = download_index(tag, index_name)
    if not existing_index:
        log(f"ERROR: Could not download existing {index_name}")
        sys.exit(1)
    log(f"Loaded {len(existing_index)} existing index entries")

    # Build lookup: (model, base_version) -> entry
    version_lookup = {}
    for entry in existing_index:
        key = (entry["model"], entry["version"])
        if key not in version_lookup:
            version_lookup[key] = entry

    # Step 3: Download appledb to find special versions
    log("Downloading firmware list from appledb...")
    r = requests.get(API_BASE, timeout=120)
    data = lzma.decompress(r.content)
    fw_list = json.loads(data)
    log(f"Parsed {len(fw_list)} firmware entries")

    # Step 4: Find all special versions and their prerequisite builds
    # appledb format: version "16.4.1 (a)" with prerequisiteBuild "20E252"
    special_map = {}  # (model, special_version) -> base_build

    for fw in fw_list:
        v = str(fw.get("version") or fw.get("osStr", ""))
        ot = fw.get("osType", "")
        if ot not in ("iOS", "iPadOS"):
            continue

        # Match " (a)", " (b)", " (c)" at end of version string
        if not re.search(r'\s\([a-z]\)\s*$', v):
            continue

        for source in fw.get("sources", []):
            prereq_build = source.get("prerequisiteBuild", "")
            if not prereq_build:
                continue

            models = source.get("deviceMap", [])
            for model in models:
                if device_type == "iphone" and not model.startswith("iPhone"):
                    continue
                if device_type == "ipad" and not model.startswith("iPad"):
                    continue

                key = (model, v)
                if key not in special_map:
                    special_map[key] = prereq_build

    log(f"Found {len(special_map)} special version entries from appledb")

    # Step 5: Build (model, build) -> base_version lookup from index
    build_to_version = {}
    for entry in existing_index:
        key = (entry["model"], entry["build"])
        if key not in build_to_version:
            build_to_version[key] = entry

    # Step 6: Create entries and upload files
    new_entries = []
    existing_index_keys = set()
    for entry in existing_index:
        existing_index_keys.add((entry["model"], entry["version"]))

    tmp_dir = "/tmp/special_kernelcache"
    os.makedirs(tmp_dir, exist_ok=True)

    upload_count = 0
    skip_count = 0
    not_found = 0

    for (model, special_version), base_build in sorted(special_map.items()):
        if (model, special_version) in existing_index_keys:
            skip_count += 1
            continue

        # Find base version entry by build
        base_entry = build_to_version.get((model, base_build))
        if not base_entry:
            log(f"  WARNING: No base entry for {model} build {base_build} (special: {special_version})")
            not_found += 1
            continue

        base_version = base_entry["version"]
        special_filename = f"{model_to_filename(model)}_{special_version}.kernelcache"
        base_filename = f"{model_to_filename(model)}_{base_version}.kernelcache"

        # Check if file already in release
        if special_filename in existing_assets:
            log(f"  SKIP {special_filename} - already in release")
            skip_count += 1
            continue

        # Download base kernelcache from release
        log(f"  {model} {special_version} <- {base_version} (build {base_build})")
        download_url = f"{BASE_RELEASE_URL}/{tag}/{base_filename}"
        tmp_path = os.path.join(tmp_dir, special_filename)

        try:
            r = requests.get(download_url, timeout=120, allow_redirects=True)
            if r.status_code != 200:
                log(f"    Failed to download base: {r.status_code}")
                not_found += 1
                continue

            with open(tmp_path, "wb") as f:
                f.write(r.content)

            # Upload with special version filename
            if upload_to_release(session, tag, tmp_path, special_filename):
                upload_count += 1
                existing_assets[special_filename] = True

                # Create index entry
                proxy_url = f"https://github.lengye.top/download/{tag}/{special_filename}"
                new_entries.append({
                    "model": model,
                    "version": special_version,
                    "build": base_entry["build"],
                    "url": proxy_url,
                    "size": len(r.content),
                })
            else:
                not_found += 1

            os.remove(tmp_path)
        except Exception as e:
            log(f"    Error: {e}")
            not_found += 1

    # Cleanup tmp dir
    for f in os.listdir(tmp_dir):
        os.remove(os.path.join(tmp_dir, f))
    os.rmdir(tmp_dir)

    # Step 7: Update index
    if new_entries:
        log(f"\nUpdating {index_name} with {len(new_entries)} new entries...")
        merged_index = existing_index + new_entries

        tmp_index = f"/tmp/{index_name}"
        with open(tmp_index, "w", encoding="utf-8") as f:
            json.dump(merged_index, f, ensure_ascii=False, indent=2)

        if upload_to_release(session, tag, tmp_index, index_name):
            log(f"SUCCESS: Updated {index_name} ({len(existing_index)} -> {len(merged_index)} entries)")
        else:
            log(f"WARNING: Failed to upload updated {index_name}")
        os.remove(tmp_index)
    else:
        log("\nNo new entries to add to index")

    # Summary
    versions_added = set()
    for entry in new_entries:
        versions_added.add(entry["version"])

    log("")
    log("=" * 55)
    log(f"Done!")
    log(f"  Uploaded: {upload_count} kernelcache files")
    log(f"  Skipped (already exists): {skip_count}")
    log(f"  Not found: {not_found}")
    if versions_added:
        log(f"  Versions added: {', '.join(sorted(versions_added))}")
    log("=" * 55)


if __name__ == "__main__":
    main()
