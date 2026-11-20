---
name: collect-outstanding-tasks
description: Collect the user's outstanding tasks (things they owe someone or committed to) from work and personal Gmail, Slack, Google Docs comments, Notion, iMessage/SMS and WhatsApp into one Work/Personal task list for the Task Inbox app, each task linked to its source with the raw message kept. Use when asked to refresh the task list or task inbox, or when a scheduled refresh runs.
metadata:
  crystallized: true
---

# Collect outstanding tasks

Pulls the last month from every source, has Claude pick out what the user owes,
and writes the task list the TODOOM app (`system/apps/task_inbox`) shows.
The app owns everything else in `data/.apps/task-inbox/` (`checked.json`,
`edits.json`, `manual.json`, `dismissed.json`); this skill writes only
`tasks.json` there (and reads `dismissed.json`), and keeps every task's `id` stable across refreshes so
a checked task stays checked.

## Set it up

Who the user is and which accounts to read live in
`data/.skills/collect-outstanding-tasks/settings.toml` (never in the code).
Copy the commented example that ships with the skill and fill it in:

```bash
mkdir -p data/.skills/collect-outstanding-tasks
cp .agents/skills/collect-outstanding-tasks/settings.example.toml \
   data/.skills/collect-outstanding-tasks/settings.toml
```

| Setting | What it turns on |
|---|---|
| `name`, `employer` | who "you" are in the prompts (blank: "the user") |
| `gmail.work_email` | Work email (Work column) |
| `gmail.personal_email` | Personal email (Personal column) |
| `slack.enabled = true` | Slack (Work) |
| `slack.look_later_emoji` (default `look-later`) | Slack messages you react to with it become Work tasks due the next day; blank turns it off |
| `google_docs.account` | Google Docs comment threads (Work) |
| `notion.account` + `notion.tasks_data_source` | the Notion to-do database (Work) |
| `mac.home` (e.g. `/Users/alex`) | texts and WhatsApp, with Contacts for names (Personal) |

