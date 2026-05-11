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


def extract_kernelcache_from_ipsw(url):
    """Extract all kernelcache entries from an IPSW ZIP.
    Returns (list of (filename, data) tuples, file_size or None)."""
    try:
        headers = http_head(url)
        file_size = int(headers.get("Content-Length", 0))
        if file_size == 0:
            log(f"    HEAD failed, trying GET for file size...")
            return [], None
    except Exception as e:
        log(f"    HEAD error: {e}")
        return [], None

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
        return results, file_size
    except Exception as e:
        log(f"    ZIP extraction error: {e}")
        return [], file_size


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
# Board config → Device identifier lookup
# ---------------------------------------------------------------------------

# Maps Apple internal board configs (e.g. J317AP) to device identifiers (iPad8,5)
# Source: iPhone Wiki / appledb cross-reference
BOARD_TO_DEVICE = {
    # ── iPad ──
    # iPad8,x
    "J71TAP": "iPad8,1", "J71T": "iPad8,1",
    "J72TAP": "iPad8,2", "J72T": "iPad8,2",
    "J71AAP": "iPad8,3",
    "J72AAP": "iPad8,4",
    "J317AP": "iPad8,5", "J317XAP": "iPad8,5", "J317": "iPad8,5",
    "J318AP": "iPad8,6", "J318XAP": "iPad8,6", "J318": "iPad8,6",
    "J320AP": "iPad8,7", "J320XAP": "iPad8,7", "J320": "iPad8,7",
    "J321AP": "iPad8,8", "J321XAP": "iPad8,8", "J321": "iPad8,8",
    "J207AP": "iPad8,9",
    "J208AP": "iPad8,9",
    "J417AP": "iPad8,10", "J417": "iPad8,10",
    "J418AP": "iPad8,10", "J418": "iPad8,10",
    "J420AP": "iPad8,11", "J420": "iPad8,11",
    "J421AP": "iPad8,12", "J421": "iPad8,12",
    # iPad11,x
    "J317X": "iPad11,1",  # need verify
    "J318X": "iPad11,2",
    "J320X": "iPad11,3",
    "J321X": "iPad11,4",
    "J322AP": "iPad11,6", "J322": "iPad11,6",
    "J323AP": "iPad11,7", "J323": "iPad11,7",
    # iPad12,x
    "J407AP": "iPad12,1", "J407": "iPad12,1",
    "J408AP": "iPad12,2", "J408": "iPad12,2",
    # iPad13,x
    "J307": "iPad13,1",
    "J308": "iPad13,2",
    "J307AP": "iPad13,1",
    "J308AP": "iPad13,2",
    "J517AP": "iPad13,4", "J517": "iPad13,4",
    "J518AP": "iPad13,5", "J518": "iPad13,5",
    "J522AP": "iPad13,6", "J522": "iPad13,6",
    "J523AP": "iPad13,7", "J523": "iPad13,7",
    "J527AP": "iPad13,8", "J527": "iPad13,8",
    "J528AP": "iPad13,9", "J528": "iPad13,9",
    "J537AP": "iPad13,10", "J537": "iPad13,10",
    "J538AP": "iPad13,11", "J538": "iPad13,11",
    "J617AP": "iPad13,16", "J617": "iPad13,16",
    "J618AP": "iPad13,17", "J618": "iPad13,17",
    "J620AP": "iPad13,18", "J620": "iPad13,18",
    "J621AP": "iPad13,19", "J621": "iPad13,19",
    # iPad14,x
    "J617": "iPad14,1",
    "J618": "iPad14,2",
    "J620": "iPad14,3",
    "J621": "iPad14,4",
    "J720AP": "iPad14,5", "J720": "iPad14,5",
    "J721AP": "iPad14,6", "J721": "iPad14,6",
    # iPad6,x (need BoardConfig suffix removal)
    "J71AP": "iPad6,11", "J71": "iPad6,11",
    "J72AP": "iPad6,12", "J72": "iPad6,12",
    "J68AP": "iPad6,3",
    "J69AP": "iPad6,4",
    "J120AP": "iPad6,7", "J120": "iPad6,7",
    "J121AP": "iPad6,8", "J121": "iPad6,8",
    # iPad7,x
    "J171AP": "iPad7,1",
    "J172AP": "iPad7,2",
    "J173AP": "iPad7,3",
    "J174AP": "iPad7,4",
    "J210AP": "iPad7,5", "J210": "iPad7,5",
    "J211AP": "iPad7,6", "J211": "iPad7,6",
    "J171": "iPad7,11",
    "J172": "iPad7,12",

    # ── iPhone ──
    # iPhone 7 / 7 Plus (A10) — iPhone9,x
    "D10AP": "iPhone9,1", "D10": "iPhone9,1",
    "D11AP": "iPhone9,2", "D11": "iPhone9,2",
    "D101AP": "iPhone9,3", "D101": "iPhone9,3",
    "D111AP": "iPhone9,4", "D111": "iPhone9,4",
    # iPhone 8 / 8 Plus / X (A11) — iPhone10,x
    "D20AP": "iPhone10,1", "D20": "iPhone10,1",
    "D21AP": "iPhone10,2", "D21": "iPhone10,2",
    "D22AP": "iPhone10,3", "D22": "iPhone10,3",
    "D201AP": "iPhone10,4", "D201": "iPhone10,4",
    "D211AP": "iPhone10,5", "D211": "iPhone10,5",
    "D221AP": "iPhone10,6", "D221": "iPhone10,6",
    # iPhone XR / XS / XS Max (A12) — iPhone11,x
    "N841AP": "iPhone11,8", "N84": "iPhone11,8",
    "D321AP": "iPhone11,2", "D321": "iPhone11,2",
    "D331AP": "iPhone11,4", "D331": "iPhone11,4",
    "D341AP": "iPhone11,6", "D341": "iPhone11,6",
    # iPhone 11 / 11 Pro / 11 Pro Max / SE2 (A13) — iPhone12,x
    "N104AP": "iPhone12,1", "N104": "iPhone12,1",
    "D421AP": "iPhone12,3", "D42": "iPhone12,3",
    "D431AP": "iPhone12,5", "D43": "iPhone12,5",
    "D79AP": "iPhone12,8", "D79": "iPhone12,8",
    # iPhone 12 / mini / Pro / Pro Max (A14) — iPhone13,x
    "D52GAP": "iPhone13,2", "D52": "iPhone13,2",
    "D53GAP": "iPhone13,1", "D53": "iPhone13,1",
    "D53pAP": "iPhone13,3", "D53p": "iPhone13,3",
    "D54pAP": "iPhone13,4", "D54p": "iPhone13,4",
    # iPhone 13 / mini / Pro / Pro Max / SE3 (A15) — iPhone14,x
    "D16AP": "iPhone14,5", "D16": "iPhone14,5",
    "D17AP": "iPhone14,4", "D17": "iPhone14,4",
    "D63AP": "iPhone14,2", "D63": "iPhone14,2",
    "D64AP": "iPhone14,3", "D64": "iPhone14,3",
    "D27AP": "iPhone14,6", "D27": "iPhone14,6",
    "D28AP": "iPhone14,7", "D28": "iPhone14,7",
    "D35AP": "iPhone14,8", "D35": "iPhone14,8",
    # iPhone 14 Pro / Pro Max (A16) — iPhone15,x
    "D73AP": "iPhone15,2", "D73": "iPhone15,2",
    "D74AP": "iPhone15,3", "D74": "iPhone15,3",
    # iPhone 15 / Plus / 15 Pro / Pro Max — iPhone15,4-5 / iPhone16,1-2
    "D37AP": "iPhone15,4", "D37": "iPhone15,4",
    "D38AP": "iPhone15,5", "D38": "iPhone15,5",
    "D83AP": "iPhone16,1", "D83": "iPhone16,1",
    "D84AP": "iPhone16,2", "D84": "iPhone16,2",
    # iPhone8,x (A9/A10 devices, iOS 15.x only)
    "N71AP": "iPhone8,1", "N71": "iPhone8,1",
    "N71mAP": "iPhone8,1",
    "N66AP": "iPhone8,2", "N66": "iPhone8,2",
    "N66mAP": "iPhone8,2",
    "N69AP": "iPhone8,4", "N69": "iPhone8,4",
    "N69uAP": "iPhone8,4",
}

