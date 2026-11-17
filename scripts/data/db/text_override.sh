#!/usr/bin/env bash
# Precheck, null-out and REPACK of duplicate article_body text (migration 0014).
# Thin shell over `python -m episteme.data.db.text_override`, which connects as
# episteme_sys_admin (the table owner); the DB-mode guard applies.
#   text_override.sh precheck --partition NAME | --all   # read-only; run BEFORE 0014; exit 1 if any row would change
#   text_override.sh report                              # read-only sizes and sampled estimate
#   text_override.sh nullout --partition NAME [--batch-rows N] [--start-after ID,SOURCE]
#   text_override.sh nullout --partition NAME --order physical [--batch-blocks N] [--start-block B]
#   text_override.sh repack --partition NAME --yes-repack
# Tee the output to D:/episteme/_runlogs/. Refuses any server except 19beta3.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../_lib/common.sh
. "$HERE/../_lib/common.sh"
load_dotenv

case "${1:-}" in
    precheck|report|nullout|repack) ;;
    *) echo "usage: $0 precheck|report|nullout|repack [options]" >&2; exit 2 ;;
esac
require_env EPISTEME_ACTOR EPISTEME_SYS_ADMIN_PASSWORD

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    for c in "$HERE/../../../.venv/Scripts/python.exe" "$HERE/../../../.venv/bin/python" python; do
        command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }
    done
fi
[ -n "$PY" ] || die "python not found (set PYTHON=/path/to/python)"

"$PY" -m episteme.data.db.text_override "$@"
exit $?
