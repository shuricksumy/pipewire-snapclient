"""End-to-end tests against the Flask layer.

Nothing here needs PipeWire, a DAC, a snapserver or root: snapclient is replaced
by fake_snapclient.py and the control port by fake_snapserver.py, so the whole
suite runs in CI.
"""
import importlib
import os
import re
import sys
import time

import pytest

from fake_snapserver import FakeSnapserver

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE = os.path.join(ROOT, "tests", "fake_snapclient.py")
INDEX = os.path.join(ROOT, "panel", "static", "index.html")


def load_app(**env):
    """Import a fresh app with the requested environment."""
    for key in ("ADMIN_PASSWORD", "ADMIN_USER", "SERVER_IP", "SNAP_PORT",
                "SNAP_CONTROL_PORT", "SNAP_WEB_PORT", "CONFIG_DIR"):
        os.environ.pop(key, None)
    os.environ.update({k: str(v) for k, v in env.items()})
    os.environ["SNAPCLIENT"] = FAKE

    # players and snapctl read their defaults at import time, so they have to be
    # reloaded too or env-driven settings silently keep the previous values.
    for name in ("app", "players", "snapctl"):
        sys.modules.pop(name, None)
    module = importlib.import_module("app")
    module.players_mod.RETRY_START = 0.2
    module.players_mod.RETRY_MAX = 0.4
    module.players_mod.NODE_WAIT_SECONDS = 1.0
    module.players_mod.list_sinks = lambda: [
        {"id": 50, "node": "alsa_output.usb-Topping_DX5-00.analog-stereo",
         "description": "Topping DX5", "bluetooth": False},
    ]
    module.players_mod.sink_present = lambda node: True
    module.players_mod.set_sink_volume = lambda node, volume: None
    return module


@pytest.fixture
def app_module(tmp_path):
    module = load_app(CONFIG_DIR=str(tmp_path))
    module.supervisor.config_path = str(tmp_path / "players.json")
    yield module
    module.supervisor.stop_all()


@pytest.fixture
def client(app_module):
    return app_module.app.test_client()


