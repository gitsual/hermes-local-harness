#!/usr/bin/env bash
# Recorded by scripts/render-assets.sh: three requests against the fictional
# home from demo/world.py, run through the real recall helper. Paths under the
# fictional HOME are shown as ~, the way a shell prompt would.
set -euo pipefail
helper="$(dirname "$0")/../skills/jarvis/jarvis-recall/scripts/jarvis-episode-recall.py"

type_out() {
  printf '\033[38;2;100;182;231m❯\033[0m '
  local s=$1 i
  for ((i = 0; i < ${#s}; i++)); do printf '%s' "${s:i:1}"; sleep 0.03; done
  printf '\n'; sleep 0.4
}

ask() {
  type_out "jarvis-episode-recall.py \"$1\""
  python3 "$helper" "$1" | sed "s|$HOME|~|g" | grep -E '^(FOUND|MODE|REASON|TITLE|ACTION_ROOT|KEY_PATH_1|NEXT)='
  echo; sleep 2.2
}

ask "continue with the stripe webhook"
ask "resume trailapp"
ask "pick up where we left off yesterday"
