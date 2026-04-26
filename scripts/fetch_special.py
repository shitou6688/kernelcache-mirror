#!/usr/bin/env python3
"""
Generate alias entries for special iOS versions (16.4.1(a), 16.4.1(b), etc.)
These RSR versions share the same kernelcache as their base version.

Outputs:
  - new_entries_iphone.json  (entries to merge into index_iphone.json)
  - new_entries_ipad.json    (entries to merge into index_ipad.json)
"""

import json
import lzma
import re
import requests

API_BASE = "https://api.appledb.dev/ios/main.json.xz"

def model_to_filename(model):
    return model.replace(",", ".")

print("Downloading firmware list from api.appledb.dev...")
r = requests.get(API_BASE, timeout=120)
data = lzma.decompress(r.content)
fw_list = json.loads(data)
print("Parsed %d entries" % len(fw_list))

# Step 1: Collect all special iOS/iPadOS versions and their base build
# appledb format: version="16.4.1 (a)", build="20E772520a", sources[].prerequisiteBuild="20E252"
special_versions = {}  # version -> { build, prereq_build, models: set }

for fw in fw_list:
    version = str(fw.get("version") or fw.get("osStr", ""))
    build = fw.get("build", "")
    os_type = fw.get("osType", "")

    if os_type not in ("iOS", "iPadOS"):
        continue

    # Match "16.4.1 (a)", "16.5.1 (c)", etc. (with space before parenthesis)
    if not re.search(r'\s\([a-z]\)\s*$', version):
        continue

    # Clean version: "16.4.1 (a)" -> "16.4.1(a)" (remove space for our index format)
    clean_version = re.sub(r'\s\(([a-z])\)', r'(\1)', version)

    for source in fw.get("sources", []):
        prereq_build = source.get("prerequisiteBuild", "")
        if not prereq_build:
            continue

        models = source.get("deviceMap", [])
        for model in models:
            if clean_version not in special_versions:
                special_versions[clean_version] = {
                    "build": build,
                    "prereq_build": prereq_build,
                    "models": set()
                }
            special_versions[clean_version]["models"].add(model)

print("\nSpecial versions found:")
for v, info in sorted(special_versions.items()):
    iphones = len([m for m in info["models"] if m.startswith("iPhone")])
    ipads = len([m for m in info["models"] if m.startswith("iPad")])
    parts = []
    if iphones: parts.append("%d iPhone" % iphones)
    if ipads: parts.append("%d iPad" % ipads)
    print("  %s (build %s, base build %s): %s" % (v, info["build"], info["prereq_build"], ", ".join(parts)))

# Step 2: Download existing index files to get base version kernelcache info
iphone_index = []
ipad_index = []

for tag, target in [("iphone-kernelcache", iphone_index), ("ipad-kernelcache", ipad_index)]:
    url = "https://github.com/BuLu0208/kernelcache-mirror/releases/download/%s/index_%s.json" % (tag, tag.split("-")[0])
    try:
        r = requests.get(url, timeout=60, allow_redirects=True)
        if r.status_code == 200:
            target.extend(r.json())
            print("\nDownloaded %s (%d entries)" % (url.split("/")[-1], len(target)))
        else:
            print("WARNING: Could not download %s (HTTP %d)" % (url, r.status_code))
    except Exception as e:
        print("WARNING: Could not download %s: %s" % (url, e))

# Step 3: Build a lookup: (model, build) -> index entry
base_lookup = {}
for entry in iphone_index + ipad_index:
    key = (entry.get("model", ""), entry.get("build", ""))
    if key not in base_lookup:
        base_lookup[key] = entry

# Step 4: Generate alias entries
new_iphone = []
new_ipad = []
existing_keys = set()

# Also track what's already in existing indices
for entry in iphone_index:
    existing_keys.add(("iphone", entry["model"], entry["version"]))
for entry in ipad_index:
    existing_keys.add(("ipad", entry["model"], entry["version"]))

for clean_version, info in sorted(special_versions.items()):
    prereq_build = info["prereq_build"]

    for model in info["models"]:
        base_key = (model, prereq_build)
        base_entry = base_lookup.get(base_key)

        if model.startswith("iPhone"):
            device_type = "iphone"
            target = new_iphone
            tag = "iphone-kernelcache"
        else:
            device_type = "ipad"
            target = new_ipad
            tag = "ipad-kernelcache"

        # Skip if already in existing index
        if (device_type, model, clean_version) in existing_keys:
            continue

        if base_entry:
            alias = {
                "model": model,
                "version": clean_version,
                "build": base_entry["build"],
                "size": base_entry["size"],
                "url": "https://github.lengye.top/download/%s/%s_%s.kernelcache" % (
                    tag, model_to_filename(model), base_entry["version"]
                ),
            }
            target.append(alias)
            existing_keys.add((device_type, model, clean_version))
        else:
            # No base entry found - this shouldn't happen for versions in range
            print("  WARNING: No base entry for %s %s (build %s)" % (model, clean_version, prereq_build))

print("\nGenerated alias entries:")
print("  iPhone: %d new entries" % len(new_iphone))
print("  iPad: %d new entries" % len(new_ipad))

# Save
with open("new_entries_iphone.json", "w", encoding="utf-8") as f:
    json.dump(new_iphone, f, ensure_ascii=False, indent=2)
with open("new_entries_ipad.json", "w", encoding="utf-8") as f:
    json.dump(new_ipad, f, ensure_ascii=False, indent=2)

if new_iphone:
    print("\nSample iPhone aliases:")
    for e in new_iphone[:3]:
        print("  %s %s -> %s" % (e["model"], e["version"], e["url"]))
if new_ipad:
    print("\nSample iPad aliases:")
    for e in new_ipad[:3]:
        print("  %s %s -> %s" % (e["model"], e["version"], e["url"]))
