"""ArenaAgent: automates arena.ai / canaryarena.ai in Agent Mode.

This module contains the core ArenaAgent class -- the orchestrator that
owns the Playwright browser lifecycle, sends prompts, and streams
responses. Heavy lifting (mode switching, code extraction, filename
resolution, state persistence, JS injection) is delegated to the mixin
classes in the sibling modules.

Backward-compat: `from arena_agent import ArenaAgent` works the same as
the old `from aai import ArenaAgent` did.
"""

import argparse
import os
import re
import sys
import time

from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.sync_api import (
    Locator,
    TimeoutError as PlaywrightTimeout,
    sync_playwright,
)

from .constants import (
    DEFAULT_CHROME_PROFILE,
    DEFAULT_STATE_FILE,
    SITES,
    AGENT_MODE_SELECTORS,
)
from .review_popup import ReviewPopupMixin
from .mode_switching import ModeSwitchingMixin
from .js_helpers import JSHelpersMixin
from .selectors import SelectorsMixin
from .filenames import FilenameMixin
from .code_extraction import CodeExtractionMixin
from .state import StateMixin
from .workspace import WorkspaceMixin

# Force line-buffered stdout so --stream is truly live, even when piped.
try:
    sys.stdout.reconfigure(line_buffering=True)
except AttributeError:
    pass  # Python < 3.7 without reconfigure support


