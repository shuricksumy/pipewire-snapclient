#!/usr/bin/env python3
"""Stand-in for snapclient: prints its arguments, then behaves as told.

Lets the whole suite run in CI with no PipeWire, no DAC and no root.

FAKE_SNAPCLIENT_MODE:
  run    stay alive until terminated (default)
  crash  exit non-zero immediately, to exercise the restart backoff

FAKE_ALSA_MODE:
  hardware  `-l` lists a hw: device as well as the plugins (default)
  plugins   `-l` lists only conversion plugins, i.e. /dev/snd was not passed
            through -- the case the panel warns about
"""
import os
import signal
import sys
import time

# `snapclient -l` prints "<index>: <name>" with the description on the next
# line. Without this the panel's enumeration would block until its timeout.
if "-l" in sys.argv[1:] or "--list" in sys.argv[1:]:
    entries = [("null", "Discard all samples (playback) or generate zero samples"),
               ("lavrate", "Rate Converter Plugin Using Libav/FFmpeg Library"),
               ("samplerate", "Rate Converter Plugin Using Samplerate Library")]
    if os.environ.get("FAKE_ALSA_MODE", "hardware") == "hardware":
        entries += [("default", "Playback/recording through the PulseAudio sound server"),
                    ("hw:CARD=DX5,DEV=0", "Topping DX5, USB Audio - Direct hardware device"),
                    ("plughw:CARD=DX5,DEV=0", "Topping DX5, USB Audio - Hardware device with all software conversions")]
    for index, (name, description) in enumerate(entries):
        print("%d: %s" % (index, name))
        print(description)
        print()
    sys.exit(0)

print("snapclient args: %s" % " ".join(sys.argv[1:]), flush=True)
print("PIPEWIRE_NODE=%s" % os.environ.get("PIPEWIRE_NODE", ""), flush=True)
print("PIPEWIRE_LATENCY=%s" % os.environ.get("PIPEWIRE_LATENCY", ""), flush=True)

if os.environ.get("FAKE_SNAPCLIENT_MODE") == "crash":
    print("simulated failure", flush=True)
    sys.exit(3)

signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
while True:
    time.sleep(0.2)
