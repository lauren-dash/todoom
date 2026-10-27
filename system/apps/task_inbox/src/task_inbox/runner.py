"""Outstanding tasks from email, Slack, texts, WhatsApp, Docs and Notion.

Services run from /home/user/workspace (the repo root). Conventions:

- Persistent state (anything written and read across runs -- cursors,
  caches, snapshots, user records): read and write it under ``DATA_DIR``
  (defined below), never a hardcoded ``data/.apps/task-inbox/`` at the
  call site. ``DATA_DIR`` defaults to ``data/.apps/task-inbox/`` but
  honors the ``TASK_INBOX_DATA_DIR`` env var, so an editing agent can point a
  throwaway instance at a *copy* of the data instead of the live store
  (see the update-app skill). Do NOT use ``Path(__file__)``-based
  paths for state -- the bug to avoid is one process writing to
  ``/home/user/workspace/data/.apps/...`` while another reads from
  ``/home/user/workspace/system/apps/<pkg>/data/...``.
- Static assets shipped alongside this file (templates, default
  configs, bundled JSON): ``Path(__file__).parent / "assets/..."`` is
  fine and is the right pattern.
- Listen port: bind ``PORT`` (defined below), which defaults to this
  app's assigned port but honors the ``TASK_INBOX_PORT`` env var, so
  an editing agent can boot a throwaway instance on a *spare* port
  alongside the live one (see the update-app skill). Never hardcode
  the port at the ``run_simple`` call.

This is a synchronous Flask app served by the threaded Werkzeug server.
The app owns its own browser origin (the forwarder routes
``http://task-inbox.<workspace-host>/`` straight to this port), so it serves
at ``/`` and root-absolute URLs, cookies, and service workers all work
unmodified -- nothing rewrites anything. Use ``flask_sock`` if you need
WebSockets.
"""

import datetime
import fcntl
import subprocess
import json
import re
import os
import threading
import uuid
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_file
from werkzeug.serving import run_simple

# Persistent state for this app lives under DATA_DIR. It defaults to
# ``data/.apps/task-inbox/`` but is overridable via the ``TASK_INBOX_DATA_DIR`` env var
# so a throwaway instance can run against a *copy* of the data while editing --
# see the update-app skill. Always read/write state through DATA_DIR;
# never hardcode ``data/.apps/task-inbox/`` at a call site, or the override is
# bypassed. A writing call site should ``DATA_DIR.mkdir(parents=True,
# exist_ok=True)`` before writing.
DATA_DIR = Path(os.environ.get("TASK_INBOX_DATA_DIR", "data/.apps/task-inbox"))

# Listen port. Defaults to this app's assigned port but is overridable via
# the ``TASK_INBOX_PORT`` env var so an editing agent can boot a throwaway
# instance on a spare port next to the live one (see the update-app skill).
# Never hardcode the port at the ``run_simple`` call, or the override is bypassed.
PORT = int(os.environ.get("TASK_INBOX_PORT", "8080"))

# The browser-side modules the workspace shell builds and every app serves from
# its own origin: the app contract (how a page talks to the shell framing it) and
# the element context menu (the right-click menu whose last rows hand the
# clicked element to a chat). A module import is a fetch without cookies, which
# the forwarder refuses across origins, so they are served here rather than from
# the shell. Relative to the repo root the service runs from, like DATA_DIR.
SHELL_STATIC_MODULES_DIR = Path("system/apps/system_interface/imbue/system_interface/static/_static")
SHELL_STATIC_MODULE_NAMES = ('app_contract.js', 'context_menu.js')

# The script every page serves (keep it on every page): it connects the page to
# the shell, reports where the page is on the handshake so the shell can reopen
# this app's window at the same place, and installs the element context menu.
# A page visited outside the shell runs it harmlessly: nothing arrives, and the
# menu's Explain and Modify rows grey out.
SHELL_PAGE_SCRIPT = """<script type="module">
  import { connectToShell } from "/_static/app_contract.js";
  import { installElementContextMenu } from "/_static/context_menu.js";
  let handshake = null;
  const connection = connectToShell({
    onHandshake: (received) => {
      handshake = received;
      connection.location(location.pathname + location.search, document.title);
    },
  });
  installElementContextMenu({ connection, handshake: () => handshake });
  window.shellConnection = connection;
</script>"""

app = Flask("task_inbox", static_folder=None)


