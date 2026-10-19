#!/usr/bin/env bash
# Daily keep-alive for the GHL internal-API session (refresh token lasts 30 days, rotates on use).
out=$("$HOME/.local/bin/ghl-session" refresh 2>&1)
rc=$?
echo "$(date -u +%FT%TZ) rc=$rc $(echo "$out" | tr -d '\n' | grep -o '"refresh_expires_in_days": [0-9.]*')" >> "$HOME/logs/ghl-session-keepalive.log"
if [ $rc -ne 0 ]; then
  "$HOME/Scripts/notify.sh" "GHL session refresh FAILED on a1 — workflow building via ghl CLI is down. Fix: log into app.gohighlevel.com in CloakBrowser, then run: ghl-session from-browser" >/dev/null 2>&1
fi
exit $rc
