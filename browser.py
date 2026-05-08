"""Browser abstraction supporting Chrome (CDP) and Firefox (WebDriver BiDi).

Both backends expose the same async API and emit normalized NetworkEvents,
so the rest of the crawler doesn't care which browser it's driving.
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import aiohttp
import websockets


DEBUG_HOST = "127.0.0.1"
DEFAULT_PORT = 9222


# ---------------------------------------------------------------------------
# Normalized network event model
# ---------------------------------------------------------------------------

@dataclass
class NetworkTiming:
    """Network timing fields normalized to milliseconds.

    `request_time_ms` is an opaque baseline (epoch ms or monotonic ms,
    depending on backend). The other three fields are offsets relative to
    that baseline. For BiDi backends `send_start_ms == send_end_ms` because
    BiDi has no separate "request sent" phase.
    """
    request_time_ms: float
    send_start_ms: float
    send_end_ms: float
    receive_headers_end_ms: float
    dns_start_ms: Optional[float] = None
    dns_end_ms: Optional[float] = None
    connect_start_ms: Optional[float] = None
    connect_end_ms: Optional[float] = None
    ssl_start_ms: Optional[float] = None
    ssl_end_ms: Optional[float] = None


@dataclass
class NetworkEvent:
    type: str  # request_will_be_sent | response_received | data_received | loading_finished | loading_failed
    request_id: str
    timestamp_ns: int                          # wall-clock ns (when we received the event)
    url: Optional[str] = None
    method: Optional[str] = None
    status: Optional[int] = None
    timing: Optional[NetworkTiming] = None
    monotonic_ms: Optional[float] = None       # browser-internal time (same scale as timing.request_time_ms)
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _detached_popen_kwargs() -> dict:
    """subprocess.Popen kwargs that detach the child from this process."""
    kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if platform.system() == "Windows":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _os_focus_window(window_class: str) -> None:
    """Raise the first visible window of *window_class* to the OS foreground.

    Windows-only (uses ctypes + user32). A no-op on other platforms.
    Swallows all errors so a focus failure never aborts the crawl.
    """
    if platform.system() != "Windows":
        return
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(window_class, None)
        if not hwnd:
            return
        user32.ShowWindow(hwnd, 9)           # SW_RESTORE — un-minimise if needed
        # Simulate a brief Alt key press/release so Windows grants the
        # foreground-lock permission to this process.
        VK_MENU = 0x12
        KEYEVENTF_KEYUP = 0x0002
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def find_chrome_path() -> Optional[str]:
    system = platform.system()
    if system == "Darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
    elif system == "Windows":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local_app = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            rf"{program_files}\Google\Chrome\Application\chrome.exe",
            rf"{program_files_x86}\Google\Chrome\Application\chrome.exe",
        ]
        if local_app:
            candidates.append(rf"{local_app}\Google\Chrome\Application\chrome.exe")
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            found = shutil.which(name)
            if found:
                return found
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/snap/bin/chromium",
        ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def find_edge_path() -> Optional[str]:
    system = platform.system()
    if system == "Darwin":
        candidates = [
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            str(Path.home() / "Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        ]
    elif system == "Windows":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local_app = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            rf"{program_files}\Microsoft\Edge\Application\msedge.exe",
            rf"{program_files_x86}\Microsoft\Edge\Application\msedge.exe",
        ]
        if local_app:
            candidates.append(rf"{local_app}\Microsoft\Edge\Application\msedge.exe")
    else:
        for name in ("microsoft-edge", "microsoft-edge-stable", "msedge"):
            found = shutil.which(name)
            if found:
                return found
        candidates = [
            "/usr/bin/microsoft-edge",
            "/usr/bin/microsoft-edge-stable",
            "/opt/microsoft/msedge/msedge",
        ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def find_firefox_path() -> Optional[str]:
    system = platform.system()
    if system == "Darwin":
        candidates = [
            "/Applications/Firefox.app/Contents/MacOS/firefox",
            str(Path.home() / "Applications/Firefox.app/Contents/MacOS/firefox"),
        ]
    elif system == "Windows":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        candidates = [
            rf"{program_files}\Mozilla Firefox\firefox.exe",
            rf"{program_files_x86}\Mozilla Firefox\firefox.exe",
        ]
    else:
        for name in ("firefox", "firefox-esr"):
            found = shutil.which(name)
            if found:
                return found
        candidates = [
            "/usr/bin/firefox",
            "/usr/bin/firefox-esr",
            "/snap/bin/firefox",
        ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


# ---------------------------------------------------------------------------
# Browser abstract base
# ---------------------------------------------------------------------------

class Browser(ABC):
    name: str = ""
    profile_dir: Path

    @abstractmethod
    async def attach_or_launch(self, target_url: str) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def navigate(self, url: str) -> None: ...

    @abstractmethod
    async def current_url(self) -> str: ...

    @abstractmethod
    async def wait_ready(self, timeout: float = 10.0) -> None: ...

    @abstractmethod
    async def evaluate(self, expression: str, await_promise: bool = True) -> object: ...

    @abstractmethod
    async def add_init_script(self, source: str) -> None: ...

    @abstractmethod
    async def click(self, x: float, y: float) -> None: ...

    @abstractmethod
    async def insert_text(self, text: str) -> None: ...

    @abstractmethod
    async def press_key(self, key: str, shift: bool = False) -> None: ...

    @abstractmethod
    async def bring_to_front(self) -> None: ...

    @abstractmethod
    def network_events(self) -> asyncio.Queue: ...


# ---------------------------------------------------------------------------
# Common WebSocket plumbing
# ---------------------------------------------------------------------------

class _WSPlumbing:
    """Manages a single WebSocket with id-correlated request/response and an
    event queue that subclasses populate from incoming messages."""

    def __init__(self):
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None

    async def _connect(self, ws_url: str) -> None:
        self.ws = await websockets.connect(ws_url, max_size=64 * 1024 * 1024)
        self._reader_task = asyncio.create_task(self._reader())

    async def _close_ws(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass

    async def _send_request(self, payload: dict) -> dict:
        assert self.ws is not None
        self._next_id += 1
        mid = self._next_id
        payload["id"] = mid
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[mid] = fut
        await self.ws.send(json.dumps(payload))
        return await fut

    async def _reader(self) -> None:
        try:
            assert self.ws is not None
            async for raw in self.ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                self._dispatch(msg)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(exc)

    def _dispatch(self, msg: dict) -> None:
        """Subclass hook. Should resolve _pending futures and route events."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Chrome (CDP)
