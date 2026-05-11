#!/usr/bin/env python3
"""
Rebuild kernelcache files from Apple IPSW.
Queries appledb for firmware metadata, extracts kernelcaches via HTTP Range requests.
Only downloads the kernelcache portion (~20MB) from each IPSW (~5GB).

FIXED: Uses BuildManifest DeviceClass directly instead of the incorrect BOARD_TO_DEVICE reverse mapping.
Each model gets its correct kernelcache file based on the BuildManifest's KernelCache→Info→Path per DeviceClass.

Usage:
  python3 scripts/rebuild_kernelcaches.py [--filter ipad|iphone] [--min-version X.Y] [--max-version X.Y]

Environment:
  OUTPUT_DIR: output directory (default: output)
"""

import json
import os
import plistlib
import re
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


def extract_files_from_ipsw(url, file_size, target_filenames=None):
    """Extract specific files from an IPSW ZIP.
    If target_filenames is None, extract BuildManifest + all kernelcache files.
    Returns dict: filename -> data bytes.
    """
    try:
        zf = ZipRangeFile(url, file_size)
        z = zipfile.ZipFile(zf)
        results = {}
        for info in z.infolist():
            if info.is_dir():
                continue
            name_lower = info.filename.lower()

            if target_filenames:
                # Only extract specified files
                for tf in target_filenames:
                    if tf.lower() in name_lower or name_lower.endswith(tf.lower()):
                        log(f"    Extracting: {info.filename} ({info.file_size // 1024 // 1024}MB)")
                        data = z.read(info.filename)
                        if len(data) > 100 * 1024:
                            results[info.filename] = data
                        break
            else:
                # Extract BuildManifest + kernelcaches
                if "buildmanifest" in name_lower or "kernelcache" in name_lower:
                    log(f"    Extracting: {info.filename} ({info.file_size // 1024 // 1024}MB)")
                    data = z.read(info.filename)
                    if len(data) > 100 * 1024:
                        results[info.filename] = data
                    elif "buildmanifest" in name_lower:
                        results[info.filename] = data  # Keep even if small
        return results
    except Exception as e:
        log(f"    ZIP extraction error: {e}")
        return {}


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
# BuildManifest-based kernel matching (CORRECT approach)
# ---------------------------------------------------------------------------

def parse_buildmanifest(bm_data):
    """Parse BuildManifest.plist bytes.
    Returns: {DeviceClass: KernelCachePath} mapping for non-Research variants.
    """
    bm = plistlib.loads(bm_data)
    device_to_kc = {}
    for bid in bm.get("BuildIdentities", []):
        info = bid.get("Info", {})
        variant = info.get("Variant", "")
        if variant.startswith("Research"):
            continue

        dc = info.get("DeviceClass", "")
        if not dc:
            continue

        manifest = bid.get("Manifest", {})
        kpath = None
        for key in ("KernelCache", "RestoreKernelCache"):
            if key in manifest:
                kpath = manifest[key].get("Info", {}).get("Path", "")
                if kpath:
                    break

        if kpath:
            # Normalize DeviceClass: appledb uses "iPad8,9", BuildManifest uses "ipad8,9"
            # Capitalize first letter to get standard model identifier
            dc_norm = dc[0].upper() + dc[1:] if dc else dc
            device_to_kc[dc_norm] = kpath

    return device_to_kc


def match_kernels_to_devices(devices, device_to_kc_path, kernel_data_map):
    """
    Match kernelcache data to device identifiers using BuildManifest DeviceClass mapping.

    This is the CORRECT approach: BuildManifest's DeviceClass directly tells us
    which model identifier each kernelcache belongs to. No reverse boardconfig lookup needed.

    Args:
        devices: list of device identifiers from appledb e.g. ['iPad8,9', 'iPad8,10']
        device_to_kc_path: {DeviceClass: KernelCachePath} from BuildManifest
        kernel_data_map: {kernel_filename_basename: data_bytes}

    Returns: dict {device_id: kernel_data}
    """
    if not device_to_kc_path or not kernel_data_map:
        return _fallback_match(devices, kernel_data_map)

    result = {}
    for dev_id in devices:
        if dev_id in device_to_kc_path:
            kc_path = device_to_kc_path[dev_id]
            kc_basename = kc_path.split("/")[-1]
            if kc_basename in kernel_data_map:
                result[dev_id] = kernel_data_map[kc_basename]
                continue

        # Device not in BuildManifest (shouldn't happen normally)
        log(f"    WARNING: {dev_id} not found in BuildManifest, using fallback")
        fallback = _fallback_match_single(dev_id, kernel_data_map)
        if fallback:
            result[dev_id] = fallback
        elif kernel_data_map:
            # Last resort: first kernel
            result[dev_id] = list(kernel_data_map.values())[0]

    return result


def _fallback_match(devices, kernel_data_map):
    """Fallback: match by platform number only (e.g., ipad8 matches ipad8, ipad8b)."""
    result = {}
    for dev_id in devices:
        kdata = _fallback_match_single(dev_id, kernel_data_map)
        if kdata:
            result[dev_id] = kdata
        elif kernel_data_map:
            result[dev_id] = list(kernel_data_map.values())[0]
    return result