Every source is optional. A blank one is turned off: `fetch` skips it, the task
list shows it as `not-run` with the setting to fill in, and it is not reported
as a gap. A missing settings file, an unknown key or a wrong type stops the run
with a message naming the file and the key. Each account also needs its
latchkey permission granted (see the template's requirements).

The Notion reader expects a database whose checkbox column is named `" "` and
title column `"Write a task"` (with optional `Notes`, `Day`, `Week`); for a
different database, change `parse_notion_row` / `fetch_notion` in
`python/collect_outstanding_tasks_skill/cot_cloud.py`.

## Run it

```bash
uv run --no-sync .agents/skills/collect-outstanding-tasks/python/collect_outstanding_tasks_skill/run.py all
```

In a chat, run the steps one at a time so the user sees progress:

**Refreshes are incremental.** The first run reads the whole 30-day window.
After that, email, Slack, texts and WhatsApp are read only since each source's
last successful fetch (plus a day of overlap; `--full` re-reads the window);
new records are added to the stored snapshot, and records that slide out of the
window leave it. Google Docs comments and Notion are read in full every time,
since they are current state (a resolved comment or checked row must drop off).
An open task whose conversation has aged out of the window is carried forward
from `open_tasks.json` until the user checks it off or removes it.

**Checked-off tasks never come back.** The app writes `completed.json` (task id ->
title, source, when) as the user checks tasks off. The skill reads it
read-only: Claude sees those tasks as DONE (in each conversation's previous
tasks and in an "already done" list) and the duplicate check folds any new
reminder about one into it, so the app shows it as done.

1. **[script] Fetch** -- `run.py fetch [--days 30] [--source <key> ...]`.
   Reads all seven sources at once into the raw store. A source that fails
   (the Mac asleep, a revoked account) is recorded as failed and keeps its last
   good snapshot; the rest still refresh. Exit 1 only if every source failed.
2. **[ai-script] Pick the tasks** -- `run.py judge [--model claude-sonnet-5]`.
   Sends only the conversations that changed since the last run to Claude, in
   small batches (8 conversations) run 8 at a time, under the confirmed rules (below); chats (texts, WhatsApp, Slack)
   are read one calendar week at a time, so past weeks stay cached. Then one
   more call folds duplicates (a weekly reminder, the same ask by email and by
   text) into one task: it asks only about the tasks that are new or reworded
   since the last check, against the rest of the list, with the model's thinking
   capped (uncapped, it thought for minutes over a long list; Haiku and
   no-thinking both folded different to-dos together). Same-looking titles are
   not folded on their own (two coffee receipts are two charges). A refresh
   with nothing new makes no call at all. Metered (Sonnet 5), typical costs:
   about $6 and ~10 minutes for a first month on a busy set of accounts; cents
   per refresh (a handful of changed conversations). The app's Refresh button
   runs `system/apps/task_inbox/refresh_tasks.sh`, and so does the refresh
   schedule if you add one: routines for a 7am and an hourly refresh, plus a
   Mac catch-up (`system/apps/task_inbox/mac_catchup.sh`) that re-reads texts
   and WhatsApp once the Mac wakes (see the template's requirements for the
   exact commands). `judge --dry-run` prints what would be sent, with no calls.
   After that, one small call rewords any task that still talks about the user
   in the third person ("she", "he", "the user", their name) so it speaks to
   them as "you" (cached in `voice.json` by the original wording; a task is only
   reworded once). The list is read by the user themselves: titles and "why"
   lines must always address them as "you" / "your".
3. **[script] Write the list** -- `run.py build`. Writes
   `data/.apps/task-inbox/tasks.json` atomically and prints the gaps.

Then tell the user how many tasks are on each tab and any gaps (a source that
failed, WhatsApp out of date) in plain words.

Source keys for `--source`: `gmail-work`, `slack`, `google-docs`, `notion`,
`gmail-personal`, `imessage`, `whatsapp`.

## What counts as a task

- Include: requests to the user they haven't answered or done, things they said
  they'd do, deadlines / sign-ups / RSVPs / forms / bills / payments, recruiter
  follow-ups addressed to them.
- Exclude: anything they already handled (replied, finished, Notion row checked,
  Docs thread resolved), sales and cold outreach, newsletters, receipts,
  verification codes, notifications with nothing to do, chit-chat.
- Tabs: work Gmail, Slack, Google Docs, Notion -> Work; personal Gmail, texts,
  WhatsApp -> Personal.
- Look-back: 30 days. Notion rows count only if unchecked and edited in that
  window.

The rules live in the `SYSTEM` prompt in `python/collect_outstanding_tasks_skill/cot_judge.py`. Changing that
prompt? Bump `PROMPT_VERSION` so every conversation is re-read under it -- a
cold re-read (~$6), so ask the user first.

## Learning from "Not a task"

The app writes `data/.apps/task-inbox/dismissed.json` (task id -> `{at, reason,
title, why, from, source, excerpt, merged_ids, ...}`) when the user marks a task
"Not a task". The skill only reads it:

- **Judge:** the removals ride in the system prompt as corrections. Removals with
  a reason come first (the user's reason is a rule that overrides the defaults), then
  bare ones; newest first; capped at `MAX_FEEDBACK_CHARS`. A dismissed task in a
  re-read conversation is flagged DISMISSED so the model never re-lists it.
- **Build:** any task whose id or `merged_ids` matches a dismissed id or its
  `merged_ids` is dropped; `dismissed_hidden` counts them.
- Dismissals are *not* in the judgement cache key: a new one steers every
  conversation read from then on (new or changed ones), and never forces a
  re-read of unchanged ones.

## Sources and access

All go through `latchkey` (no secrets in this skill):

| Source | Account / path | What is read |
|---|---|---|
| Work Gmail | `gmail.work_email` | inbox threads (not promotions/social/forums) + the user's replies in them |
| Personal Gmail | `gmail.personal_email` | same; threads Gmail doesn't mark important/personal and the user never wrote in are read from metadata (subject + preview) only |
| Slack | default latchkey Slack | search: mentions, `to:me`, `from:me`; user ids resolved to real names |
| Google Docs | `google_docs.account` (Drive) | open comment threads on docs modified in the window that involve the user |
| Notion | `notion.account` (hosted MCP) | unchecked rows of the `notion.tasks_data_source` database |
| Texts | Mac file server, `<mac.home>/Library/Messages/chat.db` (+ -wal) | messages, reactions dropped |
| WhatsApp | Mac file server, WhatsApp group container `ChatStorage.sqlite` (+ -wal) | text messages |
| Contacts | Mac file server, `AddressBook-v22.abcddb` in each `AddressBook/Sources/*/` folder under `<mac.home>/Library` | phone/email -> name for texts and WhatsApp |

The Mac sources need the Mac awake with Imbue Studio running. `chat.db` is ~1 GB
and takes ~100 s to download; it is re-downloaded only when its ETag changes
(new messages land in the -wal, fetched every run). Mac copies live in
`/var/tmp/collect-outstanding-tasks/` (rebuildable; never `/tmp`).

## Output: `data/.apps/task-inbox/tasks.json`

Top level: `generated` (ISO), `window_days`, `tasks[]`, `skipped[]`, `gaps[]`
(plain-English strings), `sources{<key>: {label, tab, status: ok|failed|not-run,
reason, last_ok_at, attempted_at, records}}`, `judge` (last model-run stats).

Each task: `id`, `tab`, `source` (`email`/`slack`/`google-docs`/`notion`/`text`/
`whatsapp`), `source_key`, `title`, `why`, `url`, `source_url`, `due`, `date`,
`from`, `stale`, `merged_ids`, `also_from`, `raw` (the source record), and for
texts/WhatsApp `context` (`[{who, date, text, this}]`, 6 before / 2 after).

- A merged task keeps the id of its oldest duplicate; the others' ids are in
  `merged_ids` (the app should treat the task as checked if any of them is)
  and their summaries in `also_from`. Wording and date come from the newest.

- `url` is a web page from the message when acting on the task means opening
  it, else the source's own link. Never an `sms:`/`whatsapp:` link (the desktop
  app opens those as a blank window): texts get null unless the message has a
  web page; WhatsApp gets `https://wa.me/<phone>` for one-to-one chats.
- `from` is a person or brand name: contact names for texts/WhatsApp, the
  sender for email, the Slack real name, the last commenter for Docs, "Notion
  to-do list" for Notion; a short-code text sender is named by the brand Claude
  reads in the text. For the user's own commitments it names the other side.
- `stale: true` marks tasks from a source that failed this run, or whose
  conversation Claude could not re-read.

## Stored state (`data/.skills/collect-outstanding-tasks/`)

- `raw/<source>.json` -- every fetched record by id plus `latest_ids` (the last
  good snapshot). Records outside the snapshot are evicted 14 days past the
  window. Email messages are fetched once and reused.
- `judgements.json` -- Claude's verdict per conversation (per chat-week),
  keyed by a hash of what was sent; only current conversations are kept.
- `look_later.json` -- Slack messages the user reacted to with the look-later
  emoji (`slack.look_later_emoji`, default `look-later`; found with the search
  `hasmy::<emoji>:`, since `reactions.list` refuses this token). Each is a Work
  task as marked, due the day after a refresh first saw the mark (Slack keeps no
  reaction time). `baseline` holds the marks already there on the first
  refresh; they stay off unless their ids are listed in `include`. Removing the
  mark removes the task. `emoji` records the emoji the marks were found with.
- `merges.json` -- the duplicate groups, keyed by a hash of the task list, plus
  `seen` (a fingerprint of every task already checked). Without `seen` the next
  run checks the whole list again.
- `settings.toml` -- the user's identity and accounts (see "Set it up").
- `fetch_status.json`, `judge_status.json`, `slack_users.json`.

## Gotchas

- Gmail meters requests per user per minute and answers an overrun with a 403
  ("Quota exceeded"); fetches are paced (`GMAIL_UNITS_PER_MINUTE`) and retried.
  A cold personal-inbox fetch (~1,200 messages) takes a few minutes.
- `claude -p` takes the prompt as one argument (128 KB cap), so batches stay
  under `MAX_BATCH_BYTES` of UTF-8.
- WhatsApp is only as fresh as WhatsApp Desktop's last sync; a gap says so when
  it is more than 2 days behind.
- Each chat-week is read on its own, so a task done in a later week can still
  show from the earlier one; the duplicate fold catches repeats, not "done".
- Slack context is only what search returns (no full-thread fetch); one unit's
  text is capped at `MAX_UNIT_CHARS` (newest kept).
- When the Mac is offline one ~20 s probe fails and both Mac sources are marked
  failed with that reason; their last snapshot and tasks stay (marked stale).
- Identities (name, emails, Notion data source, Mac home) come from
  `settings.toml`, loaded by `load_settings` in `python/collect_outstanding_tasks_skill/cot_common.py`; the
  Slack user is whoever latchkey's Slack access belongs to (`auth.test`).

## Tests

```bash
uv run --no-sync pytest .agents/skills/collect-outstanding-tasks/python
```
