#!/usr/bin/env bash
# Restart the app when it's running but has stopped responding.
#
# systemd already restarts the app when it crashes (Restart=always) and
# starts it on boot. This covers the other case: the process is alive but
# frozen, so systemd sees nothing wrong. A timer runs this every minute
# (see user-management-healthcheck.timer); after MAX_FAILS failed checks in
# a row it restarts the app and leaves a note the app logs to Papertrail
# when it comes back up.
#
# Usage: healthcheck.sh <systemd unit> <health URL> <app working directory>
# Installed to /usr/local/bin/user-management-healthcheck (root-owned), see
# deploy/README.md.
set -u

UNIT="${1:?systemd unit name required}"
URL="${2:?health URL required}"
WORKDIR="${3:?app working directory required}"
MAX_FAILS="${MAX_FAILS:-3}"
TIMEOUT="${TIMEOUT:-10}"
STATE="${STATE_DIR:-/run}/${UNIT}-healthcheck.fails"

log() { logger -t "${UNIT}-healthcheck" -- "$*"; echo "$*"; }

# Stopped on purpose, or still starting: leave it to systemd / the person.
if ! systemctl is-active --quiet "$UNIT"; then
    rm -f "$STATE"
    exit 0
fi

if err=$(curl --fail --silent --show-error --max-time "$TIMEOUT" --output /dev/null "$URL" 2>&1); then
    if [ -f "$STATE" ]; then
        log "Responding again after $(cat "$STATE") failed check(s)."
        rm -f "$STATE"
    fi
    exit 0
fi

fails=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
echo "$fails" > "$STATE"
log "Health check failed (${fails}/${MAX_FAILS}): ${err//$'\n'/ }"

if [ "$fails" -ge "$MAX_FAILS" ]; then
    log "Not responding for ${fails} checks in a row; restarting ${UNIT}."
    printf '%s, %s failed checks in a row\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$fails" > "${WORKDIR}/.healthcheck-restart"
    rm -f "$STATE"
    systemctl restart "$UNIT"
fi
exit 0
