from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.rules import classify


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg2://app:app@localhost:54391/methane"
    jwt_secret: str = "mine-methane-dev-secret"


settings = Settings()
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)
USERS = {
    "gasman": {"role": "writer", "password_hash": pwd.hash("gas123456")},
    "viewer": {"role": "reader", "password_hash": pwd.hash("view123456")},
}

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


class Reading(Base):
    __tablename__ = "readings"
    id: Mapped[int] = mapped_column(primary_key=True)
    site: Mapped[str] = mapped_column(String(80))
    ch4_pct: Mapped[float] = mapped_column(Float)
    level: Mapped[str] = mapped_column(String(20))
    note: Mapped[str] = mapped_column(String(200))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    is_retest: Mapped[bool] = mapped_column(Boolean, default=False)


class RetestTask(Base):
    __tablename__ = "retest_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_reading_id: Mapped[int] = mapped_column(ForeignKey("readings.id"))
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="open")  # open / done
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    retest_reading_id: Mapped[int | None] = mapped_column(
        ForeignKey("readings.id"), nullable=True
    )


class LoginIn(BaseModel):
    username: str
    password: str


class ReadingIn(BaseModel):
    site: str = Field(min_length=1, max_length=80)
    ch4_pct: float


class RetestTaskIn(BaseModel):
    source_reading_id: int
    deadline: datetime


class RetestSubmitIn(BaseModel):
    ch4_pct: float


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(security)) -> dict:
    if credentials is None:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        payload = jwt.decode(credentials.credentials, settings.jwt_secret, algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="无效令牌") from exc
    username = payload.get("sub")
    if username not in USERS:
        raise HTTPException(status_code=401, detail="无效令牌")
    return {"username": username, "role": payload.get("role")}


def require_writer(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "writer":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅瓦斯检查员可操作")
    return user


sockets: set[WebSocket] = set()
app = FastAPI(title="矿井瓦斯班测台")


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if "is_retest" not in {c["name"] for c in inspect(engine).get_columns("readings")}:
            db.execute(text("ALTER TABLE readings ADD COLUMN is_retest BOOLEAN DEFAULT FALSE"))
            db.commit()
        if db.query(Reading).count() == 0:
            now = datetime.now(timezone.utc)
            for site, ch4 in (("东翼-12", 0.35), ("回风巷", 1.4)):
                level, note = classify(ch4)
                db.add(
                    Reading(
                        site=site,
                        ch4_pct=ch4,
                        level=level,
                        note=note,
                        created_by="gasman",
                        created_at=now,
                    )
                )
            db.commit()
    finally:
        db.close()


async def broadcast(payload: dict):
    dead = []
    for ws in list(sockets):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        sockets.discard(ws)


def as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def task_status(task: RetestTask, now: datetime | None = None) -> str:
    if task.status == "done":
        return "done"
    now = now or datetime.now(timezone.utc)
    return "overdue" if as_utc(task.deadline) < now else "open"


def serialize_task(db: Session, task: RetestTask) -> dict:
    source = db.get(Reading, task.source_reading_id)
    retest = db.get(Reading, task.retest_reading_id) if task.retest_reading_id else None
    return {
        "id": task.id,
        "source_reading_id": task.source_reading_id,
        "source_site": source.site if source else None,
        "source_ch4_pct": source.ch4_pct if source else None,
        "deadline": as_utc(task.deadline).isoformat(),
        "status": task_status(task),
        "created_by": task.created_by,
        "created_at": as_utc(task.created_at).isoformat(),
        "retest_reading_id": task.retest_reading_id,
        "retest_ch4_pct": retest.ch4_pct if retest else None,
        "retest_level": retest.level if retest else None,
    }


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "mine-methane-shift"}


