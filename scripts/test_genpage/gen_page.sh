#!/usr/bin/env bash

set -euo pipefail

shopt -s nullglob
json_files=(./test/*.json)
shopt -u nullglob

if [ ${#json_files[@]} -eq 0 ]; then
  echo "No JSON files found in ./test/" >&2
  exit 1
fi

selected_file="${json_files[RANDOM % ${#json_files[@]}]}"

echo "Using input file: $selected_file"

python3 aai.py --site canary \
  --system "Use this file as context and analyze it, respond with all code in seperate codeblocks (wrapped in 3 backticks), make sure the html code has the filename: index.html" \
  --prompt "write small webpage to read the data and display it nicely" \
  --include-file "$selected_file" \
  --agent-mode --chat-path / \
  --code-only \
  --write-files ./out/ \
  --stable-seconds 10 \
  --activity-timeout 1200 
