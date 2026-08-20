# 🔊 Snapcast-PipeWire

[![Build_Push_Scan](https://github.com/shuricksumy/pipewire-snapclient/actions/workflows/build.yml/badge.svg)](https://github.com/shuricksumy/pipewire-snapclient/actions/workflows/build.yml)
![amd64 + arm64](https://img.shields.io/badge/arch-amd64%20%7C%20arm64-informational)
![unprivileged](https://img.shields.io/badge/runs%20as-uid%201000-success)

**Multi-room audio into real DACs, managed from a browser.** One Docker image, a web
panel that creates and supervises as many Snapcast players as you have outputs, and
audio that reaches the hardware at the source sample rate.

![The panel's player list](docs/panel-players.png)

> **Part of the [Home Audio Stack](https://github.com/shuricksumy/home-audio-stack)** — Music Assistant → Snapcast → PipeWire, into USB DACs, Bluetooth speakers and LED strips. That page maps how these projects fit together.

---

## ✨ What you get

|  | |
| :-- | :-- |
| 🏠 **Every room in sync** | Press play once; the kitchen and the living room stay together. Walk between them and the song follows without an echo. |
| 🎯 **Nothing is resampled** | A 44.1 kHz track reaches the DAC as 44.1 kHz. The volume lands on the real hardware, not a software fader. |
| 🎛️ **No compose edit to add a room** | Create, start, stop and re-point players from the panel. Several DACs from one container. |
| 🔁 **It reconnects itself** | 5s → 60s backoff that resets after a healthy session, and a watchdog for the case snapclient survives its own output disappearing. |
| 🔌 **PipeWire *or* ALSA** | Bind to a PipeWire sink, or address `hw:CARD=DX5,DEV=0` directly when PipeWire is not an option. |
| 🔒 **Runs unprivileged** | uid 1000, no root, no privileged mode. |

## 🚀 Quick start

The host needs a working PipeWire session first — see [Host setup](#️-host-setup) if you
have not done that. Then:

```bash
mkdir -p panel_config && sudo chown -R 1000:1000 panel_config
curl -O https://raw.githubusercontent.com/shuricksumy/pipewire-snapclient/main/docker-compose-panel-example.yaml
docker compose -f docker-compose-panel-example.yaml up -d
```

Browse **http://&lt;host&gt;:8080/**, press **Add player**, pick an output, point it at your
Snapserver (or [Music Assistant](#-why-this-exists)) and press Save.

> With no `ROLE` set the image runs the panel. For a single-purpose container use
> `ROLE=snapclient` or `ROLE=snapserver` — see [Other roles](#-other-roles).

## 🎛️ The panel

Each player is a supervised `snapclient` child process with its own output, so several
DACs run side by side from one container. Every field is what `ROLE=snapclient` would
take from the environment — `SERVER_IP`, `SNAP_PORT`, `CLIENT_ID`, `PIPEWIRE_NODE`,
`PIPEWIRE_LATENCY`, `SNAP_EXTRA` — editable per player at runtime and stored in
`/config/players.json`, so they come back on restart.

![The edit dialog](docs/panel-edit.png)

Each row reads the snapserver's control port, so it shows what is playing and offers
transport and volume — the same controls Snapweb and Music Assistant drive.

- A **paused** player keeps its controls. Music Assistant parks a paused group on a
  stream reporting `canControl=false`; the panel drives the last controllable one
  instead, so you can still press play.
- A **stopped** player resets its row: no leftover track, no transport.
- The **🌗 button** cycles system / light / dark and remembers the choice.

### Two ways to reach a DAC

Every player picks one output, and the dialog lists both kinds together:

| Output | What it does | When you want it |
| :-- | :-- | :-- |
| **PipeWire** | Binds the player to one sink via `PIPEWIRE_NODE`. Sample rate follows the source, volume lands on the hardware sink, and the panel watches the sink so an unplugged DAC is noticed. | The normal case, on a host with a working PipeWire session. |
| **ALSA** | Hands the device straight to `snapclient -s`, e.g. `hw:CARD=DX5,DEV=0`. Skips `PIPEWIRE_NODE` and the sink watchdog, which have no meaning off the graph. | PipeWire is broken, absent, or you want the card to yourself. |

ALSA devices are enumerated with `snapclient -l`, which needs the sound hardware passed
through **as devices, not as a volume**:

```yaml
devices:
  - /dev/snd:/dev/snd     # NOT "-v /dev/snd:/dev/snd"
```

`-v` maps the device nodes but the container's device cgroup still refuses to open them,
so `snapclient` reports "No such device" and enumeration comes back with conversion
plugins only. The panel detects exactly that and says so in a banner. If your device is
not listed, **Custom ALSA device…** lets you type it.

### Worth knowing

> ⚡ **Real-time scheduling is container-level.** Players are children of the panel
> process and inherit *its* limits, so the panel needs the same `cap_add: SYS_NICE` and
> `rtprio`/`memlock` ulimits a standalone client would — otherwise every player runs at
> normal priority and audio glitches under load. The
> [example compose file](docker-compose-panel-example.yaml) sets them.

> 🔐 **Security.** With `ADMIN_PASSWORD` unset there is no authentication at all —
> intended for a trusted LAN. Set it (and optionally `ADMIN_USER`) to put HTTP Basic auth
> in front of every route, the page included. Do not port-forward this.

**Two things the panel does not do.** Restarting the container stops every player — one
container per player survives a panel restart, this does not. And it manages *clients*,
not the server: groups, streams and server-wide settings still belong to Snapweb or
Music Assistant, which the header links to.

## 🎯 Why this exists

[**Music Assistant**](https://www.music-assistant.io/) is the library and streaming brain
— Spotify, Plex, local files, radio — and Home Assistant drives it. What it cannot do on
its own is put audio into a **USB DAC plugged into some other Linux box**, at the original
sample rate, in sync with the rest of the house.

MA solves the "in sync" half with its
[Snapcast provider](https://www.music-assistant.io/player-support/snapcast/): it ships a
built-in Snapserver and streams synchronised audio to any
[Snapcast](https://github.com/snapcast/snapcast) client on the network. **This image is
the client for the hi-fi end of that chain.**

```mermaid
flowchart LR
    subgraph MA["🎵 Music Assistant"]
        LIB["Spotify · Plex<br/>local library · radio"] --> SS["built-in<br/>Snapserver"]
    end

    SS -- "TCP 1704<br/>synced audio" --> SC["<b>this image</b><br/>panel + players"]
    SC -- "PipeWire socket" --> PW["host PipeWire"]
    PW --> DAC["🔊 USB DAC<br/>Topping DX5"]

    SS -. "other rooms" .-> OTHER["Snapdroid · snapweb<br/>ESP32 · Raspberry Pi"]

    style SC stroke-width:3px
```

**The normal way — let Music Assistant be the server.** Add the Snapcast provider in MA
(`Settings → Player Providers → Add → Snapcast`), leave the built-in server on, and point
your players at the MA host. They appear under the Snapcast provider within seconds and
can be grouped with your other rooms.

<details>
<summary><b>The advanced way — run the Snapserver here too</b></summary>

MA can use an external Snapserver instead (`ROLE=snapserver`), which is what you want when
Snapcast, not MA, is the centre of your audio setup — e.g. LedFx or another producer also
writes into the same FIFOs. Three things to know:

| | |
| :-- | :-- |
| **Version** | MA needs snapserver ≥ 0.27.0 and specifically **cannot** use 0.30.0. This image tracks the latest upstream release (0.35.0 today). |
| **Ports** | 1704, 1705 **and the 4953–5153 range** must be reachable — MA creates a stream per player in that range. `network_mode: host` is the simple answer. |
| **Stream name** | MA requires a stream named `default`. [`snapserver.conf`](snapserver.conf) here ships `Default` — rename it if you go this route. |

</details>

## 🛠️ Host setup

The container does **not** run its own PipeWire daemon — it connects to the host's through
a bind-mounted socket. So the host has to be a working PipeWire machine first.

> **Shortcut:** [`ubuntu-pipewire-install-on-host.sh`](ubuntu-pipewire-install-on-host.sh)
> does all of it for a dedicated user, verifies the socket, and prints the compose settings
> for your host: `./ubuntu-pipewire-install-on-host.sh <username>` (default: `dietpi`).

<details>
<summary><b>Step by step</b></summary>

### 0. Prerequisites

Docker Engine plus the Compose plugin ([install guide](https://docs.docker.com/engine/install/)),
and the uid of the user whose PipeWire session the container will attach to. Note it now — the
same number appears in the socket path *and* in `user:` in your compose file:

```bash
id -u    # usually 1000
```

### 1. Install PipeWire & tools

```bash
sudo apt update && sudo apt install -y \
    pipewire pipewire-audio pipewire-pulse pipewire-alsa \
    wireplumber alsa-utils rtkit
```

> **Note:** the real-time helper package is `rtkit`, **not** `rtkit-daemon` — no such package
> exists on Debian or Ubuntu, and apt aborts the entire command on one unknown name, so a single
> typo leaves nothing installed. Likewise `pipewire-audio` is the current name of what used to be
> `pipewire-audio-client-libraries`.

### 2. Add your user to the audio groups

`usermod -aG` is all-or-nothing: if **any** listed group does not exist it exits with an error and
adds *none* of them. `bluetooth`, `render`, `pulse-access` and `docker` only exist once their
package is installed, so add whichever are actually present:

```bash
for g in audio video render bluetooth lp docker; do
    getent group "$g" >/dev/null && sudo usermod -aG "$g" "$USER"
done

# Group membership only applies to new sessions -- log out and back in, then verify:
id -nG
```

### 3. Configure bit-perfect output

To allow your DAC to switch sample rates without resampling:

```bash
mkdir -p ~/.config/pipewire/pipewire.conf.d/
cat <<EOF > ~/.config/pipewire/pipewire.conf.d/bitperfect.conf
context.properties = {
    # Rate used while nothing is playing; PipeWire switches to the source rate on demand
    default.clock.rate          = 48000
    # Trim this list to the rates your DAC actually supports
    default.clock.allowed-rates = [ 44100 48000 88200 96000 176400 192000 352800 384000 ]
    default.clock.min-quantum   = 32
    default.clock.max-quantum   = 8192
}
EOF

systemctl --user restart pipewire pipewire-pulse wireplumber
```

### 4. Keep the audio stack running headless

On a server, the user's PipeWire services only start when that user logs in. Lingering keeps them
up so the DAC is available to the container across reboots and logouts:

```bash
# 1. Keep this user's services running when nobody is logged in.
#    This is also what makes systemd create and keep /run/user/<uid>/, which is
#    where the socket the container mounts lives.
sudo loginctl enable-linger "$USER"

# 2. Enable and start the audio services for the user session.
#    The '--user' flag is mandatory here.
systemctl --user enable --now pipewire.socket pipewire.service \
    pipewire-pulse.service wireplumber.service

# 3. Verify the services are running
systemctl --user status pipewire wireplumber --no-pager
```

When driving these from a root shell or a cron job rather than your own login session, point the
tools at the right bus first:

```bash
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"
```

### 5. Verify the host is ready

All four must pass **before** you start a container — every one is something the container
itself cannot fix:

```bash
# a) The socket the client bind-mounts exists and belongs to you.
#    This exact path goes in the compose 'volumes:' entry.
ls -l /run/user/$(id -u)/pipewire-0

# b) WirePlumber sees your DAC. Note the sink name -- a substring of it is PLAYER_NAME.
wpctl status

# c) The exact node.name for PIPEWIRE_NODE
pw-cli ls Node | grep -E 'node.name|node.description'

# d) Audio actually reaches the DAC (you should hear it)
speaker-test -c 2 -t sine -l 1
```

Then wire the results into your compose file:

| Check | Goes into |
| :-- | :-- |
| `id -u` (e.g. `1000`) | the socket path, plus `user: "<uid>:<gid>"` if it is **not** 1000 |
| socket path from (a) | `volumes: - /run/user/1000/pipewire-0:/tmp/pipewire-0` |
| sink name from (b) | `PLAYER_NAME`, or the panel's output picker |
| `node.name` from (c) | `PIPEWIRE_NODE`, or the panel's output picker |

Running the **server** role as well? Its bind mounts must be writable by the same uid, otherwise
snapserver cannot create its FIFOs or write its config — Docker creates a missing bind-mount
source as a root-owned directory:

```bash
mkdir -p ./snapserver_config /tmp/snapfifo
sudo chown -R "$(id -u):$(id -g)" ./snapserver_config /tmp/snapfifo
```

</details>

## 📟 Other roles

> **Heads-up:** the image defaults to `ROLE=panel`. A container that used to run a bare
> snapclient with no `ROLE` set will come up as the web panel instead — add
> `ROLE=snapclient` to keep the old behaviour.

Ready-to-edit compose files live in the repo:
[panel](docker-compose-panel-example.yaml) ·
[client](docker-compose-example.yaml) ·
[server](docker-compose-server-example.yaml) ·
[local build](docker-compose.yml).

<details>
<summary><b>Client role — one container, one DAC</b></summary>

```yaml
services:
  snapclient-dx5:
    image: ghcr.io/shuricksumy/snapcast-pipewire:latest
    container_name: snapclient-dx5
    network_mode: host
    cap_add:
      - SYS_NICE
    ulimits:
      rtprio: 95
      memlock: -1
    group_add:
      - audio
    environment:
      - ROLE=snapclient        # required: the image defaults to the web panel
      - SERVER_IP=127.0.0.1
      - SNAP_PORT=1704
      - CLIENT_ID=Lounge-DX5
      - PLAYER_NAME=DX5        # substring of the sink name in wpctl status, to set volume
      - INIT_VOL=0.5
      - PIPEWIRE_NODE=alsa_output.usb-Topping_DX5-00.analog-stereo
      - PIPEWIRE_LATENCY=2048/192000
    volumes:
      - /run/user/1000/pipewire-0:/tmp/pipewire-0
      - /dev/shm:/dev/shm
    restart: unless-stopped
```

</details>

<details>
<summary><b>Server role — the Snapserver itself</b></summary>

```yaml
services:
  snapserver:
    image: ghcr.io/shuricksumy/snapcast-pipewire:latest
    container_name: snapserver
    network_mode: host
    environment:
      - ROLE=snapserver        # required: the image defaults to the web panel
      - SNAP_PORT=1704
    volumes:
      # Must be writable by uid 1000: sudo chown -R 1000:1000 snapserver_config
      - ./snapserver_config:/config
      # Host /tmp/snapfifo becomes the container's /tmp, so the pipe:///tmp/snapfifo
      # sources in snapserver.conf are /tmp/snapfifo/snapfifo* on the host
      - /tmp/snapfifo:/tmp
    restart: unless-stopped
```

The tuned [`snapserver.conf`](snapserver.conf) in this repo is seeded into `/config` on
first run; your edits win from then on.

</details>

## 🔒 Running unprivileged

The image runs as **uid/gid 1000** (group `audio`), not root. 1000 is the uid that normally owns
`/run/user/1000/pipewire-0` on a desktop host, which is exactly the socket a player bind-mounts.

- **Host user is not 1000?** Check with `id -u` and pin the container to it: `user: "<uid>:<gid>"`.
- **Bind mounts** (`/config`, the FIFO directory) keep their host ownership, so
  `sudo chown -R 1000:1000 <dir>` once. The entrypoint stops with that exact instruction
  rather than failing halfway.
- Real-time scheduling still works: `cap_add: SYS_NICE` plus the `rtprio`/`memlock` ulimits are
  granted to the process regardless of the uid.

## ⚙️ Configuration

<details>
<summary><b>Environment variables</b></summary>

| Variable | Default | Description |
| :-- | :-- | :-- |
|ROLE|**panel**|`panel` (the web UI, the default), `snapclient` or `snapserver`.|
|SNAP_PORT|1704|The TCP streaming port. Ignored if `SERVER_IP` already carries a port.|
|SERVER_IP|127.0.0.1|Snapserver address. Accepts `host`, `host:port` or `tcp://host:port`. In the panel role it seeds the Add-player form.|
|CLIENT_ID|Snap-Node|(Client only) Name appearing in the Web UI (`--hostID`).|
|PLAYER_NAME|_(empty)_|(Client only) Substring of the sink name in `wpctl status`; picks which sink gets `INIT_VOL`. Empty = default sink.|
|INIT_VOL|1.0|(Client only) Volume set once at startup, 0.0–1.0. The sink is also unmuted.|
|PIPEWIRE_NODE|_(empty)_|(Client only) Target `node.name` from `pw-cli ls Node`. Empty = default sink.|
|PIPEWIRE_LATENCY|2048/192000|Buffer size / sample rate hint passed to PipeWire.|
|USE_ALSA|false|(Client only) `true` routes through the ALSA→PipeWire bridge. The panel has a per-player output picker instead.|
|SNAP_EXTRA|_(empty)_|(Client only) Extra arguments appended to the `snapclient` command line.|
|EXTRA_ARGS|_(empty)_|(Server only) Extra arguments appended to the `snapserver` command line.|
|DEBUG|false|`true` enables `set -x` tracing in the entrypoint.|

**Panel role only**

| Variable | Default | Description |
| :-- | :-- | :-- |
|PORT|8080|Port the panel listens on.|
|ADMIN_PASSWORD|_(empty)_|Set it to require HTTP Basic auth on every route. Empty = no authentication.|
|ADMIN_USER|admin|Username for the above.|
|SNAP_CONTROL_PORT|1705|Snapserver's JSON-RPC port — now playing, transport, volume. A separate listener from the stream port, so it is not derived from `SNAP_PORT`.|
|SNAP_WEB_PORT|1780|Snapweb's port, used for the header link.|
|CONFIG_DIR|/config|Where `players.json` is written. Must be writable by uid 1000.|
|POLL_SECONDS|5|How often the browser re-reads the player table.|
|BIND_HOST|0.0.0.0|Address the panel binds to.|

`SERVER_IP`, `SNAP_PORT`, `PIPEWIRE_LATENCY` and the ports above only *seed* the Add-player
form: each player stores its own copy, and the panel's Settings dialog can change the
defaults without touching compose.

</details>

<details>
<summary><b>Building it yourself</b></summary>

The Snapcast packages are **downloaded from the upstream GitHub release during the build** —
nothing is committed to this repo. Stage 0 of the Dockerfile resolves the release, picks the
`_<arch>_trixie_with-pipewire.deb` asset for the target architecture, and verifies each download
against the sha256 digest the GitHub API publishes for it. A missing asset, a missing digest or a
checksum mismatch fails the build.

The runtime stage then asserts that the installed binaries resolve all their shared libraries and
that `snapclient` is really linked against `libpipewire` — a stock distro package would install
fine and then reject `--player pipewire` only at runtime.

| Build arg | Default | Purpose |
| :-- | :-- | :-- |
|SNAPCAST_VERSION|latest|Release tag to install, e.g. `v0.35.0`. `latest` resolves the newest published release at build time; CI pins the tag it resolved so the layer caches.|
|SNAPCAST_SUITE|trixie|Debian suite variant of the release asset (`trixie`, `bookworm`, `bullseye`).|
|REFRESH_WEEK|0|Cache epoch. CI sets it to the ISO week so the weekly scheduled rebuild really re-runs `apt-get upgrade` instead of restoring a stale layer.|

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t snapcast-pipewire .

# Reproducible build against a specific release:
docker buildx build --build-arg SNAPCAST_VERSION=v0.35.0 -t snapcast-pipewire .
```

**Tests.** The panel's supervisor and API are covered by a pytest suite that needs **no
PipeWire, no DAC, no snapserver and no root** — `snapclient` is replaced by
[`tests/fake_snapclient.py`](tests/fake_snapclient.py) and the control port by
[`tests/fake_snapserver.py`](tests/fake_snapserver.py), so it runs anywhere and gates every
build in CI:

```bash
pip install flask pytest
python -m pytest tests/ -q
```

</details>

<details>
<summary><b>Bluetooth speakers</b></summary>

A Bluetooth sink is just another PipeWire node, so the panel lists it like any other output —
it only exists while the speaker is connected.

```bash
sudo apt-get update
sudo apt-get install bluetooth bluez bluez-tools alsa-utils
```

Pair with the Go TUI rather than raw `bluetoothctl` — install from
[bluetuith-org/bluetuith](https://github.com/bluetuith-org/bluetuith), or unpack the prebuilt
binary from [`utils`](utils/):

```bash
# uname -m reports aarch64, but the tarball is named arm64
case "$(uname -m)" in aarch64) ARCH=arm64 ;; *) ARCH=x86_64 ;; esac
tar -xzf "utils/bluetuith_0.2.6_Linux_${ARCH}.tar.gz" -C /tmp bluetuith
sudo install -m 755 /tmp/bluetuith /usr/local/bin/bluetuith

bluetuith        # scan, pair, trust, connect
```

Then find the node name for the player:

```bash
pw-cli ls Node | grep -E 'node.name|node.description'
# e.g. bluez_output.20_18_12_00_07_C4.1
```

A JBL Charge 5 that refuses to behave is covered by [`JBL-fix.sh`](JBL-fix.sh).

</details>

## 📝 Notes

- **No mDNS.** This image does not run `avahi-daemon`, so Zeroconf announcement is not
  available — point clients at `SERVER_IP` explicitly. (`snapserver` logs a harmless
  `Avahi: Failed to create client` line at startup.)
- **Healthcheck** tracks the *process*, not the connection: a client retrying against an
  unreachable server still reports healthy, because it is alive and doing what it should.
- **Maintained image.** Rebuilt weekly so Debian security updates and new Snapcast releases
  land without a commit, and scanned with Trivy on every push.

## 📜 License

MIT — see [LICENSE](LICENSE). Snapcast itself is GPL-3.0, and its packages are downloaded
from the [upstream release](https://github.com/snapcast/snapcast/releases) at build time.
