# database.py - RTO Tracker v2
from sqlalchemy import Column, String, Boolean, DateTime, Text, Integer, ForeignKey, Float
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from datetime import datetime, timezone
import os

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:////app/data/rto.db")
engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

class Device(Base):
    __tablename__ = "devices"
    hostname      = Column(String, primary_key=True, index=True)
    employee_name = Column(String, nullable=False)
    employee_id   = Column(String, nullable=False, index=True)
    team          = Column(String, nullable=True)
    platform      = Column(String, nullable=True)
    registered_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    api_token     = Column(String, nullable=True, index=True)  # per-device secret token
    # Tokens used to live forever once issued. Now they carry an expiry so a
    # leaked/misused one only stays valid for a bounded window instead of
    # indefinitely. Nullable so existing devices from before this column
    # existed aren't instantly locked out on deploy — deps.py backfills
    # these the first time an old device successfully authenticates.
    token_issued_at  = Column(DateTime, nullable=True)
    token_expires_at = Column(DateTime, nullable=True)

class Role(Base):
    """employee_id -> admin | manager | employee (default if not in table)"""
    __tablename__ = "roles"
    id            = Column(Integer, primary_key=True, autoincrement=True)
    employee_id   = Column(String, ForeignKey("devices.employee_id"), nullable=False, unique=True, index=True)
    role          = Column(String, nullable=False, default="employee")
    assigned_by   = Column(String, nullable=True)
    assigned_at   = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    managed_teams = Column(String, nullable=True)  # JSON array e.g. '["Sky Mobile","NSOS"]'
                                                    # NULL = all teams (admin), [] = employee

class DaySegment(Base):
    """One row per location segment per day. Multiple rows = split day."""
    __tablename__ = "day_segments"
    id               = Column(Integer, primary_key=True, autoincrement=True)
    employee_id      = Column(String, ForeignKey("devices.employee_id"), nullable=False, index=True)
    employee_name    = Column(String, nullable=False)
    hostname         = Column(String, nullable=False)
    date             = Column(String, nullable=False, index=True)
    segment_number   = Column(Integer, default=1)
    status           = Column(String, nullable=False)
    final_status     = Column(String, nullable=True)
    user_declared    = Column(Boolean, default=False)
    confidence       = Column(String, nullable=True)
    started_at       = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    ended_at         = Column(DateTime, nullable=True)
    duration_minutes = Column(Float, nullable=True)
    public_ip        = Column(String, nullable=True)
    lan_ip           = Column(String, nullable=True)
    vpn_active       = Column(Boolean, default=False)
    vpn_tunnel_ip    = Column(String, nullable=True)
    dns_servers      = Column(String, nullable=True)
    dns_domains      = Column(String, nullable=True)
    is_ethernet      = Column(Boolean, default=False)
    platform         = Column(String, nullable=True)
    source           = Column(String, nullable=True)
    flagged          = Column(Boolean, default=False)
    flag_reason      = Column(Text, nullable=True)
    overridden       = Column(Boolean, default=False)
    override_by      = Column(String, nullable=True)
    override_note    = Column(Text, nullable=True)

class LeaveRequest(Base):
    """Simple leave label - no approval, no balance. Just records what type on a given day."""
    __tablename__ = "leave_requests"
    id              = Column(Integer, primary_key=True, autoincrement=True)
    employee_id     = Column(String, ForeignKey("devices.employee_id"), nullable=False, index=True)
    employee_name   = Column(String, nullable=False)
    date            = Column(String, nullable=False, index=True)
    leave_type      = Column(String, nullable=False)
    half_day_period = Column(String, nullable=True)
    note            = Column(Text, nullable=True)
    applied_at      = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    applied_by      = Column(String, nullable=True)
    source          = Column(String, nullable=True)

class PublicHoliday(Base):
    __tablename__ = "public_holidays"
    id       = Column(Integer, primary_key=True, autoincrement=True)
    date     = Column(String, nullable=False, index=True, unique=True)
    name     = Column(String, nullable=False)
    country  = Column(String, default="GB")
    region   = Column(String, nullable=True)
    optional = Column(Boolean, default=False)