class ArenaAgent(
    JSHelpersMixin,
    SelectorsMixin,
    ReviewPopupMixin,
    ModeSwitchingMixin,
    FilenameMixin,
    CodeExtractionMixin,
    StateMixin,
    WorkspaceMixin,
):
    """Drive Arena.ai / CanaryArena.ai in Agent Mode via Playwright.

    Composed of eight single-concern mixins; this class itself only owns:
      - configuration (__init__)
      - browser/page lifecycle (start / close)
      - chat-entry coordination (_wait_visible, _enter_chat_if_needed)
      - the main send_prompt loop
      - debug screenshot helper
    """

    def __init__(
        self,
        site_key: str,
        profile_dir: Path,
        headless: bool = True,
        slow_mo: int = 0,
        timeout: int = 30_000,
        agent_mode: bool = False,
        direct_mode: bool = False,
        model_name: Optional[str] = None,
        chat_path: Optional[str] = None,
        reuse_chat: bool = False,
        state_file: Optional[Path] = None,
        resume: bool = False,
    ):
        self.site = SITES[site_key]
        self.site_key = site_key
        self.profile_dir = profile_dir.resolve()
        self.headless = headless
        self.slow_mo = slow_mo
        self.timeout = timeout
        self.agent_mode = agent_mode
        self.direct_mode = direct_mode
        self.model_name = model_name
        # Per-instance chat path. Defaults to the registry value. This replaces
        # the old behaviour of mutating the module-level SITES dict from main(),
        # which leaked state across runs if ArenaAgent was ever imported as a
        # library.
        self.chat_path = chat_path or self.site["chat_path"]
        # When True, we are continuing a previously-finished Arena chat. Arena
        # shows a "Was this task successful?" review popup that REPLACES the
        # chatbox in this case; we dismiss it (clicking "Keep working") before
        # attempting to send a new prompt. See _dismiss_review_popup_if_present.
        # NOTE: this flag is ALSO set automatically when `resume=True`, because
        # resuming a saved chat has the same review-popup semantics.
        self.reuse_chat = reuse_chat or resume
        # Path to the JSON state file that records the last-used chat URL.
        # When `resume=True`, the saved chat_path from this file overrides
        # `self.chat_path` in start(). After a successful run, the current
        # page URL is written back here so the next --resume can find it.
        self.state_file = (Path(state_file).expanduser().resolve()
                           if state_file else DEFAULT_STATE_FILE)
        self.resume = resume
        self.browser = None
        self.context = None
        self.page = None
        self._streamed_text = ""

    # ------------------------------------------------------------------
    # Browser lifecycle
    # ------------------------------------------------------------------


    def start(self):
        if not self.profile_dir.exists():
            raise FileNotFoundError(
                f"Chrome profile not found: {self.profile_dir}\n"
                "Make sure Chromium is installed and you have logged in once."
            )

        print(f"[info] Using Chromium profile: {self.profile_dir}", file=sys.stderr)
        print(f"[info] Target site: {self.site['url']} ({self.site['name']})", file=sys.stderr)

        # If --resume was passed, load the saved chat path from the state
        # file and override self.chat_path so we navigate straight back to
        # the previous conversation. Without this, navigating to /chat
        # would either start a brand-new chat or land on whatever Arena
        # considers "current", losing the chat we wanted to continue.
        if self.resume:
            saved = self._load_state()
            if saved is None:
                raise FileNotFoundError(
                    f"Cannot --resume: no saved chat state found at "
                    f"{self.state_file}. Run once without --resume to "
                    f"create it."
                )
            if saved.get("site") != self.site_key:
                raise ValueError(
                    f"Cannot --resume: state file was written for site "
                    f"{saved.get('site')!r} but you selected "
                    f"--site {self.site_key!r}. Re-run with the matching "
                    f"--site, or delete {self.state_file} to start fresh."
                )
            saved_path = saved.get("chat_path")
            if not saved_path:
                raise ValueError(
                    f"Cannot --resume: state file at {self.state_file} "
                    f"has no 'chat_path' field. Delete it and run without "
                    f"--resume to recreate."
                )
            self.chat_path = saved_path
            print(
                f"[info] --resume: loaded chat_path={self.chat_path} "
                f"(saved {saved.get('timestamp', 'unknown')}).",
                file=sys.stderr,
            )

        playwright = sync_playwright().start()

        self.context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            slow_mo=self.slow_mo,
            viewport={"width": 1920, "height": 1080},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(self.timeout)

        # Navigate. Use domcontentloaded (NOT networkidle) -- modern SPAs keep
        # analytics / WebSocket / SSE connections open and networkidle can hang
        # for 30s+ or fire spuriously. The subsequent element waits catch
        # anything we actually need.
        # Decide where to navigate.
        # When --agent-mode is requested and we are NOT resuming a specific
        # chat, navigate directly to the site's /agent path. This loads Arena
        # straight into Agent Mode (arena.ai/agent / canaryarena.ai/agent)
        # instead of landing on /chat and driving the mode-switch dropdown.
        # The _in_agent_mode() check below still falls back to the dropdown if
        # /agent doesn't actually enter Agent Mode, so this is safe.
        if self.resume:
            # --resume already set self.chat_path to the saved conversation URL.
            nav_path = self.chat_path
        elif self.agent_mode:
            nav_path = self.site.get("agent_path") or "/agent"
            print(
                "[info] --agent-mode: navigating directly to the agent "
                "URL instead of using the mode-switch dropdown.",
                file=sys.stderr,
            )
        else:
            nav_path = self.chat_path

        full_url = f"{self.site['url']}{nav_path}"
        print(f"[info] Navigating to {full_url}", file=sys.stderr)
        self.page.goto(full_url, wait_until="domcontentloaded")

        # Brief hydration pause. Shorter is better; subsequent _wait_visible
        # calls will synchronise on real elements.
        time.sleep(0.5)

        self._enter_chat_if_needed()

        # If --reuse-chat is set, the previously-finished chat may already be
        # showing the "Was this task successful?" review popup on page load.
        # Dismiss it now so subsequent agent/direct-mode switches and the
        # send_prompt flow don't get blocked by it. (send_prompt will also
        # re-check, so this is a defensive early dismissal.)
        if self.reuse_chat:
            self._dismiss_review_popup_if_present(timeout_ms=2_000)

        # Switch to Agent Mode only if requested AND not already in Agent Mode.
        # Checking first avoids burning ~6s on selector probes when Arena
        # already defaults to Agent Mode for this user (the common case).
        if self.agent_mode:
            if self._in_agent_mode():
                print("[info] Already in Agent Mode.", file=sys.stderr)
            else:
                self._switch_to_agent_mode()

        # Switch to Direct Mode only if requested AND not already in Direct Mode.
        if self.direct_mode:
            if self._in_direct_mode():
                print("[info] Already in Direct Mode.", file=sys.stderr)
            else:
                self._switch_to_direct_mode()

            if self.model_name:
                self._select_model(self.model_name)

        # Inject JS helpers for fast single-round-trip polling.
        self._inject_js_helpers()

        return self


    def _wait_visible(self, selector: str, timeout_ms: int = 2_000) -> Optional[Locator]:
        """Wait for `selector` to become visible and return its Locator.

        Returns None if the element does not appear within `timeout_ms`.

        Use this instead of `locator.is_visible(timeout=...)`: Playwright's
        `is_visible()` does NOT accept a timeout argument and silently returns
        the *current* visibility state without waiting, which makes every
        `try/except PlaywrightTimeout` block around it dead code.
        """
        loc = self.page.locator(selector).first
        try:
            loc.wait_for(state="visible", timeout=timeout_ms)
            return loc
        except PlaywrightTimeout:
            return None


    def _enter_chat_if_needed(self):
        """Click common entry points if the page is not already in a chat.

        Guards against accidentally clicking "New Chat" when we are already
        inside an active conversation (the input box is visible).  Without
        this check the sidebar's always-visible "New Chat" button would
        start a fresh conversation every run, discarding the existing chat
        context / history.

        CRITICAL: when `reuse_chat=True` (set by --reuse-chat OR --resume),
        we MUST NOT click "New Chat" under any circumstances -- doing so
        would destroy the very chat we are trying to continue. If the input
        box is not immediately visible we instead wait longer and let the
        caller's review-popup-dismissal logic recover the chatbox.
        """
        # Fast pre-check: if a chat input is already visible we are already
        # inside a conversation and must NOT click "New Chat".
        input_selector = self._get_input_selector()
        try:
            self.page.locator(input_selector).first.wait_for(
                state="visible", timeout=1_000,
            )
            print("[info] Already in a chat (input box visible).", file=sys.stderr)
            return
        except PlaywrightTimeout:
            pass  # No input yet — fall through.

        # When resuming/continuing a chat, DO NOT click "New Chat". The
        # review popup may be covering the chatbox; send_prompt and
        # _dismiss_review_popup_if_present handle that. Clicking "New Chat"
        # here would discard the previous conversation -- the exact
        # "chat state not being saved" bug.
        if self.reuse_chat:
            print(
                "[info] reuse_chat=True and input not yet visible; NOT "
                "clicking 'New Chat' (would destroy the chat we are "
                "resuming). Waiting for review-popup dismissal in "
                "send_prompt.",
                file=sys.stderr,
            )
            # Give Arena a few extra seconds to hydrate / restore the
            # chatbox before send_prompt's own retry logic kicks in.
            try:
                self.page.locator(input_selector).first.wait_for(
                    state="visible", timeout=5_000,
                )
                print("[info] Chatbox appeared after wait.", file=sys.stderr)
            except PlaywrightTimeout:
                print(
                    "[warn] Chatbox still not visible after 5s. "
                    "send_prompt will attempt review-popup dismissal "
                    "and retry.",
                    file=sys.stderr,
                )
            return

        entry_selectors = [
            'button:has-text("New Chat"):visible',
            'button:has-text("Start"):visible',
            'a:has-text("New Chat"):visible',
            '[aria-label*="New Chat"]:visible',
        ]
        for selector in entry_selectors:
            # 500ms per selector -- 4 selectors = 2s worst case. We do NOT want
            # to wait 2s on each one (the previous behaviour cost 8s when the
            # user was already on /chat, which is the common case).
            loc = self._wait_visible(selector, timeout_ms=500)
            if loc is not None:
                print(f"[info] Clicking entry point: {selector}", file=sys.stderr)
                loc.click()
                time.sleep(0.5)
                return

    # ------------------------------------------------------------------
    # Review popup dismissal (for --reuse-chat)
    # ------------------------------------------------------------------

    # When a previously-finished Arena chat is reopened (the --reuse-chat
    # scenario), Arena replaces the chatbox with a "Was this task successful?"
    # review panel. The panel offers three buttons (Yes / No / Keep working)
    # plus an Esc-to-close icon button (aria-label="Close review panel").
    #
    # The chat input is unreachable while this panel is visible, so we must
    # dismiss it before trying to send a prompt. "Keep working" is the
    # semantically correct choice for reuse-chat: it dismisses the review and
    # restores the chatbox for a follow-up message, without committing to a
    # Yes/No rating. We fall back to the close button (X) and finally to Esc
    # so that small markup drift in either of the buttons doesn't strand the
    # script.

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


    def send_prompt(
        self,
        prompt: str,
        max_wait_seconds: int = 0,
        stream: bool = False,
        stable_seconds: int = 5,
        wait_seconds: int = 0,
        code_only: bool = False,
        debug_dom: bool = False,
        debug_dom_path: Optional[str] = None,
        write_files: Optional[str] = None,
        dry_run: bool = False,
        activity_timeout_seconds: int = 300,
        augment_prompt: bool = True,
        auto_filename: bool = True,
        system_prompts: Optional[List[str]] = None,
        incremental_write: bool = False,
        conflict_resolution: str = "overwrite",
        workspace_wait: bool = True,
    ) -> Optional[str]:
        """Send `prompt` and return the assistant's response text.

        If `code_only` is True, returns a formatted string of just the code
        blocks found in the response (artifact cards + inline <pre> blocks).

        If `debug_dom` is True (or always when code_only finds 0 blocks),
        dumps the last assistant bubble's outerHTML to `debug_dom_path` for
        inspection.

        If `write_files` is set to a directory path, writes each artifact code
        block whose filename matches `*.*` to that directory. Implicitly
        enables `code_only` semantics for code-block extraction but does NOT
        change stdout (unless `code_only` is also set).

        `max_wait_seconds` (default 0 = infinite) is the ABSOLUTE timeout.
        We recommend leaving it at 0 and relying on `activity_timeout_seconds`
        instead, since agent-mode tasks can legitimately run for 10+ minutes.

        `activity_timeout_seconds` (default 300 = 5 min) is the NO-ACTIVITY
        timeout. If the page hasn't changed (no text delta, no new code blocks,
        no generation-state transition) for this many seconds, we give up.
        This catches genuinely-hung Arena without killing long-running tasks
        that are still making progress.

        `augment_prompt` (default True): appends an instruction to the
        prompt asking Arena to wrap all code in artifact file viewers with
        proper filenames, ensuring every file gets a named identity.
        Disable with --no-prompt-augment.

        `auto_filename` (default True when --write-files is set): auto-
        generates `snippet_N.<ext>` filenames for code blocks that don't have
        one (e.g. data-code-block divs, plain <pre>), based on the language
        label. Disable with --no-auto-filename.
        """
        # If prompt augmentation is enabled, append an instruction asking
        # Arena to use artifact file viewers with proper filenames.
        # This dramatically improves the hit rate of --write-files because
        # artifact cards carry filenames natively. Augmentation now always
        # applies to ensure every file gets a filename.
        # If prompt augmentation is enabled or system prompt additions are provided,
        # prepend system instructions first.
        # This dramatically improves the hit rate of --write-files because
        # artifact cards carry filenames natively. Augmentation now always
        # applies to ensure every file gets a filename.
        self.incremental_write = incremental_write
        self.conflict_resolution = conflict_resolution
        self.auto_filename = auto_filename
        self.write_files = write_files
        self._incremental_write_cache = {}
        self._first_write_checks = set()

        effective_prompt = self._build_final_prompt(
            prompt,
            system_prompts=system_prompts,
            augment_prompt=augment_prompt,
        )
        if len(effective_prompt) > len(prompt):
            print(
                f"[info] Prompt augmented with system instructions "
                f"(+{len(effective_prompt) - len(prompt)} chars).",
                file=sys.stderr,
            )

        # Pre-flight: if --reuse-chat is set, Arena may be displaying the
        # "Was this task successful?" review popup that REPLACES the chatbox.
        # Dismiss it before attempting to find the chat input so the input
        # lookup doesn't needlessly time out. A second defensive attempt is
        # made below in case the popup appeared between here and the input
        # lookup (race with SPA re-render).
        if self.reuse_chat:
            self._dismiss_review_popup_if_present(timeout_ms=2_000)

        input_box = self._find_chat_input()

        if input_box is None:
            # When --reuse-chat is used, Arena may be showing the
            # "Was this task successful?" review popup that REPLACES the
            # chatbox. We already tried to dismiss it once above (in the
            # pre-flight), but if the popup appeared between then and now
            # (race), try once more before falling back to generic selectors.
            if self.reuse_chat and self._dismiss_review_popup_if_present(timeout_ms=2_000):
                # Popup was just dismissed; retry the chat-input lookup with
                # the original timeout window.
                input_box = self._find_chat_input()

        if input_box is None:
            fallback = self.page.locator(
                'textarea:not([name="g-recaptcha-response"]):visible, '
                '[contenteditable="true"]:visible, '
                '[role="textbox"]:not([name="g-recaptcha-response"]):visible'
            ).first
            try:
                fallback.wait_for(state="visible", timeout=3_000)
                input_box = fallback
                print("[info] Found chat input via fallback selector.", file=sys.stderr)
            except PlaywrightTimeout:
                pass

        if input_box is None:
            print(
                "[error] Could not find the chat input box.\n"
                "Arena may have changed its UI, or the page is not in Agent Mode.\n"
                "Set ARENA_INPUT_SELECTOR to the correct selector.\n"
                "Tip: run with --headed to inspect the page live.",
                file=sys.stderr,
            )
            raise PlaywrightTimeout("Chat input not found")

        # Snapshot the current/previous assistant response BEFORE submitting.
        # Without this baseline, a completed message from an earlier turn can
        # still be the "last assistant bubble" for the first couple seconds
        # after submit. Its already-visible Copy button can make the dynamic
        # loop think the NEW response is complete before it has even started.
        baseline_poll: Dict[str, Any] = {}
        try:
            baseline_poll = self.page.evaluate("() => window.__arena_poll()") or {}
        except Exception as exc:
            print(f"[debug] pre-submit baseline poll failed: {exc}", file=sys.stderr)
        baseline_text = (baseline_poll.get("text") or "").strip()
        try:
            baseline_num_bubbles = int(baseline_poll.get("numBubbles") or 0)
        except (TypeError, ValueError):
            baseline_num_bubbles = 0
        baseline_code_signature = self._code_blocks_signature(
            baseline_poll.get("codeBlocks") or []
        )
        print(
            f"[debug] Baseline before submit: bubbles={baseline_num_bubbles}, "
            f"text={len(baseline_text)} chars, "
            f"code_sig={'yes' if baseline_code_signature else 'no'}.",
            file=sys.stderr,
        )

        print(
            f"[info] Sending prompt: {effective_prompt[:80]}{'...' if len(effective_prompt) > 80 else ''}",
            file=sys.stderr,
        )

        # Fill the input. After filling, the send button should transition
        # from disabled -> enabled (Arena disables it while the input is empty).
        # We poll for that transition instead of using a fixed sleep so we know
        # the UI has registered our text before we submit.
        input_box.fill("")
        input_box.fill(effective_prompt)

        enabled_pre_submit = self._wait_for_send_button_state(
            want_disabled=False, timeout_s=5.0, interval_s=0.1
        )
        if enabled_pre_submit:
            print("[info] Send button enabled (input accepted).", file=sys.stderr)
        else:
            # Fall back to a tiny sleep; Enter may still work even if the
            # button state probe failed (e.g. button uses a different selector).
            time.sleep(0.2)

        # Submit via Enter. This is usually the most reliable way to submit a
        # chat form. The send button should immediately go disabled (server
        # accepted the prompt).
        input_box.press("Enter")

        # Wait for the send button to flip to disabled = prompt was accepted
        # and generation has started. This is much faster and more reliable
        # than sleeping a fixed amount.
        disabled_post_submit = self._wait_for_send_button_state(
            want_disabled=True, timeout_s=5.0, interval_s=0.05
        )
        if disabled_post_submit:
            print("[info] Send button disabled (prompt accepted, generating).", file=sys.stderr)
        else:
            # Fallback: maybe Enter didn't submit. Try clicking the send button.
            send_button = self.page.locator(self._get_send_selector()).first
            try:
                send_button.wait_for(state="visible", timeout=1_000)
                if send_button.is_visible():
                    send_button.click()
                    print("[info] Clicked send button as fallback.", file=sys.stderr)
                    self._wait_for_send_button_state(
                        want_disabled=True, timeout_s=3.0, interval_s=0.05
                    )
            except PlaywrightTimeout:
                pass

        # If code_only is set, we ignore --stream for the final output (code
        # blocks aren't useful to stream -- they're extracted at the end).
        if code_only and stream:
            print(
                "[info] --code-only overrides --stream for final output; streaming disabled.",
                file=sys.stderr,
            )
            stream = False

        # Fixed-wait mode: skip dynamic detection entirely.
        if wait_seconds > 0:
            print(f"[info] Waiting fixed {wait_seconds}s before reading response...", file=sys.stderr)
            time.sleep(wait_seconds)
            return self._extract_final(
                code_only=code_only,
                debug_dom=debug_dom,
                debug_dom_path=debug_dom_path,
                write_files=write_files,
                dry_run=dry_run,
                auto_filename=auto_filename,
            )

        # --- Dynamic detection loop ---
        # Single JS round-trip per poll; interval tighter for streaming.
        interval = 0.1 if stream else 0.3
        # Primary fallback completion: response CONTENT (text + code blocks),
        # not just prose text, must be stable for --stable-seconds AND the page
        # must not look like it is generating. The previous implementation used
        # a hard-coded ~2.5s text-only window even though the CLI default said
        # 5s; that could finalize while an artifact was still being filled.
        stable_window_seconds = max(0.5, float(stable_seconds))
        primary_stable_needed = max(2, int(stable_window_seconds / interval))
        # Even when the Copy button is visible, require it and the response
        # content to remain stable for the same --stable-seconds window. This
        # is intentionally conservative for Agent Mode: some runs have quiet
        # gaps between tool calls where the visible text looks done.
        copy_stable_needed = primary_stable_needed
        # Minimum response window: don't return in the first 2 seconds after
        # submit, even if all signals look "done". This protects against the
        # race where the Stop button hasn't rendered yet and the first text
        # chunk is followed by a brief pause before streaming continues.
        min_response_seconds = 2.0

        print(
            f"[info] Waiting for response (interval={interval}s, "
            f"primary_stable={primary_stable_needed * interval:.1f}s, "
            f"copy_stable={copy_stable_needed * interval:.1f}s, "
            f"min_window={min_response_seconds}s, "
            f"timeout={max_wait_seconds if max_wait_seconds > 0 else 'infinite'}, "
            f"activity_timeout={activity_timeout_seconds}s)...",
            file=sys.stderr,
        )

        start = time.time()
        last_text = ""
        # Number of consecutive polls where the NEW response content (text +
        # code-block signature) is unchanged.
        stable_count = 0
        last_content_signature = ""
        copy_stable_count = 0
        last_code_blocks: List[Dict[str, Any]] = []
        # LATCH: flips only after the page shows a response that differs from
        # the pre-submit baseline. This prevents returning the previous turn's
        # already-complete assistant bubble.
        response_started = False
        # LATCH: once we've observed isGenerating=True OR send_button disabled
        # (i.e. generation actually started), this flips True and stays True.
        # Only after the latch is set do we trust `not isGenerating` as a
        # "generation finished" signal. Without this, we'd false-positive at
        # the very start when the Stop button hasn't rendered yet.
        ever_saw_generating = False
        # LATCH: once we've observed the Copy button visible at least once,
        # this flips True. Used for diagnostic logging so we don't spam the
        # "Copy button appeared" message on every poll.
        ever_saw_copy_button = False

        # WORKSPACE TRACKING: Arena Agent Mode populates a Workspace panel
        # (right side) with files as the task runs. The agent can finish its
        # prose answer (and show a Copy button) WHILE it is still writing
        # files. To avoid finalizing before all files are generated, we track
        # the Workspace's signature and refuse to return until it has been
        # stable for the same window as the content. `signature` changes when
        # files appear or their rendered status (e.g. line counts) grows.
        last_workspace_sig = ""
        workspace_stable_count = 0
        ever_saw_workspace_files = False
        # Pre-initialised so the progress log (which runs before the first
        # poll parses the Workspace state) never hits a NameError.
        ws_file_count = 0
        ws_writing = False

        # ACTIVITY TRACKING: last_activity_time is the wall-clock time of the
        # last observed change (text delta, code-block count change, or
        # generation-state transition). If now - last_activity_time exceeds
        # activity_timeout_seconds, we give up -- the page has truly hung.
        # This is the right way to bound agent-mode runs: long tasks stay
        # alive as long as they keep making progress, but a frozen UI is
        # detected within `activity_timeout_seconds` instead of waiting
        # forever (or being killed mid-task by an absolute timeout).
        last_activity_time = start
        last_activity_signature = ""
        # Wall-clock of last "still waiting" log. Initialized to `start` so
        # the first progress message fires 30s AFTER start (not immediately).
        last_progress_log = start

        while True:
            now = time.time()
            elapsed = now - start
            # ABSOLUTE timeout (default 0 = infinite). User can set --timeout
            # to enforce a hard cap; otherwise we wait for the Copy button.
            if max_wait_seconds > 0 and elapsed >= max_wait_seconds:
                print(
                    f"[warn] Absolute timeout reached ({max_wait_seconds}s). "
                    f"Aborting -- Arena may still be working.",
                    file=sys.stderr,
                )
                break
            # ACTIVITY timeout: if the page hasn't changed for this many
            # seconds, give up. This is the primary safety net for hung UIs.
            # Only enforce once we've seen generation start OR the new response
            # bubble/content has appeared -- otherwise the pre-generation phase
            # (just submitted, no text yet) would trip it.
            if (ever_saw_generating or response_started) and activity_timeout_seconds > 0:
                idle = now - last_activity_time
                if idle >= activity_timeout_seconds:
                    print(
                        f"[warn] No activity for {idle:.1f}s (>= {activity_timeout_seconds}s). "
                        f"Arena appears hung; aborting. "
                        f"Last text: {len(last_text)} chars, "
                        f"last code blocks: {len(last_code_blocks)}.",
                        file=sys.stderr,
                    )
                    break

            # Periodic "still waiting" progress log so the user knows the
            # script isn't dead during long runs. Emit every ~30s.
            if now - last_progress_log >= 30.0:
                last_progress_log = now
                idle = now - last_activity_time
                print(
                    f"[info] Still waiting... (elapsed={elapsed:.0f}s, "
                    f"text={len(last_text)} chars, "
                    f"code_blocks={len(last_code_blocks)}, "
                    f"workspace_files={ws_file_count}, "
                    f"workspace_writing={ws_writing}, "
                    f"idle={idle:.1f}s, "
                    f"response_started={response_started}, "
                    f"generating={ever_saw_generating}, "
                    f"copy_seen={ever_saw_copy_button})",
                    file=sys.stderr,
                )

            try:
                poll = self.page.evaluate("() => window.__arena_poll()") or {}
            except Exception as exc:
                print(f"[debug] poll failed: {exc}", file=sys.stderr)
                poll = {}

            current_text = (poll.get("text") or "").strip()
            is_generating = bool(poll.get("isGenerating"))
            code_blocks = poll.get("codeBlocks") or []
            code_signature = self._code_blocks_signature(code_blocks)
            try:
                num_bubbles = int(poll.get("numBubbles") or 0)
            except (TypeError, ValueError):
                num_bubbles = 0

            # Send-button state: when generation finishes, Arena re-enables the
            # send button. This is a stronger "done" signal than the Stop
            # button disappearing (which can be flaky on some UIs).
            send_state = poll.get("sendButton") or {}
            send_found = bool(send_state.get("found"))
            send_disabled = bool(send_state.get("disabled"))
            send_enabled = send_found and bool(send_state.get("visible")) and not send_disabled

            # Copy-button state: Arena renders the Copy button in the action
            # footer ONLY when the message is fully complete and persisted.
            # This is the STRONGEST "done" signal -- it cannot appear while
            # the model is still streaming or executing a tool call.
            copy_state = poll.get("copyButton") or {}
            copy_visible = bool(copy_state.get("found")) and bool(copy_state.get("visible"))

            # Workspace state: see the WORKSPACE TRACKING comment above.
            ws_state = poll.get("workspace") or {}
            ws_present = bool(ws_state.get("present"))
            try:
                ws_file_count = int(ws_state.get("fileCount") or 0)
            except (TypeError, ValueError):
                ws_file_count = 0
            ws_sig = ws_state.get("signature") or ""
            ws_writing = bool(ws_state.get("writing"))
            if ws_present and ws_file_count > 0:
                ever_saw_workspace_files = True

            # Decide whether we are looking at the NEW response or still seeing
            # the previous turn's assistant bubble. Completion checks and
            # streaming are disabled until this flips true.
            if not response_started:
                new_bubble_seen = num_bubbles > baseline_num_bubbles
                text_changed = bool(current_text) and current_text != baseline_text
                code_changed = bool(code_signature) and code_signature != baseline_code_signature
                if new_bubble_seen or text_changed or code_changed:
                    response_started = True
                    stable_count = 0
                    copy_stable_count = 0
                    last_text = ""
                    last_content_signature = ""
                    last_code_blocks = code_blocks if code_blocks else []
                    last_activity_time = time.time()
                    print(
                        f"[info] New response detected "
                        f"(bubbles {baseline_num_bubbles}->{num_bubbles}, "
                        f"text_changed={text_changed}, code_changed={code_changed}).",
                        file=sys.stderr,
                    )

            if response_started and code_blocks:
                last_code_blocks = code_blocks
                if self.incremental_write and self.write_files:
                    self._incremental_write_files(code_blocks, self.write_files)

            # Update the generation latch. We consider generation "active" if
            # EITHER the isGenerating probe returns True OR the send button is
            # disabled (button is disabled while input is empty OR while a
            # generation is in flight -- and we already filled the input, so
            # disabled = generating).
            if is_generating or (send_found and send_disabled):
                if not ever_saw_generating:
                    print(
                        f"[info] Generation started "
                        f"(isGenerating={is_generating}, sendDisabled={send_disabled}).",
                        file=sys.stderr,
                    )
                    # First generation sighting is itself activity -- reset timer.
                    last_activity_time = time.time()
                ever_saw_generating = True

            # Update copy-button-seen latch. Only count/log Copy for the NEW
            # response; previous turns often already have a visible Copy button.
            if response_started and copy_visible:
                copy_stable_count += 1
                if not ever_saw_copy_button:
                    print(
                        f"[info] Copy button appeared on new response.",
                        file=sys.stderr,
                    )
                    last_activity_time = time.time()
                    ever_saw_copy_button = True
            else:
                copy_stable_count = 0

            # WORKSPACE stability: once we've seen Workspace files, require the
            # Workspace signature to be unchanged for the stability window
            # before we allow completion. A changing signature means files are
            # still appearing or being written -- keep waiting.
            if ws_sig and ws_sig == last_workspace_sig:
                workspace_stable_count += 1
            else:
                workspace_stable_count = 0
            last_workspace_sig = ws_sig

            # ACTIVITY TRACKING: build a signature of the current observable
            # state. If it differs from the last signature, the page is making
            # progress -- reset the activity timer.
            current_signature = (
                f"{len(current_text)}|{current_text[:64]}|"
                f"bubbles={num_bubbles}|code={code_signature}|"
                f"response_started={response_started}|"
                f"gen={is_generating}|send={send_disabled}|copy={copy_visible}|"
                f"ws={ws_sig}|wsW={ws_writing}"
            )
            if current_signature != last_activity_signature:
                if last_activity_signature != "":
                    # Only log activity resets AFTER the first poll (the first
                    # poll always differs from the empty initial signature).
                    last_activity_time = time.time()
                last_activity_signature = current_signature

            # Stream diff: print only the new response tail. Do not stream the
            # previous turn while we are waiting for the new bubble to appear.
            if response_started and stream and current_text and current_text != last_text:
                if current_text.startswith(last_text):
                    sys.stdout.write(current_text[len(last_text):])
                else:
                    # Non-monotonic update (rare): rewrite on a new line.
                    sys.stdout.write("\n" + current_text)
                sys.stdout.flush()

            # Track NEW response content stability. This uses both prose text
            # and code-block content so artifact growth resets the timer.
            content_signature = f"text={current_text}\ncode={code_signature}"
            if response_started and (current_text or code_blocks):
                if content_signature == last_content_signature:
                    stable_count += 1
                else:
                    stable_count = 0
                    last_content_signature = content_signature
                last_text = current_text
            else:
                stable_count = 0

            # Gate: minimum response window. Never return in the first
            # `min_response_seconds` after submit -- Arena needs time to render
            # the Stop button and start streaming. This eliminates the race
            # where the first text chunk lands before isGenerating flips True.
            in_min_window = elapsed < min_response_seconds

            # WORKSPACE completion gate. Once the agent has created Workspace
            # files, refuse to finalize until the Workspace signature has been
            # stable for the same window as the content. This is THE fix for
            # "finishes before all files are generated": the prose answer can
            # look complete (Copy button visible) while the agent is still
            # writing files to the Workspace, so we block on Workspace
            # stability too. ws_writing (a visible spinner) also blocks as a
            # strong "definitely busy" hint.
            # --no-workspace-wait disables this gate (escape hatch in case the
            # signature never stabilizes, e.g. a live timer in the DOM).
            if workspace_wait:
                workspace_busy = ever_saw_workspace_files and ws_writing
                workspace_settled = (
                    not ever_saw_workspace_files
                    or workspace_stable_count >= copy_stable_needed
                )
            else:
                workspace_busy = False
                workspace_settled = True

            # Completion check #0 (highest priority): Copy button visible on
            # the NEW response, no active generation signal, and the response
            # content + Copy button have both been stable briefly. This avoids
            # the early-return race where the previous turn's Copy button is
            # still visible, and the case where prose is stable while a code
            # artifact is still growing.
            has_response_content = bool(current_text or last_code_blocks or code_blocks)
            if (response_started
                    and copy_visible
                    and has_response_content
                    and not in_min_window
                    and not is_generating
                    and not workspace_busy
                    and workspace_settled
                    and copy_stable_count >= copy_stable_needed
                    and stable_count >= copy_stable_needed):
                if stream:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                print(
                    f"[info] Response complete ({len(current_text)} chars, "
                    f"signal=copy-button-visible-stable).",
                    file=sys.stderr,
                )
                return self._finalize_response(
                    text=current_text or last_text,
                    code_blocks=self._merge_codemirror_blocks(last_code_blocks),
                    code_only=code_only,
                    debug_dom=debug_dom,
                    debug_dom_path=debug_dom_path,
                    write_files=write_files,
                    dry_run=dry_run,
                    auto_filename=auto_filename,
                )

            # Completion check #1: fallback when the UI variant does not expose
            # a reliable Copy button. Require the NEW response content to be
            # stable for --stable-seconds and require the generating probe to be
            # false. Do not use "send button enabled" as an OR condition here:
            # chat inputs can toggle around tool-call gaps, which was another
            # source of premature finalization.
            if (response_started
                    and has_response_content
                    and (ever_saw_generating or response_started)
                    and not in_min_window
                    and not workspace_busy
                    and workspace_settled
                    and stable_count >= primary_stable_needed
                    and not is_generating):
                if stream:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                done_signal = "stable-content+stop-button-gone"
                if send_enabled:
                    done_signal += "+send-button-enabled"
                print(
                    f"[info] Response complete ({len(current_text)} chars, "
                    f"signal={done_signal}).",
                    file=sys.stderr,
                )
                return self._finalize_response(
                    text=current_text or last_text,
                    code_blocks=self._merge_codemirror_blocks(last_code_blocks),
                    code_only=code_only,
                    debug_dom=debug_dom,
                    debug_dom_path=debug_dom_path,
                    write_files=write_files,
                    dry_run=dry_run,
                    auto_filename=auto_filename,
                )

            # NOTE: The old "completion check #2" (force-return after extended
            # stability) has been REMOVED. It was redundant with the activity-
            # timeout at the top of the loop, and a floating-point precision
            # issue (int(15/0.3) = 49, not 50) caused it to fire one iteration
            # BEFORE the activity-timeout, prematurely aborting long tool calls.
            # The activity-timeout is now the sole "give up after N seconds of
            # no activity" signal, which is cleaner and avoids the race.

            time.sleep(interval)

        # Timeout exit (either absolute or activity timeout fired).
        if stream and last_text:
            sys.stdout.write("\n")
            sys.stdout.flush()

        if last_text or last_code_blocks:
            print("[warn] Returning partial response after timeout.", file=sys.stderr)
            return self._finalize_response(
                text=last_text,
                code_blocks=self._merge_codemirror_blocks(last_code_blocks),
                code_only=code_only,
                debug_dom=debug_dom,
                debug_dom_path=debug_dom_path,
                write_files=write_files,
                dry_run=dry_run,
                auto_filename=auto_filename,
            )

        print("[warn] No response text found within the timeout.", file=sys.stderr)
        if debug_dom or code_only or write_files:
            self._dump_dom(debug_dom_path)
        return None


    def save_debug_screenshot(self, path: str = "arena_debug.png"):
        self.page.screenshot(path=path, full_page=True)
        print(f"[info] Debug screenshot saved: {path}", file=sys.stderr)

    def close(self):
        if self.context:
            self.context.close()

