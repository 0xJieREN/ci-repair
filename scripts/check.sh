#!/usr/bin/env bash
# Shared acceptance gate for local development, Linux servers and GitHub Actions.
set -euo pipefail
cd "$(dirname "$0")/.."
mode="${1:-full}"
case "$mode" in
  full|--unit|--colima) ;;
  *) echo 'Usage: bash scripts/check.sh [--unit|--colima]' >&2; exit 2 ;;
esac
if [ "$#" -gt 1 ]; then echo 'Only one mode may be selected' >&2; exit 2; fi
check_config_dir="$(mktemp -d "${TMPDIR:-/tmp}/ci-repair-check.XXXXXX")"
stop_colima=0
cleanup() {
  result=$?
  trap - EXIT
  if [ "$stop_colima" -eq 1 ]; then
    colima stop || result=1
  fi
  rm -rf "$check_config_dir"
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# Test runs do not need developer model credentials or persistent mini configuration.
export MSWEA_GLOBAL_CONFIG_DIR="$check_config_dir"
export MSWEA_SILENT_STARTUP=1
uv sync --locked
uv run ruff check src tests
uv run ruff format --check src tests
if [ "$mode" = '--unit' ]; then
  CI_REPAIR_DOCKER_TESTS=0 uv run pytest -q
else
  if [ "$mode" = '--colima' ]; then
    command -v colima >/dev/null
    stop_colima=1
    colima start --cpu 2 --memory 4 --disk 20
  fi
  docker info >/dev/null
  docker build -q -t ci-repair-demo:local -f examples/Dockerfile examples
  CI_REPAIR_DOCKER_TESTS=1 uv run pytest -q
fi
git diff --check
echo 'All requested checks passed.'
