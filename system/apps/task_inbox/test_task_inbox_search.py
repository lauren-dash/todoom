"""Search: the box in the top bar filters both columns, and Option-Space
opens a Spotlight-style panel whose results jump to the task. Drives the real
page in a browser against the app booted over made-up tasks."""

import json
import os
import socket
import subprocess
import sys
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Page

_DIR = Path(__file__).parent


def _task(i: str, tab: str, source: str, title: str, why: str, sender: str) -> dict:
    return {
        "id": i,
        "tab": tab,
        "source": source,
        "source_key": {"email": "gmail-work", "slack": "slack", "text": "imessage"}[
            source
        ],
        "title": title,
        "why": why,
        "url": None,
        "source_url": None,
        "due": None,
        "date": "2026-10-25T10:00:00Z",
        "from": sender,
        "stale": False,
        "merged_ids": [],
        "also_from": [],
        "raw": {"id": i, "text": why, "subject": title, "body": why},
    }


TASKS = [
    _task(
        "p1",
        "personal",
        "text",
        "Venmo Sam for dinner",
        "Sam asked you to send $42",
        "Sam Rivera",
    ),
    _task(
        "p2",
        "personal",
        "text",
        "Book the vet for Max",
        "Jordan says Max is due for shots",
        "Jordan",
    ),
    _task(
        "w1",
        "work",
        "email",
        "Send Casey the deck",
        "Casey needs the launch deck",
        "Casey Lin",
    ),
    _task(
        "w2",
        "work",
        "slack",
        "Answer Morgan on the logo",
        "Centered or bottom logo?",
        "Morgan Lee",
    ),
] + [
    _task(f"w{n}", "work", "email", f"Filler task {n}", "nothing to see", "Someone")
    for n in range(3, 25)
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def app_url(tmp_path: Path) -> Iterator[str]:
    (tmp_path / "tasks.json").write_text(
        json.dumps(
            {
                "generated": "2026-10-27T08:00:00Z",
                "window_days": 30,
                "tasks": TASKS,
                "skipped": [],
                "gaps": [],
                "sources": {
                    "gmail-work": {"label": "Work email", "status": "ok"},
                    "notion": {"label": "Notion to-do list", "status": "not-run"},
                },
            }
        )
    )
    (tmp_path / "checked.json").write_text(
        json.dumps({"p2": "2026-10-27T07:00:00+00:00"})
    )
    port = _free_port()
    env = dict(
        os.environ,
        PYTHONPATH=str(_DIR / "src"),
        TASK_INBOX_PORT=str(port),
        TASK_INBOX_DATA_DIR=str(tmp_path),
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", "from task_inbox.runner import main; main()"],
        env=env,
        cwd=_DIR.parents[2],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}/"
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(url + "health", timeout=1)
                break
            except OSError:
                # Wait a moment for the app to start, failing fast if it exited.
                try:
                    proc.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    continue
                raise AssertionError("the app exited before it started serving")
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _titles(page: Page) -> list[str]:
    return [t.inner_text() for t in page.locator(".open-list li.task .title").all()]


@pytest.mark.browser
def test_top_bar_search_filters_both_columns(page: Page, app_url: str) -> None:
    page.set_viewport_size({"width": 1200, "height": 800})
    page.goto(app_url)
    page.wait_for_selector("li.task")
    assert page.inner_text("#notes") == "", (
        "a source the user turned off is not reported as unreachable"
    )
    page.fill("#search", "cas")
    assert _titles(page) == ["Send Casey the deck"], (
        "a word matches the start of a word"
    )
    assert page.inner_text("#count-work").lower() == "1 matching"
    page.fill("#search", "asey")
    assert _titles(page) == [], "not the middle of a word"
    page.fill("#search", "$42")
    assert _titles(page) == ["Venmo Sam for dinner"]
    page.fill("#search", "logo morgan")
    assert _titles(page) == ["Answer Morgan on the logo"], "every word, any order"
    page.press("#search", "Escape")
    assert page.input_value("#search") == ""
    assert len(_titles(page)) == len([t for t in TASKS if t["id"] != "p2"])


@pytest.mark.browser
def test_option_space_opens_spotlight_and_jumps_to_the_task(
    page: Page, app_url: str
) -> None:
    page.set_viewport_size({"width": 1200, "height": 800})
    page.goto(app_url)
    page.wait_for_selector("li.task")
    page.keyboard.press("Alt+Shift+Space")
    assert not page.evaluate("document.body.classList.contains('spotting')"), (
        "only Option-Space opens it"
    )
    page.keyboard.press("Alt+Space")
    assert page.evaluate("document.activeElement.id") == "spot-in"
    page.keyboard.type("max")
    assert [g.inner_text() for g in page.locator(".spot-group").all()] == ["TOP HIT"]
    assert "done" in (page.locator(".spot-item").first.get_attribute("class") or ""), (
        "checked-off tasks are found too"
    )

    page.fill("#spot-in", "filler 2")
    page.keyboard.press("ArrowDown")
    picked = page.locator('.spot-item[aria-selected="true"] .st').inner_text()
    page.keyboard.press("Enter")
    assert not page.evaluate("document.body.classList.contains('spotting')")
    task_id = page.evaluate("document.activeElement.closest('li').id")
    assert page.locator(f"#{task_id} .title").inner_text() == picked
    assert page.locator(f"#{task_id}.flash").count() == 1

    # Cmd/Ctrl-Enter opens the original message; Escape then closes it.
    page.keyboard.press("Alt+Space")
    page.keyboard.type("deck")
    page.keyboard.press("Control+Enter")
    assert page.inner_text("#panel-title") == "Send Casey the deck"
    page.keyboard.press("Escape")
    assert not page.evaluate("document.body.classList.contains('open')")

    # The shortcut works while editing a task, toggles closed, and types nothing.
    title = page.locator("li.task .title").first
    before = title.inner_text()
    title.click()
    page.keyboard.press("Alt+Space")
    assert page.evaluate("document.body.classList.contains('spotting')")
    page.keyboard.press("Alt+Space")
    assert not page.evaluate("document.body.classList.contains('spotting')")
    page.keyboard.press("Escape")
    assert title.inner_text() == before
