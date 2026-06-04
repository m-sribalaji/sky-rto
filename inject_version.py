#!/usr/bin/env python3
"""
inject_version.py - Bake build-time secrets into checkin_core.py.
Replaces placeholders with real values from environment variables.

Usage: python3 inject_version.py <version> <filepath>
Example: python3 inject_version.py 0.0.17 client/checkin_core.py

Environment variables required:
  BAKED_SERVER_URL    - RTO server URL
  BAKED_TEAMS_WEBHOOK - Teams webhook URL
"""
import re, sys, os

if len(sys.argv) != 3:
    print(f"Usage: {sys.argv[0]} <version> <filepath>")
    sys.exit(1)

version    = sys.argv[1]
filepath   = sys.argv[2]
server_url = os.environ.get("BAKED_SERVER_URL", "")
webhook    = os.environ.get("BAKED_TEAMS_WEBHOOK", "")

if not server_url:
    print("ERROR: BAKED_SERVER_URL environment variable not set")
    sys.exit(1)

if not webhook:
    print("ERROR: BAKED_TEAMS_WEBHOOK environment variable not set")
    sys.exit(1)

content = open(filepath).read()

# Inject version
content, n1 = re.subn(
    r'_BAKED_VERSION\s*=\s*"0\.0\.0"',
    f'_BAKED_VERSION         = "{version}"',
    content
)

# Inject server URL
content, n2 = re.subn(
    r'_BAKED_SERVER_URL\s*=\s*"__SERVER_URL__"',
    f'_BAKED_SERVER_URL      = "{server_url}"',
    content
)

# Inject teams webhook
content, n3 = re.subn(
    r'_BAKED_TEAMS_WEBHOOK\s*=\s*"__TEAMS_WEBHOOK__"',
    f'_BAKED_TEAMS_WEBHOOK   = "{webhook}"',
    content
)

if n1 == 0: print("WARNING: _BAKED_VERSION placeholder not found"); sys.exit(1)
if n2 == 0: print("WARNING: __SERVER_URL__ placeholder not found"); sys.exit(1)
if n3 == 0: print("WARNING: __TEAMS_WEBHOOK__ placeholder not found"); sys.exit(1)

open(filepath, "w").write(content)
print(f"Injected: version={version}, server_url={server_url}, webhook=***")