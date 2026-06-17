#!/usr/bin/env bash

set -euo pipefail

# ---------------------------------------------------------------------------
# test.sh — convenience wrapper around aai.py for the canaryarena.ai site.
#
# Usage:
#   ./test.sh <prompt-text> [options]
#
# Positional:
#   <prompt-text>              The prompt to send. May contain spaces; quote it.
#
# Options (all optional):
#   --sys <text>               Extra system-prompt text appended to the
#                              default "respond with all code in separate
#                              codeblocks" instruction.
#   --include-file <path>      Read a local file and append its contents to
#                              the prompt as context. Also tweaks the system
#                              prompt to "Use this file as context and
#                              analyze it, ...".
#
# Chat-state persistence (forwarded straight to aai.py):
#   --resume                   Resume the most recent chat saved in the state
#                              file. Navigates to the saved chat URL instead
#                              of starting fresh. Implies --reuse-chat.
#   --reuse-chat               Continue a previously-finished Arena chat
#                              (dismisses the "Was this task successful?"
#                              review popup). Also best-effort loads the
#                              state file to navigate back to the saved chat.
#   --state-file <path>        Use a custom state file path (default:
#                              ~/.arena_agent_state.json). Use this to keep
#                              separate chat chains per project.
#   --show-state               Print the current state file and exit. Useful
#                              for debugging --resume: if chat_id is null or
#                              chat_path is /chat, the script could not
#                              detect Arena's chat ID and --resume will not
#                              navigate back to a specific chat.
#   --clear-state              Delete the state file before running, so this
#                              run starts a fresh chat chain.
#
# Debugging:
#   --headed                   Run the browser visibly (forwarded to aai.py).
#
# Examples:
#   # Fresh chat (writes state file):
#   ./test.sh "Write a snake game in python"
#
#   # Continue the previous chat:
#   ./test.sh "Now add a high-score table" --resume
#
#   # Inspect what was saved:
#   ./test.sh dummy --show-state
#
#   # Start over:
#   ./test.sh "New topic" --clear-state
#
#   # Use a project-local state file so this project's chat chain doesn't
#   # collide with other projects:
#   ./test.sh "Hello" --state-file ./.my_project_state.json
#   ./test.sh "Follow up" --resume --state-file ./.my_project_state.json
#
# Notes:
#   - --chat-path / is passed to aai.py by default (lands on the canaryarena
#     homepage, then _enter_chat_if_needed() clicks "New Chat"). When
#     --resume is set, --chat-path is omitted so aai.py can navigate to the
#     saved chat URL instead.
#   - --show-state does not require a real prompt; pass any non-empty string
#     (e.g. "dummy") to satisfy the positional argument requirement. The
#     script exits before launching the browser.
# ---------------------------------------------------------------------------

# Parse arguments: positional prompt text + optional flags.
prompt_text=""
extra_system=""
include_file=""

# New-style flags collected as arrays so paths with spaces survive intact.
resume_args=()
reuse_chat_args=()
state_file_args=()
show_state_args=()
clear_state_args=()
headed_args=()

while [ $# -gt 0 ]; do
  case "$1" in
    --sys)
      if [ $# -lt 2 ]; then
        echo "Usage: $0 <prompt-text> [--sys <text>] [--include-file <path>]" >&2
        echo "       [--resume] [--reuse-chat] [--state-file <path>]" >&2
        echo "       [--show-state] [--clear-state] [--headed]" >&2
        exit 1
      fi
      extra_system="$2"
      shift 2
      ;;
    --include-file)
      if [ $# -lt 2 ]; then
        echo "Usage: $0 <prompt-text> [--sys <text>] [--include-file <path>]" >&2
        echo "       [--resume] [--reuse-chat] [--state-file <path>]" >&2
        echo "       [--show-state] [--clear-state] [--headed]" >&2
        exit 1
      fi
      include_file="$2"
      shift 2
      ;;
    --resume)
      resume_args=(--resume)
      shift
      ;;
    --reuse-chat)
      reuse_chat_args=(--reuse-chat)
      shift
      ;;
    --state-file)
      if [ $# -lt 2 ]; then
        echo "Usage: $0 <prompt-text> [--state-file <path>]" >&2
        exit 1
      fi
      state_file_args=(--state-file "$2")
      shift 2
      ;;
    --show-state)
      show_state_args=(--show-state)
      shift
      ;;
    --clear-state)
      clear_state_args=(--clear-state)
      shift
      ;;
    --headed)
      headed_args=(--headed)
      shift
      ;;
    -h|--help)
      cat <<'USAGE'
Usage: test.sh <prompt-text> [options]

Positional:
  <prompt-text>              The prompt to send. May contain spaces; quote it.

