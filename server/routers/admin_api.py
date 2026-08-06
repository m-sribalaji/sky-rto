"""
The generic table browser behind the admin.html page — list rows, edit a
cell, delete a row, across any table in the DB. All admin-only. This is
deliberately generic (works off SQLAlchemy model introspection) rather than
one endpoint per table, so it doesn't grow every time we add a column.
"""
import logging
from fastapi import APIRouter, Request, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from database import get_db, Device, CheckIn, DaySegment, LeaveRequest, PublicHoliday, Role, AnomalyLog, TeamConfig
from deps import require_role

router = APIRouter()
logger = logging.getLogger("rto")

# Every other endpoint in this app goes out of its way to never hand back
# api_token in a response — /api/device/{hostname} even says so in its own
# docstring. This generic browser reads every column off the model via
# reflection, which quietly walked straight past that rule: any admin
# opening the Devices table could read every employee's live bearer token
# and impersonate their agent from then on. Redact it the same way here —
# enough left visible to recognise which token you're looking at, not
# enough to reuse it.
def _redact(table_name: str, field: str, value):
    if table_name == "devices" and field == "api_token" and value:
        s = str(value)
        return s[:4] + "…redacted…" + s[-4:] if len(s) > 8 else "…redacted…"
    return value

@router.get("/api/admin/tables")
async def admin_tables(request: Request, db: AsyncSession = Depends(get_db)):
    """Return row counts for all tables."""
    await require_role(request, db, "admin")
    counts = {}
    for name, model in [
        ("devices", Device), ("checkins", CheckIn), ("day_segments", DaySegment),
        ("leave_requests", LeaveRequest), ("public_holidays", PublicHoliday),
        ("roles", Role), ("anomalies", AnomalyLog), ("team_configs", TeamConfig),
    ]:
        q = await db.execute(select(func.count()).select_from(model))
        counts[name] = q.scalar()
    return counts

@router.get("/api/admin/table/{table_name}")
async def admin_table_data(
    table_name: str, request: Request,
    page: int = 0, limit: int = 50, search: str = "",
    db: AsyncSession = Depends(get_db)
):
    """Return paginated rows from any table."""
    await require_role(request, db, "admin")
    TABLE_MAP = {
        "devices":        Device,
        "checkins":       CheckIn,
        "day_segments":   DaySegment,
        "leave_requests": LeaveRequest,
        "public_holidays":PublicHoliday,
        "roles":          Role,
        "anomalies":      AnomalyLog,
        "team_configs":   TeamConfig,
    }
    model = TABLE_MAP.get(table_name)
    if not model: raise HTTPException(404, "Unknown table")

    from sqlalchemy import inspect as sa_inspect, or_, cast, Text
    mapper   = sa_inspect(model)
    cols     = [c.key for c in mapper.mapper.column_attrs]

    q = select(model)
    if search:
        filters = []
        for col in cols:
            try:
                filters.append(cast(getattr(model, col), Text).ilike(f"%{search}%"))
            except Exception:
                pass
        if filters:
            q = q.where(or_(*filters))

    total_q = await db.execute(select(func.count()).select_from(q.subquery()))
    total   = total_q.scalar()
    rows_q  = await db.execute(q.offset(page * limit).limit(limit))
    rows    = rows_q.scalars().all()

    def row_to_dict(r):
        d = {}
        for col in cols:
            val = getattr(r, col, None)
            if hasattr(val, "isoformat"):
                val = val.isoformat()
            d[col] = _redact(table_name, col, val)
        return d

    return {"table": table_name, "columns": cols, "rows": [row_to_dict(r) for r in rows],
            "total": total, "page": page, "limit": limit}

