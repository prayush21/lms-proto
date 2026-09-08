import argparse
import asyncio
import random
import time
import uuid

import httpx
import psycopg

from app.auth import issue_token
from app.config import DATABASE_ADMIN_URL
from scripts.ids import TENANT_A


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    index = (len(ordered) - 1) * p
    lower, upper = int(index), min(int(index) + 1, len(ordered) - 1)
    fraction = index - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def create_students(count: int, run_id: str) -> list[str]:
    ids = [
        str(uuid.uuid5(uuid.UUID(TENANT_A), f"load:{run_id}:{n}")) for n in range(count)
    ]
    with psycopg.connect(DATABASE_ADMIN_URL) as conn:
        for n, user_id in enumerate(ids):
            conn.execute(
                """INSERT INTO users (id,email,display_name) VALUES (%s,%s,%s)
                   ON CONFLICT (id) DO NOTHING""",
                (user_id, f"load-{run_id}-{n}@example.test", f"Load Student {n}"),
            )
            conn.execute(
                """INSERT INTO memberships (tenant_id,user_id,role)
                   VALUES (%s,%s,'student') ON CONFLICT DO NOTHING""",
                (TENANT_A, user_id),
            )
    return ids


async def student_run(
    http: httpx.AsyncClient,
    start: asyncio.Event,
    user_id: str,
    question_count: int,
    autosave_window: float,
    submit_window: float,
) -> tuple[float, float]:
    token = issue_token(TENANT_A, user_id, "student")
    headers = {"Authorization": f"Bearer {token}"}
    await start.wait()
    response = await http.post(
        "/attempts/quiz",
        headers=headers,
        json={"subjects": ["Math"], "count": question_count},
    )
    response.raise_for_status()
    attempt_id = response.json()["id"]
    delivery = await http.get(f"/attempts/{attempt_id}/delivery", headers=headers)
    delivery.raise_for_status()
    items = delivery.json()["sections"][0]["items"]
    revision = 0
    interval = max(0.25, autosave_window / 4)
    deadline = time.perf_counter() + autosave_window
    while time.perf_counter() < deadline:
        item = random.choice(items)
        revision += 1
        answer = random.choice(item["options"])["id"]
        saved = await http.put(
            f"/attempts/{attempt_id}/answers/{item['id']}",
            headers=headers,
            json={"selected_option_id": answer, "client_revision": revision},
        )
        saved.raise_for_status()
        await asyncio.sleep(interval)
    await asyncio.sleep(random.uniform(0, submit_window))
    submitted_at = time.perf_counter()
    submitted = await http.post(f"/attempts/{attempt_id}/submit", headers=headers)
    submitted.raise_for_status()
    submission_latency_ms = (time.perf_counter() - submitted_at) * 1000
    score_started = time.perf_counter()
    while True:
        heartbeat = await http.get(f"/attempts/{attempt_id}/heartbeat", headers=headers)
        heartbeat.raise_for_status()
        if heartbeat.json()["score_status"] == "complete":
            break
        if time.perf_counter() - score_started > 120:
            raise RuntimeError(f"scoring timed out for {attempt_id}")
        await asyncio.sleep(0.1)
    time_to_score_ms = (time.perf_counter() - submitted_at) * 1000
    return submission_latency_ms, time_to_score_ms


async def run(args):
    run_id = uuid.uuid4().hex[:8]
    users = create_students(args.students, run_id)
    start = asyncio.Event()
    limits = httpx.Limits(max_connections=max(100, args.students * 2))
    async with httpx.AsyncClient(
        base_url=args.base_url, timeout=30, limits=limits
    ) as http:
        tasks = [
            asyncio.create_task(
                student_run(
                    http,
                    start,
                    user,
                    args.questions,
                    args.autosave_window,
                    args.submit_window,
                )
            )
            for user in users
        ]
        print(f"releasing {args.students} students together (run {run_id})")
        start.set()
        results = await asyncio.gather(*tasks)
    submissions = [result[0] for result in results]
    scoring = [result[1] for result in results]
    print("metric             p50 ms    p95 ms    p99 ms")
    for name, values in (("submission", submissions), ("time-to-score", scoring)):
        print(
            f"{name:<17} {percentile(values, 0.50):>8.1f} "
            f"{percentile(values, 0.95):>9.1f} {percentile(values, 0.99):>9.1f}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bursty assessment workload")
    parser.add_argument("-n", "--students", type=int, default=50)
    parser.add_argument("--questions", type=int, default=6)
    parser.add_argument("--autosave-window", type=float, default=30)
    parser.add_argument("--submit-window", type=float, default=60)
    parser.add_argument("--base-url", default="http://localhost:8000")
    asyncio.run(run(parser.parse_args()))
