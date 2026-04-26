#!/usr/bin/env python3
"""
Download kernelcaches for special versions (16.4.1(a), etc.) from IPSW files.
Reads from index_special_iphone.json or index_special_ipad.json.
Extracts kernelcache from IPSW ZIP and uploads to GitHub release.
Also merges new entries into the existing index.

Usage:
  python3 download_special.py --type iphone
  python3 download_special.py --type ipad
"""

import os
import json
import struct
import zlib
import sys
import time
import requests

GITHUB_REPO = os.environ.get("GITHUB_REPO", "BuLu0208/kernelcache-mirror")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

def model_to_filename(model):
    return model.replace(",", ".")

def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)

def progress_bar(current, total, prefix=""):
    if total <= 0:
        return
    pct = float(current) / float(total)
    bar_len = 30
    filled = int(bar_len * pct)
    bar = "#" * filled + "-" * (bar_len - filled)
    mb_cur = current / 1024.0 / 1024.0
    mb_total = total / 1024.0 / 1024.0
    sys.stdout.write("\r  %s [%s] %d%% (%.1f/%.1f MB)" % (prefix, bar, int(pct*100), mb_cur, mb_total))
    sys.stdout.flush()
    if current >= total:
        print()

def find_kernelcache_in_zip(url):
    """Parse IPSW ZIP (including ZIP64) to find kernelcache entry info"""
    r = requests.head(url, timeout=30, allow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
    total = int(r.headers.get("Content-Length", 0))
    if total == 0:
        return None

    tail_size = min(131072, total)
    r = requests.get(url, headers={"Range": "bytes=%d-%d" % (total - tail_size, total - 1)},
                     timeout=60, allow_redirects=True)
    tail = r.content

    eocd_pos = tail.rfind(b'\x50\x4b\x05\x06')
    if eocd_pos == -1:
        return None

    raw_cd_off = struct.unpack_from('<I', tail, eocd_pos + 16)[0]
    raw_cd_sz = struct.unpack_from('<I', tail, eocd_pos + 12)[0]

    if raw_cd_off == 0xFFFFFFFF or raw_cd_sz == 0xFFFFFFFF:
        locator_pos = tail.rfind(b'\x50\x4b\x06\x07')
        if locator_pos == -1:
            return None
        eocd64_off = struct.unpack_from('<Q', tail, locator_pos + 8)[0]
        r = requests.get(url, headers={"Range": "bytes=%d-%d" % (eocd64_off, eocd64_off + 55)},
                         timeout=60, allow_redirects=True)
        e64 = r.content
        if e64[0:4] != b'\x50\x4b\x06\x06':
            return None
        cd_size = struct.unpack_from('<Q', e64, 40)[0]
        cd_offset = struct.unpack_from('<Q', e64, 48)[0]
    else:
        cd_offset = raw_cd_off
        cd_size = raw_cd_sz

    if cd_offset == 0 or cd_size == 0:
        return None

    log("  Downloading central directory (%d KB)..." % (cd_size / 1024))
    cd_data = b""
    pos = cd_offset
    while pos < cd_offset + cd_size:
        chunk_end = min(pos + 65536, cd_offset + cd_size)
        r = requests.get(url, headers={"Range": "bytes=%d-%d" % (pos, chunk_end - 1)},
                         timeout=60, allow_redirects=True)
        cd_data += r.content
        pos = chunk_end

    p = 0
    while p < len(cd_data) - 46:
        if cd_data[p:p+4] != b'\x50\x4b\x01\x02':
            p += 1
            continue
        try:
            (_, _, _, _, method, _, _, _,
             comp_size_raw, _, name_len, extra_len, _, _, _, _, local_off_raw) = struct.unpack_from(
                '<4sHHHHHHIIIHHHHHII', cd_data, p)
        except:
            p += 1
            continue

        if name_len == 0 or name_len > 1024:
            p += 46 + name_len + extra_len
            continue

        filename = cd_data[p+46:p+46+name_len].decode('utf-8', errors='replace')

        if 'kernelcache' not in filename.lower():
            p += 46 + name_len + extra_len
            continue

        local_off = local_off_raw
        comp_size = comp_size_raw
        if extra_len > 0:
            extra_data = cd_data[p+46+name_len:p+46+name_len+extra_len]
            ei = 0
            while ei + 4 <= len(extra_data):
                eid = struct.unpack_from('<H', extra_data, ei)[0]
                esz = struct.unpack_from('<H', extra_data, ei+2)[0]
                if eid == 0x0001:
                    off2 = ei + 4
                    if off2 + 8 <= ei + 4 + esz:
                        off2 += 8
                    if comp_size_raw == 0xFFFFFFFF and off2 + 8 <= ei + 4 + esz:
                        comp_size = struct.unpack_from('<Q', extra_data, off2)[0]
                        off2 += 8
                    if local_off_raw == 0xFFFFFFFF and off2 + 8 <= ei + 4 + esz:
                        local_off = struct.unpack_from('<Q', extra_data, off2)[0]
                ei += 4 + esz

        if comp_size == 0 or comp_size >= total:
            p += 46 + name_len + extra_len
            continue

        r = requests.get(url, headers={"Range": "bytes=%d-%d" % (local_off, local_off + 255)},
                         timeout=30, allow_redirects=True)
        lh = r.content
        lh_name_len = struct.unpack_from('<H', lh, 26)[0]
        lh_extra_len = struct.unpack_from('<H', lh, 28)[0]
        data_offset = local_off + 30 + lh_name_len + lh_extra_len

        return (filename, method, comp_size, data_offset)

    return None


def download_kernelcache_from_ipsw(ipsw_url):
    """Extract kernelcache from IPSW, return (data, size)"""
    info = find_kernelcache_in_zip(ipsw_url)
    if not info:
        return None, 0

    filename, method, comp_size, data_offset = info
    method_str = "DEFLATE" if method == 8 else "STORE"
    log("  %s (%.1f MB, %s)" % (filename, comp_size / 1024.0 / 1024.0, method_str))

    log("  Downloading kernelcache data...")
    r = requests.get(ipsw_url,
                     headers={"Range": "bytes=%d-%d" % (data_offset, data_offset + comp_size - 1)},
                     timeout=300, allow_redirects=True, stream=True)
    data = b""
    for chunk in r.iter_content(chunk_size=1024 * 1024):
        data += chunk
        progress_bar(len(data), comp_size, "DL")

    if len(data) < 100 * 1024:
        log("  Data too small (%d bytes)" % len(data))
        return None, 0

    if method == 8:
        log("  Decompressing...")
        try:
            data = zlib.decompress(data, -15)
        except:
            try:
                data = zlib.decompress(data)
            except:
                log("  Decompress failed")
                return None, 0

    return data, len(data)


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

def download_existing_index(session, tag, index_name):
    """Download existing index from release"""
    url = f"https://github.com/{GITHUB_REPO}/releases/download/{tag}/{index_name}"
    try:
        r = requests.get(url, timeout=60, allow_redirects=True)
        if r.status_code == 200:
            return r.json()
    except:
        pass
    return []

def upload_to_release(session, tag, file_path, asset_name):
    """Upload a file to a release"""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/{tag}"
    r = session.get(url, timeout=30)
    if r.status_code == 404:
        log("  Release %s not found!" % tag)
        return False
    release = r.json()
    release_id = release["id"]

    upload_url = f"https://uploads.github.com/repos/{GITHUB_REPO}/releases/{release_id}/assets"
    headers = {}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"
    headers["Content-Type"] = "application/octet-stream"

    size = os.path.getsize(file_path)
    log("  Uploading %s (%.1f MB) to %s..." % (asset_name, size / 1024.0 / 1024.0, tag))

    with open(file_path, "rb") as f:
        r = session.post(upload_url, params={"name": asset_name}, headers=headers, data=f, timeout=600)

    if r.status_code in (200, 201):
        log("  Uploaded: %s" % asset_name)
        return True
    else:
        log("  Upload failed (%d): %s" % (r.status_code, r.text[:200]))
        return False


def main():
    if "--type" not in sys.argv:
        print("Usage: python3 download_special.py --type <iphone|ipad>")
        sys.exit(1)

    idx = sys.argv.index("--type")
    device_type = sys.argv[idx + 1]

    if device_type not in ("iphone", "ipad"):
        print("Type must be 'iphone' or 'ipad'")
        sys.exit(1)

    tag = "%s-kernelcache" % device_type
    index_name = "index_%s.json" % device_type
    special_index_name = "index_special_%s.json" % device_type

    if not os.path.exists(special_index_name):
        print("%s not found! Run fetch_special.py first." % special_index_name)
        sys.exit(1)

    with open(special_index_name, "r", encoding="utf-8") as f:
        special_entries = json.load(f)

    log("Loaded %d special entries from %s" % (len(special_entries), special_index_name))

    # Check which files already exist in release
    session = get_github_session()
    existing_assets = get_release_assets(session, tag)
    log("Release '%s' has %d existing assets" % (tag, len(existing_assets)))

    # Filter out already existing files
    to_download = []
    for entry in special_entries:
        model = entry["model"]
        version = entry["version"]
        filename = "%s_%s.kernelcache" % (model_to_filename(model), version)
        if filename in existing_assets:
            log("SKIP %s - already in release" % filename)
        else:
            to_download.append(entry)

    if not to_download:
        log("All special version kernelcaches already exist in release!")
    else:
        log("Need to download %d special kernelcaches" % len(to_download))

    # Download and upload
    tmp_dir = "tmp_special"
    os.makedirs(tmp_dir, exist_ok=True)

    uploaded_entries = []
    success = 0
    failed = 0

    for i, entry in enumerate(to_download):
        model = entry["model"]
        version = entry["version"]
        build = entry["build"]
        ipsw_url = entry["ipsw_url"]
        filename = "%s_%s.kernelcache" % (model_to_filename(model), version)
        tmp_path = os.path.join(tmp_dir, filename)

        log("[%d/%d] %s %s (%s)" % (i + 1, len(to_download), model, version, build))

        data, size = download_kernelcache_from_ipsw(ipsw_url)
        if data and size > 100 * 1024:
            with open(tmp_path, "wb") as f:
                f.write(data)
            log("  Extracted: %.1f MB" % (size / 1024.0 / 1024.0))

            if upload_to_release(session, tag, tmp_path, filename):
                success += 1
                # Record for index update
                idx_entry = {
                    "model": model,
                    "version": version,
                    "build": build,
                    "url": entry["url"],
                    "size": size,
                }
                uploaded_entries.append(idx_entry)
            else:
                failed += 1

            os.remove(tmp_path)
        else:
            log("  Failed to extract kernelcache")
            failed += 1

        time.sleep(0.5)

    # Cleanup tmp dir
    for f in os.listdir(tmp_dir):
        os.remove(os.path.join(tmp_dir, f))
    os.rmdir(tmp_dir)

    # Merge into existing index
    if uploaded_entries:
        log("\nMerging %d new entries into %s..." % (len(uploaded_entries), index_name))
        existing_index = download_existing_index(session, tag, index_name)
        if not existing_index:
            log("WARNING: Could not download existing %s, starting fresh" % index_name)

        # Remove ipsw_url from entries (not needed in final index)
        # and add to existing
        existing_models = set()
        for e in existing_index:
            existing_models.add((e["model"], e["version"]))

        added = 0
        for entry in uploaded_entries:
            key = (entry["model"], entry["version"])
            if key not in existing_models:
                existing_index.append(entry)
                existing_models.add(key)
                added += 1

        log("Added %d new entries (total now %d)" % (added, len(existing_index)))

        # Save and upload updated index
        merged_path = os.path.join(tmp_dir if os.path.exists(tmp_dir) else ".", index_name)
        os.makedirs(os.path.dirname(merged_path), exist_ok=True)
        with open(merged_path, "w", encoding="utf-8") as f:
            json.dump(existing_index, f, ensure_ascii=False, indent=2)

        upload_to_release(session, tag, merged_path, index_name)
        log("Uploaded updated %s" % index_name)
        os.remove(merged_path)
    else:
        log("\nNo new entries to merge into index")

    log("")
    log("=" * 55)
    log("Done! Uploaded: %d, Failed: %d" % (success, failed))
    log("=" * 55)

    # Save failed list
    if failed > 0:
        failed_entries = []
        for entry in to_download:
            filename = "%s_%s.kernelcache" % (model_to_filename(entry["model"]), entry["version"])
            if filename not in existing_assets:
                failed_entries.append(entry)
        if failed_entries:
            with open("failed_special_%s.json" % device_type, "w", encoding="utf-8") as f:
                json.dump(failed_entries, f, ensure_ascii=False, indent=2)
            log("Failed entries saved to failed_special_%s.json" % device_type)


if __name__ == "__main__":
    main()