@router.patch("/api/admin/table/{table_name}/{row_id}")
async def admin_edit_row(
    table_name: str, row_id: str,
    request: Request, db: AsyncSession = Depends(get_db)
):
    """Edit a single row's fields - admin only."""
    await require_role(request, db, "admin")
    TABLE_MAP = {
        "devices": Device, "checkins": CheckIn, "day_segments": DaySegment,
        "leave_requests": LeaveRequest, "public_holidays": PublicHoliday,
        "roles": Role, "anomalies": AnomalyLog, "team_configs": TeamConfig,
    }
    model = TABLE_MAP.get(table_name)
    if not model: raise HTTPException(404, "Unknown table")

    from sqlalchemy import inspect as sa_inspect, String, Boolean, Integer, Float, Text, DateTime
    mapper  = sa_inspect(model)
    pk      = mapper.mapper.primary_key[0].key
    q       = await db.execute(select(model).where(getattr(model, pk) == row_id))
    row     = q.scalars().first()
    if not row: raise HTTPException(404, "Row not found")

    body = await request.json()
    col_types = {
        c.key: type(mapper.mapper.columns[c.key].type).__name__
        for c in mapper.mapper.column_attrs
    }

    for field, value in body.items():
        if field == pk: continue  # never edit PK
        if not hasattr(row, field): continue
        col_type = col_types.get(field, "String")
        # Coerce types
        try:
            if value is None or value == "":
                coerced = None
            elif col_type == "Boolean":
                coerced = str(value).lower() in ("true", "1", "yes")
            elif col_type == "Integer":
                coerced = int(value)
            elif col_type == "Float":
                coerced = float(value)
            elif col_type == "DateTime":
                from datetime import datetime as _dt
                coerced = _dt.fromisoformat(str(value).replace("Z", "+00:00"))
            else:
                coerced = str(value)
            setattr(row, field, coerced)
        except Exception as e:
            raise HTTPException(400, f"Invalid value for {field} ({col_type}): {e}")

    await db.commit()
    return {"updated": True}

@router.get("/api/admin/schema/{table_name}")
async def admin_schema(
    table_name: str, request: Request, db: AsyncSession = Depends(get_db)
):
    """Return column types for a table - used by edit UI."""
    await require_role(request, db, "admin")
    TABLE_MAP = {
        "devices": Device, "checkins": CheckIn, "day_segments": DaySegment,
        "leave_requests": LeaveRequest, "public_holidays": PublicHoliday,
        "roles": Role, "anomalies": AnomalyLog, "team_configs": TeamConfig,
    }
    model = TABLE_MAP.get(table_name)
    if not model: raise HTTPException(404, "Unknown table")
    from sqlalchemy import inspect as sa_inspect
    mapper = sa_inspect(model)
    schema = {}
    for c in mapper.mapper.column_attrs:
        col = mapper.mapper.columns[c.key]
        schema[c.key] = {
            "type":     type(col.type).__name__,
            "pk":       col.primary_key,
            "nullable": col.nullable,
        }
    return schema

@router.delete("/api/admin/table/{table_name}/{row_id}")
async def admin_delete_row(
    table_name: str, row_id: str,
    request: Request, db: AsyncSession = Depends(get_db)
):
    """Delete a row by primary key - admin only.
    Deleting a device cascades to all related records."""
    await require_role(request, db, "admin")
    TABLE_MAP = {
        "devices": Device, "checkins": CheckIn, "day_segments": DaySegment,
        "leave_requests": LeaveRequest, "public_holidays": PublicHoliday,
        "roles": Role, "anomalies": AnomalyLog, "team_configs": TeamConfig,
    }
    model = TABLE_MAP.get(table_name)
    if not model: raise HTTPException(404, "Unknown table")
    from sqlalchemy import inspect as sa_inspect
    pk  = sa_inspect(model).mapper.primary_key[0].key
    q   = await db.execute(select(model).where(getattr(model, pk) == row_id))
    row = q.scalars().first()
    if not row: raise HTTPException(404, "Row not found")

    # Cascade delete: if deleting a device, wipe all related records first
    if table_name == "devices":
        emp_id = row.employee_id
        from sqlalchemy import delete as sa_delete
        try:
            for related_model in [DaySegment, CheckIn, LeaveRequest, AnomalyLog, Role]:
                await db.execute(
                    sa_delete(related_model).where(
                        getattr(related_model, "employee_id") == emp_id
                    )
                )
            await db.flush()
            logger.info(f"Cascade delete complete for employee {emp_id}")
        except Exception as e:
            logger.error(f"Cascade delete error for {emp_id}: {e}")
            await db.rollback()
            raise HTTPException(500, f"Cascade delete failed: {str(e)}")

    await db.delete(row)
    await db.commit()
    return {"deleted": True, "cascade": table_name == "devices"}