# ---------------------------------------------------------------------------

class ChromeBrowser(Browser, _WSPlumbing):
    name = "chrome"
    profile_dir = Path.home() / "chrome-chatgpt-profile"

    def __init__(self, port: int = DEFAULT_PORT):
        _WSPlumbing.__init__(self)
        self.port = port
        self._network_q: asyncio.Queue = asyncio.Queue()
        # rid -> {url, created_ms, handshake_sent_ms}  (all in CDP monotonic ms)
        self._ws_urls: dict[str, dict] = {}

    async def attach_or_launch(self, target_url: str) -> None:
        if not port_in_use(DEBUG_HOST, self.port):
            chrome_path = find_chrome_path()
            if not chrome_path:
                print(f"[error] Chrome not found on this system ({platform.system()}).", file=sys.stderr)
                sys.exit(1)
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            args = [
                chrome_path,
                f"--remote-debugging-port={self.port}",
                f"--user-data-dir={self.profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "--restore-last-session",
                target_url,
            ]
            proc = subprocess.Popen(args, **_detached_popen_kwargs())
            print(f"[info] launched Chrome (pid={proc.pid}) profile={self.profile_dir}", file=sys.stderr)
        else:
            print(f"[info] port {self.port} in use; attaching to existing Chrome.", file=sys.stderr)

        # Wait for the debugger HTTP endpoint.
        deadline = time.monotonic() + 20.0
        async with aiohttp.ClientSession() as sess:
            while time.monotonic() < deadline:
                try:
                    async with sess.get(f"http://{DEBUG_HOST}:{self.port}/json/version") as r:
                        if r.status == 200:
                            break
                except aiohttp.ClientError:
                    pass
                await asyncio.sleep(0.3)
            else:
                raise RuntimeError(f"Chrome debugger did not respond on :{self.port}")

            # Find an existing tab for target_url's host, or reuse the first
            # page tab — never open a new tab automatically.
            target_host = urlparse(target_url).netloc
            async with sess.get(f"http://{DEBUG_HOST}:{self.port}/json") as r:
                targets = await r.json()
            ws_url = None
            # 1. Prefer a tab already on the target host.
            for t in targets:
                if t.get("type") == "page" and target_host and target_host in t.get("url", ""):
                    ws_url = t["webSocketDebuggerUrl"]
                    break
            # 2. Fall back to the first available page tab.
            if not ws_url:
                for t in targets:
                    if t.get("type") == "page":
                        ws_url = t["webSocketDebuggerUrl"]
                        break
            # 3. Only open a new tab if the browser has no page tabs at all.
            if not ws_url:
                async with sess.put(f"http://{DEBUG_HOST}:{self.port}/json/new?{target_url}") as r:
                    if r.status >= 400:
                        async with sess.get(f"http://{DEBUG_HOST}:{self.port}/json/new?{target_url}") as r2:
                            ws_url = (await r2.json())["webSocketDebuggerUrl"]
                    else:
                        ws_url = (await r.json())["webSocketDebuggerUrl"]

        await self._connect(ws_url)
        await self._call("Page.enable")
        await self._call("Network.enable")
        await self._call("Runtime.enable")
        await self._call("DOM.enable")

    async def close(self) -> None:
        await self._close_ws()

    async def navigate(self, url: str) -> None:
        await self._call("Page.navigate", {"url": url})

    async def current_url(self) -> str:
        return await self.evaluate("window.location.href") or ""

    async def wait_ready(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if (await self.evaluate("document.readyState")) == "complete":
                return
            await asyncio.sleep(0.1)

    async def evaluate(self, expression: str, await_promise: bool = True) -> object:
        result = await self._call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
        })
        return result.get("result", {}).get("value")

    async def add_init_script(self, source: str) -> None:
        await self._call("Page.addScriptToEvaluateOnNewDocument", {"source": source})

    async def click(self, x: float, y: float) -> None:
        await self._call("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1,
        })
        await asyncio.sleep(0.04)
        await self._call("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1,
        })

    async def insert_text(self, text: str) -> None:
        await self._call("Input.insertText", {"text": text})

    async def press_key(self, key: str, shift: bool = False) -> None:
        windows_codes = {"Enter": 13}
        for phase in ("keyDown", "keyUp"):
            await self._call("Input.dispatchKeyEvent", {
                "type": phase, "key": key, "code": key,
                "windowsVirtualKeyCode": windows_codes.get(key, 0),
                "modifiers": 1 if shift else 0,
            })

    async def bring_to_front(self) -> None:
        await self._call("Page.bringToFront")
        _os_focus_window("Chrome_WidgetWin_1")

    def network_events(self) -> asyncio.Queue:
        return self._network_q

    # CDP plumbing
    async def _call(self, method: str, params: Optional[dict] = None) -> dict:
        return await self._send_request({"method": method, "params": params or {}})

    def _dispatch(self, msg: dict) -> None:
        if "id" in msg and msg["id"] in self._pending:
            fut = self._pending.pop(msg["id"])
            if fut.done():
                return
            if "error" in msg:
                fut.set_exception(RuntimeError(f"CDP error: {msg['error']}"))
            else:
                fut.set_result(msg.get("result", {}))
        elif "method" in msg:
            self._handle_cdp_event(msg["method"], msg.get("params", {}))

    def _handle_cdp_event(self, method: str, params: dict) -> None:
        ts = time.time_ns()
        rid = params.get("requestId")
        if method == "Network.requestWillBeSent" and rid:
            req = params.get("request", {})
            self._network_q.put_nowait(NetworkEvent(
                type="request_will_be_sent", request_id=rid, timestamp_ns=ts,
                url=req.get("url"), method=req.get("method"),
            ))
        elif method == "Network.responseReceived" and rid:
            resp = params.get("response", {})
            t = resp.get("timing")
            timing = None
            if t:
                def _norm(v: Optional[float]) -> Optional[float]:
                    if v is None:
                        return None
                    fv = float(v)
                    return fv if fv >= 0 else None

                timing = NetworkTiming(
                    request_time_ms=t["requestTime"] * 1000.0,
                    send_start_ms=t["sendStart"],
                    send_end_ms=t["sendEnd"],
                    receive_headers_end_ms=t["receiveHeadersEnd"],
                    dns_start_ms=_norm(t.get("dnsStart")),
                    dns_end_ms=_norm(t.get("dnsEnd")),
                    connect_start_ms=_norm(t.get("connectStart")),
                    connect_end_ms=_norm(t.get("connectEnd")),
                    ssl_start_ms=_norm(t.get("sslStart")),
                    ssl_end_ms=_norm(t.get("sslEnd")),
                )
            self._network_q.put_nowait(NetworkEvent(
                type="response_received", request_id=rid, timestamp_ns=ts,
                status=resp.get("status"), timing=timing,
            ))
        elif method == "Network.dataReceived" and rid:
            self._network_q.put_nowait(NetworkEvent(
                type="data_received", request_id=rid, timestamp_ns=ts,
                monotonic_ms=(params.get("timestamp") or 0) * 1000.0,
            ))
        elif method == "Network.loadingFinished" and rid:
            self._network_q.put_nowait(NetworkEvent(
                type="loading_finished", request_id=rid, timestamp_ns=ts,
                monotonic_ms=(params.get("timestamp") or 0) * 1000.0,
            ))
        elif method == "Network.loadingFailed" and rid:
            self._network_q.put_nowait(NetworkEvent(
                type="loading_failed", request_id=rid, timestamp_ns=ts,
                error=params.get("errorText"),
            ))
        elif method == "Network.webSocketCreated" and rid:
            url = params.get("url")
            if url:
                self._ws_urls[rid] = {
                    "url": url,
                    "created_ms": (params.get("timestamp") or 0) * 1000.0,
                    "handshake_sent_ms": None,
                }
        elif method == "Network.webSocketWillSendHandshakeRequest" and rid:
            info = self._ws_urls.get(rid)
            if info:
                info["handshake_sent_ms"] = (params.get("timestamp") or 0) * 1000.0
            self._network_q.put_nowait(NetworkEvent(
                type="request_will_be_sent", request_id=rid, timestamp_ns=ts,
                url=info["url"] if info else None, method="WEBSOCKET",
                monotonic_ms=(params.get("timestamp") or 0) * 1000.0,
            ))
        elif method == "Network.webSocketHandshakeResponseReceived" and rid:
            resp = params.get("response", {})
            # WebSocketResponse carries no .timing field in CDP.
            # Use sent_ms as the request_time baseline so all offsets are small
            # and positive.  webSocketCreated has no timestamp, so we cannot
            # compute DNS/connect/stalled phases — those are left None.
            info = self._ws_urls.get(rid, {})
            sent_ms: float = info.get("handshake_sent_ms") or 0.0
            response_ms: float = (params.get("timestamp") or 0) * 1000.0
            ws_timing = NetworkTiming(
                request_time_ms=sent_ms,
                send_start_ms=0.0,
                send_end_ms=0.0,
                receive_headers_end_ms=max(0.0, response_ms - sent_ms),
            )
            self._network_q.put_nowait(NetworkEvent(
                type="response_received", request_id=rid, timestamp_ns=ts,
                status=resp.get("status"), timing=ws_timing,
            ))
        elif method == "Network.webSocketFrameReceived" and rid:
            self._network_q.put_nowait(NetworkEvent(
                type="data_received", request_id=rid, timestamp_ns=ts,
                monotonic_ms=(params.get("timestamp") or 0) * 1000.0,
            ))
        elif method == "Network.webSocketClosed" and rid:
            self._network_q.put_nowait(NetworkEvent(
                type="loading_finished", request_id=rid, timestamp_ns=ts,
                monotonic_ms=(params.get("timestamp") or 0) * 1000.0,
            ))
            self._ws_urls.pop(rid, None)
        elif method == "Network.webSocketFrameError" and rid:
            self._network_q.put_nowait(NetworkEvent(
                type="loading_failed", request_id=rid, timestamp_ns=ts,
                error=params.get("errorMessage") or "webSocketFrameError",
            ))
            self._ws_urls.pop(rid, None)


