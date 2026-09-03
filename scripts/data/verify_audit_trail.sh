#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/_lib/common.sh" 2>/dev/null || true
exec "${PYTHON:-python}" -c "
import sys
from episteme.data.db.connection import connection
from episteme.audit_trail import verify
with connection() as c:
    bad = verify(c)
print('audit chain OK' if not bad else f'CHAIN BROKEN at seq {[b[\"seq\"] for b in bad]}')
sys.exit(0 if not bad else 1)
"