Options:
  --sys <text>               Extra system-prompt text appended to the default
                             "respond with all code in separate codeblocks"
                             instruction.
  --include-file <path>      Read a local file and append its contents to the
                             prompt as context.
  --resume                   Resume the most recent chat saved in the state
                             file. Navigates to the saved chat URL instead of
                             starting fresh. Implies --reuse-chat.
  --reuse-chat               Continue a previously-finished Arena chat
                             (dismisses the "Was this task successful?"
                             review popup). Best-effort loads the state file
                             to navigate back to the saved chat.
  --state-file <path>        Use a custom state file path (default:
                             ~/.arena_agent_state.json).
  --show-state               Print the current state file and exit. Useful
                             for debugging --resume.
  --clear-state              Delete the state file before running, so this
                             run starts a fresh chat chain.
  --headed                   Run the browser visibly (forwarded to aai.py).

Examples:
  # Fresh chat (writes state file):
  ./test.sh "Write a snake game in python"

  # Continue the previous chat:
  ./test.sh "Now add a high-score table" --resume

  # Inspect what was saved:
  ./test.sh dummy --show-state

  # Start over:
  ./test.sh "New topic" --clear-state

  # Project-local state file:
  ./test.sh "Hello" --state-file ./.my_project_state.json
  ./test.sh "Follow up" --resume --state-file ./.my_project_state.json
USAGE
      exit 0
      ;;
    *)
      # Positional prompt text. Multiple positionals are joined with spaces.
      if [ -z "$prompt_text" ]; then
        prompt_text="$1"
      else
        prompt_text="$prompt_text $1"
      fi
      shift
      ;;
  esac
done

# --show-state short-circuits everything: print the state file and exit.
# aai.py's --show-state handler doesn't actually need a prompt, but our
# positional-prompt check below would reject an empty one, so we honour
# --show-state BEFORE that check. Pass a dummy prompt to satisfy argparse.
if [ ${#show_state_args[@]} -gt 0 ]; then
  cmd_args=(
    --site canary
    --prompt "dummy"
    "${show_state_args[@]}"
  )
  if [ ${#state_file_args[@]} -gt 0 ]; then
    cmd_args+=("${state_file_args[@]}")
  fi
  python3 aai.py "${cmd_args[@]}"
  exit 0
fi

if [ -z "$prompt_text" ]; then
  echo "Usage: $0 <prompt-text> [--sys <text>] [--include-file <path>]" >&2
  echo "       [--resume] [--reuse-chat] [--state-file <path>]" >&2
  echo "       [--show-state] [--clear-state] [--headed]" >&2
  exit 1
fi

# Build the system prompt. The base instruction asks for separate codeblocks;
# --include-file prepends a "use this as context" instruction; --sys appends
# extra user-supplied system text last (so it can override earlier hints).
system_prompt="respond with all code in seperate codeblocks (wrapped in 3 backticks)"
if [ -n "$include_file" ]; then
  system_prompt="Use this file as context and analyze it, $system_prompt"
fi
if [ -n "$extra_system" ]; then
  system_prompt="$system_prompt $extra_system"
fi

# Assemble the aai.py argument list.
cmd_args=(
  --site canary
  --system "$system_prompt"
  --prompt "$prompt_text"
)

if [ -n "$include_file" ]; then
  cmd_args+=(--include-file "$include_file")
fi

# Chat-state flags. --state-file and --clear-state are passed through
# unconditionally (they're harmless when no state exists). --resume and
# --reuse-chat affect navigation, so we handle them specially below.
if [ ${#state_file_args[@]} -gt 0 ]; then
  cmd_args+=("${state_file_args[@]}")
fi

if [ ${#clear_state_args[@]} -gt 0 ]; then
  cmd_args+=("${clear_state_args[@]}")
fi

# Decide the chat-path / resume strategy:
#
# - --resume:   aai.py loads the saved chat_path and navigates there. We
#               must NOT pass --chat-path /, because aai.py would still
#               honour it (the resume logic overrides self.chat_path, but
#               passing --chat-path / is misleading). --resume implies
#               --reuse-chat inside aai.py, so the review popup is
#               auto-dismissed.
#
# - --reuse-chat (without --resume): aai.py best-effort loads the state
#               file and navigates to the saved chat if one exists,
#               otherwise falls back to whatever --chat-path we pass. We
#               pass --chat-path / as the fallback (same as the default
#               behaviour) so the script still works if no state exists.
#
# - Neither:    fresh chat. Pass --chat-path / (the canaryarena landing
#               page); _enter_chat_if_needed() clicks "New Chat".
if [ ${#resume_args[@]} -gt 0 ]; then
  cmd_args+=("${resume_args[@]}")
  # Intentionally do NOT pass --chat-path; aai.py uses the saved path.
elif [ ${#reuse_chat_args[@]} -gt 0 ]; then
  cmd_args+=("${reuse_chat_args[@]}")
  cmd_args+=(--chat-path /)
else
  cmd_args+=(--chat-path /)
fi

cmd_args+=(
  --agent-mode
  --code-only
  --write-files ./out/
  --stable-seconds 10
  --activity-timeout 1200
)

if [ ${#headed_args[@]} -gt 0 ]; then
  cmd_args+=("${headed_args[@]}")
fi

# Echo the final command for debugging (to stderr so stdout stays pipe-clean
# for --code-only output).
echo "[test.sh] python3 aai.py ${cmd_args[*]}" >&2

python3 aai.py "${cmd_args[@]}"
