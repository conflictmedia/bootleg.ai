"""Package-level constants for arena_agent.

Split out from the original aai.py monolith so they can be imported by
multiple modules (agent.py, cli.py, state.py) without circular deps.
"""

from pathlib import Path


DEFAULT_CHROME_PROFILE = Path.home() / ".config" / "chromium" / "Default"

# Per-site chat-state file. After every successful run the script writes the
# final chat URL (e.g. https://arena.ai/chat/<chat_id>) here, so a subsequent
# `--resume` can navigate straight back to that conversation instead of
# landing on /chat and starting fresh. JSON, single object.
DEFAULT_STATE_FILE = Path.home() / ".arena_agent_state.json"

SITES = {
    "arena": {
        "name": "Arena",
        "url": "https://arena.ai",
        "chat_path": "/chat",
        "agent_path": "/agent",
    },
    "canary": {
        "name": "Canary Arena",
        "url": "https://canaryarena.ai",
        "chat_path": "/chat",
        "agent_path": "/agent",
    },
}

AGENT_MODE_SELECTORS = [
    'button[role="combobox"]:visible',
    'button:has-text("Agent Mode"):visible',
    'button:has-text("Agent"):visible',
    'a:has-text("Agent Mode"):visible',
    'a:has-text("Agent"):visible',
    '[role="tab"]:has-text("Agent"):visible',
    '[role="tab"]:has-text("Agent Mode"):visible',
    '[aria-label*="Agent Mode" i]:visible',
    '[aria-label*="Agent" i]:visible',
    '[data-testid*="agent" i]:visible',
    '[class*="agent" i]:visible',
    'button:has-text("Switch to Agent"):visible',
    'button:has-text("Agent chat"):visible',
]
