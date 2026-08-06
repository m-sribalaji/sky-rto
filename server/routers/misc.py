"""
Odds and ends that don't fit anywhere else: the client auto-update version
check, app-wide display settings, and the health check the load balancer
pings. Small enough that giving each its own file would be overkill.
"""
from fastapi import APIRouter, Request, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from deps import require_role, require_registered_caller, _read_app_settings, _write_app_settings, _APP_SETTINGS_DEFAULTS

router = APIRouter()

@router.get("/api/version")
async def get_version():
    """
    Returns the latest available client binary version.
    Used by agents for auto-update — avoids GitHub API auth for private repos.
    Build script writes version to /app/data/client_version.txt on each deploy.
    """
    from pathlib import Path as _Path
    version_file = _Path("/app/data/client_version.txt")
    try:
        ver = version_file.read_text().strip() if version_file.exists() else "0.0.0"
    except Exception:
        ver = "0.0.0"
    build_num = ver.split(".")[-1] if ver != "0.0.0" else "0"
    return {
        "version": ver,
        "mac_url": f"https://github.com/m-sribalaji/sky-rto/releases/download/build-{build_num}/rto-mac-arm64",
        "win_url": f"https://github.com/m-sribalaji/sky-rto/releases/download/build-{build_num}/rto-win.exe",
    }


@router.get("/api/settings")
async def get_settings(request: Request, db: AsyncSession = Depends(get_db)):
    """Return app-wide display settings — readable by any registered user."""
    await require_registered_caller(request, db)
    return _read_app_settings()

@router.put("/api/settings")
async def put_settings(request: Request, db: AsyncSession = Depends(get_db)):
    """Persist app-wide display settings — admin only."""
    await require_role(request, db, "admin")
    body = await request.json()
    current = _read_app_settings()
    for key, default_val in _APP_SETTINGS_DEFAULTS.items():
        if key in body:
            current[key] = bool(body[key]) if isinstance(default_val, bool) else body[key]
    _write_app_settings(current)
    return current

@router.get("/health")
async def health():
    return {"status": "ok"}
