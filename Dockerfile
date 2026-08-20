# syntax=docker/dockerfile:1

# --- Stage 0: Fetch the Snapcast packages from the upstream GitHub release ---
# Runs on the build host (not under QEMU) - it only downloads, and picks the
# package for $TARGETARCH explicitly. This replaces the four .deb files that
# used to be committed under pkg/; they were byte-identical to these release
# assets, so they were 10 MB of binaries in git that had to be refreshed by hand
# on every Snapcast release.
FROM --platform=$BUILDPLATFORM debian:trixie-slim AS snapcast

ARG TARGETARCH
# Release to install. "latest" resolves the newest published release at build
# time; CI pins the resolved tag so the layer caches between rebuilds.
ARG SNAPCAST_VERSION=latest
ARG SNAPCAST_SUITE=trixie

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl jq \
    && rm -rf /var/lib/apt/lists/*

# The "with-pipewire" variants are the whole point of this image: the plain
# packages install just as cleanly and then reject "--player pipewire" at
# runtime. Each asset is verified against the sha256 digest the GitHub API
# publishes for it, and a missing asset or digest fails the build rather than
# shipping an unverified package.
RUN set -eu; \
    if [ "$SNAPCAST_VERSION" = "latest" ]; then \
        api="https://api.github.com/repos/snapcast/snapcast/releases/latest"; \
    else \
        api="https://api.github.com/repos/snapcast/snapcast/releases/tags/${SNAPCAST_VERSION}"; \
    fi; \
    curl -fsSL --retry 3 --retry-delay 5 -H "Accept: application/vnd.github+json" "$api" -o /tmp/release.json; \
    tag="$(jq -r .tag_name /tmp/release.json)"; \
    echo "==> Snapcast release ${tag} (${TARGETARCH}/${SNAPCAST_SUITE}, with-pipewire)"; \
    mkdir -p /debs; \
    for name in snapclient snapserver; do \
        pattern="${name}_.*_${TARGETARCH}_${SNAPCAST_SUITE}_with-pipewire[.]deb"; \
        asset="$(jq -c --arg p "^${pattern}$" '[.assets[] | select(.name | test($p))] | first' /tmp/release.json)"; \
        [ "$asset" != "null" ] || { echo "ERROR: ${tag} has no asset matching ${pattern}" >&2; exit 1; }; \
        url="$(echo "$asset" | jq -r .browser_download_url)"; \
        sha="$(echo "$asset" | jq -r '.digest // ""' | sed 's/^sha256://')"; \
        [ -n "$sha" ] || { echo "ERROR: no sha256 digest published for $(echo "$asset" | jq -r .name)" >&2; exit 1; }; \
        echo "    $(echo "$asset" | jq -r .name)"; \
        curl -fsSL --retry 3 --retry-delay 5 "$url" -o "/debs/${name}.deb"; \
        echo "${sha}  /debs/${name}.deb" | sha256sum -c -; \
    done


# --- Stage 1: Runtime ---
FROM debian:trixie-slim

# Changing this busts the apt cache so a scheduled rebuild actually picks up new
# Debian security updates instead of restoring the whole layer from the GHA
# cache (CI sets it to the ISO week; a manual build can leave it alone).
ARG REFRESH_WEEK=0

LABEL org.opencontainers.image.title="snapcast-pipewire" \
      org.opencontainers.image.description="Snapcast client and server with native PipeWire output" \
      org.opencontainers.image.source="https://github.com/shuricksumy/pipewire-snapclient" \
      org.opencontainers.image.licenses="MIT"

# 1. Runtime dependencies
# The snapclient/snapserver .deb files declare their own library dependencies
# (libflac14, libvorbis0a, libopus0, libsoxr0, libpipewire-0.3-0t64, libavahi-client3, ...)
# and apt resolves them at install time below, so they are deliberately not listed
# here -- the -dev variants that used to be listed pulled headers and static
# archives into the runtime image for no benefit.
# What remains is what the entrypoint and the PipeWire/ALSA plumbing need:
#   pipewire-bin  wpctl / pw-cli, used for volume init and diagnostics
#   wireplumber   the session manager wpctl talks to
#   pipewire-alsa + libasound2-plugins + alsa-utils   the USE_ALSA=true bridge path
#   python3       snapserver's /usr/share/snapserver/plug-ins/meta_*.py stream helpers
#   python3-flask ROLE=panel's HTTP layer. Distro package, not pip: no wheels to
#                 audit and it gets Debian's security updates with everything else
#   procps        pgrep, used by the HEALTHCHECK below
# avahi-daemon and dbus-daemon were dropped: nothing ever started them (the
# entrypoint execs snapcast directly), so they were dead weight and CVE surface.
# mDNS discovery is not provided by this image -- point clients at SERVER_IP.
#
# apt-get upgrade applies security updates that are in the archive but not yet in
# the base image; without it those CVEs sit in the scan until Debian respins
# debian:trixie-slim.
RUN echo "cache epoch: ${REFRESH_WEEK}" && apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
    ca-certificates \
    alsa-utils \
    libasound2-plugins \
    pipewire-bin \
    pipewire-alsa \
    wireplumber \
    python3 \
    python3-flask \
    procps \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# 2. ALSA -> PipeWire bridge
# The !default lines matter: USE_ALSA=true starts snapclient with "-s default",
# so without them the ALSA path would land on the real hardware default (dmix/hw)
# instead of PipeWire and fail unless /dev/snd happens to be passed through.
RUN printf '%s\n' \
    'pcm.pipewire { type pipewire }' \
    'ctl.pipewire { type pipewire }' \
    'pcm.!default pcm.pipewire' \
    'ctl.!default ctl.pipewire' \
    > /etc/asound.conf

# 3. Install the packages fetched in stage 0.
# apt resolves their dependency lists itself; --no-install-recommends keeps the
# avahi-daemon Recommends out. No "|| apt-get -f install" fallback here -- it used
# to mask a genuinely failed install and leave the result unverified.
COPY --from=snapcast /debs/snapclient.deb /debs/snapserver.deb /tmp/
RUN apt-get update && \
    apt-get install -y --no-install-recommends /tmp/snapclient.deb /tmp/snapserver.deb && \
    rm -f /tmp/*.deb && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# 4. Verify the image can actually run what it just installed.
RUN set -eu; \
    for bin in /usr/bin/snapclient /usr/bin/snapserver; do \
        if ldd "$bin" | grep -q "not found"; then \
            echo "ERROR: unresolved shared libraries for $bin" >&2; ldd "$bin" >&2; exit 1; \
        fi; \
    done; \
    if ! ldd /usr/bin/snapclient | grep -q libpipewire; then \
        echo "ERROR: snapclient is not linked against libpipewire" >&2; exit 1; \
    fi; \
    test -d /usr/share/snapserver/snapweb; \
    snapclient --version; \
    snapserver --version

# 5. The tuned server config from this repo.
# Previously this file lived only in the repo and never reached the image, so the
# entrypoint seeded /config from the stock Debian snapserver.conf and the streams,
# 24-bit sample format and web UI settings in it were silently ignored.
COPY snapserver.conf /etc/snapserver.conf

# 6. The web panel (ROLE=panel). Inert for the other roles: nothing imports it
# and Flask is only loaded when app.py runs.
COPY panel/ /app/

# 7. Entrypoint
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# 8. Run unprivileged.
# UID/GID 1000 is the default because the host socket that gets mounted in
# (/run/user/1000/pipewire-0) is normally owned by the desktop user; if yours
# differs, override with `user: "<uid>:<gid>"` in compose. The audio group is for
# the optional /dev/snd passthrough. /config is pre-created so the snapserver role
# works with a named volume out of the box; a host bind mount keeps host ownership
# and needs `sudo chown -R 1000:1000 <dir>` once.
RUN groupadd -g 1000 snapcast && \
    useradd -u 1000 -g 1000 -G audio -M -s /usr/sbin/nologin snapcast && \
    install -d -o 1000 -g 1000 /home/snapcast /config

ENV ROLE="snapclient" \
    SNAP_PORT="1704" \
    SERVER_IP="127.0.0.1" \
    CLIENT_ID="Snap-Node" \
    PLAYER_NAME="" \
    INIT_VOL="1.0" \
    USE_ALSA="false" \
    DEBUG="false" \
    PIPEWIRE_RUNTIME_DIR="/tmp" \
    PIPEWIRE_REMOTE="pipewire-0" \
    PIPEWIRE_NODE="" \
    PIPEWIRE_LATENCY="2048/192000" \
    EXTRA_ARGS="" \
    SNAP_EXTRA="" \
    HOME="/home/snapcast" \
    CONFIG_DIR="/config" \
    PORT="8080" \
    BIND_HOST="0.0.0.0" \
    ADMIN_USER="admin" \
    ADMIN_PASSWORD="" \
    PYTHONUNBUFFERED="1"

# Named, not numeric: Docker only applies supplementary groups (i.e. the audio
# group added above, needed for the /dev/snd passthrough in USE_ALSA mode) when
# USER is a name it can look up in /etc/passwd. "USER 1000:1000" silently drops
# them. A compose override such as `user: "1003:1003"` is numeric and drops them
# again, which is what `group_add: audio` in the compose files is for.
USER snapcast

# Report unhealthy once the snapcast process for this role is gone -- the client
# entrypoint keeps the container alive across reconnects, so a container that is
# up is not by itself evidence that anything is playing. ROLE=panel has no such
# process (it supervises its own children), so it is checked over HTTP instead,
# where any answer -- 401 included -- means the panel is alive.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD if [ "$ROLE" = "panel" ]; then python3 /app/healthcheck.py; \
      else pgrep -x "$ROLE" > /dev/null; fi || exit 1

# Confirm the unprivileged user can actually execute the binaries
RUN snapclient --version > /dev/null && snapserver --version > /dev/null

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
