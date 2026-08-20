#!/usr/bin/env python3
"""snapcast-panel -- create and supervise snapclient players from a browser.

Replaces the "edit compose, docker compose up -d, ssh in to change a parameter"
loop for anyone running more than one player: every field that ROLE=snapclient
takes as an environment variable is editable here, per player, at runtime.

Deliberately small: no database, no build step, no websockets. The browser polls
/api/players and every action is a POST that returns the refreshed list.
"""

import atexit
import hmac
import logging
import os
import shutil

from flask import Flask, jsonify, request, send_from_directory

import players as players_mod
import snapctl
from players import PlayerError, SettingsError, Supervisor

log = logging.getLogger("snapcast-panel")

app = Flask(__name__, static_folder="static", static_url_path="")

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# How often the browser re-reads the player table.
POLL_SECONDS = max(1.0, float(os.environ.get("POLL_SECONDS", "5")))

supervisor = Supervisor()


# ---- auth -------------------------------------------------------------------


@app.before_request
def require_auth():
    """Gate everything -- API and the page itself -- when ADMIN_PASSWORD is set.

    Unset (the default) means no auth at all, which is why the README is explicit
    that this belongs on a trusted LAN and not on a port-forward.
    """
    if not ADMIN_PASSWORD:
        return None
    auth = request.authorization
    if (
        auth
        and auth.type == "basic"
        and hmac.compare_digest(auth.username or "", ADMIN_USER)
        and hmac.compare_digest(auth.password or "", ADMIN_PASSWORD)
    ):
        return None
    return (
        jsonify(error="authentication required"),
        401,
        {"WWW-Authenticate": 'Basic realm="snapcast-panel"'},
    )


# ---- error mapping ----------------------------------------------------------


@app.errorhandler(snapctl.SnapcastError)
def handle_snapcast_error(exc):
    return jsonify(ok=False, error=str(exc)), exc.status


@app.errorhandler(SettingsError)
def handle_settings_error(exc):
    return jsonify(ok=False, error=str(exc)), exc.status


@app.errorhandler(PlayerError)
def handle_player_error(exc):
    return jsonify(ok=False, error=str(exc)), exc.status


# ---- routes -----------------------------------------------------------------


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


def startup_warnings(alsa):
    """Things the panel can see are wrong that a user cannot guess from a log."""
    warnings = []
    if alsa and not any(d["hardware"] for d in alsa):
        warnings.append(
            "No ALSA hardware devices are visible, only conversion plugins. The "
            "ALSA output mode needs the sound devices passed through as devices, "
            "not as a volume: 'devices: [/dev/snd:/dev/snd]' in compose. "
            "(-v /dev/snd mounts the nodes but the device cgroup still blocks "
            "opening them.) PipeWire output is unaffected."
        )
    if not players_mod.list_sinks():
        warnings.append(
            "PipeWire is not reachable, so no sinks can be listed. Check the "
            "socket bind mount, e.g. '/run/user/1000/pipewire-0:/tmp/pipewire-0'. "
            "An ALSA output still works without it."
        )
    if not shutil.which(players_mod.SNAPCLIENT):
        warnings.append("snapclient was not found on PATH (%s)."
                        % players_mod.SNAPCLIENT)
    return warnings


@app.get("/api/config")
def api_config():
    alsa = players_mod.list_alsa_devices()
    return jsonify(
        poll_seconds=POLL_SECONDS,
        auth=bool(ADMIN_PASSWORD),
        defaults=supervisor.new_player_defaults(),
        sinks=players_mod.list_sinks(),
        alsa=alsa,
        warnings=startup_warnings(alsa),
        # From the stored settings, not the environment: the environment only
        # seeds them and the web UI can change them afterwards.
        snapserver={
            "host": supervisor.settings["snapserver_host"],
            "port": supervisor.settings["snapserver_port"],
            "control_port": supervisor.settings["snapserver_control_port"],
            "web_port": supervisor.settings["snapserver_web_port"],
        },
    )


@app.get("/api/settings")
def api_get_settings():
    return jsonify(settings=supervisor.settings)


@app.patch("/api/settings")
def api_patch_settings():
    settings = supervisor.update_settings(request.get_json(silent=True) or {})
    return jsonify(ok=True, settings=settings)


@app.get("/api/sinks")
def api_sinks():
    """Everything a player can be pointed at: PipeWire sinks and ALSA devices."""
    alsa = players_mod.list_alsa_devices()
    return jsonify(sinks=players_mod.list_sinks(), alsa=alsa,
                   warnings=startup_warnings(alsa))


@app.get("/api/players")
def api_players():
    return jsonify(players=supervisor.list())