# ---------------------------------------------------------------------------
# Edge (CDP) — identical protocol to Chrome; only executable and profile differ
# ---------------------------------------------------------------------------

class EdgeBrowser(ChromeBrowser):
    name = "edge"
    profile_dir = Path.home() / "edge-chatgpt-profile"

    async def attach_or_launch(self, target_url: str) -> None:
        if not port_in_use(DEBUG_HOST, self.port):
            edge_path = find_edge_path()
            if not edge_path:
                print(f"[error] Microsoft Edge not found on this system ({platform.system()}).", file=sys.stderr)
                sys.exit(1)
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            args = [
                edge_path,
                f"--remote-debugging-port={self.port}",
                f"--user-data-dir={self.profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "--restore-last-session",
                target_url,
            ]
            proc = subprocess.Popen(args, **_detached_popen_kwargs())
            print(f"[info] launched Edge (pid={proc.pid}) profile={self.profile_dir}", file=sys.stderr)
        else:
            print(f"[info] port {self.port} in use; attaching to existing Edge.", file=sys.stderr)

        # Wait for the debugger HTTP endpoint (same as Chrome).
        deadline = time.monotonic() + 20.0
        async with aiohttp.ClientSession() as sess:
            while time.monotonic() < deadline:
                try:
                    async with sess.get(f"http://{DEBUG_HOST}:{self.port}/json/version") as r:
                        if r.status == 200:
                            break
                except aiohttp.ClientError:
                    pass
                await asyncio.sleep(0.3)
            else:
                raise RuntimeError(f"Edge debugger did not respond on :{self.port}")

            target_host = urlparse(target_url).netloc
            async with sess.get(f"http://{DEBUG_HOST}:{self.port}/json") as r:
                targets = await r.json()
            ws_url = None
            for t in targets:
                if t.get("type") == "page" and target_host and target_host in t.get("url", ""):
                    ws_url = t["webSocketDebuggerUrl"]
                    break
            if not ws_url:
                for t in targets:
                    if t.get("type") == "page":
                        ws_url = t["webSocketDebuggerUrl"]
                        break
            if not ws_url:
                async with sess.put(f"http://{DEBUG_HOST}:{self.port}/json/new?{target_url}") as r:
                    if r.status >= 400:
                        async with sess.get(f"http://{DEBUG_HOST}:{self.port}/json/new?{target_url}") as r2:
                            ws_url = (await r2.json())["webSocketDebuggerUrl"]
                    else:
                        ws_url = (await r.json())["webSocketDebuggerUrl"]

        await self._connect(ws_url)
        await self._call("Page.enable")
        await self._call("Network.enable")
        await self._call("Runtime.enable")
        await self._call("DOM.enable")


