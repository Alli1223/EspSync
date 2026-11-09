#!/usr/bin/env bash
# Smoke test for the Wayland `portal` capture backend on a private, headless
# Wayland session: Sway (wlroots headless) + xdg-desktop-portal-wlr + PipeWire.
#
# It exercises the real ScreenCast portal D-Bus handshake and the PipeWire ->
# GStreamer frame path by running `espsync-cli bench --backend portal` twice.
# xdg-desktop-portal-wlr is configured to share HEADLESS-1 without a dialog, so
# this does not cover GNOME/KDE dialogs or their restore-token handling.
#
# Needs (Debian/Ubuntu): sway swaybg xdg-desktop-portal xdg-desktop-portal-wlr
#   pipewire wireplumber dbus-user-session python3-gi gir1.2-gstreamer-1.0
#   gstreamer1.0-pipewire gstreamer1.0-plugins-base gstreamer1.0-plugins-bad
#   gstreamer1.0-tools python3-numpy python3-platformdirs python3-jeepney
# Set SHOW_LOGS=1 to print the compositor/portal logs even on success.
set -euo pipefail

if [ -z "${ESPSYNC_SMOKE_INNER:-}" ]; then
  tmp=$(mktemp -d)
  export ESPSYNC_SMOKE_TMP=$tmp ESPSYNC_SMOKE_INNER=1
  export XDG_RUNTIME_DIR=$tmp/run XDG_CONFIG_HOME=$tmp/config
  mkdir -p -m 0700 "$XDG_RUNTIME_DIR"
  mkdir -p "$XDG_CONFIG_HOME/xdg-desktop-portal" "$XDG_CONFIG_HOME/xdg-desktop-portal-wlr"
  printf '[preferred]\ndefault=wlr\n' >"$XDG_CONFIG_HOME/xdg-desktop-portal/portals.conf"
  printf '[screencast]\nchooser_type=none\noutput_name=HEADLESS-1\nmax_fps=60\n' \
    >"$XDG_CONFIG_HOME/xdg-desktop-portal-wlr/config"
  export WLR_BACKENDS=headless WLR_LIBINPUT_NO_DEVICES=1 WLR_RENDERER=pixman
  export XDG_CURRENT_DESKTOP=sway XDG_SESSION_TYPE=wayland
  unset WAYLAND_DISPLAY DISPLAY
  exec dbus-run-session -- bash "$0" "$@"
fi

tmp=$ESPSYNC_SMOKE_TMP
logs=$tmp/logs
mkdir -p "$logs"

cleanup() {
  local status=$?
  kill $(jobs -p) 2>/dev/null || true
  if [ "$status" -ne 0 ] || [ -n "${SHOW_LOGS:-}" ]; then
    for f in "$logs"/*.log; do echo "===== $f"; tail -n 30 "$f"; done
  fi
  rm -rf "$tmp"
}
trap cleanup EXIT

pipewire >"$logs/pipewire.log" 2>&1 &
wireplumber >"$logs/wireplumber.log" 2>&1 &
printf 'output HEADLESS-1 resolution 1280x720 bg #204080 solid_color\ndefault_border none\n' >"$tmp/sway.conf"
sway -c "$tmp/sway.conf" >"$logs/sway.log" 2>&1 &
for _ in $(seq 100); do
  ls "$XDG_RUNTIME_DIR"/wayland-? >/dev/null 2>&1 && break
  sleep 0.1
done
WAYLAND_DISPLAY=$(basename "$(ls "$XDG_RUNTIME_DIR"/wayland-? | head -n 1)")
export WAYLAND_DISPLAY
dbus-update-activation-environment WAYLAND_DISPLAY XDG_CURRENT_DESKTOP XDG_SESSION_TYPE XDG_RUNTIME_DIR XDG_CONFIG_HOME

/usr/libexec/xdg-desktop-portal-wlr -l DEBUG >"$logs/xdpw.log" 2>&1 &
sleep 0.5
/usr/libexec/xdg-desktop-portal -v >"$logs/xdp.log" 2>&1 &
sleep 1
# Something that changes every frame, so the compositor keeps sending frames.
gst-launch-1.0 -q videotestsrc pattern=ball is-live=true \
  ! video/x-raw,width=640,height=360,framerate=30/1 ! waylandsink fullscreen=true >"$logs/anim.log" 2>&1 &
sleep 1

cd "$(dirname "$0")/.."
cfg=$tmp/espsync.json
echo "auto backend: $(python3 -c 'from espsync.capture import resolve_backend; print(resolve_backend("auto"))')"
for run in 1 2; do
  out=$(python3 -m espsync --config "$cfg" bench --backend portal --seconds 3 --regions 50)
  echo "[run $run] $out"
  frames=$(sed -n 's/.*frames=\([0-9]*\).*/\1/p' <<<"$out")
  if [ "${frames:-0}" -lt 1 ]; then
    echo "[run $run] no frames captured" >&2
    exit 1
  fi
  token=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["sync"]["portal_restore_token"])' \
    "$cfg" 2>/dev/null || true)
  echo "[run $run] saved restore token: ${token:-<none: this portal did not issue one>}"
done
echo "Wayland portal smoke test OK"