@app.post("/api/players")
def api_create_player():
    player = supervisor.create(request.get_json(silent=True) or {})
    return jsonify(ok=True, player=player.status(), players=supervisor.list()), 201


@app.patch("/api/players/<player_id>")
def api_update_player(player_id):
    player = supervisor.update(player_id, request.get_json(silent=True) or {})
    return jsonify(ok=True, player=player.status(), players=supervisor.list())


@app.delete("/api/players/<player_id>")
def api_delete_player(player_id):
    supervisor.delete(player_id)
    return jsonify(ok=True, players=supervisor.list())


@app.post("/api/players/<player_id>/<action>")
def api_player_action(player_id, action):
    if action not in ("start", "stop", "restart"):
        return jsonify(ok=False, error="unknown action"), 404
    player = supervisor.get(player_id)
    if action in ("stop", "restart"):
        player.stop()
    if action in ("start", "restart"):
        player.start()
    return jsonify(ok=True, players=supervisor.list())


@app.get("/api/players/<player_id>/logs")
def api_player_logs(player_id):
    player = supervisor.get(player_id)
    return jsonify(logs=list(player.logs))


@app.post("/api/players/<player_id>/control/<command>")
def api_player_control(player_id, command):
    """Transport control. Snapcast has no "stop" -- pause is the stop.

    The command acts on the *stream* the player's group is attached to, so it
    affects every client in that group, exactly like pressing pause in Snapweb
    or Music Assistant.
    """
    player = supervisor.get(player_id)
    if command not in snapctl.COMMANDS:
        return jsonify(ok=False, error="unsupported command"), 404

    host = player.config["server"]
    port = player.config.get("control_port", snapctl.DEFAULT_CONTROL_PORT)
    info = snapctl.describe(host, port, player.client_id, use_cache=False)
    if info is None:
        return jsonify(ok=False, error="the snapserver does not know this client"), 404
    if not info["can_control"]:
        return (
            jsonify(
                ok=False,
                error="stream %r does not support transport control"
                % info["stream_id"],
            ),
            409,
        )

    snapctl.control(host, port, info["stream_id"], command)
    return jsonify(ok=True, players=supervisor.list())


@app.post("/api/players/<player_id>/volume")
def api_player_volume(player_id):
    player = supervisor.get(player_id)
    body = request.get_json(silent=True) or {}
    percent, muted = body.get("percent"), body.get("muted")
    if percent is None and muted is None:
        return jsonify(ok=False, error="percent or muted is required"), 400
    if percent is not None:
        try:
            percent = int(percent)
        except (TypeError, ValueError):
            return jsonify(ok=False, error="percent must be a number"), 400
        if not 0 <= percent <= 100:
            return jsonify(ok=False, error="percent must be 0-100"), 400

    snapctl.set_volume(
        player.config["server"],
        player.config.get("control_port", snapctl.DEFAULT_CONTROL_PORT),
        player.client_id,
        percent=percent,
        muted=muted,
    )
    return jsonify(ok=True, players=supervisor.list())


@app.get("/api/snapcast/stale")
def api_stale_clients():
    """Clients the snapserver still remembers but nothing is using.

    Snapcast keeps a client forever once it has connected, so deleting a player
    here leaves a ghost in Snapweb and Music Assistant until someone removes it.
    """
    players = supervisor.list(with_snapcast=False)
    if not players:
        return jsonify(stale=[])
    first = players[0]
    stale = snapctl.stale_clients(
        first["server"], first.get("control_port", snapctl.DEFAULT_CONTROL_PORT)
    )
    known = {p["client_id"] for p in players}
    return jsonify(stale=[c for c in stale if c["id"] not in known])


@app.delete("/api/snapcast/client/<path:client_id>")
def api_delete_stale(client_id):
    players = supervisor.list(with_snapcast=False)
    if not players:
        return jsonify(ok=False, error="no player knows which server to ask"), 400
    if client_id in {p["client_id"] for p in players}:
        return jsonify(ok=False, error="that client belongs to a live player"), 409
    first = players[0]
    snapctl.delete_client(
        first["server"],
        first.get("control_port", snapctl.DEFAULT_CONTROL_PORT),
        client_id,
    )
    return jsonify(ok=True)


def main():
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("DEBUG") == "true" else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not ADMIN_PASSWORD:
        log.warning(
            "ADMIN_PASSWORD is not set -- every route is open to anyone who can "
            "reach this port. Intended for a trusted LAN only."
        )
    supervisor.autostart()
    atexit.register(supervisor.stop_all)

    # threaded=True so the 5s poll and the healthcheck are not queued behind a
    # slow snapserver call.
    app.run(
        host=os.environ.get("BIND_HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8080")),
        threaded=True,
    )


if __name__ == "__main__":
    main()
