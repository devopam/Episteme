#!/usr/bin/env bash
# Rotate the episteme._audit JSONL mirror: gzip mirror files older than ~30
# days in place (find's -mtime +30 is a floor comparison, so a file actually
# becomes eligible at 31 days -- errs safe), then best-effort `chattr +a`
# today's still-open mirror file (Linux only; a silent no-op everywhere
# else, including Git-for-Windows bash and macOS, which have no chattr).
#
# No DB connection and no EPISTEME_ACTOR requirement. When ops_dir isn't
# passed explicitly, resolving the default calls into Python
# (episteme.audit_trail._mirror_dir(), which needs only settings
# resolution -- no DB, no actor) using the same PY-discovery pattern
# scripts/data/verify_audit_trail.sh uses. This is deliberate, not
# incidental: config.py's EPISTEME_PROCESSED_ROOT/EPISTEME_DATA_ROOT
# fallback logic must not be reimplemented in bash, or the two can silently
# drift apart.
#
#   bash scripts/data/rotate_audit_logs.sh [ops_dir]
#
# ops_dir default: episteme.audit_trail._mirror_dir(), i.e.
#   get_settings().processed_root / "_ops" / "_audit", where processed_root
#   is EPISTEME_PROCESSED_ROOT if set, else <EPISTEME_DATA_ROOT (default
#   ".")>/02_processed.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib/common.sh
. "$HERE/_lib/common.sh"
load_dotenv

OPS_DIR="${1:-}"
if [ -z "$OPS_DIR" ]; then
    # Same PY-discovery pattern as verify_audit_trail.sh: prefer the project
    # venv's interpreter -- a plain `python` on PATH cannot import episteme.
    PY="${PYTHON:-}"
    if [ -z "$PY" ]; then
        for c in "$HERE/../../.venv/Scripts/python.exe" "$HERE/../../.venv/bin/python" python; do
            command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }
        done
    fi
    [ -n "$PY" ] || die "python not found (set PYTHON=/path/to/python, or pass ops_dir explicitly)"
    OPS_DIR="$("$PY" -c 'from episteme.audit_trail import _mirror_dir; print(_mirror_dir())')" \
        || die "failed to resolve default ops_dir via episteme.audit_trail._mirror_dir()"
fi

if [ ! -d "$OPS_DIR" ]; then
    log INFO "rotate_audit_logs: $OPS_DIR does not exist -- nothing to rotate"
    exit 0
fi

log INFO "rotate_audit_logs: scanning $OPS_DIR for mirror files older than 30 days"

rotated=0
while IFS= read -r -d '' f; do
    # chattr +a (below, applied to a previous day's file once it became
    # "today's" file) blocks the unlink() gzip needs to replace the file in
    # place. Best-effort clear it first -- same guard as the +a call -- or a
    # 30-day-old append-only file would fail gzip forever. A per-file gzip
    # failure is a WARN, not a `die`: one bad file must not stop every other
    # eligible file in this run from rotating, or "safe to run daily from
    # cron" (above) would not be true.
    if command -v chattr >/dev/null 2>&1; then
        chattr -a "$f" 2>/dev/null || true
    fi
    if gzip "$f"; then
        rotated=$((rotated + 1))
        log INFO "rotate_audit_logs: gzipped $f"
    else
        log WARN "rotate_audit_logs: gzip failed for $f -- left unrotated, continuing"
    fi
done < <(find "$OPS_DIR" -maxdepth 1 -name 'audit-*.jsonl' -mtime +30 -print0)

log INFO "rotate_audit_logs: $rotated file(s) rotated"

# Best-effort: mark today's still-open mirror file append-only on Linux. This
# must never fail the script -- chattr doesn't exist on Windows/macOS, and
# even on Linux it can fail (non-ext filesystem, no CAP_LINUX_IMMUTABLE).
if command -v chattr >/dev/null 2>&1; then
    chattr +a "$OPS_DIR/audit-$(date -u +%Y%m%d).jsonl" 2>/dev/null || true
fi

exit 0
