"""Agent / Direct mode switching and model selection.

Split out from aai.py. Arena has both "Agent Mode" and "Direct Mode";
this mixin handles the UI dance to switch between them and (in Direct
Mode) pick a specific model from the dropdown.
"""

import os
import sys
import time

from playwright.sync_api import Locator, TimeoutError as PlaywrightTimeout
from typing import Optional

from .constants import AGENT_MODE_SELECTORS


class ModeSwitchingMixin:
    """Provides agent/direct mode toggle and model-picker helpers."""

    def _switch_to_agent_mode(self):
        """Attempt to switch from Battle Mode to Agent Mode."""
        print("[info] Looking for Agent Mode switch...", file=sys.stderr)

        override = os.environ.get("ARENA_AGENT_MODE_SELECTOR")
        selectors = [override] if override else AGENT_MODE_SELECTORS

        for selector in selectors:
            # 500ms per selector. ~12 selectors = 6s worst case (was 24s).
            loc = self._wait_visible(selector, timeout_ms=500)
            if loc is not None:
                text = "Agent"
                try:
                    text = loc.inner_text().strip() or loc.get_attribute("aria-label") or "Agent"
                except Exception:
                    pass
                print(
                    f"[info] Found mode selector: {selector} (text: {text[:40]})",
                    file=sys.stderr,
                )
                loc.click()
                time.sleep(0.2)
                self._select_agent_mode_from_dropdown()
                # Poll _in_agent_mode briefly instead of a fixed 1.5s sleep.
                # 5 tries * 0.3s = 1.5s max; we exit early as soon as the
                # indicator is detected.
                for _ in range(5):
                    if self._in_agent_mode():
                        print("[info] Confirmed Agent Mode is active.", file=sys.stderr)
                        return
                    time.sleep(0.3)
                print(
                    "[info] Clicked Agent Mode switch; could not confirm but continuing.",
                    file=sys.stderr,
                )
                return

        print("[warn] Could not find an Agent Mode switch with known selectors.", file=sys.stderr)
        print("[warn] Set ARENA_AGENT_MODE_SELECTOR to the exact CSS selector if needed.", file=sys.stderr)
        print("[warn] If the page is already in Agent Mode or Battle Mode is fine, continue.", file=sys.stderr)

    def _select_agent_mode_from_dropdown(self):
        """Click the 'Agent Mode' option inside an open Radix dropdown."""
        override = os.environ.get("ARENA_AGENT_OPTION_SELECTOR")
        dropdown_selectors = [override] if override else [
            '[role="menuitem"]:has-text("Agent Mode"):visible',
            '[role="option"]:has-text("Agent Mode"):visible',
            '[role="listbox"] >> :has-text("Agent Mode"):visible',
            '[data-radix-popper-content-wrapper] >> :has-text("Agent Mode"):visible',
            '[data-radix-menu-content] >> :has-text("Agent Mode"):visible',
            ':has-text("Agent Mode"):visible',
        ]
        for selector in dropdown_selectors:
            option = self._wait_visible(selector, timeout_ms=500)
            if option is not None:
                print("[info] Selecting 'Agent Mode' from dropdown.", file=sys.stderr)
                option.click()
                return

    def _in_agent_mode(self) -> bool:
        """Check whether the page is *currently* in Agent Mode.

        Requires an element whose visible label exactly matches "Agent Mode"
        (case-insensitive). Excludes action verbs like "switch" / "try" /
        "enable" that indicate a button which *navigates into* Agent Mode
        rather than a status indicator showing we are already there.
        """
        try:
            matches = self.page.evaluate(
                r"""
                () => {
                    const nodes = document.querySelectorAll(
                        'button, [role="combobox"], [role="tab"], [role="menuitem"], ' +
                        '[role="option"], [aria-label], [class*="mode" i]'
                    );
                    const found = [];
                    for (const el of nodes) {
                        const text = (el.innerText || el.textContent || '').trim();
                        const aria = (el.getAttribute('aria-label') || '').trim();
                        const label = text || aria;
                        if (!label) continue;
                        // Exact match on the visible label.
                        if (!/^agent\s*mode$/i.test(label)) continue;
                        // Exclude buttons whose label is an *action* into Agent
                        // Mode rather than a status indicator.
                        if (/switch|try|enable|turn on|activate|start|go to/i.test(label)) continue;
                        found.push(label);
                    }
                    return found;
                }
                """
            )
            if matches:
                return True
        except Exception as exc:
            print(f"[debug] _in_agent_mode JS check failed: {exc}", file=sys.stderr)

        # Weak fallback: data-testid / class hints. Short timeouts because this
        # method is called in a tight poll loop after the mode-switch click.
        weak_indicators = [
            '[data-testid*="agent-mode" i]',
            '[data-testid*="mode-agent" i]',
            '[class*="agent-mode" i]',
            '[class*="mode-agent" i]',
        ]
        for selector in weak_indicators:
            if self._wait_visible(selector, timeout_ms=150) is not None:
                return True
        return False

    def _switch_to_direct_mode(self):
        """Attempt to switch from Battle Mode/Agent Mode to Direct Mode."""
        print("[info] Looking for Direct Mode switch...", file=sys.stderr)

        override = os.environ.get("ARENA_DIRECT_MODE_SELECTOR")
        selectors = [override] if override else [
            'button[role="combobox"]:visible',
            'button:has-text("Agent Mode"):visible',
            'button:has-text("Agent"):visible',
            'button:has-text("Direct Mode"):visible',
            'button:has-text("Direct"):visible',
            'button:has-text("Battle Mode"):visible',
            'button:has-text("Arena Mode"):visible',
            'button:has-text("Arena"):visible',
            'a:has-text("Agent Mode"):visible',
            'a:has-text("Agent"):visible',
            'a:has-text("Direct Mode"):visible',
            'a:has-text("Direct"):visible',
            '[role="tab"]:has-text("Direct"):visible',
            '[role="tab"]:has-text("Direct Mode"):visible',
            '[role="tab"]:has-text("Agent"):visible',
            '[role="tab"]:has-text("Agent Mode"):visible',
            '[aria-label*="Agent Mode" i]:visible',
            '[aria-label*="Agent" i]:visible',
            '[aria-label*="Direct Mode" i]:visible',
            '[aria-label*="Direct" i]:visible',
            '[data-testid*="agent" i]:visible',
            '[data-testid*="direct" i]:visible',
            '[class*="agent" i]:visible',
            '[class*="direct" i]:visible',
        ]

        for selector in selectors:
            loc = self._wait_visible(selector, timeout_ms=500)
            if loc is not None:
                text = "Direct"
                try:
                    text = loc.inner_text().strip() or loc.get_attribute("aria-label") or "Direct"
                except Exception:
                    pass
                print(
                    f"[info] Found mode selector: {selector} (text: {text[:40]})",
                    file=sys.stderr,
                )
                loc.click()
                time.sleep(0.2)
                self._select_direct_mode_from_dropdown()
                # Poll _in_direct_mode briefly instead of a fixed 1.5s sleep.
                for _ in range(5):
                    if self._in_direct_mode():
                        print("[info] Confirmed Direct Mode is active.", file=sys.stderr)
                        return
                    time.sleep(0.3)
                print(
                    "[info] Clicked Direct Mode switch; could not confirm but continuing.",
                    file=sys.stderr,
                )
                return

        print("[warn] Could not find a Direct Mode switch with known selectors.", file=sys.stderr)
        print("[warn] Set ARENA_DIRECT_MODE_SELECTOR to the exact CSS selector if needed.", file=sys.stderr)
        print("[warn] If the page is already in Direct Mode, continue.", file=sys.stderr)

    def _select_direct_mode_from_dropdown(self):
        """Click the 'Direct' or 'Direct Mode' option inside an open Radix dropdown."""
        override = os.environ.get("ARENA_DIRECT_OPTION_SELECTOR")
        dropdown_selectors = [override] if override else [
            '[role="menuitem"]:has-text("Direct Mode"):visible',
            '[role="menuitem"]:has-text("Direct"):visible',
            '[role="option"]:has-text("Direct Mode"):visible',
            '[role="option"]:has-text("Direct"):visible',
            '[role="listbox"] >> :has-text("Direct Mode"):visible',
            '[role="listbox"] >> :has-text("Direct"):visible',
            '[data-radix-popper-content-wrapper] >> :has-text("Direct Mode"):visible',
            '[data-radix-popper-content-wrapper] >> :has-text("Direct"):visible',
            '[data-radix-menu-content] >> :has-text("Direct Mode"):visible',
            '[data-radix-menu-content] >> :has-text("Direct"):visible',
            ':has-text("Direct Mode"):visible',
            ':has-text("Direct"):visible',
        ]
        for selector in dropdown_selectors:
            option = self._wait_visible(selector, timeout_ms=500)
            if option is not None:
                print("[info] Selecting 'Direct Mode' from dropdown.", file=sys.stderr)
                option.click()
                return

    def _in_direct_mode(self) -> bool:
        """Check whether the page is *currently* in Direct Mode.

        Requires an element whose visible label exactly matches "Direct Mode" or "Direct"
        (case-insensitive). Excludes action verbs like "switch" / "try" / "enable" etc.
        """
        try:
            matches = self.page.evaluate(
                r"""
                () => {
                    const nodes = document.querySelectorAll(
                        'button, [role="combobox"], [role="tab"], [role="menuitem"], ' +
                        '[role="option"], [aria-label], [class*="mode" i]'
                    );
                    const found = [];
                    for (const el of nodes) {
                        const text = (el.innerText || el.textContent || '').trim();
                        const aria = (el.getAttribute('aria-label') || '').trim();
                        const label = text || aria;
                        if (!label) continue;
                        // Exact match on the visible label.
                        if (!/^direct\s*mode$/i.test(label) && !/^direct$/i.test(label)) continue;
                        // Exclude buttons whose label is an *action* into Direct
                        // Mode rather than a status indicator.
                        if (/switch|try|enable|turn on|activate|start|go to/i.test(label)) continue;
                        found.push(label);
                    }
                    return found;
                }
                """
            )
            if matches:
                return True
        except Exception as exc:
            print(f"[debug] _in_direct_mode JS check failed: {exc}", file=sys.stderr)

        # Weak fallback: data-testid / class hints. Short timeouts because this
        # method is called in a tight poll loop after the mode-switch click.
        weak_indicators = [
            '[data-testid*="direct-mode" i]',
            '[data-testid*="mode-direct" i]',
            '[class*="direct-mode" i]',
            '[class*="mode-direct" i]',
        ]
        for selector in weak_indicators:
            if self._wait_visible(selector, timeout_ms=150) is not None:
                return True
        return False

    def _select_model(self, model_name: str):
        """Click the model selection dropdown and select the requested model."""
        print(f"[info] Attempting to select model: {model_name}", file=sys.stderr)

        # Try to find the model selection dropdown button using multiple selectors
        dropdown_button_selectors = [
            'div.min-w-0.grow button[aria-haspopup]:visible',
            'div.min-w-0.grow button:visible',
            'button[aria-haspopup="dialog"]:has(span.truncate):visible',
            'button:has(span.truncate.text-sm):visible',
            'button.whitespace-nowrap:has(span.truncate):visible',
            'button[aria-haspopup="dialog"]:visible',
        ]

        button_loc = None
        for selector in dropdown_button_selectors:
            button_loc = self._wait_visible(selector, timeout_ms=1000)
            if button_loc is not None:
                print(f"[info] Found model dropdown button: {selector}", file=sys.stderr)
                break

        if button_loc is None:
            print("[warn] Could not find the model dropdown button.", file=sys.stderr)
            return False

        try:
            button_loc.click()
            time.sleep(0.5)  # Wait for the dropdown dialog to animate open
        except Exception as exc:
            print(f"[warn] Failed to click model dropdown button: {exc}", file=sys.stderr)
            return False

        # Find and click the model option
        model_option_selectors = [
            f'[data-value="{model_name}"]:visible',
            f'[cmdk-item][data-value*="{model_name}" i]:visible',
            f'[role="option"]:has-text("{model_name}"):visible',
            f'[role="menuitem"]:has-text("{model_name}"):visible',
            f'[cmdk-item]:has-text("{model_name}"):visible',
            f':has-text("{model_name}"):visible',
        ]

        option_loc = None
        for selector in model_option_selectors:
            option_loc = self._wait_visible(selector, timeout_ms=1000)
            if option_loc is not None:
                print(f"[info] Found model option: {selector}", file=sys.stderr)
                break

        if option_loc is None:
            print(f"[warn] Could not find option for model '{model_name}' in the open dropdown.", file=sys.stderr)
            # Try to close dropdown by clicking elsewhere or hitting Escape
            try:
                self.page.keyboard.press("Escape")
            except Exception:
                pass
            return False

        try:
            option_loc.click()
            time.sleep(0.5)  # Wait for selection to apply
            print(f"[info] Successfully selected model '{model_name}'.", file=sys.stderr)
            return True
        except Exception as exc:
            print(f"[warn] Failed to click model option '{model_name}': {exc}", file=sys.stderr)
            return False

    # ------------------------------------------------------------------
    # In-page JS helpers (one round-trip per poll instead of many)
    # ------------------------------------------------------------------

