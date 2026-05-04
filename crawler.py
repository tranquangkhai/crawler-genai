#!/usr/bin/env python3
"""Human-like ChatGPT crawler — works with Chrome (CDP), Edge (CDP), or Firefox (BiDi).

Usage:
    python crawler.py --setup                                  # first-time OKTA login
    python crawler.py --setup --browser firefox                # same, on Firefox
    python crawler.py --setup --browser edge                   # same, on Edge
    python crawler.py "日本の首都はどこですか？"                  # default: chrome
    python crawler.py --browser firefox "..."                  # use Firefox
    python crawler.py --browser edge "..."                     # use Edge
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import random
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from browser import Browser, NetworkEvent, NetworkTiming, make_browser


APP_URLS = {
    "chatgpt": "https://chatgpt.com",
    "copilot-personal": "https://copilot.microsoft.com",
    "copilot-corporate": "https://m365.cloud.microsoft/chat",
}
CSV_PATH = Path(__file__).resolve().parent / "results.csv"
# Match conversation endpoints from both authenticated and anonymous routes.
TARGET_ENDPOINTS = {
    "chatgpt": [
        ("chatgpt-conversation", re.compile(r"/backend-(?:api|anon)/(?:f/)?conversation(?:\?|$)"), {"POST"}),
    ],
    "copilot-personal": [
        ("copilot-personal", re.compile(r"^wss?://copilot\.microsoft\.com/c/api/chat(?:\?|$)"), None),
    ],
    "copilot-corporate": [
        ("copilot-corporate", re.compile(r"^wss?://substrate\.office/m365Copilot/Chathub/(?:\?|$)"), None),
    ],
}
ANSWER_TIMEOUT_SEC = 90.0

CRAWLER_HOOK_SCRIPT = r"""
(() => {
  if (window.__crawlerInstalled) return;
  window.__crawlerInstalled = true;
  window.__crawler = {
    firstByteEpochMs: null,
    streamingStartedAt: null,
    streamingEndedAt: null,
    streamSignal: null,
    diagnostic: { activeSelector: null, selectorTries: {}, rolesSeen: [] },
  };

  const ASSISTANT_SELECTORS = [
    '[data-message-author-role="assistant"]',
    '[data-author-role="assistant"]',
    '[data-role="assistant"]',
    '[data-message-author="assistant"]',
    'article[data-message-author-role="assistant"]',
    'div.agent-turn',
  ];

  const findAssistantBubble = () => {
    for (const sel of ASSISTANT_SELECTORS) {
      const els = document.querySelectorAll(sel);
      window.__crawler.diagnostic.selectorTries[sel] = els.length;
      if (els.length > 0) {
        window.__crawler.diagnostic.activeSelector = sel;
        return els[els.length - 1];
      }
    }
    const all = document.querySelectorAll('[data-message-author-role]');
    const roles = new Set();
    let last = null;
    for (const el of all) {
      const role = el.getAttribute('data-message-author-role');
      if (role) roles.add(role);
      if (role && role !== 'user') last = el;
    }
    window.__crawler.diagnostic.rolesSeen = Array.from(roles);
    if (last) {
      window.__crawler.diagnostic.activeSelector =
        'role-fallback(' + last.getAttribute('data-message-author-role') + ')';
    }
    return last;
  };

  const isStreaming = () => {
    for (const btn of document.querySelectorAll('button')) {
      const label = (btn.getAttribute('aria-label') || '').toLowerCase();
      const testId = (btn.getAttribute('data-testid') || '').toLowerCase();
      if (label.includes('stop') || label.includes('止め') ||
          label.includes('停止') || label.includes('streaming') ||
          label.includes('ストリーミング') || testId.includes('stop')) {
        return { source: 'stop-button', label, testId };
      }
    }
    if (document.querySelector('.result-streaming, .streaming, [data-streaming="true"]')) {
      return { source: 'streaming-class' };
    }
    for (const sel of ASSISTANT_SELECTORS) {
      if (document.querySelector(sel + '[aria-busy="true"]')) {
        return { source: 'aria-busy-assistant' };
      }
    }
    if (document.querySelector('main [aria-busy="true"]')) {
      return { source: 'aria-busy-main' };
    }
    return null;
  };

  setInterval(() => {
    if (window.__crawler.firstByteEpochMs === null) {
      const bubble = findAssistantBubble();
      if (bubble && (bubble.textContent || '').trim()) {
        window.__crawler.firstByteEpochMs = Date.now();
      }
    }
    const sig = isStreaming();
    const now = Date.now();
    if (sig) {
      window.__crawler.streamSignal = sig;
      if (!window.__crawler.streamingStartedAt) {
        window.__crawler.streamingStartedAt = now;
      }
    } else if (window.__crawler.streamingStartedAt &&
               !window.__crawler.streamingEndedAt) {
      window.__crawler.streamingEndedAt = now;
    }
  }, 100);
})();
"""

WEBDRIVER_PATCH = (
    "Object.defineProperty(navigator, 'webdriver', "
    "{get: () => undefined, configurable: true});"
)

CSV_COLUMNS = [
    "run_id", "timestamp_iso", "browser", "target", "prompt",
    "typing_start", "typing_end", "enter_pressed",
    "first_byte", "answer_done",
    "net_response_received", "net_loading_finished",
    "stalled_ms", "request_sent_ms", "waiting_ms", "content_download_ms", "total_ms",
    "http_status", "request_id", "notes",
]


def iso_now(ts_ns: Optional[int] = None) -> str:
    ts = ts_ns if ts_ns is not None else time.time_ns()
    dt = datetime.fromtimestamp(ts / 1e9, tz=timezone.utc).astimezone()
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f%z")


def _looks_like_login(url: str) -> bool:
    needles = (
        "okta.com",
        "/login",
        "auth0.openai",
        "accounts.google.com",
        "login.microsoftonline.com",
        "login.live.com",
    )
    return any(n in url for n in needles)


def _match_target_endpoint(target: str, url: Optional[str], method: Optional[str]) -> Optional[str]:
    if not url:
        return None
    method_u = (method or "").upper()
    for name, pattern, methods in TARGET_ENDPOINTS[target]:
        if methods and method_u not in methods:
            continue
        if pattern.search(url):
            return name
    return None


def _compute_stalled_ms(timing: NetworkTiming) -> Optional[float]:
    """Approximate DevTools 'Connection Start > Stalled'.

    Requires DNS or connect phase data from CDP HTTP responses.
    Not available for BiDi or WebSocket connections (both lack those phases).
    """
    # Without at least one connection-phase timestamp we cannot compute stalled.
    if timing.dns_start_ms is None and timing.connect_start_ms is None:
        return None
    starts = [timing.dns_start_ms, timing.connect_start_ms, timing.send_start_ms]
    valid = [float(v) for v in starts if isinstance(v, (int, float)) and v >= 0]
    if not valid:
        return None
    return max(0.0, min(valid))


async def focus_prompt_input(browser: Browser) -> Optional[dict]:
    """Click the prompt input. Retries because the SPA can be slow to render."""
    js = r"""
    (() => {
      const selectors = [
        // ChatGPT
        '#prompt-textarea',
        'form textarea',
        // Copilot personal (copilot.microsoft.com) & corporate (m365.cloud.microsoft)
        'textarea[data-testid="chat-input"]',
        'div[data-testid="composer-input"]',
        'div[data-testid="chat-input"]',
        '[data-testid*="composer"] textarea',
        '[data-testid*="composer"] div[contenteditable="true"]',
        'div[role="textbox"][contenteditable="true"]',
        'textarea[aria-label]',
        'textarea[placeholder]',
        // Generic fallbacks
        'div[contenteditable="true"]',
        'textarea',
      ];
      for (const sel of selectors) {
        const el = document.querySelector(sel);
        if (el && el.offsetParent !== null) {
          el.scrollIntoView({block: 'center'});
          const r = el.getBoundingClientRect();
          if (r.width > 0 && r.height > 0) {
            return {x: r.left + r.width / 2, y: r.top + r.height / 2};
          }
        }
      }
      return null;
    })()
    """
    for _ in range(40):
        box = await browser.evaluate(js)
        if box:
            await browser.click(box["x"], box["y"])
            await asyncio.sleep(0.15)
            return box
        await asyncio.sleep(0.5)
    return None


async def human_type(browser: Browser, text: str) -> None:
    punctuation = set("、。,.!?！？:;：；")
    for ch in text:
        if ch == "\n":
            await browser.press_key("Enter", shift=True)
        else:
            await browser.insert_text(ch)
        delay = random.uniform(0.05, 0.20)
        if ch in punctuation:
            delay += random.uniform(0.15, 0.30)
        await asyncio.sleep(delay)


async def setup_mode(browser_name: str, target: str) -> None:
    browser = make_browser(browser_name)
    await browser.attach_or_launch(APP_URLS[target])
    print(f"[info] {browser_name} を起動しました。ブラウザ画面で OKTA 経由でログインを完了してください。", file=sys.stderr)
    print(f"[info] プロファイル: {browser.profile_dir}", file=sys.stderr)
    print("[info] ログイン完了後、ウィンドウは閉じずにこのスクリプトも Ctrl-C で終了してください。", file=sys.stderr)
    print(f"[info] 次回以降 `python crawler.py --browser {browser_name} \"<質問>\"` で利用できます。", file=sys.stderr)
    await browser.close()


async def run_prompt(prompt: str, browser_name: str, target: str) -> dict:
    browser = make_browser(browser_name)
    await browser.attach_or_launch(APP_URLS[target])
    try:
        await browser.add_init_script(WEBDRIVER_PATCH)
        await browser.add_init_script(CRAWLER_HOOK_SCRIPT)

        # Always start a fresh chat at the root.
        await browser.navigate(APP_URLS[target])
        url = ""
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            url = await browser.current_url()
            if _looks_like_login(url) or url.startswith("http"):
                break
            await asyncio.sleep(0.3)

        if _looks_like_login(url):
            print(f"[error] ログインページに遷移しています ({url})。", file=sys.stderr)
            print(f"[error] `python crawler.py --setup --browser {browser_name}` で再ログインしてください。", file=sys.stderr)
            sys.exit(2)

        await browser.wait_ready()

        # Force a fresh hook install on the now-loaded document.
        await browser.evaluate(
            "delete window.__crawlerInstalled; delete window.__crawler;",
            await_promise=False,
        )
        await browser.evaluate(CRAWLER_HOOK_SCRIPT, await_promise=False)

        box = await focus_prompt_input(browser)
        if not box:
            print("[error] プロンプト入力欄が見つかりません。", file=sys.stderr)
            # Dump input-like elements with their key attributes to aid selector debugging.
            diag = await browser.evaluate(r"""
            (() => {
              const tags = ['textarea', 'input', '[contenteditable="true"]', '[role="textbox"]'];
              const found = [];
              for (const sel of tags) {
                for (const el of document.querySelectorAll(sel)) {
                  found.push({
                    tag: el.tagName.toLowerCase(),
                    id: el.id || null,
                    role: el.getAttribute('role') || null,
                    testid: el.getAttribute('data-testid') || null,
                    placeholder: el.getAttribute('placeholder') || null,
                    ariaLabel: el.getAttribute('aria-label') || null,
                    visible: el.offsetParent !== null,
                    rect: (r => ({w: Math.round(r.width), h: Math.round(r.height)}))(el.getBoundingClientRect()),
                  });
                }
              }
              return JSON.stringify(found.slice(0, 20));
            })()
            """)
            print("[debug] input candidates:", diag, file=sys.stderr)
            dump = await browser.evaluate("document.body.innerHTML.slice(0, 1500)")
            print(str(dump)[:1500], file=sys.stderr)
            sys.exit(3)

        # ------------------------------------------------------------------
        # Network event collector
        # ------------------------------------------------------------------
        net = {
            "request_id": None,
            "endpoint_name": None,
            "request_url": None,
            "timing": None,                 # NetworkTiming
            "response_received_ts_ns": None,
            "loading_finished_ms": None,    # browser-internal monotonic ms
            "loading_finished_ts_ns": None,
            "data_received_count": 0,
            "last_data_received_ms": None,
            "last_data_received_ts_ns": None,
            "http_status": None,
            "failed": None,
        }
        answer_done = asyncio.Event()
        enter_armed = asyncio.Event()

        async def pump() -> None:
            q = browser.network_events()
            while True:
                ev: NetworkEvent = await q.get()
                if ev.type == "request_will_be_sent":
                    if net["request_id"] is not None:
                        continue
                    if not enter_armed.is_set() and (ev.method or "").upper() != "WEBSOCKET":
                        continue
                    endpoint_name = _match_target_endpoint(target, ev.url, ev.method)
                    if endpoint_name:
                        net["request_id"] = ev.request_id
                        net["endpoint_name"] = endpoint_name
                        net["request_url"] = ev.url
                elif ev.request_id == net["request_id"]:
                    if ev.type == "response_received":
                        net["timing"] = ev.timing
                        net["http_status"] = ev.status
                        net["response_received_ts_ns"] = ev.timestamp_ns
                    elif ev.type == "data_received":
                        net["data_received_count"] += 1
                        net["last_data_received_ms"] = ev.monotonic_ms
                        net["last_data_received_ts_ns"] = ev.timestamp_ns
                    elif ev.type == "loading_finished":
                        net["loading_finished_ms"] = ev.monotonic_ms
                        net["loading_finished_ts_ns"] = ev.timestamp_ns
                        answer_done.set()
                    elif ev.type == "loading_failed":
                        net["failed"] = ev.error or "loadingFailed"
                        answer_done.set()

        pump_task = asyncio.create_task(pump())

        # ------------------------------------------------------------------
        # Type prompt + press Enter
        # ------------------------------------------------------------------
        typing_start_ns = time.time_ns()
        await human_type(browser, prompt)
        typing_end_ns = time.time_ns()
        await asyncio.sleep(random.uniform(0.3, 0.8))
        enter_armed.set()
        enter_ns = time.time_ns()
        await browser.press_key("Enter")

        # ------------------------------------------------------------------
        # Wait for visual completion (stop indicator disappears).
        # ------------------------------------------------------------------
        visual_first_byte_ms = None
        visual_done_ms = None
        visual_done_source = ""
        visual_deadline = time.monotonic() + ANSWER_TIMEOUT_SEC
        while time.monotonic() < visual_deadline:
            snap = await browser.evaluate("""(window.__crawler ? {
                first: window.__crawler.firstByteEpochMs,
                streamEnd: window.__crawler.streamingEndedAt,
                streamSig: window.__crawler.streamSignal,
            } : null)""")
            if snap:
                if snap.get("first") and visual_first_byte_ms is None:
                    visual_first_byte_ms = snap["first"]
                if snap.get("streamEnd"):
                    visual_done_ms = snap["streamEnd"]
                    sig = snap.get("streamSig") or {}
                    visual_done_source = sig.get("source", "stream-signal") if isinstance(sig, dict) else "stream-signal"
                    break
            await asyncio.sleep(0.05)

        # ------------------------------------------------------------------
        # Wait for network stream end (or data-idle as proxy).
        # ------------------------------------------------------------------
        data_wait_deadline = time.monotonic() + 5.0
        last_count = net["data_received_count"]
        last_change_at = time.monotonic()
        while time.monotonic() < data_wait_deadline:
            if answer_done.is_set():
                break
            cur = net["data_received_count"]
            if cur != last_count:
                last_count = cur
                last_change_at = time.monotonic()
            elif last_count > 0 and (time.monotonic() - last_change_at) >= 1.0:
                break
            await asyncio.sleep(0.1)
        if not answer_done.is_set() and not net["failed"]:
            if net["data_received_count"] > 0:
                net["failed"] = "loadingFinished not seen (using last dataReceived)"
            elif net["timing"] is None:
                # BiDi doesn't emit per-chunk events; only loadingFinished.
                # If timing is set but loadingFinished missing, we'll fall
                # back below. If no timing at all, that's a real miss.
                pass

        pump_task.cancel()
        try:
            await pump_task
        except asyncio.CancelledError:
            pass

        # ------------------------------------------------------------------
        # Build the result row
        # ------------------------------------------------------------------
        notes = []
        if net["request_id"] is None:
            notes.append("target API request not captured")
        else:
            notes.append(f"endpoint={net['endpoint_name']}")
            notes.append(f"url={net['request_url']}")
        if visual_done_source:
            notes.append(f"done-by={visual_done_source}")
        if visual_first_byte_ms is None:
            notes.append("visual first-byte not observed")
            diag = await browser.evaluate(
                "JSON.stringify((window.__crawler && window.__crawler.diagnostic) || null)"
            )
            if diag and diag != "null":
                notes.append(f"diag={diag}")
        if visual_done_ms is None:
            notes.append("visual answer-done not observed")
        if net["failed"]:
            notes.append(f"failed={net['failed']}")

        # DevTools-equivalent timings.
        timing: Optional[NetworkTiming] = net["timing"]
        stalled_ms = request_sent_ms = waiting_ms = content_download_ms = total_ms = None
        if timing:
            stalled_ms = _compute_stalled_ms(timing)
            request_sent_ms = timing.send_end_ms - timing.send_start_ms
            waiting_ms = timing.receive_headers_end_ms - timing.send_end_ms
            finish_ms = net["loading_finished_ms"]
            if finish_ms is None and net["last_data_received_ms"] is not None:
                finish_ms = net["last_data_received_ms"]
                notes.append("finish-source=last-dataReceived")
            if finish_ms is not None:
                content_download_ms = finish_ms - (timing.request_time_ms + timing.receive_headers_end_ms)
                total_ms = finish_ms - (timing.request_time_ms + timing.send_start_ms)

        def fmt_ms(v): return f"{v:.3f}" if isinstance(v, (int, float)) else ""
        def iso_from_epoch_ms(ms): return iso_now(int(ms * 1_000_000)) if ms else ""

        net_done_ns = net["loading_finished_ts_ns"] or net["last_data_received_ts_ns"]
        return {
            "run_id": str(uuid.uuid4()),
            "timestamp_iso": iso_now(typing_start_ns),
            "browser": browser_name,
            "target": target,
            "prompt": prompt.replace("\n", "\\n"),
            "typing_start": iso_now(typing_start_ns),
            "typing_end": iso_now(typing_end_ns),
            "enter_pressed": iso_now(enter_ns),
            "first_byte": iso_from_epoch_ms(visual_first_byte_ms),
            "answer_done": iso_from_epoch_ms(visual_done_ms),
            "net_response_received": iso_now(net["response_received_ts_ns"]) if net["response_received_ts_ns"] else "",
            "net_loading_finished": iso_now(net_done_ns) if net_done_ns else "",
            "stalled_ms": fmt_ms(stalled_ms),
            "request_sent_ms": fmt_ms(request_sent_ms),
            "waiting_ms": fmt_ms(waiting_ms),
            "content_download_ms": fmt_ms(content_download_ms),
            "total_ms": fmt_ms(total_ms),
            "http_status": str(net["http_status"] or ""),
            "request_id": net["request_id"] or "",
            "notes": "; ".join(notes),
        }
    finally:
        await browser.close()


def append_csv(row: dict) -> None:
    new_file = not CSV_PATH.exists()
    with CSV_PATH.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def main() -> None:
    parser = argparse.ArgumentParser(description="Human-like ChatGPT crawler (Chrome CDP / Firefox BiDi)")
    parser.add_argument("--browser", choices=["chrome", "edge", "firefox"], default="chrome",
                        help="使用するブラウザ (default: chrome)")
    parser.add_argument(
        "--target",
        choices=["chatgpt", "copilot-personal", "copilot-corporate"],
        default="chatgpt",
        help="対象サービス (default: chatgpt)",
    )
    parser.add_argument("--setup", action="store_true",
                        help="ブラウザを起動して OKTA 手動ログイン用のプロファイルを準備する")
    parser.add_argument("prompt", nargs="?", help="ChatGPT に投入するプロンプト文字列")
    args = parser.parse_args()

    if args.setup:
        asyncio.run(setup_mode(args.browser, args.target))
        return

    if not args.prompt:
        parser.error("prompt が必要です (または --setup を指定)。")

    row = asyncio.run(run_prompt(args.prompt, args.browser, args.target))
    append_csv(row)
    def _fmt_field(value: str) -> str:
        return f"{value}ms" if value else "N/A"

    print(
        f"[done] browser={row['browser']}  target={row['target']}  run_id={row['run_id']}  "
        f"stalled={_fmt_field(row['stalled_ms'])}  "
        f"request_sent={_fmt_field(row['request_sent_ms'])}  "
        f"waiting={_fmt_field(row['waiting_ms'])}  "
        f"content_download={_fmt_field(row['content_download_ms'])}  "
        f"total={_fmt_field(row['total_ms'])}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
