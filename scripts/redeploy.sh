#!/usr/bin/env bash
# Rebuild the emAIl image, apply migrations, and recreate the app containers. The database is not touched.
# Quiet by default: full output goes to .logs/redeploy.log (view with `emaild buildlog`).
#   bash scripts/redeploy.sh        quiet
#   bash scripts/redeploy.sh -v     show everything
set -uo pipefail
cd "$(dirname "$0")/.."
mkdir -p .logs
LOG=.logs/redeploy.log
VERBOSE=0; [ "${1:-}" = "-v" ] && VERBOSE=1
: > "$LOG"

step() {  # step "label" command...
  local label="$1"; shift
  printf "  %-28s" "$label"
  local start=$SECONDS
  echo "===== $label: $* =====" >> "$LOG"
  if [ $VERBOSE = 1 ]; then
    echo; "$@" 2>&1 | tee -a "$LOG"; local rc=${PIPESTATUS[0]}
  else
    "$@" >> "$LOG" 2>&1; local rc=$?
  fi
  if [ $rc -eq 0 ]; then
    printf "✓  %ss\n" $((SECONDS - start))
  else
    printf "✗  (exit %s)\n\n--- last 25 lines of %s ---\n" "$rc" "$LOG"
    tail -n 25 "$LOG"
    echo "--- full log: emaild buildlog ---"
    exit $rc
  fi
}

echo "emAIl redeploy"
step "building image"            podman compose build
step "stopping app containers"   podman rm -f emaild_api_1 emaild_worker_1 emaild_mcp_1 emaild_telegram_1
step "applying migrations"       podman compose run --rm --no-deps api migrate
grep -h "^applied:" "$LOG" | tail -1 | sed 's/^/    /'
step "starting app containers"   podman compose up -d --no-deps api worker mcp telegram
sleep 4
podman ps --filter name=emaild --format "    {{.Names}}\t{{.Status}}"
