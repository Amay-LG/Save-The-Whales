#!/bin/sh
# PostToolUse: lint an edited .py file with the repo's ruff.toml. Skips silently if ruff isn't installed in .venv.
f=$(jq -r '.tool_input.file_path // .tool_response.filePath // ""')
case "$f" in *.py) ;; *) exit 0 ;; esac
[ -f "$f" ] || exit 0
ruff="$CLAUDE_PROJECT_DIR/.venv/bin/ruff"
[ -x "$ruff" ] || exit 0
if ! out=$(cd "$CLAUDE_PROJECT_DIR" && "$ruff" check --quiet --output-format concise "$f" 2>&1); then
  jq -n --arg r "$out" '{decision: "block", reason: ("ruff found problems; fix them:\n" + $r)}'
fi
exit 0
