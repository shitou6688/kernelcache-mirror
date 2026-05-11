#!/usr/bin/env python3
"""
Rebuild kernelcache files from Apple IPSW - TRULY CORRECT VERSION.

Root cause of the original bug:
  - BuildManifest.plist's DeviceClass field returns BOARDCONFIG (e.g. "J417ap"), NOT model identifier (e.g. "iPad8,9")
  - The old BOARD_TO_DEVICE hardcoded table had WRONG mappings (e.g. J417AP→iPad8,10 instead of iPad8,9)
  - Even the "fix" that tried to use DeviceClass directly was wrong because DeviceClass ≠ model identifier

Correct approach (this script):
  1. Fetch correct model→boardconfig mapping from ipsw.me API at startup
  2. Parse BuildManifest to get boardconfig→kernelcache_path mapping
  3. For each model: model → boardconfig (from ipsw.me) → kernelcache_path (from BuildManifest) → correct file

Usage:
  python3 scripts/rebuild_kernelcaches.py [--filter ipad|iphone] [--min-version X.Y] [--max-version X.Y]

Environment:
  OUTPUT_DIR: output directory (default: output)
"""

import json
import os
import plistlib
import re
import sys
import time
import urllib.request
import urllib.error
import zipfile
from pathlib import Path

OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "output")
APPLEDB_URL = "https://api.appledb.dev/main.json"
MIN_VERSION = None
MAX_VERSION = None
FILTER = None
CHUNK_SIZE = 1024 * 1024
REQUEST_TIMEOUT = 60
HEAD_TIMEOUT = 30


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# HTTP helpers
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
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        "Range": f"bytes={start}-{end - 1}"
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ---------------------------------------------------------------------------
# ZIP64 Range-based extraction
# ---------------------------------------------------------------------------

