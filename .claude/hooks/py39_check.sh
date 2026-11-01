#!/bin/sh
# PostToolUse: parse an edited .py file with the 3.9 grammar so 3.10+ syntax (match, etc.) is rejected on any interpreter.
f=$(jq -r '.tool_input.file_path // .tool_response.filePath // ""')
case "$f" in *.py) ;; *) exit 0 ;; esac
[ -f "$f" ] || exit 0
py="$CLAUDE_PROJECT_DIR/.venv/bin/python"
[ -x "$py" ] || py=python3
if ! out=$("$py" -c 'import ast, sys; ast.parse(open(sys.argv[1], "rb").read(), sys.argv[1], feature_version=(3, 9))' "$f" 2>&1); then
  jq -n --arg r "$out" '{decision: "block", reason: ("Python 3.9 syntax check failed; fix before continuing:\n" + $r)}'
fi
exit 0
