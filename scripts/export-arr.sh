#!/usr/bin/env bash
# Pull custom formats, quality profiles, quality definitions, naming, and
# media management config from Radarr and Sonarr. Writes raw JSON into
# $EXPORT_DIR/{radarr,sonarr}/ for use as source data when authoring
# ops/1.initial.sql.

set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"

if [[ ! -f .env ]]; then
    echo "error: .env not found. Copy .env.example to .env and fill it in." >&2
    exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

: "${RADARR_URL:?RADARR_URL not set in .env}"
: "${RADARR_API_KEY:?RADARR_API_KEY not set in .env}"
: "${SONARR_URL:?SONARR_URL not set in .env}"
: "${SONARR_API_KEY:?SONARR_API_KEY not set in .env}"
: "${EXPORT_DIR:=exports}"

command -v curl >/dev/null || { echo "curl required" >&2; exit 1; }
command -v jq   >/dev/null || { echo "jq required"   >&2; exit 1; }

endpoints=(
    "customformat"
    "qualityprofile"
    "qualitydefinition"
    "config/naming"
    "config/mediamanagement"
)

fetch() {
    local arr=$1 base_url=$2 api_key=$3
    local out_dir="$EXPORT_DIR/$arr"
    mkdir -p "$out_dir"

    # Quick connectivity + auth probe.
    local status
    status=$(curl -sS -o /dev/null -w '%{http_code}' \
        -H "X-Api-Key: $api_key" "$base_url/api/v3/system/status" || true)
    if [[ "$status" != "200" ]]; then
        echo "  ! $arr unreachable or unauthorized (HTTP $status at $base_url/api/v3/system/status)" >&2
        return 1
    fi

    for endpoint in "${endpoints[@]}"; do
        local filename="${endpoint//\//-}.json"
        local target="$out_dir/$filename"
        local url="$base_url/api/v3/$endpoint"
        local code
        code=$(curl -sS -H "X-Api-Key: $api_key" -o "$target.tmp" \
            -w '%{http_code}' "$url" || true)
        if [[ "$code" == "200" ]]; then
            jq '.' "$target.tmp" >"$target" && rm -f "$target.tmp"
            local count
            count=$(jq 'if type=="array" then length else 1 end' "$target")
            printf '  %-25s %3s items  -> %s\n' "$endpoint" "$count" "$target"
        else
            echo "  ! $endpoint failed (HTTP $code) -> $url" >&2
            rm -f "$target.tmp"
        fi
    done
}

echo "Exporting from Radarr ($RADARR_URL)"
fetch radarr "$RADARR_URL" "$RADARR_API_KEY"

echo
echo "Exporting from Sonarr ($SONARR_URL)"
fetch sonarr "$SONARR_URL" "$SONARR_API_KEY"

echo
echo "Done. Raw JSON in $EXPORT_DIR/"
