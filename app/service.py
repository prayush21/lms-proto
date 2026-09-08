import json
import random
import uuid
from collections import defaultdict
from datetime import timedelta

from fastapi import HTTPException
from psycopg import AsyncConnection


def _id() -> str:
    return str(uuid.uuid4())


async def create_question(
    conn: AsyncConnection, tenant_id: str, user_id: str, data: dict
) -> dict:
    question_id, version_id = _id(), _id()
    await conn.execute(
        "INSERT INTO questions (tenant_id,id,created_by) VALUES (%s,%s,%s)",
        (tenant_id, question_id, user_id),
    )
    await conn.execute(
        """INSERT INTO question_versions
           (tenant_id,id,question_id,version,subject,topic,subtopic,difficulty,stem,
            options,correct_option_id,explanation,created_by)
           VALUES (%s,%s,%s,1,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)""",
        (
            tenant_id,
            version_id,
            question_id,
            data["subject"],
            data["topic"],
            data.get("subtopic", ""),
            data["difficulty"],
            data["stem"],
            json.dumps(data["options"]),
            data["correct_option_id"],
            data.get("explanation", ""),
            user_id,
        ),
    )
    await conn.execute(
        "UPDATE questions SET current_version_id=%s WHERE tenant_id=%s AND id=%s",
        (version_id, tenant_id, question_id),
    )
    return {"id": question_id, "current_version_id": version_id, "version": 1}


async def add_question_version(
    conn: AsyncConnection, tenant_id: str, user_id: str, question_id: str, data: dict
) -> dict:
    row = await (
        await conn.execute(
            """SELECT q.current_version_id, qv.version
               FROM questions q JOIN question_versions qv
                 ON (qv.tenant_id,qv.id)=(q.tenant_id,q.current_version_id)
               WHERE q.tenant_id=%s AND q.id=%s FOR UPDATE OF q""",
            (tenant_id, question_id),
        )
    ).fetchone()
    if not row:
        raise HTTPException(404, "question not found")
    version_id = _id()
    version = row["version"] + 1
    await conn.execute(
        """INSERT INTO question_versions
           (tenant_id,id,question_id,version,subject,topic,subtopic,difficulty,stem,
            options,correct_option_id,explanation,created_by)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)""",
        (
            tenant_id,
            version_id,
            question_id,
            version,
            data["subject"],
            data["topic"],
            data.get("subtopic", ""),
            data["difficulty"],
            data["stem"],
            json.dumps(data["options"]),
            data["correct_option_id"],
            data.get("explanation", ""),
            user_id,
        ),
    )
    await conn.execute(
        "UPDATE questions SET current_version_id=%s WHERE tenant_id=%s AND id=%s",
        (version_id, tenant_id, question_id),
    )
    return {"id": question_id, "current_version_id": version_id, "version": version}


async def list_questions(conn: AsyncConnection) -> list[dict]:
    rows = await (
        await conn.execute(
            """SELECT q.tenant_id AS owner_tenant_id,q.id,qv.id AS version_id,qv.version,
                      qv.subject,qv.topic,qv.subtopic,qv.difficulty,qv.stem,qv.options
               FROM questions q JOIN question_versions qv
                 ON (qv.tenant_id,qv.id)=(q.tenant_id,q.current_version_id)
               ORDER BY qv.subject,qv.topic,qv.stem"""
        )
    ).fetchall()
    return rows


