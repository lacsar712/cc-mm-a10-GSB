from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from sqlalchemy import DateTime, Float, ForeignKey, String, create_engine
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
    # 若由复测提交生成，指向复测任务；否则为空
    retest_task_id: Mapped[int | None] = mapped_column(ForeignKey("retest_tasks.id"), nullable=True)


class RetestTask(Base):
    __tablename__ = "retest_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    alert_reading_id: Mapped[int] = mapped_column(ForeignKey("readings.id"))
    site: Mapped[str] = mapped_column(String(80))
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="open")  # open / done / expired
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    retest_reading_id: Mapped[int | None] = mapped_column(ForeignKey("readings.id"), nullable=True)
    submitted_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LoginIn(BaseModel):
    username: str
    password: str


class ReadingIn(BaseModel):
    site: str = Field(min_length=1, max_length=80)
    ch4_pct: float


class RetestTaskIn(BaseModel):
    alert_reading_id: int
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
    # 旧库补列：readings.retest_task_id（create_all 不会改动已存在的表）
    from sqlalchemy import inspect, text

    if "retest_task_id" not in {c["name"] for c in inspect(engine).get_columns("readings")}:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE readings ADD COLUMN retest_task_id INTEGER"))
    db = SessionLocal()
    try:
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


def as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def reading_dict(r: Reading) -> dict:
    return {
        "id": r.id,
        "site": r.site,
        "ch4_pct": r.ch4_pct,
        "level": r.level,
        "note": r.note,
        "created_by": r.created_by,
        "created_at": as_utc(r.created_at).isoformat() if r.created_at else None,
        "retest_task_id": r.retest_task_id,
    }


def task_dict(t: RetestTask) -> dict:
    # open 任务按当前时刻动态判定逾期，逾期未复测的进逾期栏
    deadline = as_utc(t.deadline)
    if t.status == "open" and datetime.now(timezone.utc) >= deadline:
        display_status = "expired"
    else:
        display_status = t.status
    return {
        "id": t.id,
        "alert_reading_id": t.alert_reading_id,
        "site": t.site,
        "deadline": deadline.isoformat(),
        "status": display_status,
        "created_by": t.created_by,
        "created_at": as_utc(t.created_at).isoformat(),
        "retest_reading_id": t.retest_reading_id,
        "submitted_by": t.submitted_by,
        "submitted_at": as_utc(t.submitted_at).isoformat() if t.submitted_at else None,
    }


@app.get("/api/readings")
def list_readings(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        rows = db.query(Reading).order_by(Reading.id.desc()).all()
        return [reading_dict(r) for r in rows]
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
        payload = reading_dict(row)
    finally:
        db.close()
    await broadcast(payload)
    return payload


@app.get("/api/retest-tasks")
def list_retest_tasks(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        tasks = db.query(RetestTask).order_by(RetestTask.id.desc()).all()
        return [task_dict(t) for t in tasks]
    finally:
        db.close()


@app.post("/api/retest-tasks", status_code=201)
def create_retest_task(body: RetestTaskIn, user: dict = Depends(require_writer)):
    db = SessionLocal()
    try:
        alert = db.get(Reading, body.alert_reading_id)
        if alert is None:
            raise HTTPException(status_code=404, detail="报警记录不存在")
        if alert.level != "报警":
            raise HTTPException(status_code=400, detail="只能对报警行拉起复测")
        deadline = body.deadline
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        deadline = deadline.astimezone(timezone.utc)
        task = RetestTask(
            alert_reading_id=alert.id,
            site=alert.site,
            deadline=deadline,
            status="open",
            created_by=user["username"],
            created_at=datetime.now(timezone.utc),
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        return task_dict(task)
    finally:
        db.close()


@app.post("/api/retest-tasks/{task_id}/submit", status_code=201)
async def submit_retest(task_id: int, body: RetestSubmitIn, user: dict = Depends(require_writer)):
    level, note = classify(body.ch4_pct)
    db = SessionLocal()
    try:
        task = db.get(RetestTask, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="复测任务不存在")
        if task.status != "open":
            raise HTTPException(status_code=400, detail="任务已结束，不能再次提交")
        now = datetime.now(timezone.utc)
        if now >= as_utc(task.deadline):
            raise HTTPException(status_code=400, detail="已超过截止时刻，不能提交复测")
        row = Reading(
            site=task.site,
            ch4_pct=body.ch4_pct,
            level=level,
            note=f"复测（任务#{task.id}，原报警#{task.alert_reading_id}）" if level != "报警" else f"复测仍报警（任务#{task.id}）",
            created_by=user["username"],
            created_at=now,
            retest_task_id=task.id,
        )
        db.add(row)
        db.flush()
        task.status = "done"
        task.retest_reading_id = row.id
        task.submitted_by = user["username"]
        task.submitted_at = now
        db.commit()
        db.refresh(row)
        db.refresh(task)
        reading_payload = reading_dict(row)
        task_payload = task_dict(task)
    finally:
        db.close()
    await broadcast(reading_payload)
    return {"task": task_payload, "reading": reading_payload}


async def broadcast(payload: dict):
    dead = []
    for ws in list(sockets):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        sockets.discard(ws)


@app.websocket("/ws/alerts")
async def alerts(ws: WebSocket):
    await ws.accept()
    sockets.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        sockets.discard(ws)
