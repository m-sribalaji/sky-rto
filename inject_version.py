#!/usr/bin/env python3
"""inject_version.py - Bake build version into checkin_core.py at compile time.
Usage: python3 inject_version.py <version> <filepath>
Example: python3 inject_version.py 0.0.17 client/checkin_core.py
"""
import re, sys

if len(sys.argv) != 3:
    print(f"Usage: {sys.argv[0]} <version> <filepath>")
    sys.exit(1)

version  = sys.argv[1]
filepath = sys.argv[2]

content = open(filepath).read()
updated = re.sub(
    r'_BAKED_VERSION\s*=\s*"0\.0\.0"',
    f'_BAKED_VERSION         = "{version}"',
    content
)

if updated == content:
    print(f"WARNING: _BAKED_VERSION pattern not found in {filepath}")
    sys.exit(1)

open(filepath, "w").write(updated)
print(f"Injected version {version} into {filepath}")