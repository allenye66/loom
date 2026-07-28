#!/bin/sh
# loom: capture Claude Code's statusLine JSON (incl. subscription rate_limits) so the loom UI
# can show live 5h/weekly usage. Claude Code pipes the JSON on stdin and uses our stdout as the
# TUI status line. We persist the JSON atomically and echo a compact usage string.
DIR="${LOOM_HOME:-$HOME/.loom}"
mkdir -p "$DIR" 2>/dev/null
in=$(cat)
printf '%s' "$in" > "$DIR/usage.json.tmp" 2>/dev/null && mv -f "$DIR/usage.json.tmp" "$DIR/usage.json" 2>/dev/null
# Compact usage for the TUI status line (best-effort; only if jq is present).
printf '%s' "$in" | jq -r '[ ((.rate_limits.five_hour.used_percentage // empty) | "5h \(.|floor)%"), ((.rate_limits.seven_day.used_percentage // empty) | "7d \(.|floor)%") ] | join(" · ")' 2>/dev/null
