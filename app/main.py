from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from psycopg import AsyncConnection
from redis.asyncio import Redis

from app.auth import decode_token, issue_token
from app.context import identity, identity_context
from app.db import get_conn, pool, set_tenant
from app import service
from app.config import REDIS_URL

redis = Redis.from_url(REDIS_URL, decode_responses=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await pool.open()
    await pool.wait()
    yield
    await redis.aclose()
    await pool.close()


app = FastAPI(title="Multi-tenant Assessment Engine", lifespan=lifespan)


@app.middleware("http")
async def session_tenant_context(request: Request, call_next):
    if request.url.path in {"/health", "/dev/tokens", "/docs", "/openapi.json"}:
        return await call_next(request)
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        return JSONResponse({"detail": "missing bearer token"}, status_code=401)
    try:
        request_identity = decode_token(header.removeprefix("Bearer "))
    except HTTPException as exc:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    token = identity_context.set(request_identity)
    try:
        return await call_next(request)
    finally:
        identity_context.reset(token)


class DevToken(BaseModel):
    tenant_id: str
    user_id: str


class QuestionBody(BaseModel):
    subject: str
    topic: str
    subtopic: str = ""
    difficulty: Literal["easy", "medium", "hard"]
    stem: str
    options: list[dict[str, str]] = Field(min_length=2)
    correct_option_id: str
    explanation: str = ""


class BlueprintSection(BaseModel):
    name: str
    time_limit_seconds: int = Field(gt=0)
    question_version_ids: list[str] = Field(min_length=1)


class BlueprintBody(BaseModel):
    name: str
    sections: list[BlueprintSection] = Field(min_length=1)


class MockStart(BaseModel):
    blueprint_id: str


class QuizStart(BaseModel):
    subjects: list[str] = []
    topics: list[str] = []
    count: int = Field(ge=1, le=100)
    selection_seed: int | None = None


class AnswerBody(BaseModel):
    selected_option_id: str
    client_revision: int = Field(ge=0)


@app.get("/health")
async def health():
    async with pool.connection() as conn:
        await conn.execute("SELECT 1")
    await redis.ping()
    return {"status": "ok", "postgres": "ok", "redis": "ok"}


@app.post("/dev/tokens")
async def dev_token(body: DevToken):
    # This unauthenticated endpoint exists only for the explicitly dev-only auth model.
    async with pool.connection() as conn:
        async with conn.transaction():
            await set_tenant(conn, body.tenant_id)
            row = await (
                await conn.execute(
                    "SELECT role FROM memberships WHERE tenant_id=%s AND user_id=%s",
                    (body.tenant_id, body.user_id),
                )
            ).fetchone()
    if not row:
        raise HTTPException(404, "membership not found")
    return {"token": issue_token(body.tenant_id, body.user_id, row["role"])}


def require(*roles: str):
    if identity().role not in roles:
        raise HTTPException(403, "role not permitted")


async def require_attempt_access(conn: AsyncConnection, attempt_id: str) -> None:
    who = identity()
    if who.role in {"proctor", "admin"}:
        return
    row = await (
        await conn.execute(
            "SELECT id FROM attempts WHERE id=%s AND user_id=%s",
            (attempt_id, who.user_id),
        )
    ).fetchone()
    if not row:
        # Ownership misses, like tenant misses hidden by RLS, intentionally look absent.
        raise HTTPException(404, "attempt not found")


@app.get("/questions")
async def questions(conn: AsyncConnection = Depends(get_conn)):
    return await service.list_questions(conn)


@app.get("/questions/{question_id}")
async def question(question_id: str, conn: AsyncConnection = Depends(get_conn)):
    row = await (
        await conn.execute(
            """SELECT q.tenant_id AS owner_tenant_id,q.id,qv.id AS version_id,qv.version,
                      qv.subject,qv.topic,qv.subtopic,qv.difficulty,qv.stem,qv.options
               FROM questions q JOIN question_versions qv
                 ON (qv.tenant_id,qv.id)=(q.tenant_id,q.current_version_id)
               WHERE q.id=%s""",
            (question_id,),
        )
    ).fetchone()
    if not row:
        raise HTTPException(404, "question not found")
    return row


@app.post("/questions", status_code=201)
async def new_question(body: QuestionBody, conn: AsyncConnection = Depends(get_conn)):
    require("author", "admin")
    who = identity()
    return await service.create_question(
        conn, who.tenant_id, who.user_id, body.model_dump()
    )


@app.post("/questions/{question_id}/versions", status_code=201)
async def new_version(
    question_id: str, body: QuestionBody, conn: AsyncConnection = Depends(get_conn)
):
    require("author", "admin")
    who = identity()
    return await service.add_question_version(
        conn, who.tenant_id, who.user_id, question_id, body.model_dump()
    )


@app.post("/blueprints", status_code=201)
async def new_blueprint(body: BlueprintBody, conn: AsyncConnection = Depends(get_conn)):
    require("author", "admin")
    who = identity()
    return await service.create_blueprint(
        conn,
        who.tenant_id,
        who.user_id,
        body.name,
        [s.model_dump() for s in body.sections],
    )


@app.get("/blueprints/{blueprint_id}")
async def get_blueprint(blueprint_id: str, conn: AsyncConnection = Depends(get_conn)):
    row = await (
        await conn.execute(
            "SELECT id,name,created_at FROM blueprints WHERE id=%s", (blueprint_id,)
        )
    ).fetchone()
    if not row:
        raise HTTPException(404, "blueprint not found")
    return row


@app.post("/attempts/mock", status_code=201)
async def mock_attempt(body: MockStart, conn: AsyncConnection = Depends(get_conn)):
    who = identity()
    return await service.start_mock(conn, who.tenant_id, who.user_id, body.blueprint_id)


@app.post("/attempts/quiz", status_code=201)
async def quiz_attempt(body: QuizStart, conn: AsyncConnection = Depends(get_conn)):
    who = identity()
    return await service.start_quiz(conn, who.tenant_id, who.user_id, body.model_dump())


@app.get("/attempts/{attempt_id}/delivery")
async def attempt_delivery(attempt_id: str, conn: AsyncConnection = Depends(get_conn)):
    await require_attempt_access(conn, attempt_id)
    return await service.delivery(conn, identity().tenant_id, attempt_id)


@app.get("/attempts/{attempt_id}/heartbeat")
async def attempt_heartbeat(attempt_id: str, conn: AsyncConnection = Depends(get_conn)):
    await require_attempt_access(conn, attempt_id)
    return await service.heartbeat(conn, identity().tenant_id, attempt_id)


@app.put("/attempts/{attempt_id}/answers/{item_id}")
async def save_answer(
    attempt_id: str,
    item_id: str,
    body: AnswerBody,
    conn: AsyncConnection = Depends(get_conn),
):
    await require_attempt_access(conn, attempt_id)
    return await service.autosave(
        conn,
        identity().tenant_id,
        attempt_id,
        item_id,
        body.selected_option_id,
        body.client_revision,
    )


@app.post("/attempts/{attempt_id}/submit")
async def submit(attempt_id: str, conn: AsyncConnection = Depends(get_conn)):
    await require_attempt_access(conn, attempt_id)
    return await service.submit_attempt(
        conn, identity().tenant_id, attempt_id, "student"
    )


@app.post("/attempts/{attempt_id}/force-submit")
async def force_submit(attempt_id: str, conn: AsyncConnection = Depends(get_conn)):
    require("proctor", "admin")
    return await service.submit_attempt(
        conn, identity().tenant_id, attempt_id, "proctor"
    )


@app.get("/attempts/{attempt_id}/report")
async def get_report(attempt_id: str, conn: AsyncConnection = Depends(get_conn)):
    await require_attempt_access(conn, attempt_id)
    attempt = await (
        await conn.execute("SELECT id FROM attempts WHERE id=%s", (attempt_id,))
    ).fetchone()
    if not attempt:
        raise HTTPException(404, "attempt not found")
    report = await (
        await conn.execute("SELECT * FROM reports WHERE attempt_id=%s", (attempt_id,))
    ).fetchone()
    if not report:
        raise HTTPException(202, "report is still processing")
    return report
