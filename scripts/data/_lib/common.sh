#!/usr/bin/env bash
# Shared shell helpers for the Episteme data pipeline (roadmap §4.6 frozen set).
#
# Source it, don't execute it:
#   . "$(dirname "${BASH_SOURCE[0]}")/../_lib/common.sh"
#
# SP1-β landed log / die / require_env / load_dotenv / write_sync_stamp fully.
# SP3 added the fetch engine (resolve_dest, size_match_skip, discover_manifest,
# http_fetch/aria2_fetch, s3_sync/aws_sync, cap_urls) and the sources.env load-order block.

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
    if [ "${EPISTEME_DRY_RUN:-0}" = "1" ]; then
        log INFO "dry-run: not writing sync stamp under $dir"
        return 0
    fi
    mkdir -p "$dir"
    date -u +%Y-%m-%dT%H:%M:%SZ > "$dir/last_sync_utc.txt"
    log INFO "write_sync_stamp: $dir/last_sync_utc.txt"
}

resolve_dest() { # resolve_dest SOURCE [SUBPATH] -> <raw root>/SOURCE[/SUBPATH]
    local src="$1" sub="${2:-}"
    local root="${EPISTEME_RAW_ROOT:-${EPISTEME_DATA_ROOT:-.}/01_raw}"
    printf '%s\n' "$root/$src${sub:+/$sub}"
}

size_match_skip() { # size_match_skip LOCAL_PATH URL — exit 0 => caller SKIPS this url (local matches remote size)
    local local_path="$1" url="$2"
    [ -s "$local_path" ] || return 1
    command -v curl >/dev/null 2>&1 || return 1   # can't verify -> fetch
    local remote_len local_len
    remote_len="$(curl -sSIL --connect-timeout 20 --max-time 90 "$url" 2>/dev/null \
        | tr -d '\r' | awk 'tolower($1)=="content-length:"{print $2}' | tail -n1)"
    local_len="$(wc -c < "$local_path" | tr -d '[:space:]')"
    [ -n "$remote_len" ] && [ "$remote_len" = "$local_len" ]
}

_scrape_listing() { # _scrape_listing BASE_URL -> cleaned filenames from an HTML/autoindex dir listing, one per line, sorted unique
    local base="$1" listing
    listing="$(curl -sSL --fail --connect-timeout 30 --max-time 120 "$base/" 2>/dev/null)" || return 1
    printf '%s\n' "$listing" \
        | grep -oE 'href="[^"]+"' | sed -E 's/href="//; s/"$//' \
        | sed 's/[?#].*$//' | grep -vE '^(\?|/|\.\./|#|https?:|$)' | sort -u
}

discover_manifest() { # discover_manifest BASE_URL REGEX... -> FIRST filename matching each regex, one line each, in regex order (no line for a non-match)
    local base="$1"; shift
    local names
    names="$(_scrape_listing "$base")" || { log ERROR "discover_manifest: cannot list $base/"; return 1; }
    local re
    for re in "$@"; do
        printf '%s\n' "$names" | grep -E "$re" | head -n1 || true
    done
}

list_manifest() { # list_manifest BASE_URL [FILTER_ERE] -> EVERY listed filename (optional ERE filter), sorted unique, NO head
    local base="$1" filt="${2:-.}"
    local names
    names="$(_scrape_listing "$base")" || { log ERROR "list_manifest: cannot list $base/"; return 1; }
    printf '%s\n' "$names" | grep -E "$filt" || true
}

