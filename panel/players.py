"""Supervised snapclient players.

Each player is a long-running `snapclient` child of this container, bound to one
PipeWire sink through its own PIPEWIRE_NODE. That per-child environment is the
whole point of supervising them in-process: N players, N sinks, one container,
instead of one container per DAC.

The launch recipe mirrors entrypoint.sh's ROLE=snapclient (same env, same
arguments, same 5s->60s reconnect backoff that resets after a healthy session),
so a player here behaves exactly like a snapclient container this image
produces. Anything you could set with SERVER_IP / CLIENT_ID / PIPEWIRE_NODE /
PIPEWIRE_LATENCY / SNAP_EXTRA is a field on a player, and a player can send its
audio to an ALSA device directly instead of through PipeWire.

Trade-off worth knowing: restarting this container stops every player. The
container-per-player approach survives a panel restart; this one does not.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque

import snapctl

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
CONFIG_PATH = os.path.join(CONFIG_DIR, "players.json")

SNAPCLIENT = os.environ.get("SNAPCLIENT", "snapclient")
PW_DUMP = os.environ.get("PW_DUMP", "pw-dump")
WPCTL = os.environ.get("WPCTL", "wpctl")

# Same backoff shape as entrypoint.sh: a session that stayed up for a while is
# not part of a failure streak, so the delay resets rather than carrying a 60s
# penalty over from an outage that has long since been fixed.
RETRY_START = 5.0
RETRY_MAX = 60.0
HEALTHY_AFTER = 60.0

# How long to wait for a player's sink to appear before giving up and reporting
# "waiting". Covers a DAC that is powered on a moment after the panel, and the
# window where PipeWire is still enumerating devices.
NODE_WAIT_SECONDS = 20.0

# Watchdog. snapclient does NOT exit when its output sink disappears -- unplug
# the DAC mid-stream and the process sits there happily, having quietly closed
# ALSA. A supervisor that only watches for process exit therefore reports a
# healthy player that cannot make a sound, forever. So while a player runs we
# also watch its sink and restart once it has been gone long enough to not be a
# blip: sinks briefly vanish when a DAC re-clocks for a new sample rate, and
# restarting on that would interrupt playback for no reason.
HEALTH_INTERVAL = 3.0
SINK_GRACE = 15.0

LOG_LINES = 200

# Any printable text, up to 64 characters. Deliberately permissive: these values
# are passed to snapclient as single argv elements, never through a shell, so the
# pattern is not a security boundary -- it only keeps control characters (which
# would corrupt the log stream and the JSON config) out. An ASCII-only rule
# would reject perfectly reasonable names like "Kitchen · DX5" or Cyrillic ones.
NAME_RE = re.compile(r"[^\x00-\x1f\x7f]{1,64}")
NODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
# ALSA PCM names are richer than node names: "hw:CARD=Codec,DEV=0", "plughw:1,0".
ALSA_DEVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:=,+/()-]{0,127}$")
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:_-]{0,255}$")
LATENCY_RE = re.compile(r"^\d{1,7}/\d{1,7}$")


def _env_int(name, fallback):
    try:
        return int(os.environ.get(name) or fallback)
    except ValueError:
        return fallback


# Seeds for a newly created player, read from the same environment variables the
# single-player roles use -- so a compose file that already sets SERVER_IP gets
# the right default in the Add-player form without saying it twice.
SNAPSERVER_HOST = os.environ.get("SERVER_IP", "")
SNAPSERVER_PORT = _env_int("SNAP_PORT", 1704)
SNAPSERVER_CONTROL_PORT = _env_int("SNAP_CONTROL_PORT", snapctl.DEFAULT_CONTROL_PORT)
# Snapserver's own web UI (snapweb). Not used by the panel, only linked to: it
# owns groups, stream assignment and every client on the server, which is
# deliberately more than this panel tries to be.
SNAPSERVER_WEB_PORT = _env_int("SNAP_WEB_PORT", 1780)
DEFAULT_LATENCY = os.environ.get("PIPEWIRE_LATENCY", "") or ""

DEFAULTS = {
    "name": "",
    "client_id": "",
    # "pipewire" plays into the host's PipeWire session and binds to one sink.
    # "alsa" talks to an ALSA device directly, which is the way out when
    # PipeWire is broken, absent, or simply not what you want on this host.
    "output_mode": "pipewire",
    "node": "",
    "alsa_device": "default",
    "server": SNAPSERVER_HOST or "127.0.0.1",
    "port": SNAPSERVER_PORT,
    "pipewire_latency": DEFAULT_LATENCY,
    "latency_ms": 0,
    "volume": 1.0,
    "autostart": True,
    "extra": "",
    # Snapserver's JSON-RPC port. Audio is 1704, control is 1705; they are
    # separate listeners, so this is not derived from `port`.
    "control_port": SNAPSERVER_CONTROL_PORT,
}


def _default_settings():
    """Panel-wide settings, editable from the web and stored with the players.

    The environment only seeds them: once saved, the stored value wins, so the
    panel can be re-pointed at another snapserver without touching compose.
    """
    return {
        "snapserver_host": SNAPSERVER_HOST,
        "snapserver_port": SNAPSERVER_PORT,
        "snapserver_control_port": SNAPSERVER_CONTROL_PORT,
        "snapserver_web_port": SNAPSERVER_WEB_PORT,
    }


class SettingsError(ValueError):
    status = 400


class PlayerError(ValueError):
    """A player definition was rejected."""

    status = 400


def validate_settings(patch, current=None):
    defaults = _default_settings()
    clean = dict(current or defaults)
    for key, value in (patch or {}).items():
        if key not in defaults:
            raise SettingsError("unknown setting %r" % key)
        clean[key] = value

    host = str(clean.get("snapserver_host", "")).strip()
    if host and not HOST_RE.fullmatch(host):
        raise SettingsError("invalid snapserver address")
    clean["snapserver_host"] = host

    for key, label in (
        ("snapserver_port", "port"),
        ("snapserver_control_port", "control port"),
        ("snapserver_web_port", "web port"),
    ):
        try:
            clean[key] = int(clean[key])
        except (TypeError, ValueError):
            raise SettingsError("%s must be a number" % label) from None
        if not 1 <= clean[key] <= 65535:
            raise SettingsError("%s must be between 1 and 65535" % label)

    return clean


def _pw_env():
    env = dict(os.environ)
    env.setdefault("PIPEWIRE_RUNTIME_DIR", "/tmp")
    env.setdefault("PIPEWIRE_REMOTE", "pipewire-0")
    return env


def list_sinks():
    """Audio sinks PipeWire exposes right now, fresh on every call.

    Best effort: no PipeWire socket means no sinks, not an error -- the panel
    still lists and runs players, it just cannot pre-validate their nodes.
    """
    if not shutil.which(PW_DUMP):
        return []
    try:
        raw = subprocess.run(
            [PW_DUMP], capture_output=True, text=True, timeout=10, env=_pw_env()
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if raw.returncode != 0:
        return []
    try:
        objects = json.loads(raw.stdout or "[]")
    except ValueError:
        return []

    sinks = []
    for item in objects:
        if not isinstance(item, dict):
            continue
        props = (item.get("info") or {}).get("props") or {}
        if props.get("media.class") != "Audio/Sink":
            continue
        name = props.get("node.name")
        if not name:
            continue
        sinks.append(
            {
                "id": item.get("id"),
                "node": name,
                "description": props.get("node.description") or name,
                # Worth surfacing: a Bluetooth sink only exists while the
                # speaker is connected, so an absent one is normal for it.
                "bluetooth": name.startswith("bluez_output."),
            }
        )
    sinks.sort(key=lambda s: s["node"])
    return sinks


_alsa_cache = {"at": 0.0, "devices": []}
ALSA_CACHE_SECONDS = 30.0


def list_alsa_devices(max_age=ALSA_CACHE_SECONDS):
    """ALSA outputs, straight from `snapclient -l`.

    This is the escape hatch for a host where PipeWire is broken or was never
    set up: snapclient can address the hardware itself. Best effort -- if the
    binary is missing the panel simply offers no ALSA choices.

    snapclient prints "<index>: <name>" with the description on the next line,
    so the name is what gets stored; `-s` takes an index or a name, and the name
    survives devices being plugged in and renumbered.
    """
    # Cached: the list only changes when hardware is plugged in, while the panel
    # polls its config every few seconds and this costs a subprocess.
    if max_age and time.time() - _alsa_cache["at"] < max_age:
        return _alsa_cache["devices"]

    if not shutil.which(SNAPCLIENT) and not os.path.exists(SNAPCLIENT):
        return []
    try:
        raw = subprocess.run(
            [SNAPCLIENT, "-l"], capture_output=True, text=True, timeout=15,
            env=_pw_env(),
        )
    except (OSError, subprocess.SubprocessError):
        _alsa_cache.update(at=time.time(), devices=[])
        return []

    devices = []
    pending = None
    for line in (raw.stdout or "").splitlines():
        head = re.match(r"^\s*(\d+):\s*(\S.*)$", line)
        if head:
            pending = {"device": head.group(2).strip(), "description": ""}
            if ALSA_DEVICE_RE.match(pending["device"]):
                devices.append(pending)
            else:
                pending = None
            continue
        if pending is not None and line.strip():
            pending["description"] = line.strip()
            pending = None

    for device in devices:
        name = device["device"]
        # Entries that address hardware rather than a conversion plugin. Shown
        # first because they are what somebody bypassing PipeWire is looking for.
        device["hardware"] = name.startswith(
            ("hw:", "plughw:", "sysdefault", "front:", "iec958:", "dmix:")
        ) or name == "default"
        device["description"] = device["description"] or name

    devices.sort(key=lambda d: (not d["hardware"], d["device"]))
    _alsa_cache.update(at=time.time(), devices=devices)
    return devices


def sink_present(node):
    return any(s["node"] == node for s in list_sinks())


def set_sink_volume(node, volume):
    """Unmute and set a sink's volume -- what entrypoint.sh does once at start."""
    if not shutil.which(WPCTL):
        return
    target = next((s for s in list_sinks() if s["node"] == node), None)
    if target is None or target.get("id") is None:
        return
    for args in (
        [WPCTL, "set-mute", str(target["id"]), "0"],
        [WPCTL, "set-volume", str(target["id"]), "%.2f" % volume],
    ):
        try:
            subprocess.run(args, capture_output=True, timeout=10, env=_pw_env())
        except (OSError, subprocess.SubprocessError):
            return


