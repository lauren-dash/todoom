<p align="center">
  <img alt="TODOOM" src="template.svg" width="480">
</p>

# TODOOM

<p align="center">
  <a href="https://studio.imbue.com/open?git_url=https://github.com/lauren-dash/todoom"><img alt="Open in Imbue Studio" height="64" src="https://img.shields.io/badge/Open%20in%20Imbue%20Studio-D8D1C0?style=for-the-badge"></a>
</p>

Didn't work? Create a Studio workspace and paste this to your agent:
` /use-template https://github.com/lauren-dash/todoom`

## Why you care

All your doom in one place.

The things you owe people are scattered across your inbox, Slack threads, Docs
comments, group texts and WhatsApp, and the ones that slip are the ones you
never wrote down. TODOOM reads all of them for you and keeps one short list of
what you still have to do, split into Personal and Work, each item linked back
to the message it came from.

## How to use it

1. **Connect your accounts.** When you adopt the template, your agent asks for
   read access to the sources you want (Gmail, Slack, Google Docs, Notion, and
   your Mac's Messages, WhatsApp and Contacts folders) and fills in a small
   settings file with your name and accounts. Skip any source you don't use.
2. **Open the TODOOM window.** Personal tasks are on the left, Work on the right.
   Each row is one thing you owe, written to you: *"Reply to Jordan about the
   field trip form -- due Fri"*, with who it's from and a link to the original
   email, thread, doc comment or chat.
3. **Find a task.** Type in the **Search** box at the top to narrow both
   columns as you type (every word you type, in any order, matched from the
   start of a word: "cas deck" finds *Send Casey the deck*). Or press
   **Option-Space** anywhere in the window for a Spotlight-style search:
   a box opens in the middle with results listed underneath (a top hit, then
   Personal, Work and Done). Arrow keys move, **Enter** jumps to the task,
   **Cmd-Enter** opens the original message, **Esc** closes. The shortcut
   works while the TODOOM window is the one you're using.
4. **Work the list.**
   - Check a task off: the check draws in, the title is struck through, and it
     moves to Done (it clears after a day). It won't come back, even if someone
     sends a reminder about it.
   - Click a title or note to edit it, or type in **+ Add a task** to add your
     own.
   - **Not a task** removes one; add a reason ("school newsletters are never
     tasks") and the next refresh learns from it.
   - **Do it for me** hands the task to a new agent chat, which checks it off
     when it is done.
   - React to a Slack message with **:look-later:** (or the emoji you pick)
     and it shows up as a Work task due the next day.
   - **View original** opens the message the task came from (for texts and
     WhatsApp, **View conversation** shows the messages around it).
5. **Refresh.** Press **Refresh** for a new list (usually under a minute), or
   ask your agent to add the refresh schedule: hourly through the day plus an
   early-morning catch-up. If a source
   couldn't be read (say your Mac was asleep), a note at the top says so and
   its tasks stay on the list marked **Out of date**. A task stays on the list until you check it off or
   remove it, however old the conversation gets. There is a light/dark switch
   next to Refresh.

The first month on a busy set of accounts costs about $6 of Claude usage;
after that each refresh reads only what's new since the last one and costs
cents.

## Ideas for making it yours

- Add a source: a second Slack workspace, a shared team inbox, or GitHub issues
  assigned to you, each as one more fetcher in the skill.
- Change the look-back from a month to two weeks (`--days 14`) for a tighter,
  cheaper list.
- Split Work into one column per project, or add a "Waiting on others" column
  for things you asked for.
- Send yourself a short morning summary of what's due today, alongside the
  morning refresh.
- Teach it your own rules, e.g. "anything from the HOA is personal" or
  "recruiter emails are never tasks", by editing the rules prompt in
  `scripts/cot_judge.py`.

## What this is

This repository is a published **Imbue Studio template**: a clean, bootable
snapshot of what an agent built, ready to adapt into your own. It is NOT the
generic workspace template -- it is this specific project.

[`template.md`](template.md) is the full manifest -- what it is, how it
works, what it needs to run, and what to adapt -- with the
machine-readable half (recipe, requirements, and the environment it needs
installed) in [`template.toml`](template.toml).
