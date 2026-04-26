#!/usr/bin/env python3
"""
Fetch firmware list from api.appledb.dev for iOS 15.7.2-16.6.1
Output: firmware_list.json
"""

import json
import lzma
import re
import requests

API_BASE = "https://api.appledb.dev/ios/main.json.xz"
OUTPUT = "firmware_list.json"
SKIP_HOSTS = ["adcdownload.apple.com", "download.developer.apple.com"]

def ver_tuple(v):
    try:
        clean = re.sub(r'\([a-z]\)', '', str(v))
        return tuple(int(x) for x in clean.split(".")[:3])
    except:
        return (0,)

def in_range(v):
    vt = ver_tuple(v)
    return (15, 7, 2) <= vt <= (16, 6, 1)

print("Downloading firmware list from api.appledb.dev...")
r = requests.get(API_BASE, timeout=120)
print("Downloaded %d bytes" % len(r.content))

data = lzma.decompress(r.content)
fw_list = json.loads(data)
print("Parsed %d firmware entries" % len(fw_list))

# Debug: check field names from first entry
if fw_list:
    print("First entry keys: %s" % list(fw_list[0].keys()))
    print("First entry: %s" % json.dumps(fw_list[0], indent=2, ensure_ascii=False)[:500])

result = []
seen = set()

for fw in fw_list:
    # Try different possible field names for version
    version = fw.get("version") or fw.get("osStr", "")
    build = fw.get("build", "")
    os_type = fw.get("osType", "")

    # Include iOS and iPadOS
    if os_type and os_type not in ("iOS", "iPadOS"):
        continue

    if not in_range(version):
        continue

    for source in fw.get("sources", []):
        if source.get("prerequisiteBuild"):
            continue

        for link in source.get("links", []):
            url = link.get("url", "")
            if not url or not link.get("active"):
                continue

            from urllib.parse import urlparse
            host = urlparse(url).hostname
            if host in SKIP_HOSTS:
                continue

            models = source.get("deviceMap", [])
            fw_type = source.get("type", "")

            for model in models:
                key = (model, build)
                if key in seen:
                    continue
                seen.add(key)
                result.append({
                    "model": model,
                    "version": str(version),
                    "build": build,
                    "url": url,
                    "type": fw_type,
                })

print("Filtered to %d unique model+build entries" % len(result))

if result:
    result.sort(key=lambda x: (x["model"], ver_tuple(x["version"])))
    print("Sample entries:")
    for r in result[:5]:
        print("  %s %s (%s) %s" % (r["model"], r["version"], r["build"], r["url"][:80]))
else:
    print("WARNING: No entries found!")
    # Show what version values exist
    versions = set()
    for fw in fw_list:
        v = fw.get("version") or fw.get("osStr", "")
        ot = fw.get("osType", "")
        if ot == "iOS" or "iPhone" in str(fw.get("osStr", "")):
            versions.add("%s (%s)" % (v, ot))
    print("iOS versions found: %s" % sorted(list(versions))[:30])

with open(OUTPUT, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)

print("Saved to %s" % OUTPUT)