def _clean_board_id(board):
    """Normalize board identifier: remove 'AP' suffix, uppercase."""
    return board.upper().replace("AP", "")

def board_to_device(board_id):
    """Convert board config (e.g. J317AP) to device identifier (e.g. iPad8,5)."""
    board = board_id.upper()
    # Try exact match first
    if board in BOARD_TO_DEVICE:
        return BOARD_TO_DEVICE[board]
    # Try without AP suffix
    clean = _clean_board_id(board)
    if clean in BOARD_TO_DEVICE:
        return BOARD_TO_DEVICE[clean]
    # Try with AP suffix
    if not board.endswith("AP"):
        if board + "AP" in BOARD_TO_DEVICE:
            return BOARD_TO_DEVICE[board + "AP"]
    return None


# ---------------------------------------------------------------------------
# BuildManifest-based kernel matching (Apple's official mapping)
# ---------------------------------------------------------------------------

def parse_buildmanifest_from_ipsw(url, file_size):
    """
    Parse BuildManifest.plist from a remote IPSW ZIP.
    Returns: {kernel_filename: [board_config_1, board_config_2, ...]}
    Uses on-demand Range requests via ZipRangeFile (only reads ~200KB total).
    """
    try:
        zf = ZipRangeFile(url, file_size)
        z = zipfile.ZipFile(zf)
        bm_data = z.read("BuildManifest.plist")
        bm = plistlib.loads(bm_data)

        kernel_map = {}
        for bid in bm.get("BuildIdentities", []):
            info = bid.get("Info", {})
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

            if not kpath:
                continue

            kname = kpath.split("/")[-1]
            if kname not in kernel_map:
                kernel_map[kname] = set()
            kernel_map[kname].add(dc.upper())

        # Convert sets to sorted lists
        return {k: sorted(v) for k, v in kernel_map.items()}
    except Exception as e:
        log(f"    BuildManifest parse error: {e}")
        return None


