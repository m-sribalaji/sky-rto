"""
Who's allowed to do what, and which teams a manager can see. Covers role
assignment (employee/manager/admin), the managed-teams scoping a manager can
be restricted to, and basic team CRUD. Split out of main.py because it was
one of the more self-contained chunks of admin-facing logic.
"""
import json
from datetime import datetime, timezone
from fastapi import APIRouter, Request, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db, Device, Role, TeamConfig
from deps import require_role, require_registered_caller, get_caller_id, get_role
from schemas import RolePayload, TeamPayload

router = APIRouter()

# -- 2. ROLES ----------------------------------------------
@router.get("/api/roles")
async def get_roles(request: Request, db: AsyncSession = Depends(get_db)):
    await require_role(request, db, "admin")
    q = await db.execute(select(Role).order_by(Role.role))
    return {"roles": [{"employee_id": r.employee_id, "role": r.role,
                       "assigned_by": r.assigned_by,
                       "managed_teams": json.loads(r.managed_teams) if r.managed_teams else None,
                       "assigned_at": r.assigned_at.isoformat()+"Z"} for r in q.scalars().all()]}

@router.post("/api/roles")
async def set_role(p: RolePayload, request: Request,
                   db: AsyncSession = Depends(get_db)):
    await require_role(request, db, "admin", caller_id=p.assigned_by)
    if p.role not in ("admin","manager","employee"):
        raise HTTPException(400, "role must be admin|manager|employee")
    dq = await db.execute(select(Device).where(Device.employee_id == p.employee_id))
    if not dq.scalars().first(): raise HTTPException(404, "Employee not found")
    q   = await db.execute(select(Role).where(Role.employee_id == p.employee_id))
    rec = q.scalars().first()
    if rec:
        rec.role=p.role; rec.assigned_by=p.assigned_by; rec.assigned_at=datetime.now(timezone.utc)
    else:
        db.add(Role(employee_id=p.employee_id, role=p.role, assigned_by=p.assigned_by))
    await db.commit()
    return {"status": "ok", "employee_id": p.employee_id, "role": p.role}

@router.get("/api/managed-teams/{employee_id}")
async def get_managed_teams_api(employee_id: str, request: Request,
                                db: AsyncSession=Depends(get_db)):
    """Get managed teams for an employee. Admin can get any, manager can get own."""
    await require_registered_caller(request, db)
    caller_id = get_caller_id(request)
    caller_role = await get_role(caller_id, db) if caller_id else "employee"
    if caller_role == "employee":
        raise HTTPException(403, "Manager or admin role required")
    if caller_role == "manager" and caller_id != employee_id:
        raise HTTPException(403, "Managers can only view their own team access")
    q = await db.execute(select(Role).where(Role.employee_id == employee_id))
    r = q.scalars().first()
    if not r: return {"employee_id": employee_id, "managed_teams": None}
    try:
        teams = json.loads(r.managed_teams) if r.managed_teams else None
    except Exception:
        teams = None
    return {"employee_id": employee_id, "role": r.role, "managed_teams": teams}

@router.put("/api/managed-teams/{employee_id}")
async def set_managed_teams_api(employee_id: str, request: Request,
                                db: AsyncSession=Depends(get_db)):
    """Set managed teams. Admin can update any, manager can update own only."""
    await require_registered_caller(request, db)
    caller_id = get_caller_id(request)
    caller_role = await get_role(caller_id, db) if caller_id else "employee"
    if caller_role == "employee":
        raise HTTPException(403, "Manager or admin role required")
    if caller_role == "manager" and caller_id != employee_id:
        raise HTTPException(403, "Managers can only update their own team access")
    body = await request.json()
    managed = body.get("managed_teams")  # null = all access, list = restricted
    q = await db.execute(select(Role).where(Role.employee_id == employee_id))
    r = q.scalars().first()
    if not r: raise HTTPException(404, "Role not found — assign a role first")
    r.managed_teams = json.dumps(managed) if managed is not None else None
    await db.commit()
    return {"employee_id": employee_id, "managed_teams": managed}

@router.delete("/api/roles/{employee_id}")
async def remove_role(employee_id: str, request: Request,
                      db: AsyncSession = Depends(get_db)):
    await require_role(request, db, "admin")
    q   = await db.execute(select(Role).where(Role.employee_id == employee_id))
    rec = q.scalars().first()
    if rec: await db.delete(rec); await db.commit()
    return {"status": "ok"}

# -- 10. TEAM CONFIG --------------------------------------
@router.get("/api/teams")
async def get_teams(db: AsyncSession = Depends(get_db)):
    q = await db.execute(select(TeamConfig).order_by(TeamConfig.name))
    return {"teams": [{"id": t.id, "name": t.name} for t in q.scalars().all()]}

@router.post("/api/teams")
async def add_team(p: TeamPayload, request: Request,
                   db: AsyncSession = Depends(get_db)):
    await require_role(request, db, "manager", caller_id=p.created_by)
    q = await db.execute(select(TeamConfig).where(TeamConfig.name == p.name))
    if q.scalars().first():
        return {"status": "exists", "name": p.name}
    db.add(TeamConfig(name=p.name, created_by=p.created_by))
    await db.commit()
    return {"status": "created", "name": p.name}

@router.delete("/api/teams/{team_id}")
async def delete_team(team_id: int, request: Request,
                      db: AsyncSession = Depends(get_db)):
    await require_role(request, db, "admin")
    q   = await db.execute(select(TeamConfig).where(TeamConfig.id == team_id))
    rec = q.scalars().first()
    if rec: await db.delete(rec); await db.commit()
    return {"status": "deleted"}
