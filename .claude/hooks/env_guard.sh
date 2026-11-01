#!/bin/sh
# PreToolUse: deny Claude access to .env (holds AISSTREAM_API_KEY). .env.example and .envrc are not matched.
input=$(cat)
tool=$(printf '%s' "$input" | jq -r '.tool_name // ""')
case "$tool" in
  Bash) target=$(printf '%s' "$input" | jq -r '.tool_input.command // ""') ;;
  Grep) target=$(printf '%s' "$input" | jq -r '[.tool_input.path, .tool_input.glob] | map(select(. != null)) | join(" ")') ;;
  *)    target=$(printf '%s' "$input" | jq -r '.tool_input.file_path // ""') ;;
esac

# Git metadata checks never read the file's contents.
if [ "$tool" = "Bash" ] && printf '%s' "$target" | grep -Eq '^git (check-ignore|ls-files)( [^;&|`$]*)?$'; then
  exit 0
fi

if printf '%s' "$target" | grep -Eq '(^|[^A-Za-z0-9_.-])\.env($|[^A-Za-z0-9_.-])'; then
  jq -n '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "deny",
    permissionDecisionReason: ".env holds secrets (AISSTREAM_API_KEY); Claude must not read or write it. Use .env.example for variable names."}}'
fi
exit 0
