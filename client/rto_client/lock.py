"""
lock.py - a tiny cross-platform file lock so two check-in triggers firing
close together (e.g. wake-from-sleep plus a scheduled poll) don't both try
to run at once. fcntl on mac, msvcrt on Windows, otherwise identical.
"""

import os

from .config import LOCK_FILE


def acquire_lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = None
    try:
        lock_fd = open(LOCK_FILE, "a+")
        lock_fd.seek(0); lock_fd.truncate()
        lock_fd.write(str(os.getpid())); lock_fd.flush()
        if os.name == "nt":
            import msvcrt
            lock_fd.seek(0)
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_fd
    except (IOError, OSError, BlockingIOError):
        try:
            if lock_fd: lock_fd.close()
        except Exception: pass
        return None

def release_lock(lock_fd):
    if not lock_fd: return
    try:
        if os.name == "nt":
            import msvcrt
            try: lock_fd.seek(0); msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception: pass
        else:
            import fcntl
            try: fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
            except Exception: pass
    except Exception: pass
    finally:
        try: lock_fd.close()
        except Exception: pass
        try: LOCK_FILE.unlink(missing_ok=True)
        except Exception: pass
