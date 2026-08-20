"""Talk to the Snapserver a player is connected to.

snapclient is only a sink: it has no idea what is playing and cannot pause
anything. The transport controls and the now-playing metadata live on the
*server*, which exposes newline-delimited JSON-RPC on its control port (1705 by
default, alongside 1704 for audio and 1780 for HTTP).

What that gets us, verified against a Music Assistant snapserver:

  * per-stream metadata -- title, artist, album, artUrl
  * capability flags -- canControl / canPause / canGoNext / canGoPrevious, which
    differ per stream and change at runtime, so the UI must read them rather
    than assume a fixed set of buttons
  * Stream.Control for play / pause / next / previous
  * Client.SetVolume, and Client.SetName

Client.SetName matters more than it sounds. snapclient's --hostID sets the
client *id*; the display name is a server-side property that starts empty, so
Snapcast and Music Assistant fall back to the client's hostname -- which is the
container's, identical for every player running in here. Verified against a real
snapserver: two clients in one container, distinct --hostID, both showed up as
the container's hostname until Client.SetName was called.
"""

import json
import socket
import threading
import time

DEFAULT_CONTROL_PORT = 1705
TIMEOUT = 6.0

# The polling path gets a much shorter timeout than an explicit user action: the
# UI asks on every tick, and a server that is switched off would otherwise block
# each poll for the full TIMEOUT. Measured: one player pointed at an unreachable
# server made /api/players take 6s, and it is additive per distinct server.
STATUS_TIMEOUT = 2.0

# Server.GetStatus returns everything in one round trip, so a short cache keeps
# a 5s UI poll from opening a socket per player per tick.
CACHE_TTL = 2.0

# A server that just failed stays "known bad" for this long, so a down snapserver
# costs one timeout per window instead of one per player per poll. Longer than
# the UI poll interval on purpose; an explicit action bypasses it.
FAIL_TTL = 15.0


class SnapcastError(RuntimeError):
    status = 502


_cache = {"key": None, "at": 0.0, "value": None}
# (host, port) -> (when, message) for servers that just refused or timed out.
_failures = {}
_cache_lock = threading.Lock()

# The last stream each client was on that could actually be controlled.
#
# Music Assistant detaches a client's group from its MA stream when you pause,
# parking it on "default" -- which reports canControl=False and carries no
# metadata. Read naively that looks like "nothing to control here": the transport
# buttons vanish and there is no way to resume from this panel. The MA stream is
# still there, still controllable, still holding the paused track, so remember it
# and keep driving that. Verified against a live Music Assistant snapserver.
_last_controllable = {}


def rpc(host, port, method, params=None, timeout=TIMEOUT):
    """One JSON-RPC call over the control port."""
    request = {"id": 1, "jsonrpc": "2.0", "method": method}
    if params is not None:
        request["params"] = params
    payload = (json.dumps(request) + "\r\n").encode()

    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(payload)
            buffer = b""
            deadline = time.time() + timeout
            while time.time() < deadline:
                # The server also pushes notifications down this socket, so read
                # lines until the one carrying our id turns up.
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        message = json.loads(line)
                    except ValueError:
                        continue
                    if message.get("id") != 1:
                        continue  # a notification
                    if "error" in message:
                        raise SnapcastError(
                            message["error"].get("message", "snapserver error")
                        )
                    return message.get("result")
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buffer += chunk
    except (OSError, socket.timeout) as exc:
        raise SnapcastError("cannot reach snapserver %s:%s (%s)" % (host, port, exc)) from None
    raise SnapcastError("no reply from snapserver %s:%s" % (host, port))


def server_status(host, port, use_cache=True):
    """Server.GetStatus, briefly cached per server -- successes and failures.

    Caching the failure matters as much as caching the result: without it, every
    player pointed at a switched-off snapserver pays a full connect timeout on
    every UI poll, and the panel becomes unusable exactly when something is
    already wrong.
    """
    key = (host, int(port))
    now = time.time()
    if use_cache:
        with _cache_lock:
            if _cache["key"] == key and now - _cache["at"] < CACHE_TTL:
                return _cache["value"]
            failed_at, error = _failures.get(key, (0.0, None))
            if error is not None and now - failed_at < FAIL_TTL:
                raise SnapcastError(error)

    try:
        status = rpc(host, port, "Server.GetStatus",
                     timeout=STATUS_TIMEOUT if use_cache else TIMEOUT)["server"]
    except SnapcastError as exc:
        with _cache_lock:
            _failures[key] = (time.time(), str(exc))
        raise
    with _cache_lock:
        _failures.pop(key, None)
        _cache.update(key=key, at=time.time(), value=status)
    return status