@app.post("/api/auth/login")
def login(body: LoginIn):
    user = USERS.get(body.username.strip())
    if not user or not pwd.verify(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    exp = datetime.now(timezone.utc) + timedelta(hours=8)
    token = jwt.encode(
        {"sub": body.username.strip(), "role": user["role"], "exp": exp},
        settings.jwt_secret,
        algorithm="HS256",
    )
    return {"access_token": token, "username": body.username.strip(), "role": user["role"]}


@app.get("/api/readings")
def list_readings(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        rows = db.query(Reading).order_by(Reading.id.desc()).all()
        return [
            {
                "id": r.id,
                "site": r.site,
                "ch4_pct": r.ch4_pct,
                "level": r.level,
                "note": r.note,
                "created_by": r.created_by,
                "is_retest": r.is_retest,
            }
            for r in rows
        ]
    finally:
        db.close()


@app.post("/api/readings", status_code=201)
async def create_reading(body: ReadingIn, user: dict = Depends(require_writer)):
    level, note = classify(body.ch4_pct)
    db = SessionLocal()
    try:
        row = Reading(
            site=body.site.strip(),
            ch4_pct=body.ch4_pct,
            level=level,
            note=note,
            created_by=user["username"],
            created_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        payload = {
            "id": row.id,
            "site": row.site,
            "ch4_pct": row.ch4_pct,
            "level": row.level,
            "note": row.note,
        }
    finally:
        db.close()
    await broadcast(payload)
    return payload


@app.get("/api/retest-tasks")
def list_retest_tasks(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        tasks = db.query(RetestTask).order_by(RetestTask.id.desc()).all()
        groups = {"open": [], "overdue": [], "done": []}
        for t in tasks:
            groups[task_status(t, now)].append(serialize_task(db, t))
        return groups
    finally:
        db.close()


@app.post("/api/retest-tasks", status_code=201)
def create_retest_task(body: RetestTaskIn, user: dict = Depends(require_writer)):
    db = SessionLocal()
    try:
        source = db.get(Reading, body.source_reading_id)
        if source is None:
            raise HTTPException(status_code=404, detail="原班测行不存在")
        if source.level != "报警":
            raise HTTPException(status_code=400, detail="只有报警行可以拉起复测任务")
        existing = (
            db.query(RetestTask)
            .filter(
                RetestTask.source_reading_id == source.id,
                RetestTask.status == "open",
            )
            .first()
        )
        if existing is not None:
            raise HTTPException(status_code=409, detail="该报警行已有待复测任务")
        deadline = body.deadline
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        task = RetestTask(
            source_reading_id=source.id,
            deadline=deadline.astimezone(timezone.utc),
            status="open",
            created_by=user["username"],
            created_at=datetime.now(timezone.utc),
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        return serialize_task(db, task)
    finally:
        db.close()


@app.post("/api/retest-tasks/{task_id}/submit", status_code=201)
async def submit_retest(task_id: int, body: RetestSubmitIn, user: dict = Depends(require_writer)):
    db = SessionLocal()
    try:
        task = db.get(RetestTask, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="复测任务不存在")
        if task.status != "open":
            raise HTTPException(status_code=400, detail="该任务已复测完成")
        now = datetime.now(timezone.utc)
        if as_utc(task.deadline) < now:
            raise HTTPException(status_code=400, detail="已超过截止时刻，不能提交复测")
        source = db.get(Reading, task.source_reading_id)
        level, note = classify(body.ch4_pct)
        row = Reading(
            site=source.site,
            ch4_pct=body.ch4_pct,
            level=level,
            note=note,
            created_by=user["username"],
            created_at=now,
            is_retest=True,
        )
        db.add(row)
        db.flush()
        task.status = "done"
        task.retest_reading_id = row.id
        db.commit()
        db.refresh(row)
        payload = {
            "id": row.id,
            "site": row.site,
            "ch4_pct": row.ch4_pct,
            "level": row.level,
            "note": row.note,
        }
        result = serialize_task(db, task)
    finally:
        db.close()
    await broadcast(payload)
    return result


@app.websocket("/ws/alerts")
async def alerts(ws: WebSocket):
    await ws.accept()
    sockets.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        sockets.discard(ws)
