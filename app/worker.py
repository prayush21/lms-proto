import asyncio
import json
import sys
import uuid
from collections import defaultdict

from app.config import REPORT_QUEUE, SCORING_QUEUE
from app.db import pool, tenant_transaction
from app.queueing import client, ensure_queues
from app.service import submit_attempt


async def tenant_ids() -> list[str]:
    async with pool.connection() as conn:
        rows = await (
            await conn.execute("SELECT id FROM tenants WHERE NOT is_platform")
        ).fetchall()
    return [str(row["id"]) for row in rows]


async def relay_once(urls: dict[str, str]) -> bool:
    sqs = client()
    found = False
    for tenant_id in await tenant_ids():
        async with tenant_transaction(tenant_id) as conn:
            row = await (
                await conn.execute(
                    """SELECT id,event_type,payload FROM outbox
                       WHERE published_at IS NULL ORDER BY created_at
                       FOR UPDATE SKIP LOCKED LIMIT 1"""
                )
            ).fetchone()
            if not row:
                continue
            found = True
            queue_url = urls[
                SCORING_QUEUE
                if row["event_type"] == "score.requested"
                else REPORT_QUEUE
            ]
            # Publishing precedes marking. A crash between them deliberately causes
            # redelivery, which the workers' processed_jobs gate absorbs.
            sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps(row["payload"]))
            await conn.execute(
                """UPDATE outbox SET published_at=clock_timestamp(),publish_attempts=publish_attempts+1
                   WHERE id=%s""",
                (row["id"],),
            )
    return found


async def process_scoring_job(payload: dict) -> bool:
    tenant_id, job_id, attempt_id = (
        payload["tenant_id"],
        payload["job_id"],
        payload["attempt_id"],
    )
    async with tenant_transaction(tenant_id) as conn:
        claimed = await (
            await conn.execute(
                """INSERT INTO processed_jobs (tenant_id,job_id,job_type)
                   VALUES (%s,%s,'score') ON CONFLICT DO NOTHING RETURNING job_id""",
                (tenant_id, job_id),
            )
        ).fetchone()
        if not claimed:
            return False
        rows = await (
            await conn.execute(
                """SELECT qv.subject,qv.topic,qv.correct_option_id,aa.selected_option_id
                   FROM attempt_items ai JOIN question_versions qv
                     ON (qv.tenant_id,qv.id)=
                        (ai.question_owner_tenant_id,ai.question_version_id)
                   LEFT JOIN attempt_answers aa
                     ON (aa.tenant_id,aa.attempt_id,aa.attempt_item_id)=
                        (ai.tenant_id,ai.attempt_id,ai.id)
                   WHERE ai.attempt_id=%s ORDER BY ai.attempt_section_id,ai.position""",
                (attempt_id,),
            )
        ).fetchall()
        if not rows:
            raise RuntimeError(f"attempt {attempt_id} has no items or is not visible")
        breakdown = defaultdict(lambda: {"correct": 0, "total": 0})
        correct = 0
        for row in rows:
            is_correct = row["selected_option_id"] == row["correct_option_id"]
            correct += int(is_correct)
            for key in (f"subject:{row['subject']}", f"topic:{row['topic']}"):
                breakdown[key]["correct"] += int(is_correct)
                breakdown[key]["total"] += 1
        total = len(rows)
        percentage = round(correct * 100 / total, 2)
        await conn.execute(
            """INSERT INTO scores
               (tenant_id,attempt_id,correct_count,question_count,percentage,breakdown)
               VALUES (%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT DO NOTHING""",
            (tenant_id, attempt_id, correct, total, percentage, json.dumps(breakdown)),
        )
        await conn.execute(
            "UPDATE submissions SET score_status='complete' WHERE attempt_id=%s",
            (attempt_id,),
        )
        report_job_id = str(uuid.uuid5(uuid.UUID(job_id), "report"))
        report_payload = {
            "job_id": report_job_id,
            "tenant_id": tenant_id,
            "attempt_id": attempt_id,
        }
        await conn.execute(
            """INSERT INTO outbox (tenant_id,id,event_type,payload)
               VALUES (%s,%s,'report.requested',%s::jsonb) ON CONFLICT DO NOTHING""",
            (tenant_id, report_job_id, json.dumps(report_payload)),
        )
    return True