def invalidate():
    with _cache_lock:
        _cache.update(key=None, at=0.0, value=None)
        _failures.clear()


def forget(client_id):
    _last_controllable.pop(client_id, None)


def _find(status, client_id):
    for group in status.get("groups", []):
        for client in group.get("clients", []):
            if client.get("id") == client_id:
                return group, client
    return None, None


def _stream(status, stream_id):
    for stream in status.get("streams", []):
        if stream.get("id") == stream_id:
            return stream
    return None


def describe(host, port, client_id, use_cache=True):
    """Everything the UI needs about one player, or None if the server has never
    seen it."""
    status = server_status(host, port, use_cache=use_cache)
    group, client = _find(status, client_id)
    if client is None:
        return None

    stream = _stream(status, group.get("stream_id")) or {}
    props = stream.get("properties") or {}

    attached = True
    if props.get("canControl"):
        _last_controllable[client_id] = stream.get("id")
    else:
        # Fall back to the stream this client last played from, as long as the
        # server still has it. That is the paused case.
        remembered = _stream(status, _last_controllable.get(client_id))
        if remembered and (remembered.get("properties") or {}).get("canControl"):
            stream = remembered
            props = stream.get("properties") or {}
            attached = False

    meta = props.get("metadata") or {}
    volume = (client.get("config") or {}).get("volume") or {}
    artists = meta.get("artist") or []
    if isinstance(artists, str):
        artists = [artists]

    return {
        "client_id": client_id,
        "connected": bool(client.get("connected")),
        # Empty until something calls Client.SetName; Snapcast then falls back
        # to the container hostname for every player in here.
        "name": (client.get("config") or {}).get("name") or "",
        "volume": volume.get("percent"),
        "muted": bool(volume.get("muted")),
        "group_id": group.get("id"),
        "group_muted": bool(group.get("muted")),
        "stream_id": stream.get("id"),
        "stream_status": stream.get("status"),
        # False when the group has been parked elsewhere (paused) but we are
        # still reporting -- and controlling -- the stream it belongs to.
        "attached": attached,
        "playback_status": props.get("playbackStatus"),
        "can_control": bool(props.get("canControl")),
        "can_pause": bool(props.get("canPause")),
        "can_next": bool(props.get("canGoNext")),
        "can_prev": bool(props.get("canGoPrevious")),
        "title": meta.get("title") or "",
        "artist": ", ".join(a for a in artists if a),
        "album": meta.get("album") or "",
        "art_url": meta.get("artUrl") or "",
    }


# ---- actions ----------------------------------------------------------------

COMMANDS = ("play", "pause", "playPause", "next", "previous")


def control(host, port, stream_id, command):
    if command not in COMMANDS:
        raise SnapcastError("unsupported command %r" % command)
    if not stream_id:
        raise SnapcastError("this player is not attached to a stream yet")
    result = rpc(host, port, "Stream.Control",
                 {"id": stream_id, "command": command, "params": {}})
    invalidate()
    return result


def set_volume(host, port, client_id, percent=None, muted=None):
    volume = {}
    if percent is not None:
        volume["percent"] = max(0, min(100, int(percent)))
    if muted is not None:
        volume["muted"] = bool(muted)
    if not volume:
        raise SnapcastError("nothing to set")
    result = rpc(host, port, "Client.SetVolume", {"id": client_id, "volume": volume})
    invalidate()
    return result


def set_name(host, port, client_id, name):
    result = rpc(host, port, "Client.SetName", {"id": client_id, "name": name})
    invalidate()
    return result


def delete_client(host, port, client_id):
    result = rpc(host, port, "Server.DeleteClient", {"id": client_id})
    invalidate()
    return result


def stale_clients(host, port):
    """Disconnected clients the server still remembers."""
    status = server_status(host, port, use_cache=False)
    out = []
    for group in status.get("groups", []):
        for client in group.get("clients", []):
            if not client.get("connected"):
                out.append({
                    "id": client.get("id"),
                    "name": (client.get("config") or {}).get("name") or "",
                    "host": (client.get("host") or {}).get("name") or "",
                })
    return out
