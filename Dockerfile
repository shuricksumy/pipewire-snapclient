# syntax=docker/dockerfile:1
FROM debian:trixie-slim

# Professional Multi-Arch Setup
ARG TARGETARCH

# 1. Install System Dependencies 
# Focusing on PipeWire, encoding libs, and avoiding PulseAudio bloat
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    dbus-daemon \
    avahi-daemon \
    alsa-utils \
    libasound2-plugins \
    pipewire-bin \
    pipewire-alsa \
    wireplumber \
    libvorbis-dev \
    libflac-dev \
    libopus0 \
    libsoxr0 \
    libsamplerate0-dev \
    python3 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# 2. Configure ALSA-PipeWire Bridge
RUN echo 'pcm.pipewire { type pipewire } ctl.pipewire { type pipewire }' > /etc/asound.conf

# 3. Copy and Install Architecture-Specific Packages
# Expects ./pkg/ to contain amd64 and arm64 debs
COPY pkg/snapclient_*_${TARGETARCH}_*_with-pipewire.deb /tmp/snapclient.deb
COPY pkg/snapserver_*_${TARGETARCH}_*.deb /tmp/snapserver.deb

RUN apt-get update && \
    apt-get install -y --no-install-recommends /tmp/snapclient.deb /tmp/snapserver.deb || apt-get install -y -f && \
    rm /tmp/*.deb && \
    rm -rf /var/lib/apt/lists/*

# 4. Setup Entrypoint
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Professional Environment Defaults
ENV ROLE="snapclient" \
    SNAP_PORT="1704" \
    SERVER_IP="127.0.0.1" \
    CLIENT_ID="Snap-Node" \
    PIPEWIRE_RUNTIME_DIR="/tmp" \
    PIPEWIRE_REMOTE="pipewire-0" \
    PIPEWIRE_NODE="" \
    PIPEWIRE_LATENCY="2048/192000" \
    EXTRA_ARGS="" \
    SNAP_EXTRA=""

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]