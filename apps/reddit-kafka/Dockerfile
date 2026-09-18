FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.24 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

COPY pyproject.toml uv.lock ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH="/app"

RUN groupadd -g 10001 appgroup && \
    useradd -m -u 10001 -g appgroup appuser

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

COPY ./src ./src
COPY ./migrations ./migrations
COPY ./schemas ./schemas

RUN chown -R appuser:appgroup /app
USER appuser

EXPOSE 8000
ENV PORT=8000

CMD ["sh", "-c", "uvicorn src.app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --workers ${WEB_CONCURRENCY:-1}"]

