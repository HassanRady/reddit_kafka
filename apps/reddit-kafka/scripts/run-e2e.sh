#!/usr/bin/env bash
set -Eeuo pipefail

e2e_project_name="${E2E_PROJECT_NAME:-reddit-kafka-e2e}"
compose=(docker compose -p "${e2e_project_name}" -f docker-compose.e2e.yml)

cleanup() {
  exit_code=$?
  if (( exit_code != 0 )); then
    "${compose[@]}" ps || true
    "${compose[@]}" logs --no-color --tail=300 || true
  fi
  if [[ "${E2E_KEEP_STACK:-0}" != "1" ]]; then
    "${compose[@]}" down --volumes --remove-orphans || true
  fi
  exit "${exit_code}"
}
trap cleanup EXIT

"${compose[@]}" up --detach --build --wait --wait-timeout 240 app-a app-b
"${compose[@]}" run --rm migrate
RUN_E2E=1 uv run --frozen pytest -q -W error tests/e2e
