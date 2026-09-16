#!/usr/bin/env python3
"""
autoimg.py - start the "ChatGPT Bulk Image Generator" extension from the CLI.

WHY THE OLD SCRIPT BROKE DOWNLOADS
----------------------------------
The previous version used Playwright (`connect_over_cdp`). Playwright takes
ownership of downloads for every browser context it attaches to: it sends the
CDP command

    Browser.setDownloadBehavior { behavior: "allowAndName", downloadPath: <tmp> }

With "allowAndName", Chromium ignores the filename that the extension passes to
`chrome.downloads.download()` and instead writes the file into Playwright's
temporary artifacts directory using a random GUID with no file extension - so
it looks like a random unopenable file - and Playwright deletes those artifacts
when the script disconnects. Manual runs work because nothing overrides the
download behavior.

THE FIX
-------
This version speaks the DevTools Protocol directly over a raw WebSocket
(standard library only, no Playwright), so no download interception is ever
installed. It also explicitly resets `Browser.setDownloadBehavior` to
"default" (or to an explicit folder with --download-dir) before clicking Start,
which repairs a browser that an earlier Playwright run left hijacked.

After clicking Start the script disconnects and exits: the extension owns the
whole generate + download loop, and nothing is holding a debugger session that
could steal or delete files.

USAGE
-----
    python3 autoimg.py --source prompts.txt
    python3 autoimg.py --source prompts.txt --folder chatgpt-bulk --prefix shot_
    python3 autoimg.py --source prompts.txt --download-dir ~/Pictures/ai
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #

DEFAULT_BROWSER = "/usr/bin/brave-browser"
DEFAULT_PORT = 9222
DEFAULT_URL = "https://chatgpt.com/"

HOST_SELECTOR = "#cbig-host"
PROMPTS_SELECTOR = ".cbig-prompts"
START_SELECTOR = ".cbig-start"


# --------------------------------------------------------------------------- #
# Minimal WebSocket client (RFC 6455, client side, no dependencies)
# --------------------------------------------------------------------------- #


class WebSocketError(RuntimeError):
    pass


class WebSocket:
    """Just enough WebSocket to speak CDP: text frames, ping/pong, close."""

    def __init__(self, url: str, timeout: float = 30.0):
        m = re.match(r"^ws://([^/:]+):(\d+)(/.*)$", url)
        if not m:
            raise WebSocketError(f"Unsupported WebSocket URL: {url}")

        host, port, path = m.group(1), int(m.group(2)), m.group(3)

        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(timeout)
        self._buf = bytearray()

        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self._sock.sendall(request.encode())

        header = bytearray()
        while b"\r\n\r\n" not in header:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise WebSocketError("Connection closed during handshake")
            header += chunk

        head, _, rest = bytes(header).partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in status_line:
            raise WebSocketError(f"WebSocket handshake failed: {status_line}")

        self._buf += rest

    # -- low level ---------------------------------------------------------- #

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise WebSocketError("Connection closed by browser")
            self._buf += chunk
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray()
        header.append(0x80 | opcode)  # FIN + opcode
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)

        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def _read_frame(self):
        b1, b2 = self._read_exact(2)
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F

        if length == 126:
            (length,) = struct.unpack(">H", self._read_exact(2))
        elif length == 127:
            (length,) = struct.unpack(">Q", self._read_exact(8))

        mask = self._read_exact(4) if masked else b""
        payload = self._read_exact(length) if length else b""
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    # -- public ------------------------------------------------------------- #

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_text(self) -> str:
        data = bytearray()
        expecting_continuation = False

        while True:
            fin, opcode, payload = self._read_frame()

            if opcode == 0x8:  # close
                raise WebSocketError("Browser closed the DevTools connection")
            if opcode == 0x9:  # ping
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode in (0x1, 0x2):
                data = bytearray(payload)
                expecting_continuation = not fin
            elif opcode == 0x0 and expecting_continuation:
                data += payload
                expecting_continuation = not fin
            else:
                continue

            if not expecting_continuation:
                return bytes(data).decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Tiny CDP client
# --------------------------------------------------------------------------- #


class CdpError(RuntimeError):
    pass


class Cdp:
    def __init__(self, ws_url: str, timeout: float = 30.0):
        self._ws = WebSocket(ws_url, timeout=timeout)
        self._next_id = 0

    def call(self, method: str, params: dict | None = None, timeout: float = 30.0):
        self._next_id += 1
        message_id = self._next_id
        self._ws.send_text(
            json.dumps({"id": message_id, "method": method, "params": params or {}})
        )

        deadline = time.monotonic() + timeout
        while True:
            if time.monotonic() > deadline:
                raise CdpError(f"Timed out waiting for reply to {method}")
            message = json.loads(self._ws.recv_text())
            if message.get("id") != message_id:
                continue  # event or stale reply
            if "error" in message:
                raise CdpError(f"{method} failed: {message['error']}")
            return message.get("result", {})

    def evaluate(self, expression: str, timeout: float = 30.0):
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": True,
            },
            timeout=timeout,
        )
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            text = (
                details.get("exception", {}).get("description")
                or details.get("text")
                or json.dumps(details)
            )
            raise CdpError(f"Page script error: {text}")
        return result.get("result", {}).get("value")

    def close(self) -> None:
        self._ws.close()


# --------------------------------------------------------------------------- #
# Browser discovery / launch
# --------------------------------------------------------------------------- #


def http_json(port: int, path: str, timeout: float = 3.0):
    url = f"http://127.0.0.1:{port}{path}"
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def cdp_available(port: int) -> bool:
    try:
        http_json(port, "/json/version", timeout=1.0)
        return True
    except Exception:
        return False


def process_running(executable: str) -> bool:
    name = Path(executable).name
    result = subprocess.run(["pgrep", "-f", name], capture_output=True, text=True)
    return result.returncode == 0


def launch_browser(executable: str, port: int, url: str, user_data_dir: str | None):
    command = [executable, f"--remote-debugging-port={port}"]
    if user_data_dir:
        command.append(f"--user-data-dir={os.path.expanduser(user_data_dir)}")
    command.append(url)

    return subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def wait_for_cdp(port: int, timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cdp_available(port):
            return
        time.sleep(0.25)
    raise SystemExit(
        f"ERROR: DevTools endpoint on port {port} never became available.\n"
        "If the browser was already running WITHOUT --remote-debugging-port, quit it\n"
        "completely (check the tray icon) and run this script again, or pass\n"
        "--user-data-dir to start a separate debuggable profile."
    )


def find_page_target(port: int, url_prefix: str):
    for target in http_json(port, "/json/list"):
        if target.get("type") != "page":
            continue
        if target.get("url", "").startswith(url_prefix):
            return target
    return None


def wait_for_page_target(port: int, url_prefix: str, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        target = find_page_target(port, url_prefix)
        if target and target.get("webSocketDebuggerUrl"):
            return target
        time.sleep(0.4)
    return None


# --------------------------------------------------------------------------- #
# Prompt file parsing
# --------------------------------------------------------------------------- #


def read_prompts(path: Path) -> list[str]:
    if not path.is_file():
        raise SystemExit(f"ERROR: File not found: {path}")

    prompts: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        # Accept "prompt", "- prompt", "* prompt", "1. prompt", "1) prompt".
        line = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line).strip()
        if line:
            prompts.append(line)

    if not prompts:
        raise SystemExit("ERROR: No prompts found.")
    return prompts


# --------------------------------------------------------------------------- #
# Page-side JavaScript (runs inside the ChatGPT tab)
# --------------------------------------------------------------------------- #

JS_PANEL_READY = """
(() => {
  const host = document.querySelector(%(host)s);
  if (!host || !host.shadowRoot) return false;
  const root = host.shadowRoot;
  return !!(root.querySelector(%(prompts)s) && root.querySelector(%(start)s));
})()
"""

JS_COMPOSER_READY = """
(() => !!(
  document.querySelector('#prompt-textarea') ||
  document.querySelector('div.ProseMirror[contenteditable="true"]') ||
  document.querySelector('main form textarea')
))()
"""

# Sets the prompts + optional settings exactly the way a human would, so the
# extension's own input/change listeners run and persist the values.
JS_APPLY = """
(() => {
  const host = document.querySelector(%(host)s);
  const root = host && host.shadowRoot;
  if (!root) return { ok: false, error: 'panel host not found' };

  const textarea = root.querySelector(%(prompts)s);
  if (!textarea) return { ok: false, error: 'prompts textarea not found' };

  const text = %(text)s;
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLTextAreaElement.prototype, 'value'
  ).set;
  setter.call(textarea, text);
  textarea.dispatchEvent(new Event('input', { bubbles: true }));

  const settings = %(settings)s;
  const applied = [];
  for (const [key, value] of Object.entries(settings)) {
    const input = root.querySelector('[data-k="' + key + '"]');
    if (!input) continue;
    if (input.type === 'checkbox') {
      input.checked = !!value;
    } else {
      const proto = input.tagName === 'TEXTAREA'
        ? window.HTMLTextAreaElement.prototype
        : window.HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, 'value').set.call(input, String(value));
    }
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    applied.push(key);
  }

  const lines = textarea.value.split('\\n').filter((s) => s.trim().length > 0);
  return { ok: true, lines: lines.length, applied: applied };
})()
"""

JS_START = """
(() => {
  const host = document.querySelector(%(host)s);
  const root = host && host.shadowRoot;
  if (!root) return { ok: false, error: 'panel host not found' };

  const button = root.querySelector(%(start)s);
  if (!button) return { ok: false, error: 'start button not found' };
  if (button.disabled) return { ok: false, error: 'start button is disabled (already running?)' };

  button.click();
  return { ok: true };
})()
"""

JS_RUNNING = """
(() => {
  const host = document.querySelector(%(host)s);
  const root = host && host.shadowRoot;
  if (!root) return false;
  const button = root.querySelector(%(start)s);
  return !!(button && button.disabled);
})()
"""

JS_LOG_TAIL = """
(() => {
  const host = document.querySelector(%(host)s);
  const root = host && host.shadowRoot;
  if (!root) return [];
  const log = root.querySelector('.cbig-log');
  if (!log) return [];
  return Array.from(log.querySelectorAll('.cbig-log-line'))
    .slice(-5)
    .map((el) => el.textContent);
})()
"""


def js(template: str, **extra) -> str:
    values = {
        "host": json.dumps(HOST_SELECTOR),
        "prompts": json.dumps(PROMPTS_SELECTOR),
        "start": json.dumps(START_SELECTOR),
    }
    values.update(extra)
    return template % values


def poll(cdp: Cdp, expression: str, timeout: float, interval: float = 0.4) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cdp.evaluate(expression):
            return True
        time.sleep(interval)
    return False


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def parse_args():
    parser = argparse.ArgumentParser(
        description="Load prompts into the ChatGPT Bulk Image Generator panel and press Start."
    )
    parser.add_argument("--source", required=True, help="Text file with one prompt per line.")
    parser.add_argument("--browser", default=DEFAULT_BROWSER, help="Browser executable.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Remote debugging port.")
    parser.add_argument("--url", default=DEFAULT_URL, help="Page to open / reuse.")
    parser.add_argument(
        "--user-data-dir",
        default=None,
        help="Optional separate profile dir (use if your normal browser is already open).",
    )
    parser.add_argument(
        "--download-dir",
        default=None,
        help="Force downloads into this folder. Omitted = restore the browser default.",
    )
    parser.add_argument("--folder", default=None, help="Panel setting: download subfolder.")
    parser.add_argument("--prefix", default=None, help="Panel setting: filename prefix.")
    parser.add_argument("--start-index", type=int, default=None, help="Panel setting: start number.")
    parser.add_argument("--pad", type=int, default=None, help="Panel setting: digits of padding.")
    parser.add_argument("--delay", type=int, default=None, help="Panel setting: delay between prompts (ms).")
    parser.add_argument("--timeout-ms", type=int, default=None, help="Panel setting: per-image timeout (ms).")
    parser.add_argument(
        "--no-download",
        action="store_true",
        help="Generate only; leave the extension's auto-download off.",
    )
    parser.add_argument(
        "--wait",
        type=int,
        default=0,
        help="Seconds to keep printing the panel log after Start (0 = exit immediately).",
    )
    return parser.parse_args()


def panel_settings(args) -> dict:
    settings: dict[str, object] = {}
    if args.folder is not None:
        settings["folder"] = args.folder
    if args.prefix is not None:
        settings["prefix"] = args.prefix
    if args.start_index is not None:
        settings["startIndex"] = args.start_index
    if args.pad is not None:
        settings["pad"] = args.pad
    if args.delay is not None:
        settings["delayMs"] = args.delay
    if args.timeout_ms is not None:
        settings["timeoutMs"] = args.timeout_ms
    settings["autoDownload"] = not args.no_download
    return settings


def reset_download_behavior(port: int, download_dir: str | None) -> None:
    """Undo any 'allowAndName' interception left behind by Playwright/other tools.

    This is the actual bug fix: with 'allowAndName' Chromium discards the
    filename from chrome.downloads.download() and writes a GUID-named file into
    a temp dir that the controlling client later deletes.
    """
    version = http_json(port, "/json/version")
    ws_url = version.get("webSocketDebuggerUrl")
    if not ws_url:
        print("WARNING: no browser-level DevTools socket; skipping download reset.")
        return

    browser = Cdp(ws_url)
    try:
        if download_dir:
            path = str(Path(os.path.expanduser(download_dir)).resolve())
            Path(path).mkdir(parents=True, exist_ok=True)
            browser.call(
                "Browser.setDownloadBehavior",
                {"behavior": "allow", "downloadPath": path, "eventsEnabled": False},
            )
            print(f"Downloads      : forced to {path}")
        else:
            browser.call("Browser.setDownloadBehavior", {"behavior": "default"})
            print("Downloads      : browser default (interception cleared)")
    except CdpError as error:
        print(f"WARNING: could not reset download behavior: {error}")
    finally:
        browser.close()


def ensure_tab(port: int, url: str) -> dict:
    target = find_page_target(port, url)
    if target and target.get("webSocketDebuggerUrl"):
        return target

    version = http_json(port, "/json/version")
    ws_url = version.get("webSocketDebuggerUrl")
    if ws_url:
        browser = Cdp(ws_url)
        try:
            browser.call("Target.createTarget", {"url": url})
        finally:
            browser.close()

    target = wait_for_page_target(port, url, timeout=30.0)
    if not target:
        raise SystemExit(f"ERROR: could not open or find a tab for {url}")
    return target


def main() -> int:
    args = parse_args()

    source = Path(args.source).expanduser().resolve()
    prompts = read_prompts(source)

    print(f"Source         : {source}")
    print(f"Prompts        : {len(prompts)}")

    if not cdp_available(args.port):
        if process_running(args.browser) and not args.user_data_dir:
            print(
                "NOTE: the browser seems to be running already but without remote\n"
                "      debugging. Quit it fully, or re-run with --user-data-dir."
            )
        print("Starting browser...")
        launch_browser(args.browser, args.port, args.url, args.user_data_dir)

    wait_for_cdp(args.port)

    # Clear any download interception BEFORE the extension starts saving files.
    reset_download_behavior(args.port, args.download_dir)

    target = ensure_tab(args.port, args.url)
    target_id = target.get("id")

    # Bring the tab to the front: ChatGPT throttles background tabs, which can
    # stall image generation.
    try:
        urllib.request.urlopen(
            f"http://127.0.0.1:{args.port}/json/activate/{target_id}", timeout=3
        ).read()
    except Exception:
        pass

    cdp = Cdp(target["webSocketDebuggerUrl"], timeout=60.0)

    try:
        cdp.call("Runtime.enable")
        cdp.call("Page.enable")
        try:
            cdp.call("Page.bringToFront")
        except CdpError:
            pass

        print("Waiting for the composer...")
        if not poll(cdp, js(JS_COMPOSER_READY), timeout=30.0):
            print("WARNING: ChatGPT composer not detected; continuing anyway.")

        print("Waiting for the extension panel...")
        if not poll(cdp, js(JS_PANEL_READY), timeout=40.0):
            raise SystemExit(
                "ERROR: extension panel not found on the page.\n"
                "Check that the extension is loaded and enabled for this profile."
            )

        print("Loading prompts into the panel...")
        applied = cdp.evaluate(
            js(
                JS_APPLY,
                text=json.dumps("\n".join(prompts)),
                settings=json.dumps(panel_settings(args)),
            )
        )
        if not applied or not applied.get("ok"):
            raise SystemExit(f"ERROR: {(applied or {}).get('error', 'could not fill the panel')}")
        print(f"Panel reports  : {applied.get('lines')} prompts")

        # Let the panel's debounced save + list render settle.
        time.sleep(0.6)

        print("Clicking Start...")
        started = cdp.evaluate(js(JS_START))
        if not started or not started.get("ok"):
            raise SystemExit(f"ERROR: {(started or {}).get('error', 'could not click Start')}")

        if poll(cdp, js(JS_RUNNING), timeout=10.0, interval=0.3):
            print("Queue is running.")
        else:
            print("WARNING: the panel did not report a running queue; check its log.")

        if args.wait > 0:
            deadline = time.monotonic() + args.wait
            seen: list[str] = []
            while time.monotonic() < deadline:
                for line in cdp.evaluate(js(JS_LOG_TAIL)) or []:
                    if line not in seen:
                        seen.append(line)
                        print(f"  {line}")
                time.sleep(2)
    finally:
        # Disconnecting is safe and intentional: the extension keeps running and
        # owns every download, exactly like a manual run.
        cdp.close()

    print()
    print("START TRIGGERED - the extension now owns generation and downloads.")
    print("Files land in your normal download folder (plus the panel's subfolder).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nautoimg.py stopped.")
        sys.exit(130)
