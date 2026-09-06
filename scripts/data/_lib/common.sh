#!/usr/bin/env bash
# Shared shell helpers for the Episteme data pipeline (roadmap §4.6 frozen set).
#
# Source it, don't execute it:
#   . "$(dirname "${BASH_SOURCE[0]}")/../_lib/common.sh"
#
# SP1-β lands log / die / require_env / load_dotenv / write_sync_stamp fully.
# discover_manifest / size_match_skip / aria2_fetch / aws_sync are minimal
# working bodies that SP3 hardens — each announces itself as a stub.

# pipefail is safe to inherit; -e / -u are the caller's choice, not ours.
set -o pipefail

# Endpoint defaults — single source of truth (SP3: scripts/data/_lib/sources.env).
# Load order everywhere (last wins): sources.env -> ./.env -> real environment.
# Capture the pre-existing environment FIRST (names for load_dotenv's guard, and a
# full snapshot), then source sources.env with `set -a` so every KEY=value is
# exported at *source* time. Re-applying the snapshot afterwards guarantees a var
# already present in the real environment is never clobbered by sources.env;
# load_dotenv() then lets ./.env override a sources.env value but not a real one.
# Capture once per process: a second `. common.sh` (e.g. an _lib helper that
# itself sources this file) must NOT recapture after sources.env is exported, or
# every .env override of an endpoint var silently stops working.
[ -n "${_COMMON_REAL_ENV_KEYS:-}" ] || _COMMON_REAL_ENV_KEYS=" $(compgen -e | tr '\n' ' ') "
_COMMON_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$_COMMON_LIB_DIR/sources.env" ]; then
    _common_real_env_snapshot="$(export -p)"
    set -a
    # shellcheck disable=SC1091
    . "$_COMMON_LIB_DIR/sources.env"
    set +a
    eval "$_common_real_env_snapshot"   # restore real-env values; keep new keys
    unset _common_real_env_snapshot
fi

log() { # log LEVEL MSG...
    local level="$1"; shift
    printf '%s [%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$level" "$*" >&2
}

die() { # die MSG [CODE]
    log ERROR "$1"
    exit "${2:-1}"
}

require_env() { # require_env VAR [VAR...] — fail fast if any is unset/empty
    local missing=0 v
    for v in "$@"; do
        if [ -z "${!v:-}" ]; then
            log ERROR "required environment variable not set: $v"
            missing=1
        fi
    done
    [ "$missing" -eq 0 ] || die "missing required environment variable(s)" 2
}

_common_repo_root() { # nearest ancestor containing pyproject.toml
    local dir="${1:-$PWD}"
    dir="$(cd "$dir" 2>/dev/null && pwd)" || return 1
    while [ -n "$dir" ] && [ "$dir" != "/" ]; do
        [ -f "$dir/pyproject.toml" ] && { printf '%s\n' "$dir"; return 0; }
        dir="$(dirname "$dir")"
    done
    return 1
}

load_dotenv() { # source <repo-root>/.env if present; values already in the
                # environment are NOT overwritten (real env wins).
    local root env_file
    root="$(_common_repo_root "$(dirname "${BASH_SOURCE[0]}")")" \
        || root="$(_common_repo_root)" \
        || { log INFO "load_dotenv: no pyproject.toml ancestor, skipping"; return 0; }
    env_file="$root/.env"
    [ -f "$env_file" ] || { log INFO "load_dotenv: no $env_file, skipping"; return 0; }
    local line key val
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"                            # tolerate CRLF .env
        case "$line" in
            ''|'#'*) continue ;;
        esac
        [ "${line#*=}" = "$line" ] && continue          # no '='
        key="${line%%=*}"
        key="${key#"${key%%[![:space:]]*}"}"            # ltrim
        key="${key%"${key##*[![:space:]]}"}"            # rtrim
        # real pre-script env wins — but only if it actually had a VALUE. An
        # exported-but-empty var (`docker run -e PGHOST`) must not block .env;
        # this matches config.py _get(), which treats "" as absent. The
        # export -p restore above already put the real value back, so ${!key}
        # here reads the real-env value, not the sources.env one.
        case "${_COMMON_REAL_ENV_KEYS:- }" in
            *" $key "*) [ -n "${!key:-}" ] && continue ;;
        esac
        val="${line#*=}"
        case "$val" in                                  # strip one matching quote pair
            \"*\") val="${val#\"}"; val="${val%\"}" ;;
            \'*\') val="${val#\'}"; val="${val%\'}" ;;
        esac
        export "$key=$val"
    done < "$env_file"
    log INFO "load_dotenv: loaded $env_file"
}

write_sync_stamp() { # write_sync_stamp DIR
    local dir="$1"
    [ -n "$dir" ] || die "write_sync_stamp: DIR required"
    mkdir -p "$dir"
    date -u +%Y-%m-%dT%H:%M:%SZ > "$dir/last_sync_utc.txt"
    log INFO "write_sync_stamp: $dir/last_sync_utc.txt"
}

discover_manifest() { # discover_manifest URL DEST
    log WARN "discover_manifest is a stub (SP3 will harden): URL=${1:-} DEST=${2:-}"
    return 0
}

size_match_skip() { # size_match_skip FILE URL — exit 0 to skip a re-download
    log WARN "size_match_skip is a stub (SP3 will harden): FILE=${1:-} URL=${2:-}"
    return 1  # until SP3 adds the HEAD/Content-Length check, never skip
}

aria2_fetch() { # aria2_fetch URL_LIST DEST
    log WARN "aria2_fetch is a stub (SP3 will harden): URL_LIST=${1:-} DEST=${2:-}"
    command -v aria2c >/dev/null 2>&1 || log WARN "aria2_fetch: aria2c not installed"
    return 0
}

aws_sync() { # aws_sync S3_URI DEST [ARGS...]
    log WARN "aws_sync is a stub (SP3 will harden): S3_URI=${1:-} DEST=${2:-}"
    command -v aws >/dev/null 2>&1 || log WARN "aws_sync: aws cli not installed"
    return 0
}
