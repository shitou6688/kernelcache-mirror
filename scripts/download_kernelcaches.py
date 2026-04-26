#!/usr/bin/env python3
"""
Download kernelcache files from Apple CDN using firmware_list.json
Reads index files to determine which kernelcaches to download for each release.
Usage: python download_kernelcaches.py --filter <release_tag>

Environment variable:
  GITHUB_TOKEN: GitHub personal access token (optional, for higher API rate limits)
"""

import json
import os
import sys
import time
import requests
from pathlib import Path

GITHUB_REPO = os.environ.get("GITHUB_REPO", "BuLu0208/kernelcache-mirror")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# Comma in model names (e.g. iPhone14,5) gets replaced with dot in filenames
def model_to_filename(model):
    return model.replace(",", ".")

def get_github_session():
    s = requests.Session()
    if GITHUB_TOKEN:
        s.headers["Authorization"] = f"token {GITHUB_TOKEN}"
    return s

def get_release_id(session, tag):
    """Get release ID for a given tag."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/{tag}"
    r = session.get(url, timeout=30)
    if r.status_code == 404:
        print(f"Release '{tag}' not found, creating...")
        return create_release(session, tag)
    r.raise_for_status()
    return r.json()["id"]

def create_release(session, tag):
    """Create a new release."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases"
    data = {
        "tag_name": tag,
        "name": tag,
        "body": f"Automated kernelcache release for {tag}",
        "draft": False,
        "prerelease": False,
    }
    r = session.post(url, json=data, timeout=30)
    r.raise_for_status()
    return r.json()["id"]

def get_existing_assets(session, release_id):
    """Get list of existing asset names in a release."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/{release_id}/assets"
    r = session.get(url, timeout=30)
    r.raise_for_status()
    return {a["name"]: a["id"] for a in r.json()}

def download_kernelcache(url, dest_path):
    """Download a single kernelcache file."""
    try:
        r = requests.get(url, timeout=300, stream=True)
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        return True
    except Exception as e:
        print(f"  Download failed: {e}")
        if os.path.exists(dest_path):
            os.remove(dest_path)
        return False

def delete_asset(session, asset_id):
    """Delete an existing asset from a release."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/assets/{asset_id}"
    r = session.delete(url, timeout=30)
    return r.status_code in (204, 200)

def upload_asset(session, release_id, file_path, asset_name):
    """Upload a file as a release asset."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/{release_id}/assets"
    params = {"name": asset_name}
    headers = {}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"
    
    size = os.path.getsize(file_path)
    print(f"  Uploading {asset_name} ({size / 1024 / 1024:.1f} MB)...")
    
    with open(file_path, "rb") as f:
        r = session.post(url, params=params, headers=headers, data=f, timeout=600)
    
    if r.status_code in (200, 201):
        print(f"  Uploaded: {asset_name}")
        return True
    else:
        print(f"  Upload failed ({r.status_code}): {r.text[:200]}")
        return False

def process_release(session, index_file, release_tag):
    """Process a single release tag."""
    print(f"\n{'='*60}")
    print(f"Processing: {release_tag}")
    print(f"{'='*60}")
    
    if not os.path.exists(index_file):
        print(f"Index file not found: {index_file}")
        return
    
    with open(index_file, "r", encoding="utf-8") as f:
        entries = json.load(f)
    
    print(f"Loaded {len(entries)} entries from {index_file}")
    
    release_id = get_release_id(session, release_tag)
    existing = get_existing_assets(session, release_id)
    print(f"Release ID: {release_id}, existing assets: {len(existing)}")
    
    tmp_dir = Path("tmp_kernelcache")
    tmp_dir.mkdir(exist_ok=True)
    
    success = 0
    skipped = 0
    failed = 0
    
    for i, entry in enumerate(entries):
        model = entry.get("model", "")
        version = entry.get("version", "")
        build = entry.get("build", "")
        url = entry.get("url", "")
        filename = f"{model_to_filename(model)}_{version}_{build}.kernelcache"
        
        if not url:
            print(f"[{i+1}/{len(entries)}] SKIP {model} {version} ({build}) - no URL")
            skipped += 1
            continue
        
        if filename in existing:
            print(f"[{i+1}/{len(entries)}] SKIP {filename} - already exists")
            skipped += 1
            continue
        
        tmp_path = tmp_dir / filename
        
        print(f"[{i+1}/{len(entries)}] {model} {version} ({build})")
        
        if download_kernelcache(url, str(tmp_path)):
            if upload_asset(session, release_id, str(tmp_path), filename):
                success += 1
            else:
                failed += 1
            tmp_path.unlink(missing_ok=True)
        else:
            failed += 1
        
        # Rate limiting
        time.sleep(1)
    
    # Cleanup
    for f in tmp_dir.iterdir():
        f.unlink(missing_ok=True)
    tmp_dir.rmdir()
    
    print(f"\n{release_tag} done: {success} uploaded, {skipped} skipped, {failed} failed")
    return success, skipped, failed

def main():
    filter_tag = None
    if "--filter" in sys.argv:
        idx = sys.argv.index("--filter")
        if idx + 1 < len(sys.argv):
            filter_tag = sys.argv[idx + 1]
    
    releases = [
        {"index": "index_iphone.json", "tag": "iphone-kernelcache"},
        {"index": "index_ipad.json", "tag": "ipad-kernelcache"},
    ]
    
    if filter_tag:
        releases = [r for r in releases if r["tag"] == filter_tag]
        if not releases:
            print(f"No matching release for filter: {filter_tag}")
            print(f"Available: {[r['tag'] for r in releases]}")
            sys.exit(1)
        print(f"Filter: only processing '{filter_tag}'")
    
    session = get_github_session()
    
    total_success = 0
    total_skipped = 0
    total_failed = 0
    
    for rel in releases:
        s, sk, f = process_release(session, rel["index"], rel["tag"])
        total_success += s
        total_skipped += sk
        total_failed += f
    
    print(f"\n{'='*60}")
    print(f"Total: {total_success} uploaded, {total_skipped} skipped, {total_failed} failed")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()
