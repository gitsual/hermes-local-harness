#!/usr/bin/env bash
# Regenerate every rendered asset under assets/ from its source and the palette
# in assets/theme.conf.
#
#   scripts/render-assets.sh            # logos + gate diagram + both gifs
#   scripts/render-assets.sh logos      # only the logos
#
# The mark is assets/brand/emblem-flux.png, an emblem generated with a local
# diffusion model (Flux dev) from the palette's own colours; everything around
# it (wordmark, diagram, terminal theme) is derived from theme.conf at render
# time. The gifs record real helper and plugin output against the fictional
# home that demo/world.py builds, never a real history.
# Needs: magick, rsvg-convert, asciinema, agg, python3.
set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
source assets/theme.conf
what=${1:-all}

font_pick() {  # first installed font of the list
  local f
  for f in "$@"; do
    if magick -list font 2>/dev/null | grep -q "Font: $f\$"; then echo "$f"; return; fi
  done
  echo Liberation-Serif
}

logos() {
  echo "-- logos"
  local emblem=assets/brand/emblem-flux.png serif mono
  serif=$(font_pick Noto-Serif-Regular Noto-Serif-Display-Regular Liberation-Serif)
  mono=$(font_pick JetBrainsMono-NF-Regular JetBrainsMono-Regular DejaVu-Sans-Mono)
  # The generated image is a medallion on a lighter square: keep the outer ring
  # and drop everything outside it (no square corners on any page colour).
  local w h r ring; ring=$(mktemp --suffix=.png)
  w=$(magick identify -format "%w" "$emblem"); h=$(magick identify -format "%h" "$emblem")
  r=$((w * 43 / 100))
  magick "$emblem" \( -size "${w}x${h}" xc:black -fill white \
      -draw "circle $((w / 2)),$((h / 2)) $((w / 2)),$((h / 2 - r))" \) \
    -alpha off -compose CopyOpacity -composite -compose Over -trim +repage \
    -bordercolor none -border 4% "$ring"
  magick "$ring" -resize 360x360 assets/logo-mark.png
  wordmark() {  # <fg> <accent> <out>
    magick \( "$ring" -resize 280x280 \) \
      \( -background none -fill "#$1" -font "$serif" -pointsize 112 label:"hermes·local" \
         \( -background none -fill "#$2" -font "$mono" -pointsize 25 -kerning 4 \
            label:"A HARNESS THAT KEEPS LOCAL AGENTS HONEST" \) \
         -gravity west -append \) \
      -gravity center -background none +smush 36 \
      -bordercolor none -border 24x16 +repage "$3"
  }
  wordmark "$COLOR_FG" "$COLOR_ACCENT" assets/logo-dark.png
  wordmark "$COLOR_BG" "$COLOR_MUTED" assets/logo-light.png
  rm -f "$ring"
}

gate() {
  echo "-- gate diagram"
  local svg; svg=$(mktemp --suffix=.svg)
  sed -e "s/@COLOR_BG@/#$COLOR_BG/g" -e "s/@COLOR_SURFACE@/#$COLOR_SURFACE/g" \
      -e "s/@COLOR_SURFACE_ALT@/#$COLOR_SURFACE_ALT/g" -e "s/@COLOR_FG@/#$COLOR_FG/g" \
      -e "s/@COLOR_FG_DIM@/#$COLOR_FG_DIM/g" -e "s/@COLOR_ACCENT@/#$COLOR_ACCENT/g" \
      -e "s/@COLOR_MUTED@/#$COLOR_MUTED/g" -e "s/@COLOR_URGENT@/#$COLOR_URGENT/g" \
      -e "s/@COLOR_BORDER_INACTIVE@/#$COLOR_BORDER_INACTIVE/g" \
      templates/gate.svg.in > "$svg"
  rsvg-convert -w 1800 "$svg" -o assets/gate.png
  rm -f "$svg"
}

agg_theme() {  # bg,fg, then the 16 ANSI colours
  echo "$TERMINAL_BG,$TERMINAL_FG,$COLOR_SURFACE,$COLOR_URGENT,$COLOR_OK,$COLOR_OK,$COLOR_MUTED,$COLOR_SURFACE_ALT,$COLOR_ACCENT,$COLOR_FG_DIM,$COLOR_BORDER_INACTIVE,$COLOR_URGENT,$COLOR_OK,$COLOR_OK,$COLOR_MUTED,$COLOR_SURFACE_ALT,$COLOR_ACCENT,$COLOR_FG"
}

record() {  # <command> <gif> <rows>
  local cast home
  cast=$(mktemp --suffix=.cast)
  home=$(python3 demo/world.py "${TMPDIR:-/tmp}/hermes-local-harness-demo")
  HOME=$home HERMES_HOME=$home/.hermes asciinema rec --overwrite --cols 100 --rows "$3" \
    -c "$1" "$cast" >/dev/null 2>&1
  agg --theme "$(agg_theme)" --font-family "JetBrainsMono Nerd Font" --font-size 15 \
    --idle-time-limit 3 --last-frame-duration 4 "$cast" "$2" >/dev/null 2>&1
  rm -f "$cast"
}

gifs() {
  echo "-- gifs (real plugin and helper output, fictional home)"
  record "python3 demo/guard.py \$HOME" assets/gifs/guard.gif 22
  record "bash demo/recall-cast.sh" assets/gifs/recall.gif 26
}

case $what in
  logos) logos ;;
  gate) gate ;;
  gifs) gifs ;;
  all) logos; gate; gifs ;;
  *) echo "usage: $0 [all|logos|gate|gifs]" >&2; exit 2 ;;
esac