http_fetch() { # http_fetch DEST_DIR [URL...]  (URLs also on stdin; line may be `URL<TAB>relpath`)
    local dest_dir="$1"; shift
    local -a urls=()
    if [ "$#" -gt 0 ]; then urls=("$@"); else mapfile -t urls; fi
    [ "${#urls[@]}" -gt 0 ] || { log INFO "http_fetch: nothing to fetch"; return 0; }

    if [ "${EPISTEME_DRY_RUN:-0}" = "1" ]; then
        # dry-run creates nothing on disk — not even the dest dir. Accumulate the
        # listing and emit it in one write: `log` forks `date` per call, and a
        # big source (bookshelf ~9.5k urls) would otherwise pay that per line.
        local line url rel out=""
        local have_curl=0; command -v curl >/dev/null 2>&1 && have_curl=1
        for line in "${urls[@]}"; do
            url="${line%%$'\t'*}"; rel="${line#*$'\t'}"; [ "$rel" = "$line" ] && rel="$(basename "$url")"
            out+="DRY: would fetch $url -> $dest_dir/$rel"$'\n'
            if [ "$have_curl" = "1" ] && [ -s "$dest_dir/$rel" ] && size_match_skip "$dest_dir/$rel" "$url"; then
                out+="DRY:   (already size-matched, would skip)"$'\n'
            fi
        done
        printf '%s' "$out" >&2
        log INFO "http_fetch: dry-run — ${#urls[@]} url(s) listed, nothing transferred"
        return 0
    fi
    mkdir -p "$dest_dir"

    if command -v aria2c >/dev/null 2>&1; then
        local aria_in rc; aria_in="$(mktemp)"
        local line url rel
        for line in "${urls[@]}"; do
            url="${line%%$'\t'*}"; rel="${line#*$'\t'}"; [ "$rel" = "$line" ] && rel="$(basename "$url")"
            printf '%s\n  dir=%s\n  out=%s\n' "$url" "$dest_dir/$(dirname "$rel")" "$(basename "$rel")" >> "$aria_in"
        done
        aria2c -c -x16 -s16 -j"${EPISTEME_DOWNLOAD_THREADS:-4}" \
            --max-tries=15 --retry-wait=30 --auto-file-renaming=false \
            --allow-overwrite=true --file-allocation=none --console-log-level=warn \
            -i "$aria_in"
        rc=$?; rm -f "$aria_in"; return "$rc"
    fi

    command -v curl >/dev/null 2>&1 || die "no download tool: install aria2c (or curl for the slow path)"
    log WARN "aria2c not found — sequential curl fallback (degraded throughput)"
    local line url rel
    for line in "${urls[@]}"; do
        url="${line%%$'\t'*}"; rel="${line#*$'\t'}"; [ "$rel" = "$line" ] && rel="$(basename "$url")"
        mkdir -p "$dest_dir/$(dirname "$rel")"
        curl -fL -C - --retry 15 --retry-delay 30 -o "$dest_dir/$rel" "$url" || die "curl failed: $url"
    done
}

cap_urls() { # cap_urls N — echo the first N lines of stdin; passthrough if N empty/0/non-numeric
    local n="${1:-}"
    case "$n" in
        ''|0|*[!0-9]*) cat ;;
        # Drain the tail after the first N lines so the upstream producer in a
        # `producer | cap_urls N | http_fetch` pipeline never takes SIGPIPE —
        # under the `pipefail` this file sets, a SIGPIPE'd producer would make
        # the whole pipeline exit 141. Output is still exactly the first N lines.
        *) { head -n "$n"; cat >/dev/null; } ;;
    esac
}

aria2_fetch() { http_fetch "$@"; }   # roadmap §4.6 name kept as an alias

s3_sync() { # s3_sync S3_URI DEST_DIR [-- extra passthrough args]
    # NOTE: passthrough args after `--` are forwarded to awscli only; the s5cmd
    # backend ignores them (s5cmd's --exclude is a pre-subcommand global with
    # different glob semantics). A wrapper that needs exact filtering on s5cmd
    # must narrow the S3_URI instead.
    local uri="$1" dest="$2"; shift 2 || true
    local -a extra=(); [ "${1:-}" = "--" ] && { shift; extra=("$@"); }
    if [ "${EPISTEME_DRY_RUN:-0}" = "1" ]; then
        # dry-run creates nothing on disk — not even the dest dir.
        # A failed dry-run probe must be visible, not swallowed — otherwise a
        # rejected flag looks like a clean "nothing to sync".
        if command -v s5cmd >/dev/null 2>&1; then
            s5cmd --no-sign-request cp --dry-run "$uri/*" "$dest/" \
                || log WARN "s3_sync: s5cmd dry-run probe failed for $uri (check --dry-run flag position on this s5cmd version)"
        elif command -v aws >/dev/null 2>&1; then
            aws s3 sync "$uri" "$dest" --no-sign-request --dryrun "${extra[@]}" \
                || log WARN "s3_sync: aws dry-run probe failed for $uri"
        else
            log INFO "DRY: would sync $uri -> $dest"
        fi
        return 0
    fi
    mkdir -p "$dest"
    if command -v s5cmd >/dev/null 2>&1; then
        s5cmd --no-sign-request sync "$uri/*" "$dest/"
    elif command -v aws >/dev/null 2>&1; then
        aws s3 sync "$uri" "$dest" --no-sign-request "${extra[@]}"
    else
        die "no S3 tool: install s5cmd (fast) or awscli"
    fi
}
aws_sync() { s3_sync "$@"; }   # roadmap §4.6 name kept as an alias
