#!/bin/bash

# --- 0. INSTALLATION ---
# Install PipeWire, the Session Manager (WirePlumber), and Audio Tools
echo ">>> Installing PipeWire and Audiophile Tools..."
sudo apt update
sudo apt install -y pipewire pipewire-audio-client-libraries \
    wireplumber pipewire-pulse alsa-utils pulseaudio-utils rtkit-daemon

# --- 1. USER SETUP ---
# Create a dedicated 'pipewire' user for the audio engine
echo ">>> Creating dedicated audio user..."
sudo useradd -m -s /bin/bash pipewire || echo "User already exists"
sudo usermod -aG audio,video,rtkit pipewire

# Enable 'lingering' so PipeWire starts on boot without needing a GUI login
echo ">>> Enabling service lingering for user 'pipewire'..."
sudo loginctl enable-linger pipewire

# --- 2. ENVIRONMENT CONFIG ---
# Ensure the user session knows where its runtime bus is
echo ">>> Configuring user profile..."
sudo -u pipewire bash -c "grep -q 'XDG_RUNTIME_DIR' ~/.bashrc || echo 'export XDG_RUNTIME_DIR=/run/user/\$(id -u)' >> ~/.bashrc"

# --- 3. BIT-PERFECT CONFIGURATION ---
# Create the config directory for the 'pipewire' user
echo ">>> Applying Audiophile Bit-Perfect Config..."
sudo -u pipewire mkdir -p /home/pipewire/.config/pipewire/pipewire.conf.d/

# Create the bit-perfect override
sudo -u pipewire tee /home/pipewire/.config/pipewire/pipewire.conf.d/bitperfect.conf <<EOF
context.properties = {
    ## Bit-Perfect Switching: Start at 44.1k, allow up to 384k for Topping DX5
    default.clock.rate          = 44100
    default.clock.allowed-rates = [ 44100 48000 88200 96000 176400 192000 352800 384000 ]
    
    ## Latency & Stability (Quantum)
    default.clock.min-quantum   = 32
    default.clock.max-quantum   = 8192
    
    ## High-Quality Path & Resampling
    stream.properties = {
        resample.quality      = 14
        channelmix.normalize  = false
        channelmix.mix-lfe    = false
    }
}

context.modules = [
    { name = libpipewire-module-rt
        args = {
            nice.level   = -11
            rt.prio      = 88
        }
        flags = [ ifexists nofail ]
    }
]
EOF

# --- 4. START SERVICES ---
# We force the user services to start immediately
echo ">>> Starting PipeWire Services..."
PIPE_UID=$(id -u pipewire)
sudo -u pipewire XDG_RUNTIME_DIR=/run/user/$PIPE_UID systemctl --user enable --now pipewire pipewire-pulse wireplumber

echo "--------------------------------------------------------"
echo "  SETUP COMPLETE! Topping DX5 is ready for Hi-Fi.       "
echo "--------------------------------------------------------"

# --- 5. USEFUL DIAGNOSTIC COMMANDS ---
cat <<EOF

--- AUDIO STATION TOOLKIT ---

Check if Topping DX5 is Default Output:
  sudo -u pipewire wpctl status

Monitor Sample Rate & Bit-Depth (Real-Time):
  sudo -u pipewire pw-top

Check Hardware Level Clock/Format (The Truth):
  cat /proc/asound/card*/pcm0p/sub0/hw_params

Play a High-Res File to test:
  sudo -u pipewire pw-play --target [ID] /path/to/music.flac

View Active Settings:
  sudo -u pipewire pw-metadata -n settings

EOF