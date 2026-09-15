#!/usr/bin/env python3
"""
autoimg.py

Automate the current/fresh Brave ChatGPT tab with the floating
ChatGPT Bulk Image Generator extension.

The current extension injects its UI directly into chatgpt.com, so this script
does NOT navigate to chrome-extension:// URLs and does NOT open a side panel.

Usage:
    uv run autoimg.py --source "/path/to/prompts.md"

Requirements:
    uv add playwright

Notes:
    - Brave is launched with your normal/default profile.
    - No --user-data-dir is supplied, so this script does not create/replace a
      separate browser profile.
    - The existing extension must already be installed/enabled in that profile.
    - If Brave is already running, the script will attach to it only when it
      was launched with --remote-debugging-port=9222. Otherwise close Brave and
      let this script launch it.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)


BRAVE_EXECUTABLE = "/usr/bin/brave-browser"
BRAVE_PROFILE_DIR = "/home/kingnit/.config/BraveSoftware/Brave-Browser"

CHATGPT_URL = "https://chatgpt.com/"
REMOTE_DEBUGGING_PORT = 9222
CDP_ENDPOINT = f"http://127.0.0.1:{REMOTE_DEBUGGING_PORT}"

# Exact selectors from the current floating extension.
PROMPTS_SELECTOR = ".cbig-prompts"
START_SELECTOR = ".cbig-start"
PANEL_SELECTOR = "#cbig-host"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run ChatGPT Bulk Image Generator from a Markdown prompt file."
    )
    parser.add_argument(
        "--source",
        required=True,
        help="Markdown/text file containing one image prompt per line.",
    )
    return parser.parse_args()


def read_prompts(source: Path) -> list[str]:
    if not source.is_file():
        raise SystemExit(f"ERROR: Source file not found: {source}")

    prompts: list[str] = []

    for raw in source.read_text(encoding="utf-8").splitlines():
        line = raw.strip()

        if not line:
            continue

        # Accept normal text plus common Markdown list formats.
        line = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line).strip()

        if line:
            prompts.append(line)

    if not prompts:
        raise SystemExit(f"ERROR: No prompts found in {source}")

    return prompts


def brave_running() -> bool:
    result = subprocess.run(
        ["pgrep", "-x", "brave-browser"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def launch_brave() -> subprocess.Popen:
    """
    Start Brave using its normal profile.

    We intentionally do NOT pass --user-data-dir, --incognito, or any profile
    replacement flags. Brave therefore uses the normal/default profile.
    """
    print("Starting Brave with the normal profile...")

    return subprocess.Popen(
        [
            BRAVE_EXECUTABLE,
            f"--remote-debugging-port={REMOTE_DEBUGGING_PORT}",
            CHATGPT_URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


async def wait_for_cdp(timeout_seconds: float = 20.0) -> None:
    """
    Poll the local Chrome DevTools endpoint without adding another dependency.
    """
    import urllib.request

    deadline = time.monotonic() + timeout_seconds

    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"{CDP_ENDPOINT}/json/version",
                timeout=1,
            ) as response:
                if response.status == 200:
                    return
        except Exception:
            pass

        await asyncio.sleep(0.25)

    raise RuntimeError(
        f"Brave did not expose DevTools on port {REMOTE_DEBUGGING_PORT}."
    )


async def connect_brave(pw) -> Browser:
    await wait_for_cdp()
    return await pw.chromium.connect_over_cdp(CDP_ENDPOINT)


async def get_context(browser: Browser) -> BrowserContext:
    if not browser.contexts:
        raise RuntimeError("No Brave browser context is available.")
    return browser.contexts[0]


async def get_chatgpt_page(context: BrowserContext) -> Page:
    """
    Reuse the existing ChatGPT tab when possible.

    If there is already another active page, we prefer a ChatGPT tab. Otherwise
    create one. No extension URL is opened.
    """
    for page in context.pages:
        try:
            if page.url.startswith(("https://chatgpt.com/", "https://chat.openai.com/")):
                return page
        except Exception:
            continue

    page = await context.new_page()
    await page.goto(CHATGPT_URL, wait_until="domcontentloaded")
    return page


async def wait_for_chatgpt(page: Page) -> None:
    """
    Wait for the ChatGPT app to be present enough for the extension content
    script to run.
    """
    await page.wait_for_load_state("domcontentloaded")

    candidates = (
        "#prompt-textarea",
        'div.ProseMirror[contenteditable="true"]',
        'div[contenteditable="true"]',
        "main",
    )

    for selector in candidates:
        try:
            await page.wait_for_selector(
                selector,
                state="attached",
                timeout=5000,
            )
            return
        except PlaywrightTimeoutError:
            continue

    print(
        "WARNING: ChatGPT's composer was not detected yet. "
        "Continuing to wait for the floating extension."
    )


async def wait_for_floating_extension(
    page: Page,
    timeout_ms: int = 20000,
) -> None:
    """
    The current extension injects #cbig-host into the ChatGPT page from its
    content script. Wait for the host and then the prompt textarea.
    """
    print("Waiting for the floating Bulk Image Generator...")

    await page.wait_for_selector(
        PANEL_SELECTOR,
        state="attached",
        timeout=timeout_ms,
    )

    # The controls are inside a Shadow DOM.
    await page.wait_for_function(
        """
        () => {
            const host = document.querySelector("#cbig-host");
            return !!host &&
                   !!host.shadowRoot &&
                   !!host.shadowRoot.querySelector(".cbig-prompts") &&
                   !!host.shadowRoot.querySelector(".cbig-start");
        }
        """,
        timeout=timeout_ms,
    )


async def floating_locator(page: Page, selector: str):
    """
    Return a locator for an element inside the extension's open Shadow DOM.
    """
    return page.locator(f"#cbig-host").locator(selector)


async def fill_prompts(page: Page, prompts: list[str]) -> None:
    prompt_text = "\n".join(prompts)

    textarea = await floating_locator(page, PROMPTS_SELECTOR)
    await textarea.wait_for(state="visible", timeout=10000)

    await textarea.fill(prompt_text)
    await page.wait_for_timeout(300)

    try:
        count = await floating_locator(page, ".cbig-num").inner_text()
        print(f"Loaded {count} prompts.")
    except Exception:
        print(f"Loaded {len(prompts)} prompts.")


async def click_start(page: Page) -> None:
    start = await floating_locator(page, START_SELECTOR)
    await start.wait_for(state="visible", timeout=10000)

    if await start.is_disabled():
        raise RuntimeError(
            "The extension's Start button is disabled. "
            "Make sure ChatGPT is fully loaded."
        )

    print("Clicking Start...")
    await start.click()


async def main() -> None:
    args = parse_args()
    source = Path(args.source).expanduser().resolve()
    prompts = read_prompts(source)

    print(f"Source:   {source}")
    print(f"Prompts:  {len(prompts)}")
    print(f"Brave:    {BRAVE_EXECUTABLE}")
    print(f"Profile:  {BRAVE_PROFILE_DIR}")
    print()

    started_by_script = False

    if not brave_running():
        launch_brave()
        started_by_script = True
        await wait_for_cdp()
    else:
        print("Brave is already running.")
        print(f"Attaching to {CDP_ENDPOINT}...")

    async with async_playwright() as pw:
        try:
            browser = await connect_brave(pw)
        except Exception as exc:
            if not started_by_script:
                raise RuntimeError(
                    "Brave is running, but this process cannot attach to it.\n\n"
                    "Close Brave completely and run this script again, so it can "
                    "start Brave with:\n\n"
                    f"  {BRAVE_EXECUTABLE} "
                    f"--remote-debugging-port={REMOTE_DEBUGGING_PORT}\n"
                ) from exc
            raise

        context = await get_context(browser)
        page = await get_chatgpt_page(context)

        await page.bring_to_front()

        # If we reused a non-ChatGPT page for some reason, navigate it now.
        if not page.url.startswith(("https://chatgpt.com/", "https://chat.openai.com/")):
            await page.goto(CHATGPT_URL, wait_until="domcontentloaded")

        print("ChatGPT page ready.")
        await wait_for_chatgpt(page)

        # Give the content script time to initialize after SPA navigation.
        await page.wait_for_timeout(1000)

        await wait_for_floating_extension(page)

        print("Floating extension found.")
        await fill_prompts(page, prompts)
        await click_start(page)

        print()
        print("Bulk generation started successfully.")
        print("The extension is now controlling the current ChatGPT tab.")
        print("Leave Brave running while generation continues.")
        print("Press Ctrl+C to stop this automation process.")
        print("Brave itself will remain open.")

        stop_event = asyncio.Event()

        def stop_handler() -> None:
            stop_event.set()

        signal.signal(signal.SIGINT, lambda *_: stop_handler())
        signal.signal(signal.SIGTERM, lambda *_: stop_handler())

        await stop_event.wait()

        # Disconnect from Brave. Do not close the user's browser.
        try:
            await browser.close()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
