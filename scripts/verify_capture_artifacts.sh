#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CAPTURE_ROOT="$ROOT_DIR/artifacts/captures"
EXPECTED_COUNT="${1:-18}"

if [[ ! -d "$CAPTURE_ROOT" ]]; then
  echo "capture root not found: $CAPTURE_ROOT" >&2
  exit 1
fi

actual_count="$(find "$CAPTURE_ROOT" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')"
echo "capture directories found: $actual_count"
echo "expected capture directories: $EXPECTED_COUNT"

if [[ "$actual_count" != "$EXPECTED_COUNT" ]]; then
  echo "capture directory count mismatch" >&2
fi

shopt -s nullglob
matches=("$CAPTURE_ROOT"/*-severe_pi_* "$CAPTURE_ROOT"/*-severe_md_*)
shopt -u nullglob

if [[ "${#matches[@]}" -eq 0 ]]; then
  echo "no severe_pi_*/severe_md_* capture directories found for spot check" >&2
  exit 2
fi

sample_dir="${matches[0]}"
event_file="$sample_dir/events/opencode_events.jsonl"

echo "spot check directory: $sample_dir"
if [[ ! -s "$event_file" ]]; then
  echo "event log missing or empty: $event_file" >&2
  exit 3
fi

line_count="$(wc -l < "$event_file" | tr -d ' ')"
echo "event log present: $event_file"
echo "event line count: $line_count"

echo "verification complete"
