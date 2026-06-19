#!/bin/bash
set -euo pipefail

# Generate per-strategy and comparison plots from downloaded cluster results.
# Automatically detects which strategies have reports and runs comparison
# with any 2+ strategies (does not require all 3).
#
# Usage:
#   ./experiments/generate-plots.sh <results-dir>
#   ./experiments/generate-plots.sh guide-run-4

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLOTS_DIR="${SCRIPT_DIR}/plots"

if [ $# -lt 1 ]; then
  echo "Usage: $0 <results-dir>"
  echo "  results-dir: path to cluster results (e.g. guide-run-4)"
  exit 1
fi

RESULTS_DIR="$1"
if [ ! -d "$RESULTS_DIR" ]; then
  echo "ERROR: $RESULTS_DIR is not a directory"
  exit 1
fi

# --- Per-strategy plots ---
echo "=== Per-strategy analysis ==="
for strategy_dir in "${RESULTS_DIR}"/*/; do
  reports_dir="${strategy_dir}reports"
  if [ -d "$reports_dir" ] && [ -n "$(ls -A "$reports_dir" 2>/dev/null)" ]; then
    strategy=$(basename "$strategy_dir")
    echo "  Generating plots for $strategy..."
    python3 "${PLOTS_DIR}/analyze_reports.py" \
      "$reports_dir" \
      -o "${strategy_dir}" \
      2>&1 | tail -3
  fi
done

# --- Comparison plots (any 2+ strategies) ---
echo ""
echo "=== Comparison analysis ==="

COMPARE_ARGS=()
STRATEGY_COUNT=0

for strategy in no-fairness las rr drr; do
  reports_dir="${RESULTS_DIR}/${strategy}/reports"
  if [ -d "$reports_dir" ] && [ -n "$(ls -A "$reports_dir" 2>/dev/null)" ]; then
    COMPARE_ARGS+=("--${strategy}" "$reports_dir")
    STRATEGY_COUNT=$((STRATEGY_COUNT + 1))
    echo "  Found: $strategy"
  fi
done

if [ "$STRATEGY_COUNT" -ge 2 ]; then
  COMPARE_OUT="${RESULTS_DIR}/comparison"
  mkdir -p "$COMPARE_OUT"
  echo "  Generating comparison plots (all sessions)..."
  python3 "${PLOTS_DIR}/compare_reports.py" \
    "${COMPARE_ARGS[@]}" \
    -o "$COMPARE_OUT" \
    2>&1 | tail -5

  COMPARE_MATCHED="${RESULTS_DIR}/comparison-matched"
  mkdir -p "$COMPARE_MATCHED"
  echo "  Generating comparison plots (matched sessions only)..."
  python3 "${PLOTS_DIR}/compare_reports.py" \
    "${COMPARE_ARGS[@]}" \
    --matched-only \
    -o "$COMPARE_MATCHED" \
    2>&1 | tail -5

  echo "  Comparison plots saved to: $COMPARE_OUT and $COMPARE_MATCHED"
else
  echo "  SKIP: Need at least 2 strategies with reports for comparison (found $STRATEGY_COUNT)."
fi

echo ""
echo "Done. Results in: $RESULTS_DIR"