def validate(config, existing_names=(), existing_ids=()):
    """Normalise and check a player definition coming from the browser.

    Every value here ends up in an argv element or an environment variable. argv
    is built as a list so there is no shell to inject into, but the patterns
    still keep obvious nonsense out of the config file and give the user a real
    error instead of a snapclient that dies three seconds after it starts.
    """
    config = dict(config or {})
    # Players stored before the output picker existed carry use_alsa, which meant
    # "the ALSA bridge to pcm.default". Translate rather than silently moving
    # them back onto PipeWire when their config is next written out.
    if "use_alsa" in config and "output_mode" not in config:
        config["output_mode"] = "alsa" if config.pop("use_alsa") else "pipewire"
        config.setdefault("alsa_device", "default")

    clean = dict(DEFAULTS)
    clean.update({k: v for k, v in config.items() if k in DEFAULTS})

    clean["name"] = str(clean["name"]).strip()
    if not NAME_RE.fullmatch(clean["name"]):
        raise PlayerError(
            "name must be 1-64 printable characters (no control characters)"
        )
    if clean["name"] in existing_names:
        raise PlayerError("a player named %r already exists" % clean["name"])

    # CLIENT_ID is what snapclient registers as (--hostID); the name is only for
    # this panel. Defaulting one to the other keeps the common case to one field.
    clean["client_id"] = str(clean["client_id"]).strip() or clean["name"]
    if not NAME_RE.fullmatch(clean["client_id"]):
        raise PlayerError(
            "client id must be 1-64 printable characters (no control characters)"
        )
    if clean["client_id"] in existing_ids:
        raise PlayerError(
            "another player already registers as %r -- ids must be unique on the "
            "snapserver" % clean["client_id"]
        )

    clean["output_mode"] = str(clean["output_mode"]).strip() or "pipewire"
    if clean["output_mode"] not in ("pipewire", "alsa"):
        raise PlayerError("output mode must be 'pipewire' or 'alsa'")

    clean["node"] = str(clean["node"]).strip()
    if clean["node"] and not NODE_RE.match(clean["node"]):
        raise PlayerError("invalid PipeWire node name")

    clean["alsa_device"] = str(clean["alsa_device"]).strip()
    if clean["output_mode"] == "alsa":
        if not clean["alsa_device"]:
            raise PlayerError("an ALSA output needs a device name")
        if not ALSA_DEVICE_RE.match(clean["alsa_device"]):
            raise PlayerError("invalid ALSA device name")

    clean["server"] = str(clean["server"]).strip()
    if not HOST_RE.match(clean["server"]):
        raise PlayerError("invalid server address")

    try:
        clean["port"] = int(clean["port"])
    except (TypeError, ValueError):
        raise PlayerError("port must be a number") from None
    if not 1 <= clean["port"] <= 65535:
        raise PlayerError("port must be between 1 and 65535")

    try:
        clean["control_port"] = int(clean["control_port"])
    except (TypeError, ValueError):
        raise PlayerError("control port must be a number") from None
    if not 1 <= clean["control_port"] <= 65535:
        raise PlayerError("control port must be between 1 and 65535")

    try:
        clean["latency_ms"] = int(clean["latency_ms"])
    except (TypeError, ValueError):
        raise PlayerError("latency must be a whole number of milliseconds") from None
    if not -2000 <= clean["latency_ms"] <= 2000:
        raise PlayerError("latency must be between -2000 and 2000 ms")

    try:
        clean["volume"] = float(clean["volume"])
    except (TypeError, ValueError):
        raise PlayerError("volume must be a number") from None
    if not 0.0 <= clean["volume"] <= 1.0:
        raise PlayerError("volume must be between 0.0 and 1.0")

    clean["pipewire_latency"] = str(clean["pipewire_latency"]).strip()
    if clean["pipewire_latency"] and not LATENCY_RE.match(clean["pipewire_latency"]):
        raise PlayerError("PipeWire latency must look like 2048/192000")

    clean["autostart"] = bool(clean["autostart"])

    # SNAP_EXTRA is split with shlex and appended to argv, never shell-evaluated.
    clean["extra"] = str(clean["extra"]).strip()
    if len(clean["extra"]) > 200:
        raise PlayerError("extra arguments are too long")
    try:
        shlex.split(clean["extra"])
    except ValueError as exc:
        raise PlayerError("extra arguments are not parseable: %s" % exc) from None

    return clean