# The task list is written by the task finder (the collect-outstanding-tasks
# skill); this app only reads it. Everything the user does is the app's own
# state, kept beside it: check-offs, edits to found tasks, and tasks they add.
TASKS_FILE = DATA_DIR / "tasks.json"
CHECKED_FILE = DATA_DIR / "checked.json"
EDITS_FILE = DATA_DIR / "edits.json"
MANUAL_FILE = DATA_DIR / "manual.json"
# Tasks the user marked "not a task", with their optional reason. The task finder
# reads this file to learn what not to pull; the app hides these tasks.
DISMISSED_FILE = DATA_DIR / "dismissed.json"
# What the user checked off, with enough of each task for the task finder to
# recognise it (and reminders about it) and not list it again. Kept in step
# with checked.json: added on check, removed on uncheck.
COMPLETED_FILE = DATA_DIR / "completed.json"
# Tasks the user handed to an agent ("Do it for me"), so the list can show it.
HANDOFFS_FILE = DATA_DIR / "handoffs.json"
SOURCE_LABELS = {
    "email": "Gmail", "slack": "Slack", "google-docs": "Google Docs", "notion": "Notion",
    "whatsapp": "WhatsApp", "text": "Messages", "manual": "added by me in TODOOM",
}
# A checked task stays visible (crossed off) for this long, then disappears.
CLEAR_AFTER = datetime.timedelta(days=1)
# Check-offs and edits for tasks the finder no longer reports are forgotten after this long.
FORGET_AFTER = datetime.timedelta(days=60)
# The fields a user may change on any task.
EDITABLE = ("title", "why", "due", "tab")
TABS = ("personal", "work")
_state_lock = threading.Lock()


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def _write_json(path: Path, value) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=1, ensure_ascii=False))
    tmp.replace(path)


class TaskFieldError(ValueError):
    """A task edit or new task the app refuses (shown to the user as the reason)."""


def _clean_fields(body: dict) -> dict:
    """The editable fields from a request body, validated; raises ValueError."""
    out = {}
    for key in EDITABLE:
        if key not in body:
            continue
        value = body[key]
        if key == "tab":
            if value not in TABS:
                raise TaskFieldError("tab must be personal or work")
        elif key == "due":
            if value in (None, ""):
                value = None
            else:
                datetime.date.fromisoformat(value)
        else:
            value = str(value or "").strip()
            if key == "title" and not value:
                raise TaskFieldError("a task needs a title")
        out[key] = value
    return out


def _all_tasks() -> tuple[dict, list[dict]]:
    """The finder's list with the user's edits applied, plus the tasks the user added."""
    data = _read_json(TASKS_FILE, {"tasks": [], "generated": None})
    edits = _read_json(EDITS_FILE, {})
    tasks = []
    for task in data.get("tasks", []):
        # An edit made before duplicates were merged may sit under a merged id.
        edit = edits.get(task["id"]) or next((edits[m] for m in task.get("merged_ids", []) if m in edits), None)
        if edit:
            task = {**task, **edit["fields"], "edited": True}
        tasks.append(task)
    tasks.extend(_read_json(MANUAL_FILE, []))
    return data, tasks


def _task_ids(task: dict) -> list[str]:
    return [task["id"], *task.get("merged_ids", [])]


@app.route("/")
def index() -> Response:
    page = (Path(__file__).parent / "page.html").read_text()
    return Response(page.replace("__SHELL__", SHELL_PAGE_SCRIPT), mimetype="text/html")


ASSETS_DIR = Path(__file__).parent / "assets"
ASSET_TYPES = {"departure-mono.woff2": "font/woff2"}


@app.route("/assets/<name>")
def asset(name: str) -> Response:
    if name not in ASSET_TYPES:
        abort(404)
    return send_file(ASSETS_DIR / name, mimetype=ASSET_TYPES[name], max_age=86400)


@app.route("/api/tasks")
def api_tasks() -> Response:
    data, all_tasks = _all_tasks()
    now = _now()
    with _state_lock:
        checked = _read_json(CHECKED_FILE, {})
        dismissed = _read_json(DISMISSED_FILE, {})
        handoffs = _read_json(HANDOFFS_FILE, {})
    tasks = []
    for task in all_tasks:
        if any(i in dismissed for i in _task_ids(task)):
            continue
        if task["id"] in handoffs:
            task = {**task, "handoff": handoffs[task["id"]]}
        # A task counts as checked if it, or any duplicate folded into it, was checked.
        stamps = [checked[i] for i in _task_ids(task) if i in checked]
        if stamps:
            checked_at = min(stamps)
            clears_at = datetime.datetime.fromisoformat(checked_at) + CLEAR_AFTER
            if clears_at <= now:
                continue
            task = {**task, "checked_at": checked_at, "clears_at": clears_at.isoformat()}
        tasks.append(task)
    return jsonify({**data, "tasks": tasks, "now": now.isoformat()})