async def create_blueprint(
    conn: AsyncConnection, tenant_id: str, user_id: str, name: str, sections: list[dict]
) -> dict:
    blueprint_id = _id()
    await conn.execute(
        "INSERT INTO blueprints (tenant_id,id,name,created_by) VALUES (%s,%s,%s,%s)",
        (tenant_id, blueprint_id, name, user_id),
    )
    for section_position, section in enumerate(sections):
        section_id = _id()
        await conn.execute(
            """INSERT INTO blueprint_sections
               (tenant_id,id,blueprint_id,position,name,time_limit_seconds)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (
                tenant_id,
                section_id,
                blueprint_id,
                section_position,
                section["name"],
                section["time_limit_seconds"],
            ),
        )
        for item_position, version_id in enumerate(section["question_version_ids"]):
            version = await (
                await conn.execute(
                    "SELECT tenant_id,id FROM question_versions WHERE id=%s",
                    (version_id,),
                )
            ).fetchone()
            if not version:
                raise HTTPException(
                    422, f"question version {version_id} is not visible"
                )
            await conn.execute(
                """INSERT INTO blueprint_items
                   (tenant_id,id,section_id,position,question_owner_tenant_id,question_version_id)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                (
                    tenant_id,
                    _id(),
                    section_id,
                    item_position,
                    version["tenant_id"],
                    version_id,
                ),
            )
    return {"id": blueprint_id, "name": name}


async def _admit(conn: AsyncConnection, tenant_id: str) -> None:
    # A per-tenant advisory lock is intentionally used instead of a Redis counter:
    # admission and attempt creation then share one failure/commit boundary.
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (tenant_id,)
    )
    row = await (
        await conn.execute(
            """SELECT tr.max_concurrent_attempts,
                      count(a.id) FILTER (WHERE a.status='in_progress' AND a.expires_at>now()) AS active
               FROM tenants t JOIN tiers tr ON tr.id=t.tier_id
               LEFT JOIN attempts a ON a.tenant_id=t.id
               WHERE t.id=%s GROUP BY tr.max_concurrent_attempts""",
            (tenant_id,),
        )
    ).fetchone()
    if not row:
        raise HTTPException(404, "tenant not found")
    if row["active"] >= row["max_concurrent_attempts"]:
        raise HTTPException(429, "tenant concurrent-attempt limit reached")


async def _insert_attempt(
    conn: AsyncConnection,
    tenant_id: str,
    user_id: str,
    kind: str,
    sections: list[dict],
    blueprint_id: str | None = None,
    quiz_spec: dict | None = None,
) -> dict:
    await _admit(conn, tenant_id)
    now = (await (await conn.execute("SELECT clock_timestamp() AS now")).fetchone())[
        "now"
    ]
    total_seconds = sum(section["time_limit_seconds"] for section in sections)
    attempt_id = _id()
    expires_at = now + timedelta(seconds=total_seconds)
    await conn.execute(
        """INSERT INTO attempts
           (tenant_id,id,user_id,kind,status,blueprint_id,quiz_spec,started_at,expires_at)
           VALUES (%s,%s,%s,%s,'in_progress',%s,%s::jsonb,%s,%s)""",
        (
            tenant_id,
            attempt_id,
            user_id,
            kind,
            blueprint_id,
            json.dumps(quiz_spec) if quiz_spec else None,
            now,
            expires_at,
        ),
    )
    cursor = now
    for section_position, section in enumerate(sections):
        section_id = _id()
        section_expires = cursor + timedelta(seconds=section["time_limit_seconds"])
        await conn.execute(
            """INSERT INTO attempt_sections
               (tenant_id,id,attempt_id,position,name,time_limit_seconds,starts_at,expires_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                tenant_id,
                section_id,
                attempt_id,
                section_position,
                section["name"],
                section["time_limit_seconds"],
                cursor,
                section_expires,
            ),
        )
        for item_position, item in enumerate(section["items"]):
            await conn.execute(
                """INSERT INTO attempt_items
                   (tenant_id,id,attempt_id,attempt_section_id,position,
                    question_owner_tenant_id,question_version_id,shuffle_seed)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    tenant_id,
                    _id(),
                    attempt_id,
                    section_id,
                    item_position,
                    item["owner_tenant_id"],
                    item["version_id"],
                    random.SystemRandom().getrandbits(63),
                ),
            )
        cursor = section_expires
    return {
        "id": attempt_id,
        "kind": kind,
        "status": "in_progress",
        "started_at": now,
        "expires_at": expires_at,
    }


