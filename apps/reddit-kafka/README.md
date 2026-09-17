# Reddit On-Demand Streaming (reddit_kafka)

This repository implements an on-demand, scalable Reddit streaming system designed for high-throughput processing, crash recovery, and multi-instance coordination. It was built as a small platform to demonstrate a production-oriented streaming architecture using modern async Python tooling.

This README explains the architecture, design decisions, how to run the system (local Docker Compose), migration strategy, testing and linting commands, and provides highlights.

---

## Architecture (high level)

- FastAPI-based control plane that exposes a small HTTP API to start, stop, and
  inspect subreddit streams.
- Redis: hot-path state (checkpoints, locks) for very low-latency updates and distributed coordination.
- PostgreSQL: durable registry and checkpoint persistence (write-behind flusher batches updates to reduce DB pressure).
- Kafka: streaming backbone for high-throughput downstream processing (raw + derived topics).
- Worker model: per-subreddit `StreamWorker` consumes Reddit via `asyncpraw`, produces to Kafka, and stores checkpoints in Redis.
- `CheckpointFlusher`: background write-behind task that periodically flushes Redis checkpoints to Postgres.
- `DistributedLockManager`: Redis-based locks to prevent duplicate streams across multiple app instances.
- `ErrorHandler` + durable `stream_errors` records for resilience and incident
  debugging.
- OpenTelemetry traces, trace-correlated JSON logs, bounded-cardinality Prometheus
  metrics, and dependency readiness checks.

Design trade-offs
- Hot writes are kept in Redis (fast, in-memory) and committed to Postgres in batches to avoid DB hot-path write contention.
- Migrations are explicit SQL files and are run once as a one-shot job during local Docker startup to avoid race conditions.
- Time handling: timestamps are standardized to naive UTC before DB writes to avoid asyncpg timezone issues.

---

## Notable Source Files

- `src/app.py` — FastAPI app, lifespan, wiring of registry, lock manager, manager, flusher, cleanup tasks.
- `src/db.py` — SQLAlchemy async engine and session factory (`async_sessionmaker`).
- `src/repositories/stream_registry.py` — Registry backed by Redis (and optionally Postgres via a session maker).
- `src/stream/worker.py` — Stream worker that consumes Reddit and writes messages and checkpoints.
- `src/tasks/checkpoint_flusher.py` — Periodic batch upserter for checkpoints from Redis -> Postgres.
- `src/observability/` — Telemetry bootstrap, HTTP tracing, structured logging, and
  the Prometheus metric registry.
- `observability/` — Collector, Prometheus alerts, Loki, Jaeger, and provisioned
  Grafana resources.
- `src/migrations.py` and `migrations/*.sql` — Simple migration runner that executes SQL files in order.
- `docker-compose.yml` — Local stack with Postgres, Redis, Kafka (Confluent images), and the app + one-shot migrate job.

---

## Quickstart (Local, using Docker Compose)

Prerequisites: Docker & Docker Compose (v2+) installed.

Build and start the full stack (Postgres, Redis, Kafka, run migrations once, then start the API):

```bash
# from repository root
docker compose up --build
```

- The `migrate` service runs `python -m src.migrations` once after Postgres becomes healthy.
- The `app` service depends on `migrate` so the API starts only after migrations finish.

To run only the migration step (helpful for iterating on SQL files):

```bash
docker compose run --rm migrate
```

To reset the local DB and re-run from scratch:

```bash
docker compose down -v
docker compose up --build
```

If you prefer not to use Docker, see the next section for running migrations and tests locally via uv.

---

## Local dev (without Docker)

