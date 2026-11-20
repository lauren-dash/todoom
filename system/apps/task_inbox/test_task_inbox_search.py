"""The TODOOM page: Today split into Personal and Work, Overdue and Snoozed,
snoozing, due dates, search (the sidebar box or Option-Space) whose results
jump to the task, and adding and checking off without waiting on the app.
Drives the real page against the app booted over made-up tasks."""

import datetime
import gzip
import json
import os
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, Route

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
        "date": (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=6)).isoformat(),
        "from": sender,
        "stale": False,
        "merged_ids": [],
        "also_from": [],
        "raw": {"id": i, "text": why, "subject": title, "body": why},
    }


def _days(n: int) -> str:
    return (datetime.date.today() + datetime.timedelta(days=n)).isoformat()


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
# Overdue: due ten days ago.
TASKS.append(dict(_task("o1", "work", "slack", "Send Jo the old report", "long overdue", "Jo"), due=_days(-10)))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def page(module_browser: Browser) -> Iterator[Page]:
    """A fresh page on the root conftest's per-module browser, which refuses pytest-playwright's own."""
    context = module_browser.new_context()
    try:
        yield context.new_page()
    finally:
        context.close()


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
        json.dumps({"p2": datetime.datetime.now(datetime.timezone.utc).isoformat()})
    )
    port = _free_port()
    env = dict(
        os.environ,
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


def _api(url: str, method: str = "GET", body: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {}


def _task_by_id(app_url: str, task_id: str) -> dict:
    return next(t for t in _api(app_url + "api/tasks")[1]["tasks"] if t["id"] == task_id)


def test_snooze_api_saves_with_and_without_a_date_and_unsnoozes(app_url: str) -> None:
    assert _api(app_url + "api/snooze", "POST", {"ids": ["w1", "w2"], "until": None})[0] == 200
    assert _task_by_id(app_url, "w1")["snoozed"]["until"] is None
    assert _api(app_url + "api/snooze", "POST", {"ids": ["w1"], "until": _days(1)})[0] == 200
    assert _task_by_id(app_url, "w1")["snoozed"]["until"] == _days(1)
    assert _api(app_url + "api/tasks/w1/snooze", "DELETE")[0] == 200
    assert "snoozed" not in _task_by_id(app_url, "w1")
    assert "snoozed" in _task_by_id(app_url, "w2"), "only the one task is unsnoozed"
    assert _api(app_url + "api/snooze", "POST", {"ids": ["w1"], "until": "next tuesday"})[0] == 400
    assert _api(app_url + "api/snooze", "POST", {"ids": ["nope"], "until": None})[0] == 404
    assert _api(app_url + "api/snooze", "POST", {"ids": [], "until": None})[0] == 404


@pytest.mark.browser
def test_today_is_split_and_overdue_tasks_can_be_snoozed_or_given_a_date(page: Page, app_url: str) -> None:
    page.set_viewport_size({"width": 1300, "height": 900})
    page.goto(app_url)
    page.wait_for_selector("li.task")
    assert page.locator(".colhead h2").all_inner_texts() == ["Personal", "Work"]
    assert page.locator('li.task[data-id="o1"]').count() == 0, "overdue tasks stay off Today"
    assert page.locator('li.task[data-id="p2"]').count() == 0, "a task checked earlier is gone from the list"
    row = page.locator("li.task").first
    box = row.bounding_box()
    assert box is not None
    height = box["height"]
    assert row.inner_text().splitlines()[0] == row.locator(".title").inner_text()
    assert "Sam Rivera" not in page.inner_text("section.list"), "a row shows the task alone, no sender or source"
    row.hover()
    box = row.bounding_box()
    assert box is not None
    assert box["height"] == height, "hovering a row never changes its height"

    page.click("[data-v=overdue]")
    assert page.locator("li.task").count() == 1
    assert "null" not in page.inner_text("section.list"), "no stray text under the list"
    assert page.locator('li.task[data-id="o1"] .view-btn').count() == 1, "an overdue task can open its original message"
    picker = page.locator('li.task[data-id="o1"] input[type=date]')
    picker.fill(_days(3))
    picker.dispatch_event("change")
    page.wait_for_selector('li.task[data-id="o1"]', state="detached")
    page.click("[data-v=work]")
    assert page.locator('li.task[data-id="o1"]').count() == 1, "a new date moves it back to its list"

    page.locator('li.task[data-id="w1"]').hover()
    page.locator('li.task[data-id="w1"] button.snooze').click()
    page.click(".menu >> text=No deadline")
    page.wait_for_selector('li.task[data-id="w1"]', state="detached")
    page.click("[data-v=snoozed]")
    assert page.locator('li.task[data-id="w1"] .chip').inner_text().lower() == "snoozed"
    page.locator('li.task[data-id="w1"]').hover()
    page.locator('li.task[data-id="w1"] .view-btn').click()
    page.wait_for_selector("body.viewing")
    assert page.locator("#v-body").inner_text() == "Casey needs the launch deck", "a snoozed task opens its original message"


@pytest.mark.browser
def test_search_opens_from_the_sidebar_or_option_space_and_jumps_to_the_task(page: Page, app_url: str) -> None:
    page.set_viewport_size({"width": 1200, "height": 800})
    page.goto(app_url)
    page.wait_for_selector("li.task")
    page.click("#open-search")
    assert page.evaluate("document.activeElement.id") == "spot-in"
    page.keyboard.press("Escape")
    page.click("h1")  # closing hands focus back to the Search button; move off it
    page.keyboard.press("Alt+Shift+Space")
    assert not page.evaluate("document.body.classList.contains('spotting')"), "only Option-Space opens it"
    page.keyboard.press("Alt+Space")
    assert page.evaluate("document.activeElement.id") == "spot-in"
    page.keyboard.type("max")
    assert [g.inner_text() for g in page.locator(".spot-group").all()] == ["TOP HIT"]
    assert "done" in (page.locator(".spot-item").first.get_attribute("class") or ""), "checked-off tasks are found too"

    page.fill("#spot-in", "filler 2")
    page.keyboard.press("ArrowDown")
    picked = page.locator('.spot-item[aria-selected="true"] .st').inner_text()
    page.keyboard.press("Enter")
    assert not page.evaluate("document.body.classList.contains('spotting')")
    assert page.locator(".view[aria-current=true]").get_attribute("data-v") == "work", "it opens the task's list"
    flashed = page.locator("li.task.flash")
    assert flashed.count() == 1 and flashed.locator(".title").inner_text() == picked

    # Cmd/Ctrl-Enter views the original; Escape then closes it.
    page.keyboard.press("Alt+Space")
    page.keyboard.type("deck")
    page.keyboard.press("Control+Enter")
    assert page.inner_text("#v-title") == "Send Casey the deck"
    page.keyboard.press("Escape")
    assert not page.evaluate("document.body.classList.contains('viewing')")

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


@pytest.mark.browser
def test_on_a_phone_tapping_a_task_beside_its_text_opens_its_details_and_actions(page: Page, app_url: str) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(app_url)
    page.wait_for_selector("li.task")
    page.click(".todaytab >> text=Work")
    row = page.locator('[data-col="work"] li.task').first
    task_id, title = row.get_attribute("data-id"), row.locator(".title").inner_text()
    assert row.locator(".actions").is_hidden(), "a phone row shows just the task"
    row.locator(".title").click()
    assert not page.evaluate("document.body.classList.contains('viewing')"), "tapping the text edits it"
    box = row.bounding_box()
    assert box is not None
    page.mouse.click(box["x"] + box["width"] - 6, box["y"] + box["height"] - 4)
    assert page.inner_text("#v-title") == title
    assert page.locator("#v-acts").inner_text().split("\n")[0] == "Snooze"
    page.click("#v-acts .v-snooze")
    page.click("#v-acts >> text=Next week")
    assert not page.evaluate("document.body.classList.contains('viewing')")
    page.wait_for_selector(f'li.task[data-id="{task_id}"]', state="detached")


@pytest.mark.browser
@pytest.mark.timeout(30)
def test_a_checked_task_stays_crossed_off_in_place_then_fades_away(page: Page, app_url: str) -> None:
    page.set_viewport_size({"width": 1300, "height": 900})
    page.goto(app_url)
    page.wait_for_selector("li.task")
    row = page.locator('li.task[data-id="p1"]')
    box = row.bounding_box()
    assert box is not None
    height = box["height"]
    row.locator("input[type=checkbox]").check()
    page.wait_for_timeout(1500)
    assert "checked" in (row.get_attribute("class") or ""), "it stays a moment, crossed off"
    box = row.bounding_box()
    assert box is not None
    assert box["height"] == height, "in the same spot, the same size"
    page.wait_for_selector('li.task[data-id="p1"]', state="detached", timeout=8000)
    page.keyboard.press("Alt+Space")
    page.keyboard.type("venmo")
    page.locator(".spot-item.done").first.click()
    page.click("#v-acts >> text=Mark not done")
    page.wait_for_selector('li.task[data-id="p1"]:not(.checked)')


@pytest.mark.browser
def test_the_weekly_recap_lists_work_you_checked_off_this_week(page: Page, app_url: str) -> None:
    assert _api(app_url + "api/tasks/w1/check", "POST", {"checked": True})[0] == 200
    assert _api(app_url + "api/tasks/p1/check", "POST", {"checked": True})[0] == 200
    done = _api(app_url + "api/recap")[1]["done"]
    assert [t["id"] for t in done] == ["w1"], "only work tasks are in the recap"
    assert done[0]["raw"]["subject"] == "Send Casey the deck", "with the original message to view"

    page.set_viewport_size({"width": 1300, "height": 900})
    page.goto(app_url)
    page.wait_for_selector("li.task")
    page.click("#open-recap")
    page.wait_for_selector(".recap-item")
    assert "1 work task done" in page.inner_text(".listhead .sub")
    assert page.locator(".recap-item").all_inner_texts()[0].startswith("Send Casey the deck")
    assert page.locator('[aria-label="Next week"]').is_disabled(), "no weeks after this one"
    page.locator(".recap-item").first.click()
    assert page.inner_text("#v-title") == "Send Casey the deck"
    page.keyboard.press("Escape")
    assert page.locator('[aria-label="Previous week"]').is_disabled(), "no weeks before the first check-off"


def _get(url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_the_list_leaves_out_original_messages_which_are_fetched_one_at_a_time(app_url: str, tmp_path: Path) -> None:
    listed = _api(app_url + "api/tasks")[1]
    assert "skipped" not in listed, "the finder's skipped notes stay out of the list"
    w1 = next(t for t in listed["tasks"] if t["id"] == "w1")
    assert "raw" not in w1
    assert w1["has_raw"] is True and w1["raw_subject"] == "Send Casey the deck"
    status, body = _api(app_url + "api/tasks/w1/raw")
    assert status == 200 and body == {"id": "w1", "raw": TASKS[2]["raw"]}
    assert _api(app_url + "api/tasks/nope/raw")[0] == 404
    stored = json.loads((tmp_path / "tasks.json").read_text())
    assert stored["tasks"][2]["raw"] == TASKS[2]["raw"] and "skipped" in stored, "the stored list keeps everything"

    added = _api(app_url + "api/tasks", "POST", {"title": "Water the plants", "tab": "personal"})[1]
    entry = _task_by_id(app_url, added["id"])
    assert entry["has_raw"] is False and "raw_subject" not in entry
    assert _api(app_url + f"api/tasks/{added['id']}/raw")[1] == {"id": added["id"], "raw": None}


def test_a_new_task_keeps_the_id_the_page_gave_it(app_url: str) -> None:
    status, task = _api(app_url + "api/tasks", "POST", {"id": "manual-0123456789ab", "title": "Call the plumber", "tab": "personal"})
    assert status == 201 and task["id"] == "manual-0123456789ab"
    assert _task_by_id(app_url, "manual-0123456789ab")["title"] == "Call the plumber"
    assert _api(app_url + "api/tasks", "POST", {"id": "manual-0123456789ab", "title": "Again", "tab": "personal"})[0] == 409
    titles = [t["title"] for t in _api(app_url + "api/tasks")[1]["tasks"] if t["source"] == "manual"]
    assert titles == ["Call the plumber"], "a repeated save adds nothing"
    # Any other id -- a found task's, or not the page's shape -- is replaced with a fresh one.
    for given in ("w1", "manual-0123456789AB", "manual-0123", None):
        task = _api(app_url + "api/tasks", "POST", {"id": given, "title": "Other", "tab": "work"})[1]
        assert task["id"] != given and re.fullmatch(r"manual-[0-9a-f]{12}", task["id"])


def test_text_answers_are_gzipped_for_browsers_that_take_it(app_url: str) -> None:
    status, headers, body = _get(app_url + "api/tasks", {"Accept-Encoding": "gzip, deflate"})
    assert status == 200 and headers["Content-Encoding"] == "gzip" and "Accept-Encoding" in headers["Vary"]
    assert int(headers["Content-Length"]) == len(body)
    assert len(json.loads(gzip.decompress(body))["tasks"]) == len(TASKS)
    _, headers, body = _get(app_url, {"Accept-Encoding": "gzip"})
    assert headers["Content-Encoding"] == "gzip" and b"TODOOM" in gzip.decompress(body)
    _, headers, body = _get(app_url + "api/tasks", {})
    assert "Content-Encoding" not in headers and json.loads(body)["tasks"], "plain for a client that does not ask"
    _, headers, _ = _get(app_url + "health", {"Accept-Encoding": "gzip"})
    assert "Content-Encoding" not in headers, "a tiny answer is not worth compressing"
    status, headers, _ = _get(app_url + "api/tasks/nope/raw", {"Accept-Encoding": "gzip"})
    assert status == 404 and "Content-Encoding" not in headers
    status, headers, _ = _get(app_url + "assets/departure-mono.woff2", {"Accept-Encoding": "gzip"})
    assert status == 200 and "Content-Encoding" not in headers, "files go out as they are"


@pytest.mark.browser
@pytest.mark.timeout(30)
def test_an_added_task_shows_at_once_and_can_be_checked_off_before_it_is_saved(page: Page, app_url: str) -> None:
    held: list[Route] = []
    page.route("**/api/tasks", lambda route: held.append(route) if route.request.method == "POST" else route.continue_())
    lists: list[str] = []
    page.on("request", lambda r: lists.append(r.url) if r.method == "GET" and r.url.endswith("/api/tasks") else None)
    page.set_viewport_size({"width": 1300, "height": 900})
    page.goto(app_url)
    page.wait_for_selector("li.task")
    page.fill("#add-personal", "Call the plumber")
    page.keyboard.press("Enter")
    row = page.locator('li.task:has(.title:text-is("Call the plumber"))')
    row.wait_for()
    task_id = row.get_attribute("data-id") or ""
    assert re.fullmatch(r"manual-[0-9a-f]{12}", task_id), task_id
    assert page.evaluate("document.activeElement.id") == "add-personal", "ready for the next one"
    assert page.input_value("#add-personal") == ""
    assert len(held) == 1 and json.loads(held[0].request.post_data or "{}")["id"] == task_id

    # Checking it off before the save lands waits for the save, then records the check.
    row.locator("input[type=checkbox]").check()
    page.wait_for_timeout(300)
    assert all(t["id"] != task_id for t in _api(app_url + "api/tasks")[1]["tasks"]), "nothing saved yet"
    held[0].continue_()
    saved: list[dict] = []
    for _ in range(50):
        saved = [t for t in _api(app_url + "api/tasks")[1]["tasks"] if t["id"] == task_id]
        if saved and saved[0].get("checked_at"):
            break
        page.wait_for_timeout(100)
    else:
        raise AssertionError("the task was not saved and checked off")
    assert saved[0]["title"] == "Call the plumber" and saved[0]["tab"] == "personal"
    assert lists == [app_url + "api/tasks"], "adding and checking off reload nothing"


@pytest.mark.browser
def test_an_added_task_that_fails_to_save_is_taken_back(page: Page, app_url: str) -> None:
    page.route("**/api/tasks", lambda route: route.fulfill(status=500, json={"error": "disk full"}) if route.request.method == "POST" else route.continue_())
    page.goto(app_url)
    page.wait_for_selector("li.task")
    page.fill("#add-work", "Book the flights")
    page.keyboard.press("Enter")
    page.wait_for_selector('li.task:has(.title:text-is("Book the flights"))', state="detached")
    assert page.inner_text("#notes") == "Couldn't save: disk full."
    assert all(t["source"] != "manual" for t in _api(app_url + "api/tasks")[1]["tasks"])


@pytest.mark.browser
@pytest.mark.timeout(30)
def test_a_check_off_that_fails_to_save_is_put_back(page: Page, app_url: str) -> None:
    page.route("**/api/tasks/w1/check", lambda route: route.fulfill(status=500, body=""))
    page.set_viewport_size({"width": 1300, "height": 900})
    page.goto(app_url)
    page.wait_for_selector("li.task")
    page.click("[data-v=work]")
    page.locator('li.task[data-id="w1"] input[type=checkbox]').check()
    page.wait_for_function("document.getElementById('notes').textContent === \"Couldn't save that check-off. Try again.\"")
    page.wait_for_selector('li.task[data-id="w1"]:not(.checked)')
    assert not page.locator('li.task[data-id="w1"] input[type=checkbox]').is_checked()
    page.wait_for_timeout(6000)  # past the time a checked task lingers
    assert page.locator('li.task[data-id="w1"]').count() == 1, "it stays on the list"


@pytest.mark.browser
def test_view_fetches_the_original_message_once_and_search_finds_its_subject(page: Page, app_url: str, tmp_path: Path) -> None:
    stored = json.loads((tmp_path / "tasks.json").read_text())
    stored["tasks"][3]["raw"]["subject"] = "Quarterly numbers"
    (tmp_path / "tasks.json").write_text(json.dumps(stored))
    fetched: list[str] = []
    page.on("request", lambda r: fetched.append(r.url) if r.url.endswith("/raw") else None)
    page.set_viewport_size({"width": 1300, "height": 900})
    page.goto(app_url)
    page.wait_for_selector("li.task")
    assert fetched == [], "no original is loaded with the list"
    page.click("[data-v=work]")
    for _ in range(2):
        page.locator('li.task[data-id="w1"]').hover()
        page.locator('li.task[data-id="w1"] button.view-btn').click()
        page.wait_for_function("document.body.classList.contains('viewing')")
        assert page.inner_text("#v-title") == "Send Casey the deck"
        assert page.inner_text("#v-body") == "Casey needs the launch deck"
        page.keyboard.press("Escape")
    assert fetched == [app_url + "api/tasks/w1/raw"], "fetched the first time only"

    page.keyboard.press("Alt+Space")
    page.keyboard.type("quarterly")
    assert page.locator(".spot-item .st").all_inner_texts() == ["Answer Morgan on the logo"]
