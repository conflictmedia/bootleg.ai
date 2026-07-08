"""arena_agent -- automate arena.ai / canaryarena.ai in Agent Mode.

Package created by splitting the original monolithic `aai.py` into
single-concern modules. The public API is unchanged:

    from arena_agent import ArenaAgent, main
    from arena_agent.constants import SITES, DEFAULT_STATE_FILE
"""

from .agent import ArenaAgent
from .cli import main
from .constants import (
    DEFAULT_CHROME_PROFILE,
    DEFAULT_STATE_FILE,
    SITES,
    AGENT_MODE_SELECTORS,
)
from .prompt_files import _append_included_files_to_prompt

__all__ = [
    "ArenaAgent",
    "main",
    "DEFAULT_CHROME_PROFILE",
    "DEFAULT_STATE_FILE",
    "SITES",
    "AGENT_MODE_SELECTORS",
    "_append_included_files_to_prompt",
]