async def start_mock(
    conn: AsyncConnection, tenant_id: str, user_id: str, blueprint_id: str
) -> dict:
    blueprint = await (
        await conn.execute(
            "SELECT id FROM blueprints WHERE tenant_id=%s AND id=%s",
            (tenant_id, blueprint_id),
        )
    ).fetchone()
    if not blueprint:
        raise HTTPException(404, "blueprint not found")
    rows = await (
        await conn.execute(
            """SELECT bs.id AS section_id,bs.position AS section_position,bs.name,
                      bs.time_limit_seconds,bi.position AS item_position,
                      bi.question_owner_tenant_id AS owner_tenant_id,
                      bi.question_version_id AS version_id
               FROM blueprint_sections bs JOIN blueprint_items bi
                 ON (bi.tenant_id,bi.section_id)=(bs.tenant_id,bs.id)
               WHERE bs.tenant_id=%s AND bs.blueprint_id=%s
               ORDER BY bs.position,bi.position""",
            (tenant_id, blueprint_id),
        )
    ).fetchall()
    sections_by_id: dict[str, dict] = {}
    for row in rows:
        section = sections_by_id.setdefault(
            str(row["section_id"]),
            {
                "name": row["name"],
                "time_limit_seconds": row["time_limit_seconds"],
                "items": [],
            },
        )
        section["items"].append(
            {"owner_tenant_id": row["owner_tenant_id"], "version_id": row["version_id"]}
        )
    if not sections_by_id:
        raise HTTPException(422, "blueprint has no questions")
    return await _insert_attempt(
        conn,
        tenant_id,
        user_id,
        "mock",
        list(sections_by_id.values()),
        blueprint_id=blueprint_id,
    )


async def start_quiz(
    conn: AsyncConnection, tenant_id: str, user_id: str, spec: dict
) -> dict:
    rows = await (
        await conn.execute(
            """SELECT q.tenant_id AS owner_tenant_id,qv.id AS version_id,qv.difficulty,
                      qv.subject,qv.topic
               FROM questions q JOIN question_versions qv
                 ON (qv.tenant_id,qv.id)=(q.tenant_id,q.current_version_id)
               WHERE NOT EXISTS (
                   SELECT 1 FROM attempt_items ai JOIN attempts a
                     ON (a.tenant_id,a.id)=(ai.tenant_id,ai.attempt_id)
                   WHERE a.user_id=%s AND a.started_at > now()-interval '30 days'
                     AND ai.question_owner_tenant_id=qv.tenant_id
                     AND ai.question_version_id=qv.id
               )""",
            (user_id,),
        )
    ).fetchall()
    subjects = set(spec.get("subjects") or [])
    topics = set(spec.get("topics") or [])
    candidates = [
        row
        for row in rows
        if (not subjects or row["subject"] in subjects)
        and (not topics or row["topic"] in topics)
    ]
    count = spec["count"]
    if len(candidates) < count:
        raise HTTPException(
            422, f"only {len(candidates)} unseen matching questions are available"
        )
    seed = spec.get("selection_seed")
    rng = random.Random(
        seed if seed is not None else random.SystemRandom().getrandbits(63)
    )
    bands: dict[str, list] = defaultdict(list)
    for row in candidates:
        bands[row["difficulty"]].append(row)
    for values in bands.values():
        rng.shuffle(values)
    chosen = []
    # Round-robin gives a stable spread even when count is not divisible by three.
    while len(chosen) < count:
        progressed = False
        for band in ("easy", "medium", "hard"):
            if bands[band] and len(chosen) < count:
                chosen.append(bands[band].pop())
                progressed = True
        if not progressed:
            break
    items = [
        {"owner_tenant_id": row["owner_tenant_id"], "version_id": row["version_id"]}
        for row in chosen
    ]
    section = {
        "name": "Practice quiz",
        "time_limit_seconds": count * 60,
        "items": items,
    }
    return await _insert_attempt(
        conn, tenant_id, user_id, "quiz", [section], quiz_spec=spec
    )


