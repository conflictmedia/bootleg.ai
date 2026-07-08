"""Chat-state persistence (the fix for "chat state not being saved").

Split out of aai.py. The state file is a single JSON object:
  {
    "site": "arena" | "canary",
    "chat_url": "https://arena.ai/chat/<id>",
    "chat_path": "/chat/<id>",
    "timestamp": "2025-06-17T12:34:56Z"
  }

- _load_state() reads it. Returns None if missing/corrupt.
- _save_state() reads self.page.url, derives chat_path, and writes
  the JSON atomically (tmp + rename). Called from cli.main() right
  after send_prompt() returns a non-empty response.

_save_state() is defensive: if page/url is unavailable (e.g.
browser already closed) it logs a warning and returns rather than
raising, so a state-save failure never masks an otherwise-
successful run.
"""

import json
import sys
import datetime
from urllib.parse import urlparse

from typing import Any, Dict, Optional


class StateMixin:
    """Provides `_load_state` / `_save_state` for ArenaAgent."""

    def _load_state(self) -> Optional[Dict[str, Any]]:
        """Read the JSON state file. Returns None if missing or unreadable."""
        try:
            raw = self.state_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            print(f"[warn] Could not read state file {self.state_file}: {exc}",
                  file=sys.stderr)
            return None
        try:
            data = __import__("json").loads(raw)
        except ValueError as exc:
            print(f"[warn] State file {self.state_file} is corrupt ({exc}); "
                  f"ignoring.", file=sys.stderr)
            return None
        if not isinstance(data, dict):
            print(f"[warn] State file {self.state_file} is not a JSON object; "
                  f"ignoring.", file=sys.stderr)
            return None
        return data

    def _save_state(self) -> bool:
        """Capture self.page.url and persist it to the state file.

        Returns True on success, False on failure (with a warning logged).
        Failures are non-fatal: a successful run should still be reported
        to the user even if we couldn't persist the chat URL.
        """
        if self.page is None:
            print("[warn] Cannot save chat state: page is None.", file=sys.stderr)
            return False
        try:
            current_url = self.page.url
        except Exception as exc:
            print(f"[warn] Cannot read page.url for state save: {exc}",
                  file=sys.stderr)
            return False
        if not current_url:
            print("[warn] Cannot save chat state: page.url is empty.",
                  file=sys.stderr)
            return False

        # Derive the path portion (e.g. "/chat/abc-123") from the full URL.
        # We use urllib so we don't reinvent URL parsing.
        from urllib.parse import urlparse
        parsed = urlparse(current_url)
        chat_path = parsed.path or self.chat_path
        # If Arena redirected to the bare /chat (no chat id), don't overwrite
        # a previously-saved richer path -- keep the saved one if it's more
        # specific.
        if chat_path.rstrip("/") == "/chat" or chat_path == "/":
            saved = self._load_state()
            if saved and saved.get("site") == self.site_key:
                saved_path = saved.get("chat_path", "")
                if len(saved_path) > len(chat_path):
                    chat_path = saved_path

        import datetime
        state = {
            "site": self.site_key,
            "chat_url": current_url,
            "chat_path": chat_path,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }

        # Atomic write: tmp file in the same dir, then rename.
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
            tmp.write_text(
                __import__("json").dumps(state, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.state_file)
        except OSError as exc:
            print(f"[warn] Could not write state file {self.state_file}: {exc}",
                  file=sys.stderr)
            return False

        print(
            f"[info] Chat state saved: site={state['site']} "
            f"chat_path={state['chat_path']} -> {self.state_file}",
            file=sys.stderr,
        )
        return True

