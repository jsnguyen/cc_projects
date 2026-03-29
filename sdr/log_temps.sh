#!/usr/bin/env bash
# Log 433 MHz temperature sensors to weekly .npz chunks
# Usage: ./sdr/log_temps.sh [output_dir]

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUT_DIR="${1:-sdr/sdr}"

rtl_433 -F json -M time:iso 2>/dev/null | python "$SCRIPT_DIR/temp_logger.py" "$OUT_DIR"