async def submit_attempt(
    conn: AsyncConnection, tenant_id: str, attempt_id: str, trigger: str
) -> dict:
    updated = await (
        await conn.execute(
            """UPDATE attempts SET status='submitted',submitted_at=clock_timestamp(),
                      submission_trigger=%s
               WHERE tenant_id=%s AND id=%s AND status='in_progress'
               RETURNING submitted_at""",
            (trigger, tenant_id, attempt_id),
        )
    ).fetchone()
    if updated:
        await conn.execute(
            """INSERT INTO submissions (tenant_id,attempt_id,trigger,submitted_at)
               VALUES (%s,%s,%s,%s)""",
            (tenant_id, attempt_id, trigger, updated["submitted_at"]),
        )
        job_id = _id()
        payload = {"job_id": job_id, "tenant_id": tenant_id, "attempt_id": attempt_id}
        await conn.execute(
            """INSERT INTO outbox (tenant_id,id,event_type,payload)
               VALUES (%s,%s,'score.requested',%s::jsonb)""",
            (tenant_id, job_id, json.dumps(payload)),
        )
    row = await (
        await conn.execute(
            """SELECT a.id,a.status,s.trigger,s.submitted_at
               FROM attempts a LEFT JOIN submissions s
                 ON (s.tenant_id,s.attempt_id)=(a.tenant_id,a.id)
               WHERE a.tenant_id=%s AND a.id=%s""",
            (tenant_id, attempt_id),
        )
    ).fetchone()
    if not row:
        raise HTTPException(404, "attempt not found")
    return row


async def heartbeat(conn: AsyncConnection, tenant_id: str, attempt_id: str) -> dict:
    row = await (
        await conn.execute(
            """SELECT a.id,a.status,a.expires_at,s.score_status,sc.scored_at,
                      greatest(0,extract(epoch FROM a.expires_at-clock_timestamp()))::integer AS remaining
               FROM attempts a LEFT JOIN submissions s
                 ON (s.tenant_id,s.attempt_id)=(a.tenant_id,a.id)
               LEFT JOIN scores sc
                 ON (sc.tenant_id,sc.attempt_id)=(a.tenant_id,a.id)
               WHERE a.tenant_id=%s AND a.id=%s""",
            (tenant_id, attempt_id),
        )
    ).fetchone()
    if not row:
        raise HTTPException(404, "attempt not found")
    if row["status"] == "in_progress" and row["remaining"] <= 0:
        submitted = await submit_attempt(conn, tenant_id, attempt_id, "deadline")
        return {
            "status": submitted["status"],
            "remaining_seconds": 0,
            "score_status": "queued",
            "scored_at": None,
        }
    section = await (
        await conn.execute(
            """SELECT id,name,greatest(0,extract(epoch FROM expires_at-clock_timestamp()))::integer remaining
               FROM attempt_sections WHERE tenant_id=%s AND attempt_id=%s
                 AND starts_at<=clock_timestamp() AND expires_at>clock_timestamp()
               ORDER BY position LIMIT 1""",
            (tenant_id, attempt_id),
        )
    ).fetchone()
    return {
        "status": row["status"],
        "remaining_seconds": row["remaining"],
        "score_status": row["score_status"],
        "scored_at": row["scored_at"],
        "current_section": section,
    }


