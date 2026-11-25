#!/usr/bin/env bash
set -euo pipefail
umask 077

mkdir -p "$XDG_RUNTIME_DIR"
chmod 0700 "$XDG_RUNTIME_DIR"

: "${BRIDGE_TOKEN:?Set BRIDGE_TOKEN before starting the bridge}"
if [[ ! "${VNC_PASSWORD:-}" =~ ^[a-zA-Z0-9]{8}$ ]]; then
  echo "VNC_PASSWORD must contain exactly 8 ASCII letters or digits" >&2
  exit 1
fi

mkdir -p /data
: > "$XAUTHORITY"
xauth -q -f "$XAUTHORITY" add "$DISPLAY" MIT-MAGIC-COOKIE-1 "$(mcookie)"

declare -A children=()
bridge_pid=""
cleanup() {
  local deadline
  local -a running
  trap '' INT TERM

  # Let the bridge close AT-SPI and SQLite while X11 and D-Bus still run.
  if [[ -n "$bridge_pid" ]] && kill -TERM "$bridge_pid" 2>/dev/null; then
    deadline=$((SECONDS + 4))
    while (( SECONDS < deadline )) && kill -0 "$bridge_pid" 2>/dev/null; do
      sleep 0.1
    done
  fi

  mapfile -t running < <(jobs -pr)
  if (( ${#running[@]} == 0 )); then return; fi
  kill -TERM "${running[@]}" 2>/dev/null || true
  deadline=$((SECONDS + 2))
  while (( SECONDS < deadline )) && [[ -n "$(jobs -pr)" ]]; do
    sleep 0.1
  done

  # A client or a GI call can hang when the desktop disappears. Exit after
  # the deadline so the container's restart policy can start a fresh session.
  mapfile -t running < <(jobs -pr)
  if (( ${#running[@]} > 0 )); then
    echo "Shutdown deadline exceeded. Terminating remaining processes." >&2
    kill -KILL "${running[@]}" 2>/dev/null || true
  fi
}
trap cleanup EXIT
trap 'exit 0' INT TERM

# Manage the bus as a child so Docker's stop signal first reaches this
# supervisor and the bridge can disconnect before the session is torn down.
session_socket="$XDG_RUNTIME_DIR/session-bus"
rm -f "$session_socket"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$session_socket"
dbus-daemon --session --nofork --nopidfile --address="$DBUS_SESSION_BUS_ADDRESS" &
children["$!"]="D-Bus session"
for ((attempt = 0; attempt < 50; attempt++)); do
  if [[ -S "$session_socket" ]]; then break; fi
  sleep 0.1
done
[[ -S "$session_socket" ]]

# Keep Qt accessibility screen coordinates aligned with X11 pixels.
Xvfb "$DISPLAY" -screen 0 "$DISPLAY_RESOLUTION" -dpi 96 -nolisten tcp -auth "$XAUTHORITY" &
children["$!"]="Xvfb"
for ((attempt = 0; attempt < 100; attempt++)); do
  if xdpyinfo >/dev/null 2>&1; then break; fi
  sleep 0.1
done
xdpyinfo >/dev/null

openbox &
children["$!"]="Openbox"

# Let the standard session service start at-spi-bus-launcher and at-spi2-registryd.
AT_SPI_BUS_ADDRESS=$(python3 - <<'PY'
from gi.repository import Gio, GLib

bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
bus.call_sync(
    "org.a11y.Bus", "/org/a11y/bus", "org.freedesktop.DBus.Properties", "Set",
    GLib.Variant("(ssv)", ("org.a11y.Status", "IsEnabled", GLib.Variant("b", True))),
    None, Gio.DBusCallFlags.NONE, 10_000, None,
)
address, = bus.call_sync(
    "org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus", "GetAddress", None,
    GLib.VariantType.new("(s)"), Gio.DBusCallFlags.NONE, 10_000, None,
).unpack()
print(address)
PY
)
export AT_SPI_BUS_ADDRESS
xprop -root -f AT_SPI_BUS 8s -set AT_SPI_BUS "$AT_SPI_BUS_ADDRESS"
printf 'export DBUS_SESSION_BUS_ADDRESS=%q\nexport AT_SPI_BUS_ADDRESS=%q\n' \
  "$DBUS_SESSION_BUS_ADDRESS" "$AT_SPI_BUS_ADDRESS" > "$XDG_RUNTIME_DIR/bridge-session.env"

# The password file has mode 0600. VNC is reachable only through the noVNC WebSocket.
printf '%s\n' "$VNC_PASSWORD" > "$XDG_RUNTIME_DIR/vnc-password"
unset VNC_PASSWORD
x11vnc -norc -display "$DISPLAY" -auth "$XAUTHORITY" -localhost -rfbport 5900 \
  -passwdfile "$XDG_RUNTIME_DIR/vnc-password" -forever &
children["$!"]="x11vnc"
websockify --web=/usr/share/novnc 0.0.0.0:6080 127.0.0.1:5900 &
children["$!"]="websockify"

env -u BRIDGE_TOKEN -u MCP_TOKEN /opt/wechat/wechat &
children["$!"]="WeChat"

"$@" &
bridge_pid=$!
children["$bridge_pid"]="Bridge"
status=0
wait -n -p finished "${!children[@]}" || status=$?
component="A desktop or bridge process"
if [[ -n "${finished:-}" ]]; then component="${children[$finished]}"; fi
echo "$component exited unexpectedly with status $status" >&2
if (( status == 0 )); then status=1; fi
exit "$status"
