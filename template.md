---
title: "TODOOM"
description: "All your doom in one place"
thumbnail: "template.svg"
version: v1
format: v2
---

# TODOOM

This file is the manifest for the **TODOOM** template (slug:
`todo`). It is the one document a future agent reads to understand,
present, and adapt this template. If you are an agent in a workspace that was
created from this template, this file is your script: read all of it, then
follow "How to adapt it" below.

## What it is

All your doom in one place: one to-do list built from your email, Slack, texts, WhatsApp, Google Docs and Notion, with Personal and Work columns.

**TODOOM** gives you one to-do list for everything you owe people, built
automatically from where those asks actually arrive: your work and personal
email, Slack, Google Docs comments, your Notion to-do database, and the texts
and WhatsApp messages on your Mac. Claude reads the last month of each source
the first time, and after that only what is new since the last refresh. It picks out the things you still have to do (an unanswered question, something
you said you'd send, a form, a bill, an RSVP) and leaves out what you already
handled, newsletters, receipts and chit-chat. The result is a single window
with two columns, **Personal** on the left and **Work** on the right. Each task
is a short line written to you ("Jordan asked you to sign the permission
slip"), with who it's from, when it's due, and a link back to the original
message, and you can always see that message itself. You can check tasks off
(with a small animation; they clear after a day), edit them in place, add your
own, turn a Slack message into a task by reacting to it with a look-later
emoji, search them (a search box in the top bar, and a Spotlight-style search
panel on Option-Space), mark one "Not a task" with an optional reason that teaches the next
refresh, or press **Do it for me** to hand the task to a new agent chat. An
open task stays on the list until you check it off or remove it, however old
its conversation gets, and a task you checked off never comes back, even when
someone sends a reminder about it. A Refresh button pulls a fresh list in the
background; a daily schedule can do the same each morning.

## How it works

The snapshot includes these paths (each is a repo-root-relative path copied
from the original agent onto a clean default-workspace-template base):

- `system/apps/task_inbox`
- `system/supervisord.conf.d/task-inbox.conf`
- `.agents/skills/collect-outstanding-tasks`

- `system/apps/task_inbox` -- the **TODOOM** app (a Flask web app, display name
  "TODOOM"). It serves the two-column page (`src/task_inbox/page.html`, in a
  monochrome monospace "HQ" style with the bundled Departure Mono font under
  the OFL licence) and a small JSON API (`src/task_inbox/runner.py`): list
  tasks, add, edit, check off, "Not a task", hand off, refresh. Search runs in
  the page: the top-bar box filters both columns, and Option-Space opens
  a Spotlight-style panel (results grouped as top hit, Personal, Work, Done;
  Enter jumps to the task). It reads the
  task list `data/.apps/task-inbox/tasks.json` and keeps its own state beside
  it (`checked.json`, `edits.json`, `manual.json`, `dismissed.json`, and
  `completed.json`, a record of each task you checked off), so a refresh never
  loses your check-marks or edits. `refresh_tasks.sh` runs one
  refresh of the skill under a lock (so a button press and a scheduled run never
  overlap) and points `claude` at the workspace's most recently used account
  when it runs outside a chat. **Do it for me** opens a new agent chat through
  the shell's `startWithText`, with the task, its source and a `curl` to check
  it off when done.
- `system/supervisord.conf.d/task-inbox.conf` -- the supervisord program
  `task-inbox`. It registers the app with `system/scripts/forward_port.py`
  (from `app.toml`, `http://localhost:8080`) and runs the `task-inbox`
  console script, which listens on 127.0.0.1:8080 (`TASK_INBOX_PORT`
  overrides it). The app is in the uv workspace, so `uv sync --all-packages`
  installs it.
- `.agents/skills/collect-outstanding-tasks` -- the task finder skill. `run.py
  fetch` reads each source that is turned on in its settings file, in parallel,
  through `latchkey curl` (Gmail and Drive APIs, Slack Web API, Notion's
  hosted MCP server, and the Mac's files through Imbue Studio's file sharing:
  `chat.db`, WhatsApp's `ChatStorage.sqlite` and the Contacts databases,
  copied to `/var/tmp` and read with SQLite). It keeps every raw record under
  `data/.skills/collect-outstanding-tasks/raw/`. After the first run, email,
  Slack, texts and WhatsApp are read only since each source's last successful
  fetch (plus a day of overlap; `run.py fetch --full` re-reads the whole
  window); Google Docs comments and Notion are read in full every time, since a
  resolved comment or checked row has to drop off. `run.py judge` sends only the
  conversations that changed to Claude (`claude -p`, Sonnet 5), with chats
  read one week at a time and verdicts cached. It then folds duplicates and
  rewords anything still in the third person into "you". New conversations are
  read in small batches (8 at a time, side by side), and the duplicate check
  asks only about tasks that are new since the last check, with the model's
  thinking capped; it never folds two tasks just because their titles look
  alike. Slack messages you reacted to with the look-later emoji
  (`slack.look_later_emoji`, default `look-later`) become Work tasks due the
  next day, kept in `look_later.json`; a found task the mark points at keeps
  its own wording and gains the due date. `run.py build` writes
  `tasks.json` for the app, and keeps the list it wrote in `open_tasks.json` so
  an open task whose conversation has aged out of the window is carried
  forward. Who you are and which accounts to read come from
  `data/.skills/collect-outstanding-tasks/settings.toml` (a commented example
  ships as `settings.example.toml`).

At runtime: the app's Refresh button (POST `/api/refresh`) starts
`refresh_tasks.sh` in the background, which runs `run.py all`. The skill
writes `tasks.json`, and the open page picks up the new list. The skill also
reads the app's `dismissed.json`, so every "Not a task" (and its reason)
steers later refreshes, and its `completed.json`, read-only: Claude sees
checked-off tasks as done, and the duplicate check folds any new reminder about
one into it, so the app shows it as done instead of listing it again.

## Recipe

This template is version `v1`. It is not a fork of the
workspace it came from -- it is DERIVED from it by a recipe: include these
paths, leave these out, apply these published-version rules. An update re-runs
the recipe against the current workspace and publishes the result as the next
version, so anything excluded stays excluded even though it still exists in the
source workspace.

The recipe is machine-read, so it lives in the sibling
[`template.toml`](template.toml) -- its `[recipe]` table -- along with
the structured requirements and the environment this template needs
installed. That file is authoritative for all of it; this one holds the prose.

## Requirements

Everything the adopting agent must deal with before this template is really
theirs. Two kinds of entry, handled at different times:

- **Activation** -- what must be SET UP before anything runs, in the
  machine-readable `requires_` forms below. The adopting agent acts on these
  ITSELF, first, before asking anything.
- **Adaptation** -- what must be DECIDED or REWIRED, in prose. Worked through
  interactively with the user, after activation.


Activation:

- requires_permission: google-gmail-api / google-gmail-read-all (user-approved; the adopting agent initiates it during setup, once for each Google account set as `gmail.work_email` and `gmail.personal_email` in the settings file; reads inbox threads and your replies)
- requires_permission: slack-api / slack-read-all (user-approved; the adopting agent initiates it during setup; searches your mentions, DMs and your own messages, and looks up people's names)
- requires_permission: google-drive-api / google-drive-read-files (user-approved; the adopting agent initiates it during setup for the `google_docs.account` account; lists recently changed Google Docs)
- requires_permission: google-drive-api / google-drive-read-comments (user-approved; the adopting agent initiates it during setup for the same account; reads open comment threads on those Docs)
- requires_permission: notion-mcp-api / notion-mcp-read-all (user-approved; the adopting agent initiates it during setup for the `notion.account` account; queries your Notion to-do database through Notion's hosted MCP server)
- requires_permission: minds-api-proxy / minds-file-server-read-~/Library/Messages (user-approved; the adopting agent initiates a file-sharing READ request for `~/Library/Messages` during setup; texts. macOS also requires Full Disk Access for Imbue Studio in System Settings > Privacy & Security, or the Mac refuses `chat.db`)
- requires_permission: minds-api-proxy / minds-file-server-read-~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared (user-approved; the adopting agent initiates a file-sharing READ request for that folder during setup; WhatsApp Desktop's message database)
- requires_permission: minds-api-proxy / minds-file-server-read-~/Library/Application Support/AddressBook (user-approved; the adopting agent initiates a file-sharing READ request for that folder during setup; Contacts, so texts and WhatsApp show names instead of numbers)
- requires_llm: calls Claude through the keyless subscription path (`claude -p`, via the skill's copy of use-ai-integration's `claude_p.py`, model `claude-sonnet-5`); `refresh_tasks.sh` sets `CLAUDE_CONFIG_DIR` to the workspace's most recently used account when it runs outside a chat. An adopter on the keyed litellm path (ANTHROPIC_API_KEY) must switch the model calls in `scripts/cot_judge.py` (`claude_completer`) per the use-ai-integration skill.

Every source is optional: a permission you skip just leaves that source turned
off. There are no secrets to supply. Every service goes through latchkey.

Adaptation:

- **Choose your Slack look-later mark (optional).** A Slack message you react
  to with `:look-later:` becomes a Work task, due the next day ("Look at Sam's
  Slack message in #launch"), quoting the message as written. Pick another
  emoji with `slack.look_later_emoji` in the settings file, or leave it blank
  to turn this off. Marks already there on the first refresh are left out, so
  an old backlog doesn't flood the list; removing the reaction takes the task
  off.
- **Fill in the settings file.** The skill reads who you are and which accounts
  to use from `data/.skills/collect-outstanding-tasks/settings.toml`, which
  does not ship (it is your data). Copy
  `.agents/skills/collect-outstanding-tasks/settings.example.toml` there and
  fill in your name, employer, work and personal Gmail addresses,
  `slack.enabled`, the Google Docs account, the Notion account label and to-do
  data source, and your Mac home folder (e.g. `/Users/alex`). Leave a source
  blank to turn it off. A run with no settings file stops with a message
  naming the file.
- **Point the Notion reader at your database.** It reads one Notion database
  (`notion.tasks_data_source`, a `collection://...` id) and expects the
  publisher's to-do database layout: a checkbox column named `" "` and a title
  column named `"Write a task"`, with optional `Notes` / `Day` / `Week`. If
  your database uses other columns, change `parse_notion_row` and the filter
  in `fetch_notion` (`scripts/cot_cloud.py`), or turn Notion off.
- **Add the refresh schedule, if you want one.** The publisher's list
  refreshed every hour through the day (8am-10pm) plus a 7am catch-up run that
  is not missed if the workspace was down overnight. The schedule lives in
  machine state, so it does not ship. Add both lines through the
  manage-scheduled-tasks skill, in one cron file such as
  `data/.state/cron.d/todo-refresh` (two runs never overlap; a run that finds
  one going is skipped):
  - the 7am catch-up job, through `run_job.sh`:
    `* * * * *   root   /home/user/workspace/system/libs/automations/with_agent_env.sh env MINDS_JOB_STATE_DIR=/home/user/workspace/data/.state/jobs /home/user/workspace/system/libs/automations/run_job.sh todo-refresh --every 1d --at 7 --retry-after 10m bash /home/user/workspace/system/apps/task_inbox/refresh_tasks.sh >> /var/log/supervisor/todo-refresh.log 2>&1`
  - the hourly daytime refresh, a plain cron line:
    `0 8-22 * * *   root   /home/user/workspace/system/libs/automations/with_agent_env.sh bash /home/user/workspace/system/apps/task_inbox/refresh_tasks.sh >> /var/log/supervisor/todo-refresh.log 2>&1`

  Without them the list refreshes only when you press Refresh.
- **Texts and WhatsApp only refresh while your Mac is awake** with Imbue Studio
  running. When it is not, both are marked as not read this time and their
  last tasks stay, marked stale. WhatsApp is only as fresh as WhatsApp
  Desktop's last sync (the list says when it is more than 2 days behind).
- **Text and WhatsApp tasks show the conversation instead of opening the
  app.** The Studio desktop opens `sms:` / `whatsapp:` links as a blank
  window, so those tasks link to a web page from the message (or
  `https://wa.me/<number>` for one-to-one WhatsApp chats) and otherwise show
  the surrounding messages inline.
- **Decide how much a first run may cost.** The first month on a busy set of
  accounts is about $6 of Claude usage and ~10 minutes; later refreshes cost
  cents. `run.py judge --dry-run` shows what would be sent before you spend
  anything.

## Environment

What this template needs INSTALLED, beyond what the template already has.
Declared in `template.toml`'s `[environment]` table; an adopting agent
converges it at ITS OWN pinned apt snapshot timestamp, so package versions come
out consistent with the rest of that agent's environment rather than frozen to
whatever this publisher happened to have.

Nothing extra -- runs on the stock workspace environment. The included code
uses only what the template already has: `flock` (util-linux) in
`refresh_tasks.sh`, SQLite through Python's standard library, `uv`, the
`claude` CLI, `latchkey`, and Flask (a normal uv dependency of the app).

## How to adapt it

Instructions for the NEXT agent -- the one adapting this template into a
new agent. This is the `use-template` skill's template path; in short:

1. Read this entire file first, especially "Requirements" below. It holds two
   kinds of entry and they are handled at different times: the machine-readable
   `requires_` lines are ACTIVATION (set them up before anything runs), and
   the prose bullets are ADAPTATION (decide or rewire them afterwards).
2. Present the template to the user in plain, non-technical language: what
   it is, what it does, and what it needs from them (name the activation
   requirements).
3. Ask whether they want to use the same connectors (e.g. their own Slack).
   If YES: ACTIVATE FIRST -- initiate every `requires_permission` line NOW
   via a latchkey permission request (see the `latchkey` skill; the request
   opens the approval/login flow in the Imbue Studio app), wire up any
   `requires_secret` values, start the services, and get the app showing
   THE USER'S OWN DATA. Done for a data-backed app means the user can open it
   and see their own data -- NOT that a service starts or an endpoint returns
   200. Then tell them it is live and to take a look.
4. Only AFTER that (or immediately, if they chose different connectors -- the
   swap is then the first adaptation) ask: "How do you want to adapt it?"
5. Work through each requirement interactively, one at a time. Translate each
   into plain language, ask for a decision only when you genuinely need one,
   and resolve the obvious ones yourself.
6. When done, append a dated entry to "Adaptation history" below (never
   rewrite earlier entries) and commit.

## Publication history

This template's changelog: what each published version changed. The PUBLISHER
appends one entry per version (newest last); earlier entries are never rewritten.
This is distinct from "Adaptation history" below, which is the ADOPTERS' log.

### v1 (2026-10-27) -- first version: the two-column TODOOM app with search and the task finder skill, reading seven sources from a fill-in settings file, refreshing incrementally, keeping open tasks until you clear them, never bringing back what you checked off, and turning Slack look-later marks into tasks

## Adaptation history

Each agent that adapts this template appends one dated entry below. Earlier
entries are never rewritten.