def wait_for(predicate, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def create(client, **over):
    body = {"name": "Lounge", "server": "192.168.111.50",
            "node": "alsa_output.usb-Topping_DX5-00.analog-stereo",
            "autostart": False}
    body.update(over)
    return client.post("/api/players", json=body)


# ---- the page ---------------------------------------------------------------


def test_index_is_served(client):
    res = client.get("/")
    assert res.status_code == 200
    assert b"Snapcast Players" in res.data


def test_the_ui_calls_the_api_relative_to_the_document():
    """Absolute /api/... paths break under Home Assistant Ingress.

    Ingress serves the page from /api/hassio_ingress/<token>/ and strips that
    prefix before the request arrives, so an absolute path would leave the
    prefix behind and hit Home Assistant instead of the panel.
    """
    page = open(INDEX).read()
    assert "document.baseURI" in page
    # Comments explain the rule and quote the bad form, so check the code only.
    code = re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", page, flags=re.S))
    # No fetch/XHR to a root-anchored API path anywhere in the page.
    assert not re.search(r"""(fetch|open)\(\s*["'`]/""", code)
    assert not re.search(r"""["'`]/api/""", code)


def test_names_are_escaped_before_being_rendered():
    page = open(INDEX).read()
    assert "esc(p.name)" in page


# ---- CRUD -------------------------------------------------------------------


def test_create_lists_and_deletes_a_player(client):
    res = create(client)
    assert res.status_code == 201
    player = res.get_json()["player"]
    assert player["name"] == "Lounge"
    assert player["client_id"] == "Lounge"
    assert player["state"] == "stopped"

    listed = client.get("/api/players").get_json()["players"]
    assert [p["name"] for p in listed] == ["Lounge"]

    res = client.delete("/api/players/%s" % player["id"])
    assert res.status_code == 200
    assert res.get_json()["players"] == []


def test_creating_with_autostart_starts_it(client):
    player = create(client, autostart=True).get_json()["player"]
    assert wait_for(
        lambda: client.get("/api/players").get_json()["players"][0]["running"]
    ), "autostart player never reached running"
    client.post("/api/players/%s/stop" % player["id"])


@pytest.mark.parametrize(
    "bad,message",
    [
        ({"name": ""}, "name"),
        ({"server": "not a host"}, "server"),
        ({"port": 0}, "port"),
        ({"pipewire_latency": "nope"}, "PipeWire latency"),
        ({"volume": 9}, "volume"),
    ],
)
def test_invalid_input_is_rejected_with_a_message(client, bad, message):
    res = create(client, **bad)
    assert res.status_code == 400
    assert message.lower() in res.get_json()["error"].lower()


def test_a_duplicate_name_is_rejected(client):
    create(client)
    res = create(client)
    assert res.status_code == 400
    assert "already exists" in res.get_json()["error"]


def test_updating_a_player(client):
    player = create(client).get_json()["player"]
    res = client.patch("/api/players/%s" % player["id"],
                       json={"server": "10.0.0.9", "output_mode": "alsa",
                             "alsa_device": "hw:CARD=DX5,DEV=0"})
    assert res.status_code == 200
    updated = res.get_json()["player"]
    assert updated["server"] == "10.0.0.9"
    assert updated["output_mode"] == "alsa"
    assert updated["alsa_device"] == "hw:CARD=DX5,DEV=0"


def test_actions_on_an_unknown_player_are_404(client):
    assert client.post("/api/players/nope/start").status_code == 400
    assert client.get("/api/players/nope/logs").status_code == 400


def test_an_unknown_action_is_rejected(client):
    player = create(client).get_json()["player"]
    assert client.post("/api/players/%s/frobnicate" % player["id"]).status_code == 404


# ---- lifecycle --------------------------------------------------------------


def test_start_stop_restart_and_logs(client):
    player = create(client).get_json()["player"]
    pid = player["id"]

    assert client.post("/api/players/%s/start" % pid).status_code == 200
    assert wait_for(
        lambda: client.get("/api/players").get_json()["players"][0]["running"]
    )

    logs = client.get("/api/players/%s/logs" % pid).get_json()["logs"]
    assert any("snapclient args" in line for line in logs)
    assert any("--hostID Lounge" in line for line in logs)

    assert client.post("/api/players/%s/restart" % pid).status_code == 200
    assert wait_for(
        lambda: client.get("/api/players").get_json()["players"][0]["running"]
    )

    body = client.post("/api/players/%s/stop" % pid).get_json()
    assert body["players"][0]["running"] is False
    # A stopped player must reset its row: no stale detail, no now-playing.
    assert body["players"][0]["detail"] == ""
    assert body["players"][0]["snapcast"] is None
    assert body["players"][0]["uptime"] == 0


def test_several_players_run_at_once_each_on_its_own_sink(client):
    a = create(client, name="Lounge",
               node="alsa_output.usb-Topping_DX5-00.analog-stereo",
               autostart=True).get_json()["player"]
    b = create(client, name="Kitchen",
               node="alsa_output.usb-FiiO_K3-00.analog-stereo",
               autostart=True).get_json()["player"]

    assert wait_for(lambda: all(
        p["running"] for p in client.get("/api/players").get_json()["players"]))

    # The child's own output arrives through the pump thread a moment after the
    # process is up, so wait for the line rather than racing it.
    def logs(player):
        return "\n".join(
            client.get("/api/players/%s/logs" % player["id"]).get_json()["logs"])

    assert wait_for(
        lambda: "PIPEWIRE_NODE=alsa_output.usb-Topping_DX5-00.analog-stereo" in logs(a))
    assert wait_for(
        lambda: "PIPEWIRE_NODE=alsa_output.usb-FiiO_K3-00.analog-stereo" in logs(b))

    client.post("/api/players/%s/stop" % a["id"])
    client.post("/api/players/%s/stop" % b["id"])


# ---- sinks and settings -----------------------------------------------------


def test_sinks_are_listed(client):
    sinks = client.get("/api/sinks").get_json()["sinks"]
    assert sinks[0]["node"] == "alsa_output.usb-Topping_DX5-00.analog-stereo"


def test_settings_round_trip(client):
    res = client.patch("/api/settings", json={"snapserver_host": "10.0.0.5",
                                              "snapserver_port": 1804})
    assert res.status_code == 200
    assert res.get_json()["settings"]["snapserver_host"] == "10.0.0.5"

    config = client.get("/api/config").get_json()
    assert config["snapserver"]["host"] == "10.0.0.5"
    assert config["defaults"]["server"] == "10.0.0.5"
    assert config["defaults"]["port"] == 1804


def test_bad_settings_are_rejected(client):
    res = client.patch("/api/settings", json={"snapserver_port": 0})
    assert res.status_code == 400
    assert "port" in res.get_json()["error"]


def test_environment_seeds_the_defaults(tmp_path):
    module = load_app(CONFIG_DIR=str(tmp_path), SERVER_IP="10.1.2.3", SNAP_PORT="1904")
    try:
        config = module.app.test_client().get("/api/config").get_json()
        assert config["defaults"]["server"] == "10.1.2.3"
        assert config["defaults"]["port"] == 1904
    finally:
        module.supervisor.stop_all()


# ---- auth -------------------------------------------------------------------


def test_without_a_password_everything_is_open(client):
    assert client.get("/api/players").status_code == 200
    assert client.get("/").status_code == 200


def test_admin_password_gates_every_route(tmp_path):
    module = load_app(CONFIG_DIR=str(tmp_path), ADMIN_PASSWORD="s3cret")
    try:
        client = module.app.test_client()
        for path in ("/", "/api/players", "/api/sinks", "/api/config"):
            res = client.get(path)
            assert res.status_code == 401, path
            assert "Basic" in res.headers["WWW-Authenticate"]

        good = client.get("/api/players", auth=("admin", "s3cret"))
        assert good.status_code == 200
        assert client.get("/api/players", auth=("admin", "wrong")).status_code == 401
        assert client.get("/api/players", auth=("nobody", "s3cret")).status_code == 401
    finally:
        module.supervisor.stop_all()


def test_a_custom_admin_user(tmp_path):
    module = load_app(CONFIG_DIR=str(tmp_path), ADMIN_PASSWORD="pw", ADMIN_USER="alex")
    try:
        client = module.app.test_client()
        assert client.get("/api/players", auth=("alex", "pw")).status_code == 200
        assert client.get("/api/players", auth=("admin", "pw")).status_code == 401
    finally:
        module.supervisor.stop_all()


# ---- talking to a snapserver ------------------------------------------------


@pytest.fixture
def snapserver():
    server = FakeSnapserver()
    yield server
    server.close()


def test_now_playing_is_reported_for_a_running_player(client, snapserver, app_module):
    player = create(client, name="DX5", client_id="DX5", server="127.0.0.1",
                    control_port=snapserver.port, autostart=True).get_json()["player"]
    assert wait_for(
        lambda: client.get("/api/players").get_json()["players"][0]["running"])

    app_module.snapctl.invalidate()
    listed = client.get("/api/players").get_json()["players"][0]
    assert listed["snapcast"]["title"] == "O eterne Deus"
    assert listed["snapcast"]["artist"] == "Raphaela Gromes"
    assert listed["snapcast"]["can_control"] is True

    # The panel names the client on the server: --hostID sets the id only, so
    # every player in this container would otherwise show the container hostname.
    assert wait_for(lambda: snapserver.clients["DX5"]["name"] == "DX5")
    client.post("/api/players/%s/stop" % player["id"])


def test_a_stopped_player_asks_the_server_nothing(client, snapserver):
    """No stale now-playing, and no wasted round trip."""
    create(client, name="DX5", client_id="DX5", server="127.0.0.1",
           control_port=snapserver.port)
    before = len(snapserver.calls)
    listed = client.get("/api/players").get_json()["players"][0]
    assert listed["snapcast"] is None
    assert len(snapserver.calls) == before


def test_transport_control_is_forwarded(client, snapserver, app_module):
    player = create(client, name="DX5", client_id="DX5", server="127.0.0.1",
                    control_port=snapserver.port, autostart=True).get_json()["player"]
    assert wait_for(
        lambda: client.get("/api/players").get_json()["players"][0]["running"])

    res = client.post("/api/players/%s/control/playPause" % player["id"])
    assert res.status_code == 200
    assert ("Stream.Control", {"id": "ma-dx5", "command": "playPause", "params": {}}) \
        in snapserver.calls
    client.post("/api/players/%s/stop" % player["id"])


def test_transport_on_an_uncontrollable_stream_is_a_clear_409(
        client, snapserver, app_module):
    """A plain pipe stream cannot be controlled; say so rather than failing oddly."""
    snapserver.clients["Ghost"]["connected"] = True
    player = create(client, name="Ghost", client_id="Ghost", server="127.0.0.1",
                    control_port=snapserver.port, autostart=True).get_json()["player"]
    assert wait_for(
        lambda: client.get("/api/players").get_json()["players"][0]["running"])

    res = client.post("/api/players/%s/control/play" % player["id"])
    assert res.status_code == 409
    assert "does not support" in res.get_json()["error"]
    client.post("/api/players/%s/stop" % player["id"])


def test_volume_is_forwarded(client, snapserver):
    player = create(client, name="DX5", client_id="DX5", server="127.0.0.1",
                    control_port=snapserver.port, autostart=True).get_json()["player"]
    assert wait_for(
        lambda: client.get("/api/players").get_json()["players"][0]["running"])

    assert client.post("/api/players/%s/volume" % player["id"],
                       json={"percent": 55}).status_code == 200
    assert snapserver.clients["DX5"]["volume"] == 55

    assert client.post("/api/players/%s/volume" % player["id"],
                       json={"muted": True}).status_code == 200
    assert snapserver.clients["DX5"]["muted"] is True

    assert client.post("/api/players/%s/volume" % player["id"],
                       json={}).status_code == 400
    assert client.post("/api/players/%s/volume" % player["id"],
                       json={"percent": 500}).status_code == 400
    client.post("/api/players/%s/stop" % player["id"])


def test_an_unreachable_snapserver_is_reported_not_fatal(client):
    """The panel still lists players when the server is down."""
    player = create(client, name="DX5", server="127.0.0.1", control_port=9,  # discard port
                    autostart=True).get_json()["player"]
    assert wait_for(
        lambda: client.get("/api/players").get_json()["players"][0]["running"])
    listed = client.get("/api/players").get_json()["players"][0]
    assert listed["snapcast"] is None
    assert "cannot reach snapserver" in (listed["snapcast_error"] or "")
    client.post("/api/players/%s/stop" % player["id"])


def test_an_unreachable_server_does_not_stall_the_poll(client, app_module):
    """A down snapserver must not make the panel unusable.

    Measured before the failure cache existed: one running player pointed at an
    unreachable server made every /api/players take the full 6s connect timeout,
    and it was additive per distinct server -- so the UI froze exactly when
    something was already wrong. The first call now pays the shorter status
    timeout and later ones are served from the failure cache.
    """
    app_module.snapctl.STATUS_TIMEOUT = 0.4
    app_module.snapctl.invalidate()
    # 203.0.113.0/24 is TEST-NET-3: reserved for documentation, so it is
    # guaranteed not to answer -- unlike a port on localhost, which refuses fast.
    player = create(client, name="Away", server="203.0.113.1", control_port=1705,
                    autostart=True).get_json()["player"]
    assert wait_for(
        lambda: client.get("/api/players").get_json()["players"][0]["running"])

    began = time.time()
    first = client.get("/api/players").get_json()["players"][0]
    first_took = time.time() - began
    assert first_took < 3.0, "one poll took %.1fs" % first_took
    assert first["snapcast"] is None
    assert first["snapcast_error"]

    began = time.time()
    for _ in range(5):
        client.get("/api/players")
    assert time.time() - began < 1.0, "repeat polls are not served from the cache"
    client.post("/api/players/%s/stop" % player["id"])


def test_stale_clients_exclude_live_players(client, snapserver):
    create(client, name="DX5", client_id="DX5", server="127.0.0.1",
           control_port=snapserver.port)
    stale = client.get("/api/snapcast/stale").get_json()["stale"]
    assert [c["id"] for c in stale] == ["Ghost"]

    assert client.delete("/api/snapcast/client/Ghost").status_code == 200
    assert "Ghost" not in snapserver.clients
    # A live player's client must not be deletable through that route.
    assert client.delete("/api/snapcast/client/DX5").status_code == 409


# ---- outputs and warnings ---------------------------------------------------


def test_outputs_list_both_pipewire_and_alsa(client, monkeypatch):
    monkeypatch.setenv("FAKE_ALSA_MODE", "hardware")
    body = client.get("/api/sinks").get_json()
    assert body["sinks"][0]["node"] == "alsa_output.usb-Topping_DX5-00.analog-stereo"
    assert any(d["device"] == "hw:CARD=DX5,DEV=0" for d in body["alsa"])
    # config carries the same lists, so the page can draw the picker on first load
    config = client.get("/api/config").get_json()
    assert config["sinks"] and config["alsa"]


def test_a_plugins_only_listing_warns_about_dev_snd(client, monkeypatch, app_module):
    """-v /dev/snd mounts the nodes but the device cgroup still blocks opening
    them, so enumeration returns only conversion plugins."""
    monkeypatch.setenv("FAKE_ALSA_MODE", "plugins")
    app_module.players_mod._alsa_cache["at"] = 0.0
    warnings = client.get("/api/config").get_json()["warnings"]
    assert any("devices: [/dev/snd:/dev/snd]" in w for w in warnings), warnings


def test_creating_an_alsa_player(client):
    res = create(client, name="Direct", output_mode="alsa",
                 alsa_device="hw:CARD=DX5,DEV=0", node="")
    assert res.status_code == 201
    player = res.get_json()["player"]
    assert player["output_mode"] == "alsa"
    assert player["alsa_device"] == "hw:CARD=DX5,DEV=0"


def test_an_alsa_player_rejects_a_bad_device(client):
    res = create(client, output_mode="alsa", alsa_device="not a device", node="")
    assert res.status_code == 400
    assert "alsa" in res.get_json()["error"].lower()