class CheckIn(Base):
    """Legacy - kept for backwards compat. New code uses DaySegment."""
    __tablename__ = "checkins"
    id            = Column(Integer, primary_key=True, autoincrement=True)
    employee_id   = Column(String, ForeignKey("devices.employee_id"), nullable=False, index=True)
    employee_name = Column(String, nullable=False)
    hostname      = Column(String, nullable=False)
    date          = Column(String, nullable=False, index=True)
    timestamp     = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    public_ip     = Column(String, nullable=True)
    lan_ip        = Column(String, nullable=True)
    vpn_tunnel_ip = Column(String, nullable=True)
    vpn_active    = Column(Boolean, default=False)
    ssid          = Column(String, nullable=True)
    is_ethernet   = Column(Boolean, default=False)
    dns_servers   = Column(String, nullable=True)
    dns_domains   = Column(String, nullable=True)
    platform      = Column(String, nullable=True)
    auto_status   = Column(String, nullable=True)
    final_status  = Column(String, nullable=True)
    user_declared = Column(Boolean, default=False)
    confidence    = Column(String, nullable=True)
    flagged       = Column(Boolean, default=False)
    flag_reason   = Column(Text, nullable=True)
    overridden    = Column(Boolean, default=False)
    override_by   = Column(String, nullable=True)
    override_note = Column(Text, nullable=True)
    source        = Column(String, nullable=True)
    force_update  = Column(Boolean, default=False)

class AnomalyLog(Base):
    __tablename__ = "anomalies"
    id            = Column(Integer, primary_key=True, autoincrement=True)
    employee_id   = Column(String, index=True)
    employee_name = Column(String)
    detected_at   = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    anomaly_type  = Column(String)
    description   = Column(Text)
    severity      = Column(String)
    resolved      = Column(Boolean, default=False)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Auto-migrate: add any missing columns to existing tables
        # This handles schema changes without requiring manual ALTER TABLE
        await conn.run_sync(_migrate_columns)

def _migrate_columns(conn):
    """Add any columns that exist in the ORM model but not in the DB.
    Safe to run on every startup — skips columns that already exist."""
    import sqlalchemy as sa
    inspector = sa.inspect(conn)
    for table in Base.metadata.sorted_tables:
        existing = {col["name"] for col in inspector.get_columns(table.name)}
        for col in table.columns:
            if col.name not in existing:
                col_type = col.type.compile(dialect=conn.dialect)
                nullable  = "NULL" if col.nullable else "NOT NULL"
                default   = ""
                if col.default is not None and hasattr(col.default, "arg"):
                    arg = col.default.arg
                    if callable(arg):
                        pass  # skip callable defaults
                    elif isinstance(arg, str):
                        default = f" DEFAULT '{arg}'"
                    else:
                        default = f" DEFAULT {arg}"
                try:
                    conn.execute(sa.text(
                        f'ALTER TABLE "{table.name}" ADD COLUMN '
                        f'"{col.name}" {col_type}{default}'
                    ))
                    print(f"[migration] Added column {table.name}.{col.name}")
                except Exception as e:
                    # Column may already exist in some edge cases
                    print(f"[migration] Skipped {table.name}.{col.name}: {e}")

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

class TeamConfig(Base):
    """Admin-managed list of team names available at registration."""
    __tablename__ = "team_configs"
    id         = Column(Integer, primary_key=True, autoincrement=True)
    name       = Column(String, nullable=False, unique=True, index=True)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

class TeamsMessage(Base):
    """
    One row per employee, tracking the single persistent Teams card that
    represents their attendance status. Instead of a new message every
    check-in, the same card gets edited in place — this table is just the
    memory of "which message is theirs", so the next update knows whether
    to create a new card (first time) or edit the existing one (every time
    after). The actual edit/create happens through a Power Automate flow,
    since only Power Automate (not the old Incoming Webhook) can update a
    previously-posted Teams message.
    """
    __tablename__ = "teams_messages"
    employee_id = Column(String, primary_key=True)
    message_id  = Column(String, nullable=False)
    updated_at  = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                          onupdate=lambda: datetime.now(timezone.utc))