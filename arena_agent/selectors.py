"""Chat-input and send-button selector discovery.

Split out of aai.py. Selectors can be overridden via the
ARENA_INPUT_SELECTOR / ARENA_SEND_SELECTOR env vars; otherwise we build a
comma-joined candidate list.
"""

import os
import sys
import time

from playwright.sync_api import Locator, TimeoutError as PlaywrightTimeout
from typing import Optional


class SelectorsMixin:
    """Provides chat-input / send-button selector helpers for ArenaAgent."""

    def _get_input_selector(self) -> str:
        override = os.environ.get("ARENA_INPUT_SELECTOR")
        if override:
            return override
        candidates = [
            'textarea:not([name="g-recaptcha-response"]):visible',
            'textarea[placeholder*="Ask" i]:visible',
            'textarea[placeholder*="Message" i]:visible',
            'textarea[placeholder*="Prompt" i]:visible',
            '[role="textbox"]:not([name="g-recaptcha-response"]):visible',
            '[contenteditable="true"]:visible',
            'input[placeholder*="Ask" i]:visible',
            'input[placeholder*="Message" i]:visible',
            'input[placeholder*="Prompt" i]:visible',
        ]
        return ", ".join(candidates)

    def _get_send_selector(self) -> str:
        override = os.environ.get("ARENA_SEND_SELECTOR")
        if override:
            return override
        candidates = [
            'button[type="submit"]:visible',
            'button[aria-label*="Send" i]:visible',
            'button:has-text("Send"):visible',
            'button[aria-label*="submit" i]:visible',
            'button[aria-label*="arrow" i]:visible',
        ]
        return ", ".join(candidates)

    def _find_chat_input(self) -> Optional[Locator]:
        selector = self._get_input_selector()
        loc = self.page.locator(selector).first
        try:
            loc.wait_for(state="visible", timeout=15_000)
            return loc
        except PlaywrightTimeout:
            return None

    def _wait_for_send_button_state(
        self,
        want_disabled: bool,
        timeout_s: float = 10.0,
        interval_s: float = 0.1,
    ) -> bool:
        """Poll the send button until its disabled state matches `want_disabled`.

        Returns True if the state was reached within `timeout_s`, False on timeout.
        Uses the in-page `__arena_send_button_state()` JS helper so each poll is
        a single round-trip.

        `want_disabled=True`  -> wait until button is disabled (prompt was accepted)
        `want_disabled=False` -> wait until button is enabled  (input ready / gen done)
        """
        start = time.time()
        last_state = None
        while time.time() - start < timeout_s:
            try:
                state = self.page.evaluate("() => window.__arena_send_button_state()") or {}
            except Exception as exc:
                print(f"[debug] send_button_state poll failed: {exc}", file=sys.stderr)
                state = {}
            last_state = state
            if not state.get('found'):
                # Button not found yet; keep polling briefly. If it never
                # appears, the caller's existing Enter-key path will still
                # work as a fallback.
                time.sleep(interval_s)
                continue
            is_disabled = bool(state.get('disabled'))
            if is_disabled == want_disabled:
                return True
            time.sleep(interval_s)
        if last_state:
            print(
                f"[debug] _wait_for_send_button_state(want_disabled={want_disabled}) "
                f"timed out after {timeout_s}s; last state: {last_state}",
                file=sys.stderr,
            )
        return False

