#!/usr/bin/env bash
set -euo pipefail

[[ $# -eq 1 ]] || { echo "usage: $0 FILE.json" >&2; exit 2; }
file="$1"
[[ -f "$file" ]] || { echo "not found: $file" >&2; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "jq unavailable" >&2; exit 3; }

echo "===== FILE ====="
printf 'path: %s\n' "$(realpath "$file")"
printf 'bytes: %s\n' "$(stat -c '%s' "$file")"
printf 'human: %s\n' "$(du -h "$file" | cut -f1)"

echo
echo "===== ROOT ====="
root="$(jq -r 'type' "$file")"
printf 'type: %s\n' "$root"

case "$root" in
array)
    printf 'length: '; jq -r 'length' "$file"
    echo
    echo "===== FIRST ITEM SHAPE ====="
    jq '.[0] |
        if type == "object" then
          to_entries[:80] |
          map({
            key: .key,
            type: (.value | type),
            size: (
              if ((.value|type)=="array") or ((.value|type)=="object") or ((.value|type)=="string")
              then (.value|length) else null end
            )
          })
        else
          {type:type, preview:(if type=="string" then .[0:300] else . end)}
        end' "$file"
    ;;
object)
    echo
    echo "===== TOP-LEVEL SHAPE ====="
    jq 'to_entries[:100] |
        map({
          key:.key,
          type:(.value|type),
          size:(
            if ((.value|type)=="array") or ((.value|type)=="object") or ((.value|type)=="string")
            then (.value|length) else null end
          )
        })' "$file"
    ;;
*)
    jq 'if type=="string" then .[0:500] else . end' "$file"
    ;;
esac
