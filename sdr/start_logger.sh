#!/usr/bin/env bash
# Start temp logger + web dashboard in a tmux session named "sdr"
# Usage: ./sdr/start_logger.sh [output_dir]

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
OUT_DIR="${1:-sdr/sdr}"
SESSION="sdr"

# Use project venv if available
PYTHON="python3"
if [ -f "$PROJECT_DIR/.venv/bin/python" ]; then
    PYTHON="$PROJECT_DIR/.venv/bin/python"
fi

if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "Session '$SESSION' already running. Attach with: tmux attach -t $SESSION"
    exit 0
fi

# Window 0: rtl_433 -> temp_logger
tmux new-session -d -s "$SESSION" -n logger \
    "cd '$PROJECT_DIR' && rtl_433 -F json -M time:iso 2>/dev/null | $PYTHON '$SCRIPT_DIR/temp_logger.py' '$OUT_DIR'"

# Window 1: web dashboard
tmux new-window -t "$SESSION" -n web \
    "cd '$PROJECT_DIR' && $PYTHON '$SCRIPT_DIR/web_temps.py' --dir '$OUT_DIR'"

# Default to logger window on attach
tmux select-window -t "$SESSION:logger"

echo "Started sdr session with 2 windows:"
echo "  logger: rtl_433 | temp_logger.py"
echo "  web:    http://0.0.0.0:8433"
echo ""
echo "  attach:  tmux attach -t $SESSION"
echo "  stop:    tmux kill-session -t $SESSION"