def match_kernels_via_buildmanifest(kernels, devices, bm_kernel_map):
    """
    Match kernelcache files to device identifiers using BuildManifest.plist.
    
    Args:
        kernels: list of (filename, data) tuples
        devices: list of device identifiers e.g. ['iPad8,11', 'iPad8,12']
        bm_kernel_map: {kernel_filename: [board_config, ...]} from BuildManifest
    
    Returns: dict {device_id: kernel_data}
    """
    if not bm_kernel_map:
        return _fallback_match(kernels, devices)

    # Build reverse map: board_config → kernel_filename
    board_to_kernel = {}
    for kname, boards in bm_kernel_map.items():
        for board in boards:
            board_to_kernel[board] = kname

    # Build kernel filename → data lookup
    kernel_data_map = {}
    for kname, kdata in kernels:
        short = kname.split("/")[-1]
        kernel_data_map[short] = kdata

    # Match each device to its kernel
    result = {}
    for dev_id in devices:
        # Find all board configs that map to this device
        # (one device can have multiple board configs)
        matched_kname = None
        for board, kname in board_to_kernel.items():
            mapped_dev = board_to_device(board)
            if mapped_dev == dev_id:
                matched_kname = kname
                break
        
        if matched_kname and matched_kname in kernel_data_map:
            result[dev_id] = kernel_data_map[matched_kname]
            continue

        # Fallback: try platform-number-only match
        fallback = _fallback_match_single(dev_id, kernel_data_map)
        if fallback:
            result[dev_id] = fallback
            continue

        # Last resort: first kernel
        if kernels:
            result[dev_id] = kernels[0][1]

    return result


def _fallback_match(kernels, devices):
    """Fallback: match by platform number only (old behavior)."""
    result = {}
    for dev_id in devices:
        kdata = _fallback_match_single(dev_id, {k[0].split("/")[-1]: k[1] for k in kernels})
        if kdata:
            result[dev_id] = kdata
        elif kernels:
            result[dev_id] = kernels[0][1]
    return result