# ---------------------------------------------------------------------------
# Firefox (WebDriver BiDi)
# ---------------------------------------------------------------------------

# W3C WebDriver special-key code points (subset).
_BIDI_KEYS = {"Enter": "", "Shift": ""}


class FirefoxBrowser(Browser, _WSPlumbing):
    name = "firefox"
    profile_dir = Path.home() / "firefox-chatgpt-profile"

    def __init__(self, port: int = DEFAULT_PORT):
        _WSPlumbing.__init__(self)
        self.port = port
        self._network_q: asyncio.Queue = asyncio.Queue()
        self._context_id: Optional[str] = None

    async def attach_or_launch(self, target_url: str) -> None:
        # Firefox without geckodriver does NOT expose WebDriver Classic HTTP
        # endpoints (/status, POST /session). It only exposes the BiDi
        # WebSocket, and writes its URL to stderr. So we capture stderr to a
        # log file and parse the ws:// URL from it.
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        stderr_log = self.profile_dir / "firefox-stderr.log"

        if port_in_use(DEBUG_HOST, self.port):
            # Verify it's not Chrome from an earlier run.
            async with aiohttp.ClientSession() as sess:
                chrome_info: Optional[str] = None
                try:
                    async with sess.get(
                        f"http://{DEBUG_HOST}:{self.port}/json/version",
                        timeout=aiohttp.ClientTimeout(total=2),
                    ) as r:
                        if r.status == 200:
                            data = await r.json()
                            chrome_info = str(data.get("Browser", ""))
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    pass
            if chrome_info:
                raise RuntimeError(
                    f"Port {self.port} is held by {chrome_info}. Close that "
                    f"Chrome window (or `pkill -f 'Google Chrome'`), then re-run."
                )
            print(f"[info] port {self.port} in use; attaching as Firefox.", file=sys.stderr)
        else:
            ff_path = find_firefox_path()
            if not ff_path:
                print(f"[error] Firefox not found on this system ({platform.system()}).", file=sys.stderr)
                sys.exit(1)
            args = [
                ff_path,
                "-profile", str(self.profile_dir),
                "-no-remote",
                "--remote-debugging-port", str(self.port),
                target_url,
            ]
            # Truncate any old log and capture Firefox's stderr — that's where
            # it announces "WebDriver BiDi listening on ws://...".
            log_f = stderr_log.open("w")
            kwargs = _detached_popen_kwargs()
            kwargs["stderr"] = log_f
            proc = subprocess.Popen(args, **kwargs)
            log_f.close()  # Popen dup'd the FD; child keeps it open.
            print(f"[info] launched Firefox (pid={proc.pid}) profile={self.profile_dir}", file=sys.stderr)

        ws_url = await self._discover_bidi_ws_url(stderr_log, timeout=30.0)
        await self._connect(ws_url)

        # Best-effort: clear any stale session lingering from a previous run.
        # Firefox in pure-BiDi mode allows only one session at a time, and if
        # the previous run never called session.end the next session.new will
        # fail with "Maximum number of active sessions".
        try:
            await asyncio.wait_for(self._call("session.end", {}), timeout=2.0)
        except Exception:
            pass

        try:
            await self._call("session.new", {"capabilities": {"alwaysMatch": {}}})
        except RuntimeError as e:
            if "Maximum" in str(e) or "session not created" in str(e):
                raise RuntimeError(
                    "Firefox already has an active BiDi session that we couldn't "
                    "release. Quit Firefox completely (Cmd-Q or `pkill -f firefox`) "
                    "and re-run."
                ) from e
            raise

        await self._call("session.subscribe", {"events": [
            "browsingContext.navigationStarted",
            "browsingContext.load",
            "network.beforeRequestSent",
            "network.responseStarted",
            "network.responseCompleted",
            "network.fetchError",
        ]})

        # Find a context already on target_url's host if possible; otherwise
        # the first top-level context. The crawler will navigate explicitly.
        target_host = urlparse(target_url).netloc
        tree = await self._call("browsingContext.getTree", {})
        contexts = tree.get("contexts", [])
        if not contexts:
            raise RuntimeError("no browsing context available in Firefox")
        chosen = None
        for ctx in contexts:
            ctx_url = ctx.get("url", "") or ""
            if target_host and target_host in ctx_url:
                chosen = ctx
                break
        self._context_id = (chosen or contexts[0])["context"]

    async def _discover_bidi_ws_url(self, stderr_log: Path, timeout: float) -> str:
        """Discover the BiDi WebSocket URL.

        We collect candidate URLs from (a) Firefox's stderr log, and (b) a
        few well-known paths on the same port. Each candidate is verified
        with a real WebSocket handshake — Firefox sometimes serves HTTP at
        the listening port's root, so we can't trust the URL until it
        actually upgrades.
        """
        url_re = re.compile(r"ws://[^\s\"',>]+")
        tried_per_round: list[str] = []
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            candidates: list[str] = []
            # 1. URLs printed by Firefox at startup (most reliable).
            if stderr_log.exists():
                try:
                    content = stderr_log.read_text(errors="replace")
                    candidates.extend(url_re.findall(content))
                except OSError:
                    pass
            # 2. Common fall-back paths.
            for path in ("/session", "/", ""):
                candidates.append(f"ws://{DEBUG_HOST}:{self.port}{path}")

            # Dedupe while preserving order.
            seen: set[str] = set()
            ordered: list[str] = []
            for url in candidates:
                if url not in seen:
                    seen.add(url)
                    ordered.append(url)
            tried_per_round = ordered

            for url in ordered:
                ok = await self._ws_handshake_succeeds(url)
                if ok:
                    print(f"[info] BiDi WebSocket: {url}", file=sys.stderr)
                    return url

            await asyncio.sleep(0.5)

        # Exhausted: dump diagnostics to stderr to help adjust selectors.
        stderr_excerpt = ""
        if stderr_log.exists():
            try:
                stderr_excerpt = stderr_log.read_text(errors="replace")[-2000:]
            except OSError:
                stderr_excerpt = "(could not read stderr log)"
        raise RuntimeError(
            f"No working Firefox BiDi WebSocket found. Tried: {tried_per_round}\n"
            f"--- {stderr_log} (tail) ---\n{stderr_excerpt}"
        )

    @staticmethod
    async def _ws_handshake_succeeds(url: str) -> bool:
        try:
            ws = await asyncio.wait_for(
                websockets.connect(url, max_size=64 * 1024 * 1024),
                timeout=2.0,
            )
        except Exception:
            return False
        try:
            await ws.close()
        except Exception:
            pass
        return True

    async def close(self) -> None:
        # End the BiDi session so it doesn't linger and block future runs.
        # Firefox keeps the browser process alive after session.end, so the
        # OKTA-logged-in profile state is preserved.
        if self.ws is not None:
            try:
                await asyncio.wait_for(self._call("session.end", {}), timeout=2.0)
            except Exception:
                pass
        await self._close_ws()

    async def navigate(self, url: str) -> None:
        await self._call("browsingContext.navigate", {
            "context": self._context_id, "url": url, "wait": "interactive",
        })

    async def current_url(self) -> str:
        v = await self.evaluate("window.location.href")
        return v or ""

    async def wait_ready(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if (await self.evaluate("document.readyState")) == "complete":
                return
            await asyncio.sleep(0.1)

    async def evaluate(self, expression: str, await_promise: bool = True) -> object:
        result = await self._call("script.evaluate", {
            "expression": expression,
            "target": {"context": self._context_id},
            "awaitPromise": await_promise,
            "resultOwnership": "none",
        })
        return self._unmarshal_bidi_value(result.get("result"))

    async def add_init_script(self, source: str) -> None:
        # BiDi requires a function declaration. Wrap source in an IIFE-friendly
        # function so its top-level statements still execute exactly as in CDP.
        wrapped = "function() { " + source + " }"
        await self._call("script.addPreloadScript", {"functionDeclaration": wrapped})

    async def click(self, x: float, y: float) -> None:
        await self._call("input.performActions", {
            "context": self._context_id,
            "actions": [{
                "type": "pointer", "id": "mouse1",
                "actions": [
                    {"type": "pointerMove", "x": int(x), "y": int(y)},
                    {"type": "pointerDown", "button": 0},
                    {"type": "pointerUp", "button": 0},
                ],
            }],
        })

    async def insert_text(self, text: str) -> None:
        # BiDi has no Input.insertText equivalent; we synthesise key down/up
        # for each character. Works for most printable Unicode (incl. CJK).
        actions = []
        for ch in text:
            actions.append({"type": "keyDown", "value": ch})
            actions.append({"type": "keyUp", "value": ch})
        await self._call("input.performActions", {
            "context": self._context_id,
            "actions": [{"type": "key", "id": "kbd1", "actions": actions}],
        })

    async def press_key(self, key: str, shift: bool = False) -> None:
        v = _BIDI_KEYS.get(key, key)
        actions: list[dict] = []
        if shift:
            actions.append({"type": "keyDown", "value": _BIDI_KEYS["Shift"]})
        actions += [
            {"type": "keyDown", "value": v},
            {"type": "keyUp", "value": v},
        ]
        if shift:
            actions.append({"type": "keyUp", "value": _BIDI_KEYS["Shift"]})
        await self._call("input.performActions", {
            "context": self._context_id,
            "actions": [{"type": "key", "id": "kbd1", "actions": actions}],
        })

    async def bring_to_front(self) -> None:
        try:
            await self._call("browsingContext.activate", {"context": self._context_id})
        except Exception:
            pass
        _os_focus_window("MozillaWindowClass")

    def network_events(self) -> asyncio.Queue:
        return self._network_q

    # BiDi plumbing
    async def _call(self, method: str, params: Optional[dict] = None) -> dict:
        return await self._send_request({"method": method, "params": params or {}})

    def _dispatch(self, msg: dict) -> None:
        msg_type = msg.get("type")
        if msg_type in ("success", "error") and "id" in msg and msg["id"] in self._pending:
            fut = self._pending.pop(msg["id"])
            if fut.done():
                return
            if msg_type == "error":
                fut.set_exception(RuntimeError(f"BiDi error: {msg.get('error')} {msg.get('message')}"))
            else:
                fut.set_result(msg.get("result", {}))
        elif msg_type == "event":
            self._handle_bidi_event(msg.get("method", ""), msg.get("params", {}))

    def _handle_bidi_event(self, method: str, params: dict) -> None:
        ts = time.time_ns()
        req = params.get("request", {}) if method.startswith("network.") else {}
        rid = req.get("request") if req else None

        if method == "network.beforeRequestSent" and rid:
            self._network_q.put_nowait(NetworkEvent(
                type="request_will_be_sent", request_id=rid, timestamp_ns=ts,
                url=req.get("url"), method=req.get("method"),
            ))
        elif method == "network.responseStarted" and rid:
            response = params.get("response", {})
            timings = req.get("timings", {})
            timing = None
            if timings:
                request_start = float(timings.get("requestStart", 0))
                response_start = float(timings.get("responseStart", request_start))
                # BiDi has no separate sendStart/sendEnd phase. We anchor
                # request_time to requestStart and treat send_* as 0, so
                # waiting_ms = responseStart - requestStart.
                timing = NetworkTiming(
                    request_time_ms=request_start,
                    send_start_ms=0.0,
                    send_end_ms=0.0,
                    receive_headers_end_ms=response_start - request_start,
                )
            self._network_q.put_nowait(NetworkEvent(
                type="response_received", request_id=rid, timestamp_ns=ts,
                status=response.get("status"), timing=timing,
            ))
        elif method == "network.responseCompleted" and rid:
            timings = req.get("timings", {})
            response_end = float(timings.get("responseEnd")) if timings.get("responseEnd") is not None else None
            self._network_q.put_nowait(NetworkEvent(
                type="loading_finished", request_id=rid, timestamp_ns=ts,
                monotonic_ms=response_end,
            ))
        elif method == "network.fetchError" and rid:
            self._network_q.put_nowait(NetworkEvent(
                type="loading_failed", request_id=rid, timestamp_ns=ts,
                error=params.get("errorText") or "fetchError",
            ))

    @staticmethod
    def _unmarshal_bidi_value(remote: Optional[dict]):
        """BiDi returns values as {type, value} (sometimes {type, handle}).
        We only need the JSON-y types for our use case."""
        if not remote:
            return None
        t = remote.get("type")
        if t == "null" or t == "undefined":
            return None
        if t in ("string", "number", "boolean"):
            return remote.get("value")
        if t == "object":
            v = remote.get("value")
            if isinstance(v, list):
                # BiDi returns objects as [[k, v], ...]; convert to dict.
                try:
                    return {kv[0]: FirefoxBrowser._unmarshal_bidi_value(kv[1]) for kv in v}
                except Exception:
                    return v
            return v
        if t == "array":
            v = remote.get("value", [])
            return [FirefoxBrowser._unmarshal_bidi_value(x) for x in v]
        return remote.get("value")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_browser(name: str, port: int = DEFAULT_PORT) -> Browser:
    name = name.lower()
    if name == "chrome":
        return ChromeBrowser(port=port)
    if name == "edge":
        return EdgeBrowser(port=port)
    if name == "firefox":
        return FirefoxBrowser(port=port)
    raise ValueError(f"unknown browser: {name!r} (expected 'chrome', 'edge', or 'firefox')")