async def process_report_job(payload: dict) -> bool:
    tenant_id, job_id, attempt_id = (
        payload["tenant_id"],
        payload["job_id"],
        payload["attempt_id"],
    )
    async with tenant_transaction(tenant_id) as conn:
        claimed = await (
            await conn.execute(
                """INSERT INTO processed_jobs (tenant_id,job_id,job_type)
                   VALUES (%s,%s,'report') ON CONFLICT DO NOTHING RETURNING job_id""",
                (tenant_id, job_id),
            )
        ).fetchone()
        if not claimed:
            return False
        score = await (
            await conn.execute(
                """SELECT s.*,a.kind FROM scores s JOIN attempts a
                     ON (a.tenant_id,a.id)=(s.tenant_id,s.attempt_id)
                   WHERE s.attempt_id=%s""",
                (attempt_id,),
            )
        ).fetchone()
        if not score:
            raise RuntimeError(f"score for {attempt_id} is not ready")
        cohort = await (
            await conn.execute(
                """SELECT s.percentage FROM scores s JOIN attempts a
                     ON (a.tenant_id,a.id)=(s.tenant_id,s.attempt_id)
                   WHERE a.kind=%s""",
                (score["kind"],),
            )
        ).fetchall()
        values = [float(row["percentage"]) for row in cohort]
        current = float(score["percentage"])
        below = sum(value < current for value in values)
        equal = sum(value == current for value in values)
        percentile = round(100 * (below + 0.5 * equal) / len(values), 2)
        subject, topic = {}, {}
        for key, value in score["breakdown"].items():
            target = subject if key.startswith("subject:") else topic
            target[key.split(":", 1)[1]] = value
        overall = {
            "correct": score["correct_count"],
            "total": score["question_count"],
            "percentage": current,
        }
        await conn.execute(
            """INSERT INTO reports
               (tenant_id,attempt_id,overall,per_subject,per_topic,percentile)
               VALUES (%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s)
               ON CONFLICT (tenant_id,attempt_id) DO NOTHING""",
            (
                tenant_id,
                attempt_id,
                json.dumps(overall),
                json.dumps(subject),
                json.dumps(topic),
                percentile,
            ),
        )
    return True


async def consume(queue_name: str, handler) -> None:
    urls = ensure_queues()
    sqs = client()
    url = urls[queue_name]
    while True:
        response = sqs.receive_message(
            QueueUrl=url,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=10,
            VisibilityTimeout=30,
        )
        for message in response.get("Messages", []):
            try:
                await handler(json.loads(message["Body"]))
            except Exception as exc:
                print(f"{queue_name} job failed and will be retried: {exc}", flush=True)
                continue
            sqs.delete_message(QueueUrl=url, ReceiptHandle=message["ReceiptHandle"])


async def relay() -> None:
    urls = ensure_queues()
    while True:
        if not await relay_once(urls):
            await asyncio.sleep(0.25)


async def deadlines() -> None:
    while True:
        for tenant_id in await tenant_ids():
            async with tenant_transaction(tenant_id) as conn:
                rows = await (
                    await conn.execute(
                        """SELECT id FROM attempts WHERE status='in_progress'
                           AND expires_at<=clock_timestamp() FOR UPDATE SKIP LOCKED LIMIT 100"""
                    )
                ).fetchall()
                for row in rows:
                    await submit_attempt(conn, tenant_id, str(row["id"]), "deadline")
        await asyncio.sleep(1)


def replay_dlq(queue_name: str) -> int:
    urls = ensure_queues()
    if queue_name not in (SCORING_QUEUE, REPORT_QUEUE):
        raise SystemExit(f"queue must be {SCORING_QUEUE!r} or {REPORT_QUEUE!r}")
    sqs, count = client(), 0
    while True:
        response = sqs.receive_message(
            QueueUrl=urls[f"{queue_name}-dlq"],
            MaxNumberOfMessages=10,
            WaitTimeSeconds=1,
        )
        messages = response.get("Messages", [])
        if not messages:
            break
        for message in messages:
            sqs.send_message(QueueUrl=urls[queue_name], MessageBody=message["Body"])
            sqs.delete_message(
                QueueUrl=urls[f"{queue_name}-dlq"],
                ReceiptHandle=message["ReceiptHandle"],
            )
            count += 1
    return count


async def run(mode: str) -> None:
    await pool.open()
    await pool.wait()
    try:
        if mode == "relay":
            await relay()
        elif mode == "score":
            await consume(SCORING_QUEUE, process_scoring_job)
        elif mode == "report":
            await consume(REPORT_QUEUE, process_report_job)
        elif mode == "deadlines":
            await deadlines()
        else:
            raise SystemExit("mode must be relay, score, report, deadlines, or replay")
    finally:
        await pool.close()


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "replay":
        print(f"replayed {replay_dlq(sys.argv[2])} messages")
    else:
        asyncio.run(run(sys.argv[1] if len(sys.argv) > 1 else ""))