def _fallback_match_single(dev_id, kernel_data_map):
    """Match a single device to kernel by platform number."""
    device_type = "iPad" if "iPad" in dev_id else "iPhone"
    m = re.match(rf"{device_type}(\d+)", dev_id)
    if not m:
        return None
    platform_num = m.group(1)
    prefix = device_type.lower()

    # Try exact match first
    for kname in sorted(kernel_data_map.keys()):
        if kname == f"{prefix}{platform_num}":
            return kernel_data_map[kname]

    # Try prefix match
    for kname in sorted(kernel_data_map.keys()):
        m2 = re.match(rf"{prefix}(\d+)", kname)
        if m2 and m2.group(1) == platform_num:
            return kernel_data_map[kname]

    return None


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
    log("  Kernelcache Rebuilder (FIXED)")
    log(f"  Filter: {FILTER or 'all'}")
    log(f"  Version range: {MIN_VERSION or 'any'} - {MAX_VERSION or 'any'}")
    log("=" * 60)

    # Step 1: Query appledb
    log("Querying appledb API...")
    appledb = json.loads(http_get(APPLEDB_URL))

    ios_entries = appledb.get("ios", [])
    if FILTER == "ipad":
        ios_entries = [e for e in ios_entries if e.get("osStr") in ("iPadOS", "iOS")]
    elif FILTER == "iphone":
        ios_entries = [e for e in ios_entries if e.get("osStr") == "iOS"]

    # Filter versions
    if MIN_VERSION or MAX_VERSION:
        ios_entries = [e for e in ios_entries if version_in_range(e.get("version", ""), MIN_VERSION, MAX_VERSION)]

    # Skip beta/RC versions
    ios_entries = [e for e in ios_entries if "beta" not in e.get("version", "").lower()
                   and "beta" not in e.get("build", "").lower()
                   and "RC" not in e.get("version", "")
                   and "RC" not in e.get("build", "")]

    # Deduplicate by URL
    seen = set()
    firmwares = []
    for entry in ios_entries:
        v = entry.get("version", "")
        b = entry.get("build", "")

        sources = entry.get("sources", [])
        ipsw_sources = [s for s in sources if s.get("type") == "ipsw"]
        if not ipsw_sources:
            continue

        for s in ipsw_sources:
            links = s.get("links", [])
            active_links = [l for l in links if not l.get("auth") and l.get("active")]
            if not active_links or not s.get("deviceMap"):
                continue

            url = active_links[0]["url"]
            if url in seen:
                continue
            seen.add(url)

            devices = s["deviceMap"]
            if FILTER == "ipad":
                devices = [d for d in devices if "iPad" in d]
                if not devices:
                    continue
            elif FILTER == "iphone":
                devices = [d for d in devices if "iPhone" in d]
                if not devices:
                    continue

            firmwares.append({
                "url": url,
                "devices": devices,
                "version": v,
                "build": b,
            })

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

    # Cache: avoid re-processing same IPSW
    ipsw_cache = {}  # url -> (device_to_kc_path, kernel_data_map)

    for idx, fw in enumerate(firmwares):
        version = fw["version"]
        build = fw["build"]
        devices = fw["devices"]
        url = fw["url"]

        log(f"\n[{idx + 1}/{total}] {version} ({build}) - {len(devices)} devices")

        if not version_in_range(version, MIN_VERSION, MAX_VERSION):
            log(f"  SKIP (out of version range)")
            continue

        # Check cache
        if url in ipsw_cache:
            device_to_kc_path, kernel_data_map = ipsw_cache[url]
            log(f"  Using cached IPSW data ({len(kernel_data_map)} kernels)")
        else:
            # Get file size
            file_size = None
            try:
                hdrs = http_head(url)
                file_size = int(hdrs.get("Content-Length", 0))
            except:
                pass

            if not file_size or file_size == 0:
                log(f"  FAIL: cannot get file size")
                fail += 1
                continue

            # Extract BuildManifest + kernelcaches in one pass
            log(f"  Extracting from IPSW ({file_size // 1024 // 1024}MB)...")
            files = extract_files_from_ipsw(url, file_size)
            if not files:
                log(f"  FAIL: no files extracted")
                fail += 1
                time.sleep(1)
                continue

            # Parse BuildManifest
            bm_data = None
            for fname, data in files.items():
                if "buildmanifest" in fname.lower():
                    bm_data = data
                    break

            device_to_kc_path = {}
            if bm_data:
                device_to_kc_path = parse_buildmanifest(bm_data)
                log(f"  BuildManifest: {len(device_to_kc_path)} device->kernel mappings")

            # Build kernel data map
            kernel_data_map = {}
            for fname, data in files.items():
                if "kernelcache" in fname.lower():
                    basename = fname.split("/")[-1]
                    kernel_data_map[basename] = data
                    log(f"  Kernel: {basename} ({len(data) // 1024 // 1024}MB)")

            if not kernel_data_map:
                log(f"  FAIL: no kernelcache in IPSW")
                fail += 1
                continue

            ipsw_cache[url] = (device_to_kc_path, kernel_data_map)

        # Match kernels to devices using BuildManifest (CORRECT method)
        device_to_kernel = match_kernels_to_devices(devices, device_to_kc_path, kernel_data_map)

        # Save each device's kernelcache
        for dev_id in devices:
            kernel_data = device_to_kernel.get(dev_id)
            if not kernel_data:
                log(f"  WARN: no kernel for {dev_id}")
                fail += 1
                continue

            filename = f"{dev_id.replace(',', '.')}_{version}.kernelcache"
            filepath = out_dir / filename
            if filepath.exists() and filepath.stat().st_size > 100 * 1024:
                skip += 1
                continue
            filepath.write_bytes(kernel_data)
            sz = len(kernel_data)
            # Build download URL
            if dev_id.startswith("iPad"):
                dl_url = f"https://kernel0.jumo8.top/ipad/{filename}"
            else:
                dl_url = f"https://kernel0.jumo8.top/{filename}"
            index.append({"model": dev_id, "version": version, "size": sz, "url": dl_url, "build": build})
            log(f"  SAVED: {filename} ({sz // 1024 // 1024}MB)")
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
