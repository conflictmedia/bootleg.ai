"""Arena "Was this task successful?" review-popup dismissal.

Split out from aai.py. Handles the popup that Arena shows when a
previously-finished chat is reopened (--reuse-chat / --resume). Without
dismissing it the chatbox is unreachable.
"""

import sys
import time

from playwright.sync_api import Locator, TimeoutError as PlaywrightTimeout
from typing import Optional


class ReviewPopupMixin:
    """Provides `_dismiss_review_popup_if_present` for ArenaAgent.

    When a previously-finished Arena chat is reopened (the --reuse-chat
    scenario), Arena replaces the chatbox with a "Was this task successful?"
    review panel. The panel offers three buttons (Yes / No / Keep working)
    plus an Esc-to-close icon button (aria-label="Close review panel").

    The chat input is unreachable while this panel is visible, so we must
    dismiss it before trying to send a prompt. "Keep working" is the
    semantically correct choice for reuse-chat: it dismisses the review and
    restores the chatbox for a follow-up message, without committing to a
    Yes/No rating. We fall back to the close button (X) and finally to Esc
    so that small markup drift in either of the buttons doesn't strand the
    script.
    """

    _REVIEW_POPUP_HEADING_SELECTORS = [
        'span:has-text("Was this task successful?"):visible',
        'div:has-text("Was this task successful?"):visible',
    ]
    _REVIEW_POPUP_CLOSE_SELECTORS = [
        'button[aria-label="Close review panel"]:visible',
        'button[aria-label*="review panel" i]:visible',
    ]
    _REVIEW_POPUP_KEEP_WORKING_SELECTORS = [
        # Scoped to the panel via :has-text on the heading ancestor.
        ':has-text("Was this task successful?") button:has-text("Keep working"):visible',
        'button:has-text("Keep working"):visible',
    ]

    def _review_popup_is_visible(self, timeout_ms: int = 200) -> bool:
        """Quickly probe whether the review popup is currently shown."""
        for selector in self._REVIEW_POPUP_HEADING_SELECTORS:
            loc = self.page.locator(selector).first
            try:
                loc.wait_for(state="visible", timeout=timeout_ms)
                return True
            except PlaywrightTimeout:
                continue
        return False

    def _dismiss_review_popup_if_present(
        self, timeout_ms: int = 2_000
    ) -> bool:
        """Dismiss Arena's "Was this task successful?" review popup.

        Returns True if the popup was found and dismissed, False if it was
        not present. Tries, in order:
          1. Click "Keep working" (preferred -- preserves the chatbox context)
          2. Click the close (X) button with aria-label="Close review panel"
          3. Press Esc as a last resort

        After a successful dismissal, waits briefly for Arena to restore the
        chatbox.
        """
        if not self._review_popup_is_visible(timeout_ms=min(timeout_ms, 500)):
            return False

        print(
            "[info] Review popup detected ('Was this task successful?'). "
            "Dismissing to restore chatbox for --reuse-chat...",
            file=sys.stderr,
        )

        # 1. Try "Keep working".
        for selector in self._REVIEW_POPUP_KEEP_WORKING_SELECTORS:
            loc = self._wait_visible(selector, timeout_ms=1_000)
            if loc is not None:
                try:
                    loc.click()
                    print(
                        "[info] Clicked 'Keep working' on review popup.",
                        file=sys.stderr,
                    )
                    time.sleep(0.5)
                    if not self._review_popup_is_visible(timeout_ms=300):
                        return True
                    # Popup still visible after click; try the next selector.
                except Exception as exc:
                    print(
                        f"[debug] 'Keep working' click failed ({exc}); "
                        f"trying next dismissal path.",
                        file=sys.stderr,
                    )

        # 2. Try the close (X) button.
        for selector in self._REVIEW_POPUP_CLOSE_SELECTORS:
            loc = self._wait_visible(selector, timeout_ms=1_000)
            if loc is not None:
                try:
                    loc.click()
                    print(
                        "[info] Clicked close button on review popup.",
                        file=sys.stderr,
                    )
                    time.sleep(0.5)
                    if not self._review_popup_is_visible(timeout_ms=300):
                        return True
                except Exception as exc:
                    print(
                        f"[debug] close-button click failed ({exc}).",
                        file=sys.stderr,
                    )

        # 3. Last resort: Esc.
        try:
            self.page.keyboard.press("Escape")
            print("[info] Pressed Esc to dismiss review popup.", file=sys.stderr)
            time.sleep(0.5)
            if not self._review_popup_is_visible(timeout_ms=300):
                return True
        except Exception as exc:
            print(f"[debug] Esc press failed ({exc}).", file=sys.stderr)

        print(
            "[warn] Could not dismiss the review popup. The chatbox may "
            "remain unavailable; send_prompt will likely fail to find the "
            "input box.",
            file=sys.stderr,
        )
        return False

