import asyncio
import os

import httpx
import psycopg
from psycopg.rows import dict_row

from app.auth import issue_token
from app.config import DATABASE_URL, PLATFORM_TENANT_ID
from app.db import pool
from app.worker import process_scoring_job
from scripts.ids import STUDENT_A, STUDENT_B, TENANT_A, TENANT_B

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")


def client(tenant: str, user: str) -> httpx.Client:
    token = issue_token(tenant, user, "student")
    return httpx.Client(base_url=BASE_URL, headers={"Authorization": f"Bearer {token}"})


def start_quiz(api: httpx.Client, count: int = 1) -> tuple[str, dict]:
    response = api.post("/attempts/quiz", json={"subjects": ["Math"], "count": count})
    assert response.status_code == 201, response.text
    attempt_id = response.json()["id"]
    delivery = api.get(f"/attempts/{attempt_id}/delivery")
    assert delivery.status_code == 200, delivery.text
    for item in delivery.json()["sections"][0]["items"]:
        assert "correct_option_id" not in item
        assert "explanation" not in item
    return attempt_id, delivery.json()


def tenant_query(sql: str, params=()):
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (TENANT_A,))
        return conn.execute(sql, params).fetchall()


def test_cross_tenant_object_is_404():
    with client(TENANT_A, STUDENT_A) as alpha, client(TENANT_B, STUDENT_B) as beta:
        alpha_questions = alpha.get("/questions").json()
        private = next(
            q for q in alpha_questions if str(q["owner_tenant_id"]) == TENANT_A
        )
        assert alpha.get(f"/questions/{private['id']}").status_code == 200
        assert beta.get(f"/questions/{private['id']}").status_code == 404


def test_rls_blocks_deliberately_unfiltered_query():
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (TENANT_B,))
        # Deliberately no WHERE tenant_id=...: RLS is the only isolation mechanism here.
        owners = {
            str(r["tenant_id"]) for r in conn.execute("SELECT tenant_id FROM questions")
        }
    assert TENANT_A not in owners
    assert owners == {TENANT_B, PLATFORM_TENANT_ID}


def test_duplicate_submit_returns_identical_result_and_one_submission():
    with client(TENANT_A, STUDENT_A) as api:
        attempt_id, _ = start_quiz(api)
        first = api.post(f"/attempts/{attempt_id}/submit")
        second = api.post(f"/attempts/{attempt_id}/submit")
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    rows = tenant_query(
        "SELECT count(*) AS n FROM submissions WHERE attempt_id=%s", (attempt_id,)
    )
    assert rows[0]["n"] == 1


def test_out_of_order_autosave_does_not_clobber():
    with client(TENANT_A, STUDENT_A) as api:
        attempt_id, body = start_quiz(api)
        item = body["sections"][0]["items"][0]
        options = item["options"]
        newer = api.put(
            f"/attempts/{attempt_id}/answers/{item['id']}",
            json={"selected_option_id": options[0]["id"], "client_revision": 2},
        )
        stale = api.put(
            f"/attempts/{attempt_id}/answers/{item['id']}",
            json={"selected_option_id": options[1]["id"], "client_revision": 1},
        )
    assert newer.json()["accepted"] is True
    assert stale.json() == {
        "selected_option_id": options[0]["id"],
        "client_revision": 2,
        "accepted": False,
    }


def test_redelivered_scoring_job_does_not_double_write():
    with client(TENANT_A, STUDENT_A) as api:
        attempt_id, _ = start_quiz(api)
        assert api.post(f"/attempts/{attempt_id}/submit").status_code == 200
    payload = tenant_query(
        "SELECT payload FROM outbox WHERE event_type='score.requested' AND payload->>'attempt_id'=%s",
        (attempt_id,),
    )[0]["payload"]

    async def redeliver():
        await pool.open()
        await pool.wait()
        try:
            await process_scoring_job(payload)
            await process_scoring_job(payload)
        finally:
            await pool.close()

    asyncio.run(redeliver())
    score_count = tenant_query(
        "SELECT count(*) AS n FROM scores WHERE attempt_id=%s", (attempt_id,)
    )
    processed_count = tenant_query(
        "SELECT count(*) AS n FROM processed_jobs WHERE job_id=%s AND job_type='score'",
        (payload["job_id"],),
    )
    assert score_count[0]["n"] == 1
    assert processed_count[0]["n"] == 1
