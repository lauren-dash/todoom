#!/usr/bin/env bash
# Catch up on texts and WhatsApp as soon as the Mac is awake again.
# They can only be read while the Mac is on, so a refresh that ran while it slept
# missed them. Run every few minutes: if the last read of either failed, try just
# those two now. A Mac that is still asleep fails in seconds and costs nothing;
# one that is awake gets read and the list rebuilt.
set -uo pipefail
cd /home/user/workspace
STATUS=data/.skills/collect-outstanding-tasks/fetch_status.json
missed=$(python3 - "$STATUS" <<'PY'
import json, sys
try:
    status = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    status = {}
print(" ".join(k for k in ("imessage", "whatsapp") if k in status and not status[k].get("ok")))
PY
)
[ -z "$missed" ] && exit 0
args=()
for s in $missed; do args+=(--source "$s"); done
exec bash system/apps/task_inbox/refresh_tasks.sh "${args[@]}"