class ZipRangeFile:
    def __init__(self, url, file_size):
        self._url = url
        self._file_size = file_size
        self._pos = 0
        self._cache = {}

    def read(self, size=-1):
        if size is None or size < 0:
            size = self._file_size - self._pos
        result = b""
        remaining = size
        while remaining > 0 and self._pos < self._file_size:
            chunk_start = (self._pos // CHUNK_SIZE) * CHUNK_SIZE
            if chunk_start not in self._cache:
                chunk_end = min(chunk_start + CHUNK_SIZE, self._file_size)
                self._cache[chunk_start] = http_range(self._url, chunk_start, chunk_end)
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
        if whence == 0: self._pos = offset
        elif whence == 1: self._pos += offset
        elif whence == 2: self._pos = self._file_size + offset
        return self._pos

    def tell(self): return self._pos
    def seekable(self): return True
    def readable(self): return True


def extract_files_from_ipsw(url, file_size):
    """Extract BuildManifest + all kernelcache files from IPSW ZIP."""
    try:
        zf = ZipRangeFile(url, file_size)
        z = zipfile.ZipFile(zf)
        results = {}
        for info in z.infolist():
            if info.is_dir():
                continue
            name_lower = info.filename.lower()
            if "buildmanifest" in name_lower or "kernelcache" in name_lower:
                log(f"    Extracting: {info.filename} ({info.file_size // 1024 // 1024}MB)")
                data = z.read(info.filename)
                if len(data) > 100 * 1024 or "buildmanifest" in name_lower:
                    results[info.filename] = data
        return results
    except Exception as e:
        log(f"    ZIP extraction error: {e}")
        return {}


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------

def parse_version(v):
    parts = v.split(".")
    result = []
    for p in parts:
        try: result.append(int(p))
        except ValueError: result.append(0)
    return tuple(result)


def version_in_range(version, min_v, max_v):
    v = parse_version(version)
    if min_v and v < parse_version(min_v): return False
    if max_v and v > parse_version(max_v): return False
    return True


# ---------------------------------------------------------------------------
# STEP 1: Build correct model → boardconfig mapping from ipsw.me
# ---------------------------------------------------------------------------

def fetch_ipsw_me_boardconfigs(device_list):
    """Fetch correct model→boardconfig mapping from ipsw.me API.
    Returns: {model_identifier: boardconfig}
    e.g. {"iPad8,9": "J417AP", "iPad8,5": "J320AP", ...}
    """
    mapping = {}
    seen = set()
    for model in device_list:
        if model in seen:
            continue
        seen.add(model)
        try:
            import urllib.parse
            url = f"https://api.ipsw.me/v4/device/{urllib.parse.quote(model)}"
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            bc = data.get("boardconfig", "")
            if bc:
                mapping[model] = bc.upper()
        except Exception as e:
            pass  # Skip models not found on ipsw.me
    return mapping


# ---------------------------------------------------------------------------
# STEP 2: Parse BuildManifest - boardconfig → kernelcache path
# ---------------------------------------------------------------------------

def parse_buildmanifest(bm_data):
    """Parse BuildManifest.plist.
    Returns: {boardconfig_upper: kernelcache_filename_basename}
    e.g. {"J417AP": "kernelcache.release.ipad8b", "J317AP": "kernelcache.release.ipad8"}
    """
    bm = plistlib.loads(bm_data)
    board_to_kc = {}
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

        if kpath and dc:
            # DeviceClass in BuildManifest is boardconfig (e.g. "j417ap")
            board_upper = dc.upper().rstrip("P")  # Handle "J417AP" vs "J417AP" - keep as-is
            # Actually, keep the full boardconfig including AP suffix
            board_upper = dc.upper()
            kc_basename = kpath.split("/")[-1]
            board_to_kc[board_upper] = kc_basename
            # Also store without AP/XAP suffix for matching flexibility
            if board_upper.endswith("XAP"):
                board_to_kc[board_upper[:-3]] = kc_basename  # J317XAP -> J317
            elif board_upper.endswith("AP"):
                board_to_kc[board_upper[:-2]] = kc_basename  # J417AP -> J417

    return board_to_kc


# ---------------------------------------------------------------------------
# STEP 3: Correct matching - model → boardconfig → kernelcache
# ---------------------------------------------------------------------------

def match_kernels_correct(devices, model_to_board, board_to_kc, kernel_data_map):
    """
    Correctly match kernelcache data to device identifiers.
    
    Flow: model_identifier → boardconfig (from ipsw.me) → kernelcache_basename (from BuildManifest) → data
    
    Args:
        devices: list of model identifiers e.g. ['iPad8,9', 'iPad8,10'] from appledb
        model_to_board: {model: boardconfig} from ipsw.me
        board_to_kc: {boardconfig: kernelcache_filename} from BuildManifest
        kernel_data_map: {kernelcache_filename: data_bytes}
    
    Returns: dict {model_id: kernel_data}
    """
    result = {}
    
    for dev_id in devices:
        # Step 1: model → boardconfig
        board = model_to_board.get(dev_id)
        
        if not board:
            # Try without comma (e.g. "iPad8.9" → "iPad8,9")
            for key, val in model_to_board.items():
                if key.replace(",", ".") == dev_id or key == dev_id:
                    board = val
                    break
        
        if not board:
            log(f"    WARNING: No boardconfig for {dev_id}, trying fallback")
            result[dev_id] = _fallback_single(dev_id, kernel_data_map)
            continue
        
        # Step 2: boardconfig → kernelcache basename
        kc_basename = board_to_kc.get(board)
        if not kc_basename:
            # Try without AP suffix
            board_no_ap = board
            if board.endswith("AP"):
                board_no_ap = board[:-2]
            elif board.endswith("XAP"):
                board_no_ap = board[:-3]
            kc_basename = board_to_kc.get(board_no_ap)
        
        if not kc_basename:
            log(f"    WARNING: {dev_id} (board={board}) not in BuildManifest, trying fallback")
            result[dev_id] = _fallback_single(dev_id, kernel_data_map)
            continue
        
        # Step 3: kernelcache basename → data
        if kc_basename in kernel_data_map:
            result[dev_id] = kernel_data_map[kc_basename]
        else:
            # Try partial match
            for kname, kdata in kernel_data_map.items():
                if kc_basename.lower() in kname.lower() or kname.lower() in kc_basename.lower():
                    result[dev_id] = kdata
                    break
            else:
                log(f"    WARNING: {dev_id} kernel '{kc_basename}' not found in extracted files")
                result[dev_id] = _fallback_single(dev_id, kernel_data_map)
    
    return result


def _fallback_single(dev_id, kernel_data_map):
    """Last resort fallback."""
    device_type = "iPad" if "iPad" in dev_id else "iPhone"
    m = re.match(rf"{device_type}(\d+)", dev_id)
    if not m:
        return list(kernel_data_map.values())[0] if kernel_data_map else None
    platform_num = m.group(1)
    prefix = device_type.lower()
    for kname in sorted(kernel_data_map.keys()):
        if re.match(rf"{prefix}{platform_num}[^a-z]", kname.lower()):
            return kernel_data_map[kname]
    for kname in sorted(kernel_data_map.keys()):
        if re.match(rf"{prefix}{platform_num}", kname.lower()):
            return kernel_data_map[kname]
    return list(kernel_data_map.values())[0] if kernel_data_map else None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global MIN_VERSION, MAX_VERSION, FILTER

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--filter" and i + 1 < len(args):
            FILTER = args[i + 1].lower(); i += 2
        elif args[i] == "--min-version" and i + 1 < len(args):
            MIN_VERSION = args[i + 1]; i += 2
        elif args[i] == "--max-version" and i + 1 < len(args):
            MAX_VERSION = args[i + 1]; i += 2
        else:
            i += 1

    log("=" * 60)
    log("  Kernelcache Rebuilder v3 (TRULY CORRECT)")
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

    if MIN_VERSION or MAX_VERSION:
        ios_entries = [e for e in ios_entries if version_in_range(e.get("version", ""), MIN_VERSION, MAX_VERSION)]

    ios_entries = [e for e in ios_entries if "beta" not in e.get("version", "").lower()
                   and "beta" not in e.get("build", "").lower()
                   and "RC" not in e.get("version", "")
                   and "RC" not in e.get("build", "")]

    # Collect firmwares and all unique device identifiers
    seen_urls = set()
    firmwares = []
    all_devices = set()
    for entry in ios_entries:
        v = entry.get("version", "")
        b = entry.get("build", "")
        for s in entry.get("sources", []):
            if s.get("type") != "ipsw":
                continue
            links = [l for l in s.get("links", []) if not l.get("auth") and l.get("active")]
            if not links or not s.get("deviceMap"):
                continue
            url = links[0]["url"]
            if url in seen_urls:
                continue
            seen_urls.add(url)
            devices = s["deviceMap"]
            if FILTER == "ipad":
                devices = [d for d in devices if "iPad" in d]
            elif FILTER == "iphone":
                devices = [d for d in devices if "iPhone" in d]
            if not devices:
                continue
            firmwares.append({"url": url, "devices": devices, "version": v, "build": b})
            all_devices.update(devices)

    firmwares.sort(key=lambda x: parse_version(x["version"]))
    log(f"Found {len(firmwares)} firmware entries, {len(all_devices)} unique devices")

    # Step 2: Fetch correct model→boardconfig mapping from ipsw.me
    log(f"Fetching boardconfig mapping for {len(all_devices)} devices from ipsw.me...")
    model_to_board = fetch_ipsw_me_boardconfigs(list(all_devices))
    log(f"Got {len(model_to_board)} boardconfig mappings")
    # Log a few for verification
    for m in sorted(model_to_board.keys())[:5]:
        log(f"  {m} → {model_to_board[m]}")
    log(f"  ...")

    # Step 3: Extract kernelcaches
    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(exist_ok=True)
    index = []
    total = len(firmwares)
    success = 0
    fail = 0
    skip = 0
    ipsw_cache = {}

    for idx, fw in enumerate(firmwares):
        version = fw["version"]
        build = fw["build"]
        devices = fw["devices"]
        url = fw["url"]

        log(f"\n[{idx + 1}/{total}] {version} ({build}) - {len(devices)} devices")

        if not version_in_range(version, MIN_VERSION, MAX_VERSION):
            continue

        # Use cache for same IPSW
        if url in ipsw_cache:
            board_to_kc, kernel_data_map = ipsw_cache[url]
            log(f"  Using cached IPSW data ({len(kernel_data_map)} kernels)")
        else:
            file_size = None
            try:
                hdrs = http_head(url)
                file_size = int(hdrs.get("Content-Length", 0))
            except:
                pass
            if not file_size:
                log(f"  FAIL: cannot get file size")
                fail += 1; continue

            log(f"  Extracting from IPSW ({file_size // 1024 // 1024}MB)...")
            files = extract_files_from_ipsw(url, file_size)
            if not files:
                log(f"  FAIL: no files extracted")
                fail += 1; time.sleep(1); continue

            # Parse BuildManifest
            bm_data = None
            for fname, data in files.items():
                if "buildmanifest" in fname.lower():
                    bm_data = data; break

            board_to_kc = {}
            if bm_data:
                board_to_kc = parse_buildmanifest(bm_data)
                log(f"  BuildManifest: {len(board_to_kc)} board→kernel mappings")
                # Log mappings for debugging
                for bc, kc in sorted(board_to_kc.items()):
                    log(f"    {bc} → {kc}")

            kernel_data_map = {}
            for fname, data in files.items():
                if "kernelcache" in fname.lower():
                    basename = fname.split("/")[-1]
                    kernel_data_map[basename] = data
                    log(f"  Kernel: {basename} ({len(data) // 1024 // 1024}MB)")

            if not kernel_data_map:
                log(f"  FAIL: no kernelcache in IPSW")
                fail += 1; continue

            ipsw_cache[url] = (board_to_kc, kernel_data_map)

        # CORRECT matching: model → boardconfig → kernelcache
        device_to_kernel = match_kernels_correct(devices, model_to_board, board_to_kc, kernel_data_map)

        # Save
        for dev_id in devices:
            kernel_data = device_to_kernel.get(dev_id)
            if not kernel_data:
                log(f"  WARN: no kernel for {dev_id}")
                fail += 1; continue

            filename = f"{dev_id.replace(',', '.')}_{version}.kernelcache"
            filepath = out_dir / filename
            if filepath.exists() and filepath.stat().st_size > 100 * 1024:
                skip += 1; continue
            filepath.write_bytes(kernel_data)
            sz = len(kernel_data)
            if dev_id.startswith("iPad"):
                dl_url = f"https://kernel0.jumo8.top/ipad/{filename}"
            else:
                dl_url = f"https://kernel0.jumo8.top/{filename}"
            index.append({"model": dev_id, "version": version, "size": sz, "url": dl_url, "build": build})
            log(f"  SAVED: {filename} ({sz // 1024 // 1024}MB)")
            success += 1

        time.sleep(0.5)

    # Generate index
    index_path = out_dir / "index.json"
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    existing = list(out_dir.glob("*.kernelcache"))
    log(f"\n{'=' * 60}")
    log(f"  Done! Success: {success}, Skip: {skip}, Fail: {fail}")
    log(f"  Total files in output: {len(existing)}")
    log(f"  Index: {len(index)} entries")
    log(f"{'=' * 60}")


if __name__ == "__main__":
    main()
