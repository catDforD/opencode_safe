#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ISSUES_FILE="$ROOT_DIR/tasks/issues.json"
COMPOSE_FILE="$ROOT_DIR/infra/docker-compose.yml"
RUNNER_ENV_FILE="$ROOT_DIR/config/runner.env"
COOLDOWN_SECONDS="${CAPTURE_COOLDOWN_SECONDS:-20}"

require_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "missing required command: $cmd" >&2
    exit 1
  fi
}

if [[ ! -f "$ISSUES_FILE" ]]; then
  echo "issues file not found: $ISSUES_FILE" >&2
  exit 1
fi

require_cmd jq
require_cmd make

mapfile -t ISSUE_IDS < <(jq -r '.[].id' "$ISSUES_FILE")

if [[ "${#ISSUE_IDS[@]}" -eq 0 ]]; then
  echo "no issue ids found in $ISSUES_FILE" >&2
  exit 1
fi

echo "found ${#ISSUE_IDS[@]} issue(s) in $ISSUES_FILE"

log_memory() {
  local stage="$1"
  echo "----- memory snapshot: $stage -----"
  free -m
  echo "-----------------------------------"
}

cleanup_between_runs() {
  if [[ "${USE_DOCKER_SANDBOX:-1}" != "1" ]]; then
    return
  fi

  echo "restarting sandbox services to release container memory"
  (
    cd "$ROOT_DIR/infra"
    docker compose restart audit runner
  )

  echo "pruning unused docker resources"
  docker system prune -f

  log_memory "before cooldown"
  echo "sleeping ${COOLDOWN_SECONDS}s for host GC"
  sleep "$COOLDOWN_SECONDS"
  log_memory "after cooldown"
}

if [[ "${USE_DOCKER_SANDBOX:-1}" == "1" ]]; then
  require_cmd docker
  require_cmd free

  if [[ ! -f "$COMPOSE_FILE" ]]; then
    echo "docker compose file not found: $COMPOSE_FILE" >&2
    exit 1
  fi

  if [[ ! -f "$RUNNER_ENV_FILE" ]]; then
    echo "runner env file not found: $RUNNER_ENV_FILE" >&2
    echo "copy config/runner.env.example to config/runner.env and fill in real secrets first" >&2
    exit 1
  fi

  (
    cd "$ROOT_DIR/infra"
    docker compose --profile runner up -d audit runner
  )

  if [[ "${RUN_INSIDE_DOCKER:-0}" != "1" ]]; then
    echo "docker services are up, but this script is configured to avoid host-side execution." >&2
    echo "run this script from inside the prepared sandbox with RUN_INSIDE_DOCKER=1 when the repo is mounted there." >&2
    exit 1
  fi
fi

total="${#ISSUE_IDS[@]}"
failed_issues=()
for idx in "${!ISSUE_IDS[@]}"; do
  issue_id="${ISSUE_IDS[$idx]}"
  printf '正在执行 [%d/%d]: %s\n' "$((idx + 1))" "$total" "$issue_id"

  run_status=0
  (
    cd "$ROOT_DIR"
    make capture ISSUE="$issue_id"
  ) || run_status=$?

  if [[ "$run_status" -ne 0 ]]; then
    echo "capture failed for $issue_id with exit code $run_status" >&2
    failed_issues+=("$issue_id")
  fi

  cleanup_between_runs
done

if [[ "${#failed_issues[@]}" -gt 0 ]]; then
  echo "completed with failures: ${failed_issues[*]}" >&2
  exit 1
fi

echo "all capture runs completed"