@app.route("/api/tasks", methods=["POST"])
def api_add() -> Response:
    body = request.get_json(silent=True) or {}
    try:
        fields = _clean_fields({"tab": "personal", **body})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if "title" not in fields:
        return jsonify({"error": "a task needs a title"}), 400
    task = {
        "id": "manual-" + uuid.uuid4().hex[:12], "source": "manual", "from": None, "url": None,
        "why": "", "due": None, **fields, "date": _now().isoformat(), "raw": None,
    }
    with _state_lock:
        manual = _read_json(MANUAL_FILE, [])
        manual.append(task)
        _write_json(MANUAL_FILE, manual)
    return jsonify(task), 201


@app.route("/api/tasks/<task_id>", methods=["PATCH"])
def api_edit(task_id: str) -> Response:
    try:
        fields = _clean_fields(request.get_json(silent=True) or {})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    with _state_lock:
        manual = _read_json(MANUAL_FILE, [])
        for task in manual:
            if task["id"] == task_id:
                task.update(fields)
                _write_json(MANUAL_FILE, manual)
                return jsonify(task)
        data = _read_json(TASKS_FILE, {"tasks": []})
        if task_id not in {t["id"] for t in data.get("tasks", [])}:
            abort(404)
        edits = _read_json(EDITS_FILE, {})
        entry = edits.get(task_id, {"fields": {}})
        entry["fields"].update(fields)
        entry["at"] = _now().isoformat()
        edits[task_id] = entry
        _write_json(EDITS_FILE, edits)
    return jsonify({"id": task_id, **entry["fields"]})


@app.route("/api/tasks/<task_id>", methods=["DELETE"])
def api_delete(task_id: str) -> Response:
    # Only tasks the user added can be deleted; found tasks are checked off instead.
    with _state_lock:
        manual = _read_json(MANUAL_FILE, [])
        kept = [t for t in manual if t["id"] != task_id]
        if len(kept) == len(manual):
            abort(404)
        _write_json(MANUAL_FILE, kept)
    return jsonify({"id": task_id, "deleted": True})


@app.route("/api/tasks/<task_id>/edit", methods=["DELETE"])
def api_undo_edit(task_id: str) -> Response:
    with _state_lock:
        edits = _read_json(EDITS_FILE, {})
        if edits.pop(task_id, None) is None:
            abort(404)
        _write_json(EDITS_FILE, edits)
    return jsonify({"id": task_id, "edited": False})


# Pulling a fresh list runs the task finder, which takes a minute or more and
# costs a little, so it runs detached: it keeps going if this app is stopped.
REFRESH_SCRIPT = Path("system/apps/task_inbox/refresh_tasks.sh")


def _refresh_running() -> bool:
    lock = DATA_DIR / "refresh.lock"
    if not lock.exists():
        return False
    with open(lock) as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
        return False


def _refresh_status() -> dict:
    last = _read_json(DATA_DIR / "refresh_status.json", None)
    return {"running": _refresh_running(), "last": last}


@app.route("/api/refresh")
def api_refresh_status() -> Response:
    return jsonify(_refresh_status())


@app.route("/api/refresh", methods=["POST"])
def api_refresh() -> Response:
    if not _refresh_running():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            ["bash", str(REFRESH_SCRIPT)], start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    return jsonify({**_refresh_status(), "running": True}), 202


def _excerpt(task: dict) -> str:
    """A short piece of the source message, so the finder can see what was rejected."""
    raw = task.get("raw") or {}
    text = raw.get("text") or raw.get("snippet") or raw.get("body") or ""
    if not text and isinstance(raw.get("comment"), dict):
        text = raw["comment"].get("content", "")
    # Marketing emails pad previews with invisible characters; drop them.
    text = re.sub(r"[\u034f\u00ad\u200b-\u200f\u2060-\u2064\ufeff]", "", str(text))
    return " ".join(text.split())[:500]


def _handoff_message(task: dict, note: str) -> str:
    """The first message of the agent chat that does a task for the user."""
    lines = [f"Please do this task for me from my TODOOM list: \"{task['title']}\""]
    if task.get("why"):
        lines.append(f"Details: {task['why']}")
    origin = " · ".join(x for x in [SOURCE_LABELS.get(task.get("source"), task.get("source")), task.get("from"), (task.get("date") or "")[:10]] if x)
    lines.append(f"Where it came from: {origin}")
    link = task.get("url") or task.get("source_url")
    if link:
        lines.append(f"Link: {link}")
    if task.get("due"):
        lines.append(f"Due: {task['due']}")
    excerpt = _excerpt(task)
    if excerpt:
        lines.append(f"Original message (excerpt): {excerpt}")
    if note:
        lines.append(f"My notes: {note}")
    lines += [
        "",
        "Do it end to end if you can, using my connected accounts, or a browser I can watch for anything that needs my sign-in. "
        "Check with me before paying for anything, sending messages to other people on my behalf, or doing anything that can't be undone.",
        f"When it's done, check it off in my TODOOM app: curl -s -X POST http://127.0.0.1:{PORT}/api/tasks/{task['id']}/check "
        "-H 'Content-Type: application/json' -d '{\"checked\": true}'",
    ]
    return "\n".join(lines)


