#!/usr/bin/env bash
# Start SDR temperature server in a tmux session named "sdr"
# Usage: ./sdr/start_logger.sh [data_dir]

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="${1:-sdr}"
SESSION="sdr"

if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "Session '$SESSION' already running. Attach with: tmux attach -t $SESSION"
    exit 0
fi

tmux new-session -d -s "$SESSION" \
    "cd '$SCRIPT_DIR' && rtl_433 -F json -M time:iso 2>/dev/null | python3 temp_server.py --dir '$DATA_DIR'"

echo "Started sdr session: rtl_433 | temp_server.py"
echo "  dashboard: http://0.0.0.0:8433"
echo "  attach:    tmux attach -t $SESSION"
echo "  stop:      tmux kill-session -t $SESSION"
