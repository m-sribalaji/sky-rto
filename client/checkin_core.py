#!/usr/bin/env python3
"""
checkin_core.py - thin re-export shim over rto_client/.

The logic used to all live in this one file; it's now split up in
rto_client/ (config, network, classify, queue, api, lock, update, missed,
checkin) because 1300 lines in one module was getting hard to navigate.
This file exists purely so `import checkin_core` from the compiled agent
binaries (rto_agent_mac.py / rto_agent_win.py) keeps working unchanged —
same public names, same behavior, just imported from the new package.

PUBLIC API (called by agents):
    run_checkin(force=False)
    run_reset()
    run_retry()
"""

from rto_client.config import (
    load_config, save_config, get_hostname,
    CONFIG_DIR, CONFIG_FILE, LOG_FILE,
    _sync_device_auth,
)
from rto_client.api import server_reachable
from rto_client.update import check_and_apply_update
from rto_client.checkin import run_checkin, run_reset, run_retry
