#!/usr/bin/env bash
# Rotate the episteme._audit JSONL mirror: gzip mirror files older than 30
# days in place, then best-effort `chattr +a` today's still-open mirror file
# (Linux only; a silent no-op everywhere else, including Git-for-Windows
# bash and macOS, which have no chattr).
#
# Filesystem-only -- no DB connection, no episteme import. Safe to run daily
# from cron/Task Scheduler.
#
#   bash scripts/data/rotate_audit_logs.sh [ops_dir]
#
# ops_dir default: ${EPISTEME_PROCESSED_ROOT:-./02_processed}/_ops/_audit
# -- must match episteme.audit_trail._mirror_dir() exactly.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib/common.sh
. "$HERE/_lib/common.sh"
load_dotenv

OPS_DIR="${1:-${EPISTEME_PROCESSED_ROOT:-./02_processed}/_ops/_audit}"

if [ ! -d "$OPS_DIR" ]; then
    log INFO "rotate_audit_logs: $OPS_DIR does not exist -- nothing to rotate"
    exit 0
fi

log INFO "rotate_audit_logs: scanning $OPS_DIR for mirror files older than 30 days"

rotated=0
while IFS= read -r -d '' f; do
    gzip "$f" || die "gzip failed for $f"
    rotated=$((rotated + 1))
    log INFO "rotate_audit_logs: gzipped $f"
done < <(find "$OPS_DIR" -maxdepth 1 -name 'audit-*.jsonl' -mtime +30 -print0)

log INFO "rotate_audit_logs: $rotated file(s) rotated"

# Best-effort: mark today's still-open mirror file append-only on Linux. This
# must never fail the script -- chattr doesn't exist on Windows/macOS, and
# even on Linux it can fail (non-ext filesystem, no CAP_LINUX_IMMUTABLE).
if command -v chattr >/dev/null 2>&1; then
    chattr +a "$OPS_DIR/audit-$(date -u +%Y%m%d).jsonl" 2>/dev/null || true
fi

exit 0
