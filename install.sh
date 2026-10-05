#!/usr/bin/env bash
# Install the scope guard plugin and the recall skill into a Hermes home.
# It never edits config.yaml and never patches Hermes source: those steps are
# printed at the end so you can review them first.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
hermes_home="${HERMES_HOME:-$HOME/.hermes}"
stamp="$(date +%Y%m%d-%H%M%S)"
with_soul=0

for arg in "$@"; do
  case "$arg" in
    --with-soul) with_soul=1 ;;
    -h|--help)
      echo "usage: $0 [--with-soul]   (HERMES_HOME defaults to ~/.hermes)"
      exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

[[ -d "$hermes_home" ]] || { echo "Hermes home not found: $hermes_home" >&2; exit 1; }

install_dir() {
  local src="$1" dest="$2"
  if [[ -e "$dest" ]]; then
    mv "$dest" "$dest.bak-$stamp"
    echo "backup: $dest.bak-$stamp"
  fi
  mkdir -p "$(dirname "$dest")"
  cp -r "$src" "$dest"
  echo "installed: $dest"
}

install_dir "$repo/plugins/jarvis-scope-guard" "$hermes_home/plugins/jarvis-scope-guard"
install_dir "$repo/skills/jarvis/jarvis-recall" "$hermes_home/skills/jarvis/jarvis-recall"
chmod +x "$hermes_home/skills/jarvis/jarvis-recall/scripts/"*

if (( with_soul )); then
  if [[ -e "$hermes_home/SOUL.md" ]]; then
    cp "$hermes_home/SOUL.md" "$hermes_home/SOUL.md.bak-$stamp"
    echo "backup: $hermes_home/SOUL.md.bak-$stamp"
  fi
  cp "$repo/soul/SOUL.example.md" "$hermes_home/SOUL.md"
  echo "installed: $hermes_home/SOUL.md"
fi

cat <<EOF

Next steps (manual, review before applying):
  1. Merge config/hermes.fragment.yaml into $hermes_home/config.yaml
  2. Install config/ollama/20-local-agent.conf as an Ollama systemd drop-in
  3. Only if you use gpt-oss: cd $hermes_home/hermes-agent && git apply $repo/patches/terminal-notify-anyof.patch
  4. systemctl --user restart hermes-gateway   (reload does not pick up Python changes)
  5. Check: hermes prompt-size   and   ollama ps   (CONTEXT column must say 65536)
EOF
