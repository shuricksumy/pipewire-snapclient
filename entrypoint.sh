#!/bin/bash
# Snapcast launcher for the PipeWire image. One image, two roles -- see README.md
# for the full environment-variable table.

set -uo pipefail

# DEBUG=true turns on command tracing. It used to be on unconditionally, which
# buried every log line below in xtrace noise.
[ "${DEBUG:-false}" = "true" ] && set -x

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$1] ➡️ $2"; }

ROLE="${ROLE:-snapclient}"
SNAP_PORT="${SNAP_PORT:-1704}"
USE_ALSA="${USE_ALSA:-false}"
PLAYER_NAME="${PLAYER_NAME:-}"
VOLUME_SETTING="${INIT_VOL:-1.0}"

# --- ROLE: SNAPSERVER ---
if [ "$ROLE" = "snapserver" ]; then
    CONFIG_FILE="/config/snapserver.conf"

    if ! mkdir -p /config 2>/dev/null || [ ! -w /config ]; then
        log "ERROR" "/config is not writable by uid $(id -u)."
        log "ERROR" "This image runs unprivileged. Fix the host directory once with:"
        log "ERROR" "    sudo chown -R 1000:1000 ./snapserver_config"
        log "ERROR" "or pin the container to a different uid with 'user:' in compose."
        exit 1
    fi

    # Seed from the tuned config baked into the image on first run only; after
    # that the copy in the volume wins so local edits survive image updates.
    if [ ! -f "$CONFIG_FILE" ]; then
        log "INFO" "No $CONFIG_FILE yet -- seeding from the image default."
        cp /etc/snapserver.conf "$CONFIG_FILE"
    fi

    log "INFO" "Launching Snapserver on port $SNAP_PORT..."
    exec snapserver -c "$CONFIG_FILE" --server.tcp.port "$SNAP_PORT" ${EXTRA_ARGS:-}
fi

# --- ROLE: SNAPCLIENT ---
if [ "$ROLE" != "snapclient" ]; then
    log "ERROR" "Unknown ROLE '$ROLE' (expected 'snapclient' or 'snapserver')."
    exit 1
fi

# Forward `docker stop` to snapclient instead of waiting out the 10s SIGKILL
# timer inside the reconnect sleep below.
CHILD_PID=""
terminate() {
    log "INFO" "Shutdown signal received, stopping snapclient..."
    [ -n "$CHILD_PID" ] && kill -TERM "$CHILD_PID" 2>/dev/null
    exit 0
}
trap terminate TERM INT

PW_SOCKET="${PIPEWIRE_RUNTIME_DIR:-/tmp}/${PIPEWIRE_REMOTE:-pipewire-0}"
if [ ! -S "$PW_SOCKET" ]; then
    log "WARN" "PipeWire socket not found at $PW_SOCKET."
    log "WARN" "Check the bind mount, e.g. '/run/user/1000/pipewire-0:/tmp/pipewire-0'."
elif [ ! -w "$PW_SOCKET" ]; then
    log "WARN" "PipeWire socket $PW_SOCKET is not writable by uid $(id -u)."
    log "WARN" "It is owned by the host desktop user -- match it with 'user:' in compose."
fi

log "INFO" "--- Audio Engine Diagnostics ---"
wpctl status || log "WARN" "Cannot talk to PipeWire; volume control will be unavailable."

log "INFO" "--- Snapclient Device List ---"
snapclient -l || true

# Volume init -- once at container start, not on every reconnect.
# wpctl prints sinks as ' │  *   50. Topping DX5 ...', so the id is the first
# number followed by a dot. The previous 'grep -oE [0-9]+' matched bare digits and
# happily picked up digits from the device name itself (DX5 -> 5).
TARGET_ID=""
if [ -n "$PLAYER_NAME" ]; then
    TARGET_ID=$(wpctl status 2>/dev/null \
        | awk '/Sinks:/{inside=1; next} inside && /├─|└─/{inside=0} inside' \
        | grep -F "$PLAYER_NAME" \
        | grep -oE '[0-9]+\.' | head -n 1 | tr -d '.')
fi

if [ -n "$TARGET_ID" ]; then
    log "INFO" "Sink id $TARGET_ID matches '$PLAYER_NAME'. Setting volume to $VOLUME_SETTING."
    wpctl set-mute "$TARGET_ID" 0 || log "WARN" "Could not unmute sink $TARGET_ID."
    wpctl set-volume "$TARGET_ID" "$VOLUME_SETTING" || log "WARN" "Could not set volume on sink $TARGET_ID."
else
    if [ -n "$PLAYER_NAME" ]; then
        log "WARN" "No sink matching '$PLAYER_NAME' -- falling back to the default sink."
    else
        log "INFO" "PLAYER_NAME not set -- initialising the default sink."
    fi
    # Unmute here too: the default-sink path used to set the volume but leave a
    # muted sink muted, which looks exactly like "no audio".
    wpctl set-mute @DEFAULT_AUDIO_SINK@ 0 || true
    wpctl set-volume @DEFAULT_AUDIO_SINK@ "$VOLUME_SETTING" || true
fi

# Build the connection URI. SERVER_IP may be a bare host, host:port, or a full URI.
HOST_IP="${SERVER_IP:-127.0.0.1}"
case "$HOST_IP" in
    *://*)    HOST_URI="$HOST_IP" ;;
    *:[0-9]*) HOST_URI="tcp://$HOST_IP" ;;
    *)        HOST_URI="tcp://$HOST_IP:$SNAP_PORT" ;;
esac

export PIPEWIRE_NODE="${PIPEWIRE_NODE:-}"
export PIPEWIRE_LATENCY="${PIPEWIRE_LATENCY:-}"

if [ "$USE_ALSA" = "true" ]; then
    log "INFO" "Mode: ALSA bridge (pcm.default -> PipeWire)"
    PLAYER_TYPE="alsa"
    PLAYER_OPTS="-s default"
else
    log "INFO" "Mode: native PipeWire"
    PLAYER_TYPE="pipewire"
    PLAYER_OPTS=""
fi

# Reconnect loop with exponential backoff
RETRY_DELAY=5
MAX_DELAY=60

while true; do
    log "INFO" "Connecting to $HOST_URI via $PLAYER_TYPE as '${CLIENT_ID:-Snap-Node}'..."
    STARTED_AT=$SECONDS

    snapclient --player "$PLAYER_TYPE" \
        ${PLAYER_OPTS} \
        ${SNAP_EXTRA:-} \
        --hostID "${CLIENT_ID:-Snap-Node}" \
        "$HOST_URI" &
    CHILD_PID=$!
    wait "$CHILD_PID"
    CHILD_PID=""

    # A session that stayed up is not part of a failure streak, so don't carry a
    # 60s backoff over from an outage that has long since been resolved.
    if [ $(( SECONDS - STARTED_AT )) -ge 60 ]; then
        RETRY_DELAY=5
    fi

    log "WARN" "Snapclient exited. Retrying in ${RETRY_DELAY}s..."
    sleep "$RETRY_DELAY"
    RETRY_DELAY=$(( RETRY_DELAY * 2 > MAX_DELAY ? MAX_DELAY : RETRY_DELAY * 2 ))
done