class Player:
    """One supervised snapclient process."""

    def __init__(self, config, supervisor):
        self.config = config
        self.id = config["id"]
        self._supervisor = supervisor
        self._proc = None
        self._thread = None
        self._wake = threading.Event()  # interrupts the backoff sleep
        self._lock = threading.RLock()

        self.desired = False
        self.named_on_server = False
        self.state = "stopped"
        self.detail = ""
        self.started_at = None
        self.restarts = 0
        self.last_exit = None
        self.logs = deque(maxlen=LOG_LINES)

    # ---- reporting ---------------------------------------------------------

    @property
    def client_id(self):
        return self.config.get("client_id") or self.config["name"]

    def status(self):
        with self._lock:
            return {
                **self.config,
                "client_id": self.client_id,
                "state": self.state,
                "detail": self.detail,
                "running": self.state == "running",
                "uptime": (time.time() - self.started_at) if self.started_at else 0,
                "restarts": self.restarts,
                "last_exit": self.last_exit,
                "node_present": None,  # filled in by the supervisor, which batches
            }

    def log(self, line):
        self.logs.append("%s %s" % (time.strftime("%H:%M:%S"), line.rstrip()))

    # ---- lifecycle ---------------------------------------------------------

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                self.desired = True
                self._wake.set()  # cut short a backoff sleep
                return
            self.desired = True
            self.state = "starting"
            self.detail = ""
            self._wake.clear()
            self._thread = threading.Thread(
                target=self._supervise, name="player-%s" % self.id, daemon=True
            )
            self._thread.start()

    def stop(self, timeout=10.0):
        with self._lock:
            self.desired = False
            proc = self._proc
            thread = self._thread
        self._wake.set()
        if proc is not None:
            self._terminate(proc)
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                # Belt and braces: the lock in _supervise should make this
                # unreachable, but never leave a live snapclient behind.
                with self._lock:
                    proc = self._proc
                if proc is not None:
                    self._terminate(proc)
                thread.join(timeout=timeout)
        with self._lock:
            self.state = "stopped"
            # A stopped player must not keep showing what it was doing: stale
            # detail (and, in the UI, stale now-playing) reads as if it were
            # still running.
            self.detail = ""
            self.started_at = None

    def _terminate(self, proc):
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        except OSError:
            pass

    # ---- the supervisor loop -----------------------------------------------

    def _supervise(self):
        delay = RETRY_START
        while self.desired:
            ready, why = self._prepare()
            if not ready:
                with self._lock:
                    self.state = "waiting"
                    self.detail = why
                self.log("not ready: %s" % why)
                if self._sleep(delay):
                    break
                delay = min(delay * 2, RETRY_MAX)
                continue

            started = time.time()
            proc = None
            # Deciding to launch and recording the child must be atomic against
            # stop(). Otherwise a stop that lands in between reads self._proc as
            # None, terminates nothing, and leaves an orphaned snapclient holding
            # the sink for good -- with no way to stop it from the panel.
            with self._lock:
                if not self.desired:
                    break
                try:
                    proc = self._spawn()
                except OSError as exc:
                    self.state = "failed"
                    self.detail = "cannot start snapclient: %s" % exc
                else:
                    self._proc = proc
                    self.state = "running"
                    self.detail = ""
                    self.started_at = started

            if proc is None:
                self.log(self.detail)
                if self._sleep(delay):
                    break
                delay = min(delay * 2, RETRY_MAX)
                continue

            if (self._pipewire_output() and self.config.get("node")
                    and self.config.get("volume") is not None):
                set_sink_volume(self.config["node"], self.config["volume"])

            # The output pump has to run alongside the watchdog, not instead of
            # it: reading the child's stdout blocks until the child exits.
            reader = threading.Thread(
                target=self._pump,
                args=(proc,),
                name="player-%s-log" % self.id,
                daemon=True,
            )
            reader.start()
            self._watch(proc)
            code = proc.wait()
            reader.join(timeout=2)

            with self._lock:
                self._proc = None
                self.started_at = None
                self.last_exit = code
            self.log("snapclient exited with code %s" % code)

            if not self.desired:
                break

            with self._lock:
                self.restarts += 1
            # A session that stayed up is not part of a failure streak.
            if time.time() - started >= HEALTHY_AFTER:
                delay = RETRY_START
            with self._lock:
                self.state = "backoff"
                self.detail = "restarting in %ds" % int(delay)
            if self._sleep(delay):
                break
            delay = min(delay * 2, RETRY_MAX)

        with self._lock:
            if not self.desired:
                self.state = "stopped"
                self.detail = ""

    def _watch(self, proc):
        """Wait for the child to exit, restarting it if its sink goes away.

        PipeWire outputs only: an ALSA device is not in the graph, so there is
        nothing to poll and snapclient reports the device failing by itself.
        """
        node = self.config.get("node") if self._pipewire_output() else None
        absent_since = None
        while proc.poll() is None:
            if not self.desired:
                return
            if node and not sink_present(node):
                absent_since = absent_since or time.time()
                gone = time.time() - absent_since
                if gone >= SINK_GRACE:
                    self.log(
                        "sink %s has been gone %ds -- restarting the player"
                        % (node, int(gone))
                    )
                    with self._lock:
                        self.state = "waiting"
                        self.detail = "output sink disappeared"
                    self._terminate(proc)
                    return
                with self._lock:
                    self.detail = "sink missing for %ds" % int(gone)
            elif absent_since is not None:
                absent_since = None
                self.log("sink %s is back" % node)
                with self._lock:
                    self.detail = ""
            self._wake.wait(timeout=HEALTH_INTERVAL)
            if not self.desired:
                return
            self._wake.clear()

    def _sleep(self, seconds):
        """Interruptible backoff. Returns True if we were told to stop."""
        self._wake.wait(timeout=seconds)
        self._wake.clear()
        return not self.desired

    def _prepare(self):
        """Wait for the player's sink, so we do not launch into a missing DAC."""
        node = self.config.get("node") if self._pipewire_output() else None
        if not node:
            return True, ""

        # Say why we are sitting here. Without this the row keeps whatever state
        # it had -- "starting" on the first attempt, "backoff" on a later one --
        # with no explanation for the whole wait, which reads as a hung panel
        # rather than a DAC that has not been switched on yet.
        with self._lock:
            self.state = "waiting"
            self.detail = "waiting for sink %s" % node

        deadline = time.time() + NODE_WAIT_SECONDS
        while time.time() < deadline:
            if sink_present(node):
                with self._lock:
                    self.detail = ""
                return True, ""
            if not self.desired:
                return False, "stopped"
            time.sleep(1.0)

        if not shutil.which(PW_DUMP):
            # No way to check; let snapclient try and report for itself.
            return True, ""
        return False, "sink %s is not present (is the device connected?)" % node

    def _pipewire_output(self):
        return self.config.get("output_mode", "pipewire") != "alsa"

    def _spawn(self):
        cfg = self.config
        env = _pw_env()
        # Per-child environment is what makes several players in one container
        # work: each one points at its own sink. Meaningless for an ALSA output,
        # which goes straight to the device rather than through the graph.
        if self._pipewire_output():
            if cfg.get("node"):
                env["PIPEWIRE_NODE"] = cfg["node"]
            if cfg.get("pipewire_latency"):
                env["PIPEWIRE_LATENCY"] = cfg["pipewire_latency"]

        args = [SNAPCLIENT, "--hostID", self.client_id]
        if self._pipewire_output():
            args += ["--player", "pipewire"]
        else:
            # Straight to the card. "default" still lands on PipeWire via
            # /etc/asound.conf; a hw:... device bypasses it entirely.
            args += ["--player", "alsa", "-s", cfg.get("alsa_device") or "default"]
        if cfg.get("latency_ms"):
            args += ["--latency", str(cfg["latency_ms"])]
        if cfg.get("extra"):
            args += shlex.split(cfg["extra"])
        args.append("tcp://%s:%d" % (cfg["server"], cfg["port"]))

        self.log("launching: %s" % " ".join(args))
        return subprocess.Popen(
            args,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

    def _pump(self, proc):
        """Drain the child's output into the ring buffer until it exits."""
        if proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                self.log(line)
        except (OSError, ValueError):
            pass


class Supervisor:
    def __init__(self, config_path=CONFIG_PATH):
        self._players = {}
        self._lock = threading.RLock()
        self.config_path = config_path
        self.settings = _default_settings()
        self.load()

    # ---- persistence -------------------------------------------------------

    def load(self):
        try:
            with open(self.config_path) as handle:
                stored = json.load(handle)
        except (OSError, ValueError):
            # A missing config is the first run; a corrupt one must not stop the
            # panel booting, or there is no way to fix it from the web.
            return
        try:
            self.settings = validate_settings(stored.get("settings") or {})
        except SettingsError:
            self.settings = _default_settings()
        for entry in stored.get("players", []):
            try:
                config = validate(entry)
            except PlayerError:
                continue
            config["id"] = entry.get("id") or uuid.uuid4().hex[:8]
            with self._lock:
                self._players[config["id"]] = Player(config, self)

    def save(self):
        with self._lock:
            payload = {
                "settings": self.settings,
                "players": [p.config for p in self._players.values()],
            }
        try:
            os.makedirs(os.path.dirname(self.config_path) or ".", exist_ok=True)
            tmp = self.config_path + ".tmp"
            with open(tmp, "w") as handle:
                json.dump(payload, handle, indent=2)
            os.replace(tmp, self.config_path)  # atomic: never a half-written config
        except OSError:
            pass

    def update_settings(self, patch):
        with self._lock:
            self.settings = validate_settings(patch, self.settings)
        self.save()
        return self.settings

    # ---- CRUD --------------------------------------------------------------

    def list(self, with_snapcast=True):
        sinks = {s["node"] for s in list_sinks()}
        with self._lock:
            players = list(self._players.values())

        out = []
        for player in players:
            status = player.status()
            status["node_present"] = (
                status["node"] in sinks if status["node"] else None
            )
            status["snapcast"] = None
            status["snapcast_error"] = None
            out.append(status)

        if with_snapcast:
            self._attach_snapcast(players, out)

        out.sort(key=lambda p: p["name"].lower())
        return out

    def _attach_snapcast(self, players, statuses):
        """Fold in what the snapserver knows: now playing, volume, capabilities.

        One Server.GetStatus per distinct server covers every player and the
        result is briefly cached, so a 5s UI poll costs one short-lived socket
        rather than one per player.
        """
        for player, status in zip(players, statuses):
            # A stopped player has nothing to report. Asking anyway would show a
            # track and live transport buttons next to a player that is not
            # running, which reads as if it were still playing.
            if player.state != "running":
                continue
            host = player.config.get("server")
            port = player.config.get("control_port", snapctl.DEFAULT_CONTROL_PORT)
            if not host:
                continue
            try:
                info = snapctl.describe(host, port, player.client_id)
            except snapctl.SnapcastError as exc:
                status["snapcast_error"] = str(exc)
                continue
            status["snapcast"] = info
            if info is None:
                continue
            # snapclient's --hostID sets the client id, not the display name, so
            # Snapcast falls back to this container's hostname -- identical for
            # every player in here. Name it once, the first time we see it
            # connected. Verified: two clients in one container both report the
            # container's hostname until this runs.
            if (
                info["connected"]
                and not player.named_on_server
                and info["name"] != player.config["name"]
            ):
                try:
                    snapctl.set_name(host, port, player.client_id, player.config["name"])
                    player.named_on_server = True
                    player.log(
                        "named this client %r on the snapserver" % player.config["name"]
                    )
                    status["snapcast"]["name"] = player.config["name"]
                except snapctl.SnapcastError as exc:
                    player.log("could not set the snapserver name: %s" % exc)

    def get(self, player_id):
        with self._lock:
            player = self._players.get(player_id)
        if player is None:
            raise PlayerError("no such player")
        return player

    def new_player_defaults(self):
        """Seed values for a new player, from the live settings."""
        return {
            "server": self.settings["snapserver_host"] or DEFAULTS["server"],
            "port": self.settings["snapserver_port"],
            "control_port": self.settings["snapserver_control_port"],
        }

    def create(self, config):
        with self._lock:
            names = {p.config["name"] for p in self._players.values()}
            ids = {p.client_id for p in self._players.values()}
            seeded = {**self.new_player_defaults(), **(config or {})}
            clean = validate(seeded, existing_names=names, existing_ids=ids)
            clean["id"] = uuid.uuid4().hex[:8]
            player = Player(clean, self)
            self._players[clean["id"]] = player
        self.save()
        if clean["autostart"]:
            player.start()
        return player

    def update(self, player_id, config):
        player = self.get(player_id)
        with self._lock:
            others = [p for p in self._players.values() if p.id != player_id]
            clean = validate(
                {**player.config, **(config or {})},
                existing_names={p.config["name"] for p in others},
                existing_ids={p.client_id for p in others},
            )
            clean["id"] = player.id
        was_running = player.state != "stopped"
        player.stop()
        with self._lock:
            player.config = clean
            player.named_on_server = False  # re-apply under the new name
        self.save()
        if was_running:
            player.start()
        return player

    def delete(self, player_id):
        player = self.get(player_id)
        player.stop()
        with self._lock:
            self._players.pop(player_id, None)
        snapctl.forget(player.client_id)
        self.save()

    # ---- bulk --------------------------------------------------------------

    def autostart(self):
        with self._lock:
            players = list(self._players.values())
        for player in players:
            if player.config.get("autostart"):
                player.start()

    def stop_all(self):
        with self._lock:
            players = list(self._players.values())
        for player in players:
            player.stop(timeout=5)