async def delivery(conn: AsyncConnection, tenant_id: str, attempt_id: str) -> dict:
    clock = await heartbeat(conn, tenant_id, attempt_id)
    attempt = await (
        await conn.execute(
            "SELECT id,kind,status,started_at,expires_at FROM attempts WHERE tenant_id=%s AND id=%s",
            (tenant_id, attempt_id),
        )
    ).fetchone()
    if not attempt:
        raise HTTPException(404, "attempt not found")
    rows = await (
        await conn.execute(
            """SELECT s.id AS section_id,s.position AS section_position,s.name,s.starts_at,s.expires_at,
                      ai.id AS item_id,ai.position,ai.shuffle_seed,qv.subject,qv.topic,qv.subtopic,
                      qv.difficulty,qv.stem,qv.options,aa.selected_option_id,aa.client_revision
               FROM attempt_sections s JOIN attempt_items ai
                 ON (ai.tenant_id,ai.attempt_section_id)=(s.tenant_id,s.id)
               JOIN question_versions qv
                 ON (qv.tenant_id,qv.id)=(ai.question_owner_tenant_id,ai.question_version_id)
               LEFT JOIN attempt_answers aa
                 ON (aa.tenant_id,aa.attempt_id,aa.attempt_item_id)=
                    (ai.tenant_id,ai.attempt_id,ai.id)
               WHERE s.tenant_id=%s AND s.attempt_id=%s
                 AND (%s <> 'in_progress' OR
                      (s.starts_at<=clock_timestamp() AND s.expires_at>clock_timestamp()))
               ORDER BY s.position,ai.position""",
            (tenant_id, attempt_id, attempt["status"]),
        )
    ).fetchall()
    sections: dict[str, dict] = {}
    for row in rows:
        section = sections.setdefault(
            str(row["section_id"]),
            {
                "id": row["section_id"],
                "position": row["section_position"],
                "name": row["name"],
                "starts_at": row["starts_at"],
                "expires_at": row["expires_at"],
                "items": [],
            },
        )
        options = list(row["options"])
        random.Random(row["shuffle_seed"]).shuffle(options)
        section["items"].append(
            {
                "id": row["item_id"],
                "position": row["position"],
                "subject": row["subject"],
                "topic": row["topic"],
                "subtopic": row["subtopic"],
                "difficulty": row["difficulty"],
                "stem": row["stem"],
                "options": options,
                "answer": {
                    "selected_option_id": row["selected_option_id"],
                    "client_revision": row["client_revision"],
                }
                if row["client_revision"] is not None
                else None,
            }
        )
    # Correct answers and explanations are never selected by this query.
    return {**attempt, **clock, "sections": list(sections.values())}


async def autosave(
    conn: AsyncConnection,
    tenant_id: str,
    attempt_id: str,
    item_id: str,
    option_id: str,
    revision: int,
) -> dict:
    attempt = await (
        await conn.execute(
            """SELECT status,expires_at>clock_timestamp() AS live
               FROM attempts WHERE tenant_id=%s AND id=%s""",
            (tenant_id, attempt_id),
        )
    ).fetchone()
    if not attempt:
        raise HTTPException(404, "attempt not found")
    if attempt["status"] != "in_progress" or not attempt["live"]:
        if attempt["status"] == "in_progress":
            await submit_attempt(conn, tenant_id, attempt_id, "deadline")
        raise HTTPException(409, "attempt is no longer accepting answers")
    item = await (
        await conn.execute(
            """SELECT ai.id FROM attempt_items ai JOIN attempt_sections s
                 ON (s.tenant_id,s.id)=(ai.tenant_id,ai.attempt_section_id)
               WHERE ai.tenant_id=%s AND ai.attempt_id=%s AND ai.id=%s
                 AND s.starts_at<=clock_timestamp() AND s.expires_at>clock_timestamp()""",
            (tenant_id, attempt_id, item_id),
        )
    ).fetchone()
    if not item:
        raise HTTPException(404, "attempt item not found in the active section")
    saved = await (
        await conn.execute(
            """INSERT INTO attempt_answers
               (tenant_id,attempt_id,attempt_item_id,selected_option_id,client_revision)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (tenant_id,attempt_id,attempt_item_id) DO UPDATE
                 SET selected_option_id=excluded.selected_option_id,
                     client_revision=excluded.client_revision,updated_at=clock_timestamp()
                 WHERE excluded.client_revision > attempt_answers.client_revision
               RETURNING selected_option_id,client_revision""",
            (tenant_id, attempt_id, item_id, option_id, revision),
        )
    ).fetchone()
    if saved:
        return {**saved, "accepted": True}
    current = await (
        await conn.execute(
            """SELECT selected_option_id,client_revision FROM attempt_answers
               WHERE tenant_id=%s AND attempt_id=%s AND attempt_item_id=%s""",
            (tenant_id, attempt_id, item_id),
        )
    ).fetchone()
    return {**current, "accepted": False}
