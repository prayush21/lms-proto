# Multi-tenant assessment engine prototype

A compact FastAPI/PostgreSQL/Redis/ElasticMQ prototype focused on tenant isolation,
attempt materialization, race-safe autosave/submission, and at-least-once workers.

## Run it

Requirements: Docker with Compose. All commands are run from this directory.

```bash
docker compose up --build -d
docker compose run --rm api python -m scripts.seed
curl http://localhost:8000/health
```

Create a development token for the seeded Alpha student:

```bash
curl -s http://localhost:8000/dev/tokens \
  -H 'content-type: application/json' \
  -d '{"tenant_id":"10000000-0000-0000-0000-000000000001","user_id":"10000000-0000-0000-0000-000000000101"}'
```

The interactive API is at <http://localhost:8000/docs>. Seed output identifies
both student accounts. JWTs are signed development sessions; protected routes
derive tenancy only from their signed `tenant_id` claim.

To rebuild the database after changing `db/init.sql`:

```bash
docker compose down -v
docker compose up --build -d
docker compose run --rm api python -m scripts.seed
```

## Prove the mechanics

The integration tests expect the Compose services to be running:

```bash
docker compose exec api pytest -q
```

They prove cross-tenant 404 behavior, an intentionally unfiltered RLS query,
idempotent submission, monotonic autosave, and idempotent scoring redelivery.

Run a short burst locally:

```bash
docker compose run --rm api python -m scripts.load \
  -n 50 --autosave-window 10 --submit-window 10 --base-url http://api:8000
```

For the requested full submission shape, omit both window flags (defaults are
30 seconds of autosaving followed by submissions spread over 60 seconds). The
script prints p50/p95/p99 API submission latency and end-to-end time-to-score.

Watch the pipeline:

```bash
docker compose logs -f relay scoring-worker report-worker deadline-worker
```

Replay either dead-letter queue:

```bash
docker compose run --rm api python -m app.worker replay fast-scoring
docker compose run --rm api python -m app.worker replay report-generation
```

## Design map

- Middleware validates the session and resets its request `ContextVar` in a
  `finally` block.
- Every application transaction runs `set_config('app.tenant_id', ..., true)`,
  PostgreSQL's transaction-local equivalent of `SET LOCAL`.
- The runtime role is not a table owner and cannot bypass forced RLS.
- Mock and quiz starts both write `attempt_sections` and `attempt_items`; those
  rows pin immutable question-version IDs and per-item shuffle seeds.
- Submission's conditional state transition, unique submission row, and scoring
  outbox event commit together.
- The relay is intentionally at-least-once across a crash. Worker transactions
  claim `processed_jobs` before writing scores/reports.
- Percentiles query only scores visible under the job's tenant context and only
  compare the same attempt kind.

Redis is included in readiness checks but deliberately kept out of correctness
paths: PostgreSQL remains authoritative for timers, admission, and idempotency.

The global catalog is owned by a reserved platform tenant. Customer RLS policies
can read it but cannot write it; this avoids nullable ownership and preserves
composite foreign keys. Taxonomy and reports are intentionally basic for this
mechanics-focused prototype.
