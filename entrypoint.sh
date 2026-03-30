#!/bin/bash
# -e: Exit on error | -x: Print every command (DEBUG MODE)
set -ex

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$1] ➡️ $2"
}

ROLE="${ROLE:-snapclient}"
SNAP_PORT="${SNAP_PORT:-1704}"
USE_ALSA="${USE_ALSA:-false}"

# If INIT_VOL is not set or empty, default to 1.0
VOLUME_SETTING="${INIT_VOL:-1.0}"

# --- ROLE: SNAPSERVER ---
if [ "$ROLE" = "snapserver" ]; then
    log "INFO" "Setting up Snapserver environment..."
    mkdir -p /tmp /config
    chmod 777 /tmp 

    VOLUME_CONFIG="/config/snapserver.conf"
    [ ! -f "$VOLUME_CONFIG" ] && cp /etc/snapserver.conf "$VOLUME_CONFIG"

    log "INFO" "Launching Snapserver on port $SNAP_PORT..."
    (
        exec snapserver -c "$VOLUME_CONFIG" --server.tcp.port "$SNAP_PORT" ${EXTRA_ARGS:-}
    ) || { log "ERROR" "🛑 SERVER CRASHED"; exit 1; }

# --- ROLE: SNAPCLIENT ---
elif [ "$ROLE" = "snapclient" ]; then
    log "INFO" "--- Audio Engine Diagnostics ---"
    
    # 1. Check PipeWire Connection
    wpctl status || log "WARN" "Cannot connect to PipeWire! Check socket mount."

    # 2. Device List for Debugging
    log "INFO" "--- Snapclient Device List ---"
    snapclient -l || true

    # 2. Targeted Volume Initialization
    # This regex finds the line under your PLAYER_NAME that points to the hardware
    TARGET_ID=$(wpctl status | grep -A 20 "Sinks:" | grep "${PLAYER_NAME}" | grep -oE '[0-9]+' | head -n 1)

    if [ -n "$TARGET_ID" ]; then
        log "INFO" "Found Sink ID: $TARGET_ID. Setting volume to $VOLUME_SETTING"
        wpctl set-mute "$TARGET_ID" 0
        wpctl set-volume "$TARGET_ID" "$VOLUME_SETTING"
    else
        log "WARN" "Could not trace stream $PLAYER_NAME to a hardware sink. Using default."
        wpctl set-volume @DEFAULT_AUDIO_SINK@ "$VOLUME_SETTING" || true
    fi

    # 4. Build Connection URI
    HOST_IP="${SERVER_IP:-127.0.0.1}"
    [[ "$HOST_IP" != *"://"* ]] && HOST_URI="tcp://$HOST_IP:$SNAP_PORT" || HOST_URI="$HOST_IP"

    # 5. Export variables for the binary
    export PIPEWIRE_NODE="${PIPEWIRE_NODE:-}"
    export PIPEWIRE_LATENCY="${PIPEWIRE_LATENCY:-}"

    # --- Engine Selection Logic ---
    if [ "$USE_ALSA" = "true" ]; then
        log "INFO" "🚀 Mode: ALSA Bridge (Best for dynamic sample rates)"
        PLAYER_TYPE="alsa"
        PLAYER_OPTS="-s default"
    else
        log "INFO" "🚀 Mode: Native PipeWire"
        PLAYER_TYPE="pipewire"
        PLAYER_OPTS=""
    fi
    
    log "INFO" "--- Starting Snapclient 0.35 connecting to $HOST_URI via $PLAYER_TYPE ---"

    (
        exec snapclient --player ${PLAYER_TYPE} \
            ${PLAYER_OPTS:-} \
            ${SNAP_EXTRA:-} \
            --hostID "${CLIENT_ID:-Snap-Node}" \
            "$HOST_URI"
    ) || { log "ERROR" "🛑 CLIENT CRASHED"; exit 1; }

else
    log "ERROR" "Unknown ROLE: $ROLE."
    exit 1
fi