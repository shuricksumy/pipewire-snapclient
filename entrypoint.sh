#!/bin/bash
set -x
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$1] ➡️ $2"; }

ROLE="${ROLE:-snapclient}"
SNAP_PORT="${SNAP_PORT:-1704}"
USE_ALSA="${USE_ALSA:-false}"
VOLUME_SETTING="${INIT_VOL:-1.0}"

# --- ROLE: SNAPSERVER ---
if [ "$ROLE" = "snapserver" ]; then
    log "INFO" "Setting up Snapserver..."
    mkdir -p /tmp /config
    chmod 777 /tmp
    VOLUME_CONFIG="/config/snapserver.conf"
    [ ! -f "$VOLUME_CONFIG" ] && cp /etc/snapserver.conf "$VOLUME_CONFIG"
    log "INFO" "Launching Snapserver on port $SNAP_PORT..."
    exec snapserver -c "$VOLUME_CONFIG" --server.tcp.port "$SNAP_PORT" ${EXTRA_ARGS:-}

# --- ROLE: SNAPCLIENT ---
elif [ "$ROLE" = "snapclient" ]; then
    log "INFO" "--- Audio Engine Diagnostics ---"
    wpctl status || log "WARN" "Cannot connect to PipeWire! Check socket mount."

    log "INFO" "--- Snapclient Device List ---"
    snapclient -l || true

    # Volume init — runs ONCE at container start, not on every reconnect
    TARGET_ID=$(wpctl status | grep -A 20 "Sinks:" | grep "${PLAYER_NAME}" | grep -oE '[0-9]+' | head -n 1)
    if [ -n "$TARGET_ID" ]; then
        log "INFO" "Found Sink ID: $TARGET_ID. Setting volume to $VOLUME_SETTING"
        wpctl set-mute "$TARGET_ID" 0
        wpctl set-volume "$TARGET_ID" "$VOLUME_SETTING"
    else
        log "WARN" "Could not find sink '$PLAYER_NAME'. Using default sink."
        wpctl set-volume @DEFAULT_AUDIO_SINK@ "$VOLUME_SETTING" || true
    fi

    HOST_IP="${SERVER_IP:-127.0.0.1}"
    [[ "$HOST_IP" != *"://"* ]] && HOST_URI="tcp://$HOST_IP:$SNAP_PORT" || HOST_URI="$HOST_IP"

    export PIPEWIRE_NODE="${PIPEWIRE_NODE:-}"
    export PIPEWIRE_LATENCY="${PIPEWIRE_LATENCY:-}"

    if [ "$USE_ALSA" = "true" ]; then
        log "INFO" "Mode: ALSA Bridge"
        PLAYER_TYPE="alsa"
        PLAYER_OPTS="-s default"
    else
        log "INFO" "Mode: Native PipeWire"
        PLAYER_TYPE="pipewire"
        PLAYER_OPTS=""
    fi

    # Reconnect loop with exponential backoff
    RETRY_DELAY=5
    MAX_DELAY=60

    while true; do
        log "INFO" "Connecting to $HOST_URI via $PLAYER_TYPE..."
        snapclient --player "${PLAYER_TYPE}" \
            ${PLAYER_OPTS:-} \
            ${SNAP_EXTRA:-} \
            --hostID "${CLIENT_ID:-Snap-Node}" \
            "$HOST_URI" || true

        log "WARN" "Snapclient exited. Retrying in ${RETRY_DELAY}s..."
        sleep "$RETRY_DELAY"
        RETRY_DELAY=$(( RETRY_DELAY * 2 > MAX_DELAY ? MAX_DELAY : RETRY_DELAY * 2 ))
    done

else
    log "ERROR" "Unknown ROLE: $ROLE."
    exit 1
fi
