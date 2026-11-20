from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, sync_playwright

# This app is a standalone project whose tests run from its own directory, so the
# repo root's conftest.py (where the other suites get `module_browser`) does not
# reach them. Same fixture: Fortress when it is installed, else Playwright's own.
FORTRESS_CHROMIUM_PATH = Path("/opt/fortress/tilion-fortress/tilion")


@pytest.fixture(scope="module")
def module_browser() -> Iterator[Browser]:
    """A Chromium for one test module, closed with its Playwright when the module ends."""
    launch_args = (
        {"executable_path": str(FORTRESS_CHROMIUM_PATH)} if FORTRESS_CHROMIUM_PATH.exists() else {}
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**launch_args)
        try:
            yield browser
        finally:
            browser.close()
