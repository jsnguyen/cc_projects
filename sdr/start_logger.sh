#!/usr/bin/env bash
# Start SDR temperature server + Telegram bot in a tmux session named "sdr"
# Usage: ./sdr/start_logger.sh [data_dir]
#
# Requires env vars for the Telegram bot:
#   TELEGRAM_BOT_TOKEN
#   TELEGRAM_USER_ID

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="${1:-sdr}"
SESSION="sdr"

if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "Session '$SESSION' already running. Attach with: tmux attach -t $SESSION"
    exit 0
fi

# Window 0: rtl_433 | temp_server
tmux new-session -d -s "$SESSION" -n server \
    "cd '$SCRIPT_DIR' && rtl_433 -F json -M time:iso 2>/dev/null | python3 temp_server.py --dir '$DATA_DIR'"

# Window 1: Telegram bot (if config file or env var exists)
if [ -n "$TELEGRAM_BOT_TOKEN" ] || [ -f "$HOME/.sdr_tg.json" ]; then
    tmux new-window -t "$SESSION" -n tgbot
    tmux set-option -t "$SESSION:tgbot" remain-on-exit on
    tmux send-keys -t "$SESSION:tgbot" "cd '$SCRIPT_DIR' && python3 tg_temps.py" Enter
fi

tmux select-window -t "$SESSION:server"

echo "Started sdr session:"
echo "  server:    rtl_433 | temp_server.py"
echo "  dashboard: http://0.0.0.0:8433"
{ [ -n "$TELEGRAM_BOT_TOKEN" ] || [ -f "$HOME/.sdr_tg.json" ]; } && echo "  tgbot:     running"
echo ""
echo "  attach:    tmux attach -t $SESSION"
echo "  stop:      tmux kill-session -t $SESSION"
