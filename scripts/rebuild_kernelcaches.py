#!/usr/bin/env python3
"""
Rebuild kernelcache files from Apple IPSW.
Queries appledb for firmware metadata, extracts kernelcaches via HTTP Range requests.
Only downloads the kernelcache portion (~20MB) from each IPSW (~5GB).

Usage:
  python3 scripts/rebuild_kernelcaches.py [--filter ipad|iphone] [--min-version X.Y]

Environment:
  OUTPUT_DIR: output directory (default: output)
"""

import json
import os
import struct
import sys
import time
import urllib.request
import urllib.error
import zipfile
from io import BytesIO
from pathlib import Path

OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "output")
APPLEDB_URL = "https://api.appledb.dev/main.json"
MIN_VERSION = None
MAX_VERSION = None
FILTER = None  # "ipad" or "iphone"
CHUNK_SIZE = 1024 * 1024  # 1MB chunks
REQUEST_TIMEOUT = 60
HEAD_TIMEOUT = 30


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# HTTP helpers (no external dependencies)
# ---------------------------------------------------------------------------

def http_get(url, timeout=REQUEST_TIMEOUT):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http_head(url, timeout=HEAD_TIMEOUT):
    req = urllib.request.Request(url, method="HEAD", headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return dict(r.headers)
    except urllib.error.HTTPError:
        return {}


def http_range(url, start, end, timeout=REQUEST_TIMEOUT):
    """Download a byte range."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        "Range": f"bytes={start}-{end - 1}"
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ---------------------------------------------------------------------------
# ZIP64 Range-based file extraction (no full IPSW download)
# ---------------------------------------------------------------------------

class ZipRangeFile:
    """A file-like object that reads from a remote ZIP via HTTP Range requests.
    Works with Python's zipfile.ZipFile for reading entries."""

    def __init__(self, url, file_size):
        self._url = url
        self._file_size = file_size
        self._pos = 0
        self._cache = {}  # chunk_start -> bytes

    def read(self, size=-1):
        if size is None or size < 0:
            size = self._file_size - self._pos
        result = b""
        remaining = size
        while remaining > 0 and self._pos < self._file_size:
            chunk_start = (self._pos // CHUNK_SIZE) * CHUNK_SIZE
            if chunk_start not in self._cache:
                chunk_end = min(chunk_start + CHUNK_SIZE, self._file_size)
                log(f"    Downloading chunk {chunk_start}-{chunk_end} ({(chunk_end - chunk_start) // 1024 // 1024}MB)...")
                self._cache[chunk_start] = http_range(self._url, chunk_start, chunk_end)
                # Evict old chunks to save memory (keep last 5)
                keys = sorted(self._cache.keys())
                while len(keys) > 5:
                    del self._cache[keys.pop(0)]
            chunk = self._cache[chunk_start]
            offset_in_chunk = self._pos - chunk_start
            available = min(len(chunk) - offset_in_chunk, remaining)
            result += chunk[offset_in_chunk:offset_in_chunk + available]
            self._pos += available
            remaining -= available
        return result

    def seek(self, offset, whence=0):
        if whence == 0:
            self._pos = offset
        elif whence == 1:
            self._pos += offset
        elif whence == 2:
            self._pos = self._file_size + offset
        return self._pos

    def tell(self):
        return self._pos

    def seekable(self):
        return True

    def readable(self):
        return True


def extract_kernelcache_from_ipsw(url):
    """Extract all kernelcache entries from an IPSW ZIP.
    Returns list of (filename, data) tuples."""
    try:
        headers = http_head(url)
        file_size = int(headers.get("Content-Length", 0))
        if file_size == 0:
            log(f"    HEAD failed, trying GET for file size...")
            # Some CDNs don't support HEAD, skip size check
            return []
    except Exception as e:
        log(f"    HEAD error: {e}")
        return []

    log(f"    IPSW size: {file_size // 1024 // 1024}MB")

    try:
        zf = ZipRangeFile(url, file_size)
        z = zipfile.ZipFile(zf)
        results = []
        for info in z.infolist():
            name = info.filename.lower()
            if "kernelcache" not in name or info.is_dir():
                continue
            log(f"    Extracting: {info.filename} ({info.file_size // 1024 // 1024}MB compressed: {info.compress_size // 1024 // 1024}MB)")
            data = z.read(info.filename)
            if len(data) > 100 * 1024:  # At least 100KB
                results.append((info.filename, data))
            else:
                log(f"    Skipping (too small: {len(data)} bytes)")
        return results
    except Exception as e:
        log(f"    ZIP extraction error: {e}")
        return []


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------

def parse_version(v):
    """Parse version string to tuple for comparison."""
    parts = v.split(".")
    result = []
    for p in parts:
        try:
            result.append(int(p))
        except ValueError:
            result.append(0)
    return tuple(result)


def version_in_range(version, min_v, max_v):
    v = parse_version(version)
    if min_v and v < parse_version(min_v):
        return False
    if max_v and v > parse_version(max_v):
        return False
    return True


# ---------------------------------------------------------------------------
# Device matching
# ---------------------------------------------------------------------------

def match_kernel_to_devices(kernel_filename, device_identifiers):
    """
    Match a kernelcache filename to the correct device identifiers.
    kernel_filename: e.g. 'kernelcache.release.ipad6b'
    device_identifiers: e.g. ['iPad6,3', 'iPad6,4', 'iPad6,7', 'iPad6,8']
    Returns list of matching device identifiers.
    """
    # Extract platform name: kernelcache.release.<platform>
    name = kernel_filename.split("/")[-1]
    # Remove known prefixes
    for prefix in ["kernelcache.release.", "kernelcache.development.", "kernelcache.debug."]:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    else:
        return []

    # Extract platform number (e.g., 'ipad6b' -> '6', 'ipad14p' -> '14')
    import re
    m = re.match(r"ipad(\d+)", name)
    if not m:
        return []
    platform_num = int(m.group(1))

    # For devices like iPad14,1 - the model number before comma
    matched = []
    for dev_id in device_identifiers:
        parts = dev_id.replace("iPad", "").split(",")
        try:
            dev_num = int(parts[0])
        except (ValueError, IndexError):
            continue
        if dev_num == platform_num:
            matched.append(dev_id)

    return matched if matched else device_identifiers  # Fallback to all


# ---------------------------------------------------------------------------
# Main: query appledb, extract, save
# ---------------------------------------------------------------------------

def main():
    global MIN_VERSION, MAX_VERSION, FILTER

    # Parse arguments
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--filter" and i + 1 < len(args):
            FILTER = args[i + 1].lower()
            i += 2
        elif args[i] == "--min-version" and i + 1 < len(args):
            MIN_VERSION = args[i + 1]
            i += 2
        elif args[i] == "--max-version" and i + 1 < len(args):
            MAX_VERSION = args[i + 1]
            i += 2
        else:
            i += 1

    log("=" * 60)
    log("  Kernelcache Rebuilder")
    log(f"  Filter: {FILTER or 'all'}")
    log(f"  Version range: {MIN_VERSION or 'any'} - {MAX_VERSION or 'any'}")
    log("=" * 60)

    # Step 1: Query appledb
    log("Querying appledb API...")
    appledb = json.loads(http_get(APPLEDB_URL))

    ios_entries = appledb.get("ios", [])
    if FILTER == "ipad":
        ios_entries = [e for e in ios_entries if e.get("osStr") == "iPadOS"]
    elif FILTER == "iphone":
        ios_entries = [e for e in ios_entries if e.get("osStr") == "iOS"]

    # Filter versions
    if MIN_VERSION or MAX_VERSION:
        ios_entries = [e for e in ios_entries if version_in_range(e.get("version", ""), MIN_VERSION, MAX_VERSION)]

    # Deduplicate by (version, build)
    seen = set()
    firmwares = []
    for entry in ios_entries:
        v = entry.get("version", "")
        b = entry.get("build", "")
        key = (v, b)
        if key in seen:
            continue
        seen.add(key)

        # Find IPSW sources
        sources = entry.get("sources", [])
        ipsw_sources = [s for s in sources if s.get("type") == "ipsw"]
        if not ipsw_sources:
            continue

        # Pick the best source (no auth, active, with deviceMap)
        best = None
        for s in ipsw_sources:
            links = s.get("links", [])
            active_links = [l for l in links if not l.get("auth") and l.get("active")]
            if active_links and s.get("deviceMap"):
                best = {
                    "url": active_links[0]["url"],
                    "devices": s["deviceMap"],
                    "version": v,
                    "build": b,
                }
                break  # Use the first valid one

        if best:
            firmwares.append(best)

    # Sort by version
    firmwares.sort(key=lambda x: parse_version(x["version"]))

    log(f"Found {len(firmwares)} firmware entries to process")

    # Step 2: Extract kernelcaches
    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(exist_ok=True)

    index = []
    total = len(firmwares)
    success = 0
    fail = 0
    skip = 0

    for idx, fw in enumerate(firmwares):
        version = fw["version"]
        build = fw["build"]
        devices = fw["devices"]
        url = fw["url"]

        log(f"\n[{idx + 1}/{total}] {version} ({build}) - {len(devices)} devices")

        if not version_in_range(version, MIN_VERSION, MAX_VERSION):
            log(f"  SKIP (out of version range)")
            continue

        # Extract kernelcaches from IPSW
        kernels = extract_kernelcache_from_ipsw(url)
        if not kernels:
            log(f"  FAIL: no kernelcache found")
            fail += 1
            time.sleep(1)
            continue

        # Match kernels to devices
        if len(kernels) == 1:
            # Simple case: one kernel for all devices
            kernel_name, kernel_data = kernels[0]
            for dev_id in devices:
                filename = f"{dev_id.replace(',', '.')}.{version}.kernelcache"
                filepath = out_dir / filename
                if filepath.exists() and filepath.stat().st_size > 100 * 1024:
                    skip += 1
                    continue
                filepath.write_bytes(kernel_data)
                sz = len(kernel_data)
                index.append({"model": dev_id, "version": version, "size": sz})
                log(f"  SAVED: {filename} ({sz // 1024 // 1024}MB)")
                success += 1
        else:
            # Multiple kernels: match by platform number
            log(f"  Multiple kernels found: {[k[0].split('/')[-1] for k in kernels]}")
            assigned_devices = set()
            for kernel_name, kernel_data in kernels:
                matched = match_kernel_to_devices(kernel_name, devices)
                # Remove already assigned devices
                matched = [d for d in matched if d not in assigned_devices]
                if not matched:
                    continue
                for dev_id in matched:
                    assigned_devices.add(dev_id)
                    filename = f"{dev_id.replace(',', '.')}.{version}.kernelcache"
                    filepath = out_dir / filename
                    if filepath.exists() and filepath.stat().st_size > 100 * 1024:
                        skip += 1
                        continue
                    filepath.write_bytes(kernel_data)
                    sz = len(kernel_data)
                    index.append({"model": dev_id, "version": version, "size": sz})
                    log(f"  SAVED: {filename} ({sz // 1024 // 1024}MB)")
                    success += 1

            # Any unassigned devices: use the first kernel as fallback
            unassigned = [d for d in devices if d not in assigned_devices]
            if unassigned:
                log(f"  WARNING: {len(unassigned)} devices unassigned, using first kernel as fallback")
                for dev_id in unassigned:
                    filename = f"{dev_id.replace(',', '.')}.{version}.kernelcache"
                    filepath = out_dir / filename
                    if filepath.exists() and filepath.stat().st_size > 100 * 1024:
                        skip += 1
                        continue
                    filepath.write_bytes(kernels[0][1])
                    sz = len(kernels[0][1])
                    index.append({"model": dev_id, "version": version, "size": sz})
                    log(f"  SAVED: {filename} ({sz // 1024 // 1024}MB) [fallback]")
                    success += 1

        time.sleep(0.5)

    # Step 3: Generate index
    index_path = out_dir / "index.json"
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    # Summary
    existing = list(out_dir.glob("*.kernelcache"))
    log(f"\n{'=' * 60}")
    log(f"  Done! Success: {success}, Skip: {skip}, Fail: {fail}")
    log(f"  Total files in output: {len(existing)}")
    log(f"  Index: {len(index)} entries")
    log(f"{'=' * 60}")


if __name__ == "__main__":
    main()
