# Snapcast-PipeWire

A high-performance, multi-architecture (amd64, arm64) Docker container running Snapcast v0.35.0 with native PipeWire support. Optimized for bit-perfect audio delivery to high-end DACs like the Topping DX5.

## Features
- Dual Role Strategy: Use a single image for both snapserver and snapclient.

- Native PipeWire: Built against the with-pipewire Debian package for ultra-low latency and bit-perfect sample rate switching.

- Auto-Bootstrap: Automatically creates a professional snapserver.conf in your mounted volume on the first run.

- Hardware Control: Automatically initializes your DAC volume to 1.0 (100%) on startup via wpctl.

## 🛠️ Host Setup (Preparation)

Before running the container, your host system must be configured for PipeWire bit-perfect output.

### Install PipeWire & Tools

Run the following on your host machine to install the necessary audio stack:


```bash
sudo apt update && sudo apt install -y pipewire pipewire-audio-client-libraries \
    wireplumber pipewire-pulse alsa-utils rtkit-daemon
```

### Configure Bit-Perfect Output

To allow your DAC to switch sample rates without resampling, create a configuration override for PipeWire:

```bash
mkdir -p ~/.config/pipewire/pipewire.conf.d/
cat <<EOF > ~/.config/pipewire/pipewire.conf.d/bitperfect.conf
context.properties = {
    default.clock.rate          = 44100
    default.clock.allowed-rates = [ 44100 48000 88200 96000 176400 192000 352800 384000 ]
    default.clock.min-quantum   = 32
    default.clock.max-quantum   = 8192
}
EOF
```
Restart PipeWire with ```systemctl --user restart pipewire wireplumber```

### ENVIRONMENT CONFIG

Add your user to all needed groups:
```Bash
sudo usermod -aG bluetooth,audio,lp,pulse-access,video,render,docker $USER
```

Ensure the user session knows where its PipeWire runtime bus is located. This is critical for wpctl and PipeWire to communicate.

```Bash
echo ">>> Configuring environment for PipeWire..."

# Add to .bashrc if not already present
if ! grep -q "XDG_RUNTIME_DIR" ~/.bashrc; then
  echo 'export XDG_RUNTIME_DIR="/run/user/$(id -u)"' >> ~/.bashrc
  echo ">>> XDG_RUNTIME_DIR added to ~/.bashrc"
fi

# Apply to current session immediately
export XDG_RUNTIME_DIR="/run/user/$(id -u)"

# Verification: This should show your DAC (Topping DX5)
wpctl status
```

### Host Audio Stack Activation
On a server, PipeWire services usually only start when a user logs in physically. To ensure your Topping DX5 is always available to the Docker container, run the following:

```Bash
# 1. Enable 'Linger' for your user. 
# This ensures PipeWire/WirePlumber stay running even when you are logged out.
sudo loginctl enable-linger $(whoami)

# 2. Configure the Environment for the current session
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"

# 3. Enable and Start the Audio Services for the user session
# The '--user' flag is mandatory here.
systemctl --user enable --now pipewire.service pipewire-pulse.service wireplumber.service

# 4. Verify Services are Running
systemctl --user status pipewire wireplumber --no-pager
```

## 🎧 Bluetooth Hi-Fi Playing Guide

### Install the Core Engine

This installs the Bluetooth daemon, the ALSA bridge, and the management utilities.
```Bash
sudo apt-get update
sudo apt-get install bluetooth bluez bluez-tools alsa-utils
```

### Manage Devices (The "Lazy" TUI)

- Install go pacakge ```https://github.com/bluetuith-org/bluetuith``` or use from ``` utils ``` folder 

- Instead of complex commands, use the Go-based TUI to scan and pair:
```Bash
# Start the manager
~/go/bin/bluetuith
```
- Identify Node Names
Use this to find the Permanent Name of your FiiO, JBL, or Topping DX5:
```Bash
pw-cli ls Node | grep -E 'node.name|node.description'
```

- Set in docker compose your node like
```
- PIPEWIRE_NODE="bluez_output.20_18_12_00_07_C4.1"
```

##  🚀 Deployment (Docker Compose)

### Server Role (The Engine)

This instance manages audio streams and the Web UI.

```yaml
services:
  snapserver:
    image: ghcr.io/${GITHUB_USER}/snapcast-pipewire:latest
    container_name: snapserver
    network_mode: host
    environment:
      - ROLE=snapserver
      - SNAP_PORT=1704
    volumes:
      - ./snapserver_config:/config
      - /tmp/snapfifo:/tmp
    restart: unless-stopped
```

### Client Role (The Topping DX5 Node)
This instance connects to the server and outputs to your DAC.

```yaml
snapclient-dx5:
    image: ghcr.io/${GITHUB_USER}/snapcast-pipewire:latest
    container_name: snapclient-dx5
    network_mode: host
    cap_add:
      - SYS_NICE
    ulimits:
      rtprio: 99
      memlock: -1
    environment:
      - ROLE=snapclient
      - SERVER_IP=127.0.0.1
      - CLIENT_ID=Lounge-DX5
      - PLAYER_NAME=DX5 # part of name like in wpctl status Audio - to set volume
      - PIPEWIRE_NODE=alsa_output.usb-Topping_DX5-00.analog-stereo
      - PIPEWIRE_LATENCY=2048/192000
    volumes:
      - /run/user/1000/pipewire-0:/tmp/pipewire-0
      - /dev/shm:/dev/shm
      - /dev/snd:/dev/snd
    restart: unless-stopped
```

## ⚙️ Configuration Variables

| Variable | Default | Description |
| :-- | :-- | :-- |
|ROLE|snapclient|Set to ```snapserver``` or ```snapclient```|
|SNAP_PORT|1704|The TCP streaming port.|
|SERVER_IP|127.0.0.1|(Client only) IP of the Snapserver.|
|CLIENT_ID|Snap-Node|(Client only) Name appearing in the Web UI.|
|PIPEWIRE_NODE|50 or {name}|(Client only) The target output string from wpctl status.|
|PIPEWIRE_LATENCY|2048/192000|Defines the buffer size and sample rate.|
|USE_ALSA| false | Set to true to use ALSA bridge (best for dynamic sample rates, but adds latency)|
|INIT_VOL| Initial volume level (0.0 to 1.0) |See Compose|


## 🏗️ Build Requirements

The Dockerfile expects the local Debian packages in the pkg/ folder to support the multi-arch build:
```Bash
pkg/snapclient_*_amd64_*_with-pipewire.deb
pkg/snapclient_*_arm64_*_with-pipewire.deb
pkg/snapserver_*_amd64_*.deb
pkg/snapserver_*_arm64_*.deb
```

Build Command:

```Bash
docker buildx build --platform linux/amd64,linux/arm64 -t snapcast-pipewire
```