This project uses uv with dependencies declared in `pyproject.toml` and pinned in
`uv.lock`. Install [uv](https://docs.astral.sh/uv/getting-started/installation/),
then run:

```bash
# create .venv and install locked runtime and development dependencies
uv sync --frozen

# run migrations against a running Postgres instance
uv run --frozen python -m src.migrations

# run tests
uv run --frozen pytest -v

# static checks
uv run --frozen mypy src
uv run --frozen ruff check src
```

Notes
- `src/migrations.py` has two helpers: `run_migrations()` (executes SQL files in `migrations/`) and `create_tables_from_models()` (convenience helper that uses SQLAlchemy `Base.metadata`). Prefer SQL-file migrations for production-like reproducibility.

---

## API (control plane)

- GET /health
  - Process liveness without dependency checks.

- GET /ready
  - Readiness for Redis, PostgreSQL, Kafka, and Reddit initialization.

- GET /metrics
  - Prometheus metrics. Hidden from OpenAPI and blocked at the public AWS ALB.

- POST /streams?subreddit=<name>
  - Start streaming the subreddit (creates registry entry, worker is started on the instance that acquired the lock).
  - Example: `curl -X POST "http://localhost:8000/streams?subreddit=python"`

- GET /streams
  - List registry entries and stream metadata.

- POST /streams/{stream_id}/stop
  - Tell the instance to stop the stream (worker exits gracefully, writes final checkpoint to Redis, flusher persists it to Postgres).

The system uses optimistic worker placement and Redis locks to ensure only one instance streams a given subreddit.

---

## Migrations and schema

- SQL migrations live in the `migrations/` directory (e.g. `migrations/001_initial_schema.sql`).
- Pending migrations are run in lexicographic order by `src/migrations.py` and
  recorded with checksums in the `schema_migrations` table.
- The Docker Compose file is configured with a one-shot `migrate` service so schema changes run once during startup.

Production note: In real deployments prefer running migrations as a release step (CI/CD job) instead of a containerized one-shot run by the orchestration system.

---

## Tests, linting, typing

- Unit tests are under `tests/unit/` and use `pytest` + `pytest-asyncio`.
- Type checks: `mypy src` (SQLAlchemy `async_sessionmaker` typed, functions annotated).
- Linting: `ruff check src`.

Recommended dev flow:

```bash
# run tests
uv run --frozen pytest -q

# static checks
uv run --frozen mypy src
uv run --frozen ruff check src
```

### End-to-end tests

The E2E suite starts two independent application containers and real Kafka,
Redis, and PostgreSQL services. It verifies migrations and schema contracts;
API validation and health; atomic concurrent creation and duplicate rejection;
Kafka delivery (including deleted authors); Redis and PostgreSQL checkpoints;
recoverable, malformed-comment, and fatal-error handling; durable error records;
cross-instance and idempotent termination; lock loss and stale-owner safety;
dead-stream cleanup and stable restart identity; and that production stops after
termination.

Reddit and AWS Glue are replaced at their application boundaries with deterministic
test doubles, so the suite needs no cloud credentials and does not depend on live
Reddit availability. The production application, worker, Kafka producer, state
stores, migrations, and multi-instance control flow remain unchanged.

Broker/database outages and live Reddit/AWS compatibility remain deployment-level
smoke or chaos-test concerns; deterministic worker delivery failures, migration
checksum failures, and serializer behavior are covered by the unit suite.

Run the suite from `apps/reddit-kafka`:

```bash
./scripts/run-e2e.sh
```

On failure, service state and recent logs are printed automatically. Set
`E2E_KEEP_STACK=1` to leave the containers running for diagnosis.

---

## Configuration

Environment is configured with pydantic settings in `src/config.py` (or via `.env` file in the project root). Important variables:

- POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB / POSTGRES_HOST / POSTGRES_PORT
- REDIS_HOST / REDIS_PORT / REDIS_USER / REDIS_PASSWORD
- KAFKA_BOOTSTRAP_SERVERS / KAFKA_RAW_TEXT_TOPIC
- REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET / REDDIT_USER_AGENT
- OTEL_EXPORTER_OTLP_ENDPOINT / OTEL_TRACE_SAMPLE_RATIO / TRACES_ENABLED
- OTEL_LOGS_EXPORT_ENABLED / JSON_LOGS / LOG_LEVEL / DEPLOYMENT_ENVIRONMENT

The provided `docker-compose.yml` includes reasonable local defaults. Use a `.env` file to override them for your environment.

Start the complete local telemetry profile with:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317 \
  docker compose --profile observability up --build
```

See [`observability/README.md`](observability/README.md) for dashboards, alerts,
signal definitions, and production guidance.

---

## Production considerations

- Migrations: run via CI/CD release step, use a database migration tool (e.g. Alembic) for more advanced schema versioning.
- Lock safety: Redis-based locks are sufficient for the demo; consider RedLock or a database-backed leader election for stronger guarantees across geo-distributed instances.
- Checkpoint durability: write-behind flusher reduces DB pressure. If you need stronger durability guarantees, flush on lifecycle events (stop/pause) immediately and consider increasing flush frequency.
- Observability: Prometheus metrics, OpenTelemetry tracing, trace-correlated JSON
  logs, readiness checks, alert rules, and a provisioned Grafana/Jaeger/Loki stack
  are included. The dashboard distinguishes Reddit comments observed, processing
  outcomes, Kafka-acknowledged messages, and delivered payload bytes. See
  [`observability/README.md`](observability/README.md).

---

## highlights


- Designed and implemented an on-demand Reddit streaming platform using FastAPI, asyncpraw, Redis (hot-path locks & checkpoints), PostgreSQL (durable registry), and Kafka for high-throughput message routing.
- Implemented a write-behind `CheckpointFlusher` to batch checkpoint upserts to Postgres, reducing DB pressure while maintaining crash-recovery semantics.
- Built distributed coordination using Redis for a lightweight lock manager that prevents duplicate stream workers across multiple instances.
- Ensured robust asynchronous behavior with cancel-safe workers, graceful shutdown (final checkpoint flush), and a one-shot migration runner for deterministic schema setup.
- Added unit tests (pytest + pytest-asyncio), static typing (mypy), and linting (ruff) to maintain code quality across an async codebase.

---



