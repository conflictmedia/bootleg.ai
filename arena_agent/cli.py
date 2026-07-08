"""Command-line entry point for arena_agent.

Split out of aai.py. Builds the argparse parser, wires up the
ArenaAgent, runs send_prompt, and persists chat state.

Invoke via:
    python -m arena_agent --site arena --prompt "Hello"
or via the backward-compat shim:
    python aai.py --site arena --prompt "Hello"
"""

import argparse
import sys

from pathlib import Path

from .agent import ArenaAgent
from .constants import (
    DEFAULT_CHROME_PROFILE,
    DEFAULT_STATE_FILE,
    SITES,
)
from .prompt_files import _append_included_files_to_prompt


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Send a prompt to Arena.ai/CanaryArena.ai Agent Mode using "
            "existing Chromium cookies."
        )
    )
    parser.add_argument(
        "--site",
        choices=list(SITES.keys()),
        default="arena",
        help="Which Arena environment to use (default: arena).",
    )
    parser.add_argument(
        "--prompt",
        required=True,
        help="The prompt to send to Agent Mode.",
    )
    parser.add_argument(
        "--system-prompt",
        "--system",
        dest="system_prompts",
        action="append",
        default=None,
        help=(
            "System prompt instruction to prepend to the user prompt. "
            "May be supplied multiple times. Example: "
            "--system-prompt 'You are an expert programmer' --system-prompt 'Use Python 3.10'"
        ),
    )
    parser.add_argument(
        "--include-file",
        "--context-file",
        dest="include_files",
        action="append",
        metavar="PATH",
        default=None,
        help=(
            "Read a local text file and append its contents to the prompt as "
            "context. May be supplied multiple times. Example: "
            "--include-file ./app.py --include-file ./README.md"
        ),
    )
    parser.add_argument(
        "--include-file-max-chars",
        type=int,
        default=0,
        help=(
            "Maximum characters to include from each --include-file "
            "(default: 0 = no truncation)."
        ),
    )
    parser.add_argument(
        "--profile",
        type=Path,
        default=DEFAULT_CHROME_PROFILE,
        help=f"Path to the Chromium profile directory (default: {DEFAULT_CHROME_PROFILE}).",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run the browser visibly (useful for debugging).",
    )
    parser.add_argument(
        "--agent-mode",
        action="store_true",
        help="Attempt to switch from Battle Mode to Agent Mode before sending the prompt.",
    )
    parser.add_argument(
        "--direct-mode",
        action="store_true",
        help="Attempt to switch to Direct Mode before sending the prompt.",
    )
    parser.add_argument(
        "--model",
        dest="model_name",
        default=None,
        help="Model name to select if in Direct Mode (e.g., minimax-m3, Max, etc.).",
    )
    parser.add_argument(
        "--chat-path",
        default="/chat",
        help="Path appended to the base URL (default: /chat). Use '/' for the landing page.",
    )
    parser.add_argument(
        "--reuse-chat",
        action="store_true",
        help=(
            "Indicate that we are continuing a previously-finished Arena "
            "chat. Arena shows a 'Was this task successful?' review popup "
            "that REPLACES the chatbox in this case; this flag makes the "
            "script auto-dismiss that popup (clicking 'Keep working', then "
            "falling back to the close button and finally Esc) so the "
            "chatbox is restored before sending the new prompt. "
            "NOTE: this flag is also set implicitly by --resume."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Resume the most recent chat saved in --state-file. Reads the "
            "saved chat_path from the state file and navigates there "
            "instead of /chat, so Arena loads the exact previous "
            "conversation (with full message history). Implies "
            "--reuse-chat (so the review popup is auto-dismissed). After "
            "the run completes, the state file is updated with the new "
            "current chat URL, so subsequent --resume calls keep chaining "
            "onto the same conversation. Errors out if no state file "
            "exists yet -- run once without --resume to create it."
        ),
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_FILE,
        help=(
            f"Path to the JSON chat-state file used by --resume and "
            f"written after every successful run (default: "
            f"{DEFAULT_STATE_FILE}). Use a different path to maintain "
            f"separate chat chains per project."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=0,
        help=(
            "ABSOLUTE maximum seconds to wait for a response (default: 0 = "
            "infinite, Ctrl+C to abort). Agent-mode tasks can legitimately "
            "run for 10+ minutes; we recommend leaving this at 0 and relying "
            "on --activity-timeout to catch genuinely-hung runs."
        ),
    )
    parser.add_argument(
        "--activity-timeout",
        type=int,
        default=300,
        help=(
            "NO-ACTIVITY timeout in seconds (default: 300 = 5 min). If the "
            "page hasn't changed (no text delta, no new code blocks, no "
            "generation-state transition) for this many seconds, give up. "
            "Set to 0 to disable. This is the primary safety net for hung "
            "UIs -- long tasks stay alive as long as they keep making "
            "progress."
        ),
    )
    parser.add_argument(
        "--stable-seconds",
        type=int,
        default=5,
        help=(
            "Fallback stability window in seconds (default: 5). Only used if "
            "the Stop button never disappears; primary completion is the "
            "Copy-button-visible signal."
        ),
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        help="Print the response as it is generated (streaming).",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=0,
        help=(
            "After sending, wait this many seconds before reading the response "
            "(default: 0 = dynamic detection)."
        ),
    )
    parser.add_argument(
        "--screenshot",
        action="store_true",
        help="Save a full-page screenshot after the response finishes.",
    )
    parser.add_argument(
        "--code-only",
        action="store_true",
        help=(
            "Print only the code blocks from the response (artifact file "
            "viewers + inline <pre> blocks). Each block is preceded by a "
            "'===== [N/total] filename (LANG) =====' header. Log lines go "
            "to stderr so stdout is pipe-friendly."
        ),
    )
    parser.add_argument(
        "--debug-dom",
        metavar="PATH",
        nargs="?",
        const="arena_debug_dom.html",
        default=None,
        help=(
            "Dump the last assistant bubble's outerHTML to a file for "
            "debugging selector issues. If PATH is omitted, defaults to "
            "arena_debug_dom.html. Note: --code-only auto-dumps to this "
            "path if zero code blocks are found, even without --debug-dom."
        ),
    )
    parser.add_argument(
        "--write-files",
        metavar="DIR",
        default=None,
        help=(
            "Write each code block to the given directory. The directory "
            "is created if it doesn't exist. Filenames are sanitized to "
            "basename only (no subdirectories). Blocks without a filename "
            "get an auto-generated snippet_N.<ext> name based on language. "
            "Combine with --dry-run to preview which files would be written."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "With --write-files: list which files would be written without "
            "actually writing them. Useful for verifying the filename "
            "filter before committing to disk."
        ),
    )
    parser.add_argument(
        "--no-prompt-augment",
        action="store_true",
        help=(
            "Do NOT prepend the filename instruction to the prompt. By "
            "default, the prompt is augmented with a system instruction "
            "asking the model to wrap all code in artifact file viewers "
            "with proper filenames, ensuring every file gets a name. Use "
            "this flag if you want to send the prompt verbatim and rely "
            "solely on --auto-filename for blocks without a filename."
        ),
    )
    parser.add_argument(
        "--no-auto-filename",
        action="store_true",
        help=(
            "With --write-files: do NOT auto-generate filenames for blocks "
            "without one. Only blocks that already carry a filename "
            "(artifact file viewers) will be written. data-code-block "
            "divs and plain <pre> blocks will be skipped."
        ),
    )
    args = parser.parse_args()

    try:
        prompt = _append_included_files_to_prompt(
            args.prompt,
            args.include_files,
            max_chars_per_file=args.include_file_max_chars,
        )
    except Exception as exc:
        parser.error(str(exc))

    agent = ArenaAgent(
        site_key=args.site,
        profile_dir=args.profile,
        headless=not args.headed,
        agent_mode=args.agent_mode,
        direct_mode=args.direct_mode,
        model_name=args.model_name,
        chat_path=args.chat_path,
        reuse_chat=args.reuse_chat,
        state_file=args.state_file,
        resume=args.resume,
    )

    try:
        agent.start()
        response = agent.send_prompt(
            prompt,
            max_wait_seconds=args.timeout,
            stream=args.stream,
            stable_seconds=args.stable_seconds,
            wait_seconds=args.wait_seconds,
            code_only=args.code_only,
            debug_dom=args.debug_dom is not None,
            debug_dom_path=args.debug_dom,
            write_files=args.write_files,
            dry_run=args.dry_run,
            augment_prompt=not args.no_prompt_augment,
            auto_filename=not args.no_auto_filename,
            activity_timeout_seconds=args.activity_timeout,
            system_prompts=args.system_prompts,
        )
        if response:
            if args.code_only:
                # Code blocks already include their own formatting.
                print(response)
            elif not args.stream:
                print("\n" + "=" * 60)
                print("RESPONSE")
                print("=" * 60)
                print(response)
                print("=" * 60)
            # Persist the chat URL so a subsequent --resume can find this
            # conversation. We do this AFTER printing the response so the
            # user sees output first, and only on a non-empty response so
            # a failed run doesn't overwrite a previously-good state file.
            # _save_state() is defensive: it logs a warning and returns
            # False on failure rather than raising, so a state-save
            # hiccup never masks a successful run.
            agent._save_state()
        else:
            if args.code_only:
                print("[warn] No code blocks found in the response.", file=sys.stderr)
            else:
                print("[warn] No response text found.", file=sys.stderr)
            # Even on an empty response, the page URL may have changed
            # (e.g. Arena created the chat but the model returned nothing).
            # Try to save state so a retry --resume can find the same
            # conversation -- but only if we actually have a non-/chat URL.
            agent._save_state()
        if args.screenshot:
            agent.save_debug_screenshot()
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        if agent.page:
            agent.save_debug_screenshot()
        sys.exit(1)
    finally:
        agent.close()

