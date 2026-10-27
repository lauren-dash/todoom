#!/usr/bin/env bash
# Pull a fresh task list: runs the collect-outstanding-tasks skill once.
# Used by the app's Refresh button and by the daily schedule. A run started
# while another is going is skipped (exit 75), so the two never overlap.
set -uo pipefail
cd /home/user/workspace
DATA_DIR="${TASK_INBOX_DATA_DIR:-data/.apps/task-inbox}"
mkdir -p "$DATA_DIR"
exec 9>"$DATA_DIR/refresh.lock"
flock -n 9 || { echo "a refresh is already running"; exit 75; }
# Outside a chat nothing points claude at the workspace's signed-in account, and
# the AI step fails; use the most recently used account, as the chat does.
if [ -z "${CLAUDE_CONFIG_DIR:-}" ]; then
  account=$(python3 -c 'import json, os; d = json.load(open(os.path.expanduser("~/.minds/accounts/index.json"))); print(d.get("mru") or "")' 2>/dev/null)
  [ -n "$account" ] && [ -d "$HOME/.minds/accounts/$account" ] && export CLAUDE_CONFIG_DIR="$HOME/.minds/accounts/$account"
fi
started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
uv run .agents/skills/collect-outstanding-tasks/scripts/run.py all > "$DATA_DIR/refresh.log" 2>&1
code=$?
printf '{"started_at": "%s", "finished_at": "%s", "exit_code": %d}\n' "$started" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$code" > "$DATA_DIR/refresh_status.json"
exit $code
