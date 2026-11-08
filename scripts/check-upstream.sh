#!/usr/bin/env bash
# Compare new upstream commits with the `patched` branch. Only fetches: never merges or pulls.
#
#   check-upstream.sh            print the report
#   check-upstream.sh --notify   also message the owner on Telegram, once per upstream commit
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."

UPSTREAM_REF=${UPSTREAM_REF:-upstream/main}
OURS=patched
STATE=${CCBOT_DIR:-$HOME/.ccbot}/upstream_checked
ENV_FILE=${CCBOT_DIR:-$HOME/.ccbot}/.env

[ "$UPSTREAM_REF" = upstream/main ] && git fetch -q upstream main

up=$(git rev-parse "$UPSTREAM_REF")
base=$(git merge-base "$OURS" "$up")
count=$(git rev-list --count "$base..$up")
if [ "$count" -eq 0 ]; then
    echo "Upstream has nothing new (at ${up:0:8})."
    exit 0
fi

theirs=$(git diff --name-only "$base" "$up" | sort)
ours=$(git diff --name-only "$base" "$OURS" | sort)
overlap=$(comm -12 <(echo "$theirs") <(echo "$ours"))
if conflicts=$(git merge-tree --write-tree --name-only "$OURS" "$up"); then
    verdict="Trial merge: clean."
else
    verdict="Trial merge: CONFLICTS in
$(echo "$conflicts" | sed -n '2,/^$/p' | sed '/^$/d; s/^/  /')"
fi

report="ccbot upstream has $count new commit(s), not merged:
$(git log --format='  %h %s' "$base..$up" | cut -c1-90 | head -20)

Files both sides changed:
$(if [ -n "$overlap" ]; then echo "$overlap" | sed 's/^/  /'; else echo "  (none)"; fi)

$verdict"
echo "$report"

if [ "${1:-}" = --notify ] && [ "$(cat "$STATE" 2>/dev/null)" != "$up" ]; then
    token=$(sed -n 's/^TELEGRAM_BOT_TOKEN=//p' "$ENV_FILE" | tr -d "\"'")
    owner=$(sed -n 's/^ALLOWED_USERS=//p' "$ENV_FILE" | tr -d "\"'" | cut -d, -f1)
    # The token goes on stdin so it stays out of the process list
    curl -sS -m 20 -o /dev/null --fail --config - \
        --data-urlencode "chat_id=$owner" --data-urlencode "text=$report" \
        <<<"url = \"https://api.telegram.org/bot$token/sendMessage\""
    echo "$up" >"$STATE"
fi