@app.route("/api/tasks/<task_id>/handoff", methods=["POST"])
def api_handoff(task_id: str) -> Response:
    note = str((request.get_json(silent=True) or {}).get("note") or "").strip()
    _, all_tasks = _all_tasks()
    task = next((t for t in all_tasks if t["id"] == task_id), None)
    if task is None:
        abort(404)
    with _state_lock:
        handoffs = _read_json(HANDOFFS_FILE, {})
        handoffs[task_id] = {"at": _now().isoformat(), "note": note}
        _write_json(HANDOFFS_FILE, handoffs)
    return jsonify({"id": task_id, "message": _handoff_message(task, note), **handoffs[task_id]})


@app.route("/api/tasks/<task_id>/dismiss", methods=["POST", "PATCH"])
def api_dismiss(task_id: str) -> Response:
    reason = str((request.get_json(silent=True) or {}).get("reason") or "").strip()
    _, all_tasks = _all_tasks()
    task = next((t for t in all_tasks if t["id"] == task_id), None)
    with _state_lock:
        dismissed = _read_json(DISMISSED_FILE, {})
        if request.method == "PATCH":
            if task_id not in dismissed:
                abort(404)
            dismissed[task_id]["reason"] = reason
        else:
            if task is None or task.get("source") == "manual":
                abort(404)
            dismissed[task_id] = {
                "at": _now().isoformat(), "reason": reason, "tab": task.get("tab"),
                "source": task.get("source"), "source_key": task.get("source_key"),
                "title": task.get("title"), "why": task.get("why"), "from": task.get("from"),
                "excerpt": _excerpt(task), "merged_ids": task.get("merged_ids", []),
            }
        _write_json(DISMISSED_FILE, dismissed)
    return jsonify({"id": task_id, **dismissed[task_id]})


@app.route("/api/tasks/<task_id>/dismiss", methods=["DELETE"])
def api_undismiss(task_id: str) -> Response:
    with _state_lock:
        dismissed = _read_json(DISMISSED_FILE, {})
        if dismissed.pop(task_id, None) is None:
            abort(404)
        _write_json(DISMISSED_FILE, dismissed)
    return jsonify({"id": task_id, "dismissed": False})


@app.route("/api/tasks/<task_id>/check", methods=["POST"])
def api_check(task_id: str) -> Response:
    want = bool((request.get_json(silent=True) or {}).get("checked", True))
    _, all_tasks = _all_tasks()
    task = next((t for t in all_tasks if t["id"] == task_id), None)
    if task is None:
        abort(404)
    known = {i for t in all_tasks for i in _task_ids(t)}
    now = _now()
    with _state_lock:
        checked = _read_json(CHECKED_FILE, {})
        completed = _read_json(COMPLETED_FILE, {})
        if want:
            checked.setdefault(task_id, now.isoformat())
            completed[task_id] = _completed_entry(task, checked[task_id])
        else:
            for i in _task_ids(task):
                checked.pop(i, None)
                completed.pop(i, None)
        checked = {
            k: v for k, v in checked.items()
            if k in known or datetime.datetime.fromisoformat(v) + FORGET_AFTER > now
        }
        _write_json(CHECKED_FILE, checked)
        _write_json(COMPLETED_FILE, completed)
    return jsonify({"id": task_id, "checked": want})


def _completed_entry(task: dict, at: str) -> dict:
    return {
        "at": at, "title": task.get("title"), "why": task.get("why"), "tab": task.get("tab"),
        "source": task.get("source"), "source_key": task.get("source_key"), "from": task.get("from"),
        "date": task.get("date"), "merged_ids": task.get("merged_ids", []),
    }


@app.route("/_static/<basename>")
def shell_module(basename: str) -> Response:
    # The two shell-built modules and nothing else: a name that is not one of
    # them is a 404, so this route can never read outside that directory.
    if basename not in SHELL_STATIC_MODULE_NAMES:
        abort(404)
    module_path = SHELL_STATIC_MODULES_DIR / basename
    if not module_path.is_file():
        abort(404)
    # Flask resolves a relative path against the app's own directory, not the cwd.
    return send_file(module_path.absolute(), mimetype="text/javascript")


@app.route("/health")
def health() -> Response:
    return Response('{"status": "ok"}', mimetype="application/json")


def main() -> None:
    run_simple(
        "127.0.0.1", PORT, app, threaded=True, use_reloader=False, use_debugger=False
    )


if __name__ == "__main__":
    main()