def _fallback_match_single(dev_id, kernel_data_map):
    """Match a single device to kernel by platform number."""
    device_type = "iPad" if "iPad" in dev_id else "iPhone"
    m = re.match(rf"{device_type}(\d+)", dev_id)
    if not m:
        return None
    platform_num = m.group(1)
    prefix = device_type.lower()

    # Try exact match with letter suffix first
    for kname in sorted(kernel_data_map.keys()):
        if kname == f"{prefix}{platform_num}":
            return kernel_data_map[kname]
    
    # Try prefix match (ipad8 matches ipad8, ipad8b, etc.)
    # Use the first one found
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
    log("  Kernelcache Rebuilder")
    log(f"  Filter: {FILTER or 'all'}")
    log(f"  Version range: {MIN_VERSION or 'any'} - {MAX_VERSION or 'any'}")
    log("=" * 60)

    # Step 1: Query appledb
    log("Querying appledb API...")
    appledb = json.loads(http_get(APPLEDB_URL))

    ios_entries = appledb.get("ios", [])
    if FILTER == "ipad":
        # iPad firmwares can be tagged as either "iPadOS" or "iOS" in appledb
        ios_entries = [e for e in ios_entries if e.get("osStr") in ("iPadOS", "iOS")]
    elif FILTER == "iphone":
        ios_entries = [e for e in ios_entries if e.get("osStr") == "iOS"]

    # Filter versions
    if MIN_VERSION or MAX_VERSION:
        ios_entries = [e for e in ios_entries if version_in_range(e.get("version", ""), MIN_VERSION, MAX_VERSION)]

    # Skip beta/RC versions (keep only release versions)
    ios_entries = [e for e in ios_entries if "beta" not in e.get("version", "").lower()
                   and "beta" not in e.get("build", "").lower()
                   and "RC" not in e.get("version", "")
                   and "RC" not in e.get("build", "")]

    # Deduplicate by URL (same IPSW shouldn't be processed twice)
    seen = set()
    firmwares = []
    for entry in ios_entries:
        v = entry.get("version", "")
        b = entry.get("build", "")

        # Find IPSW sources
        sources = entry.get("sources", [])
        ipsw_sources = [s for s in sources if s.get("type") == "ipsw"]
        if not ipsw_sources:
            continue

        # Pick the best source (no auth, active, with deviceMap)
        for s in ipsw_sources:
            links = s.get("links", [])
            active_links = [l for l in links if not l.get("auth") and l.get("active")]
            if not active_links or not s.get("deviceMap"):
                continue

            url = active_links[0]["url"]
            if url in seen:
                continue
            seen.add(url)

            # Filter devices by type
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

        # Get file size (needed for both extraction and BuildManifest)
        file_size = None
        try:
            hdrs = http_head(url)
            file_size = int(hdrs.get("Content-Length", 0))
        except:
            pass
        
        # Extract kernelcaches from IPSW
        kernels, actual_file_size = extract_kernelcache_from_ipsw(url)
        if not kernels:
            log(f"  FAIL: no kernelcache found")
            fail += 1
            time.sleep(1)
            continue
        
        # Use actual file_size from extraction if HEAD failed
        if (not file_size or file_size == 0) and actual_file_size:
            file_size = actual_file_size

        # Parse BuildManifest to get board→kernel mapping
        bm_map = None
        if file_size and file_size > 0:
            bm_map = parse_buildmanifest_from_ipsw(url, file_size)
            if bm_map:
                log(f"  BuildManifest: {len(bm_map)} kernel variants, {sum(len(v) for v in bm_map.values())} boards")
            else:
                log(f"  BuildManifest: parse failed, using fallback matching")

        # Match kernels to devices using BuildManifest
        device_to_kernel = match_kernels_via_buildmanifest(kernels, devices, bm_map)
        
        for dev_id in devices:
            kernel_data = device_to_kernel.get(dev_id)
            if not kernel_data:
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
                url = f"https://kernel0.jumo8.top/ipad/{filename}"
            else:
                url = f"https://kernel0.jumo8.top/{filename}"
            index.append({"model": dev_id, "version": version, "size": sz, "url": url, "build": build})
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
