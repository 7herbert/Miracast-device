#!/bin/bash
# Long-run soak / reconnect-stress monitor for castd.
#
#   sudo bash tools/soak_monitor.sh [HOURS] [INTERVAL_S] [CSV_PATH]
#
# Defaults: 72 hours, 30 s sampling, CSV under $HOME. Ctrl+C at any time
# prints the summary and exits cleanly.
#
# Two ways to use it:
#   - 72h soak: start it and walk away. Every sample records service
#     liveness, systemd restart count, memory of castd + uxplay, the
#     /health endpoint's FSM state, and new ERROR lines in the journal.
#     Anomalies are printed the moment they happen.
#   - Reconnect stress: start it with a short interval (e.g. `... 1 5`)
#     and manually cycle Windows/iPhone connects and disconnects. Every
#     failed recovery shows up as an anomaly (castd restarted, health
#     unreachable, uxplay missing while idle).
#
# Pass criteria for the 72h soak: zero restarts, zero health outages,
# castd RSS flat (no monotonic growth), anomaly count 0.
set -u

DURATION_HOURS="${1:-72}"
INTERVAL_S="${2:-30}"
CSV="${3:-$HOME/castd-soak-$(date +%Y%m%d-%H%M%S).csv}"
HEALTH_URL="http://127.0.0.1:8973/health"

start_ts=$(date +%s)
end_ts=$((start_ts + DURATION_HOURS * 3600))
errors_total=0
anomalies=0
prev_restarts=""

rss_kb() {
    awk '/VmRSS/{print $2; found=1} END{if(!found) print 0}' "/proc/$1/status" 2>/dev/null || echo 0
}

summary() {
    local elapsed=$(( $(date +%s) - start_ts ))
    echo
    echo "=== castd soak summary ==="
    echo "elapsed:            $((elapsed / 3600))h $((elapsed % 3600 / 60))m"
    echo "anomalies:          $anomalies"
    echo "systemd NRestarts:  ${prev_restarts:-unknown}"
    echo "journal ERROR lines seen: $errors_total"
    awk -F, 'NR>1 && $4+0 > m {m=$4} END {print "max castd RSS:      " m " kB"}' "$CSV"
    awk -F, 'NR>1 && $6+0 > m {m=$6} END {print "max uxplay RSS:     " m " kB"}' "$CSV"
    echo "full log:           $CSV"
}
trap 'summary; exit 0' INT TERM

echo "timestamp,active,nrestarts,castd_rss_kb,uxplay_alive,uxplay_rss_kb,health_state,heartbeat_age_s,errors_total,anomaly" > "$CSV"
echo "soak monitor running until $(date -d "@$end_ts" 2>/dev/null || date -r "$end_ts" 2>/dev/null); log: $CSV"

while [ "$(date +%s)" -lt "$end_ts" ]; do
    ts=$(date -Is)
    active=$(systemctl is-active castd 2>/dev/null)
    nrestarts=$(systemctl show castd -p NRestarts --value 2>/dev/null)
    mainpid=$(systemctl show castd -p MainPID --value 2>/dev/null)
    castd_rss=$(rss_kb "${mainpid:-0}")

    uxplay_pid=$(pgrep -x uxplay | head -1)
    if [ -n "$uxplay_pid" ]; then
        ux_alive=1
        ux_rss=$(rss_kb "$uxplay_pid")
    else
        ux_alive=0
        ux_rss=0
    fi

    health_json=$(curl -fsS --max-time 3 "$HEALTH_URL" 2>/dev/null || true)
    hstate=$(sed -n 's/.*"state": *"\([A-Z]*\)".*/\1/p' <<<"$health_json")
    hage=$(sed -n 's/.*"seconds_since_heartbeat": *\([0-9.]*\).*/\1/p' <<<"$health_json")

    new_errors=$(journalctl -u castd --since "-${INTERVAL_S} seconds" --no-pager 2>/dev/null \
        | grep -cE "ERROR|CRITICAL|Traceback")
    errors_total=$((errors_total + new_errors))

    anomaly=""
    [ "$active" != "active" ] && anomaly="${anomaly}castd_not_active;"
    if [ -n "$prev_restarts" ] && [ "$nrestarts" != "$prev_restarts" ]; then
        anomaly="${anomaly}castd_restarted;"
    fi
    [ -z "$hstate" ] && anomaly="${anomaly}health_unreachable;"
    # uxplay is intentionally stopped while a Miracast session owns the
    # display -- only flag it missing outside of that.
    if [ "$ux_alive" = 0 ] && [ "$hstate" != "MIRACAST" ]; then
        anomaly="${anomaly}uxplay_down;"
    fi

    if [ -n "$anomaly" ]; then
        anomalies=$((anomalies + 1))
        echo "[$ts] ANOMALY: $anomaly (state=$hstate errors_new=$new_errors)"
    fi

    echo "$ts,$active,$nrestarts,$castd_rss,$ux_alive,$ux_rss,$hstate,$hage,$errors_total,$anomaly" >> "$CSV"
    prev_restarts="$nrestarts"
    sleep "$INTERVAL_S"
done

summary
