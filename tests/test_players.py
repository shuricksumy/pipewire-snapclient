"""Supervisor tests. No PipeWire, no DAC, no snapcast binary, no root."""
import json
import os
import time

import pytest

import players as players_mod
from players import PlayerError, Supervisor, validate

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE = os.path.join(ROOT, "tests", "fake_snapclient.py")


@pytest.fixture
def supervisor(tmp_path, monkeypatch):
    monkeypatch.setattr(players_mod, "SNAPCLIENT", FAKE)
    monkeypatch.setattr(players_mod, "RETRY_START", 0.2)
    monkeypatch.setattr(players_mod, "RETRY_MAX", 0.4)
    monkeypatch.setattr(players_mod, "NODE_WAIT_SECONDS", 1.0)
    # No PipeWire in the test environment: report the sink as present so the
    # readiness gate does not block, and skip the wpctl volume call.
    monkeypatch.setattr(players_mod, "list_sinks", list)
    monkeypatch.setattr(players_mod, "sink_present", lambda node: True)
    monkeypatch.setattr(players_mod, "set_sink_volume", lambda node, vol: None)

    sup = Supervisor(config_path=str(tmp_path / "players.json"))
    yield sup
    sup.stop_all()


def make(sup, **over):
    config = {
        "name": "Lounge",
        "node": "alsa_output.usb-Topping_DX5-00.analog-stereo",
        "server": "192.168.111.50",
        "autostart": False,
    }
    config.update(over)
    return sup.create(config)


def wait_for(predicate, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


# ---- validation -------------------------------------------------------------


@pytest.mark.parametrize(
    "bad,message",
    [
        ({"name": ""}, "name"),
        ({"name": "x" * 80}, "name"),
        ({"node": "has spaces"}, "node"),
        ({"server": "host with spaces"}, "server"),
        ({"port": 0}, "port"),
        ({"port": 99999}, "port"),
        ({"port": "abc"}, "port"),
        ({"control_port": 0}, "control port"),
        ({"latency_ms": 99999}, "latency"),
        ({"volume": 5}, "volume"),
        ({"pipewire_latency": "garbage"}, "PipeWire latency"),
        ({"extra": 'unbalanced "quote'}, "parseable"),
        ({"extra": "x" * 300}, "too long"),
    ],
)
def test_bad_definitions_are_rejected(bad, message):
    config = {"name": "Valid", "server": "127.0.0.1"}
    config.update(bad)
    with pytest.raises(PlayerError) as err:
        validate(config)
    assert message.lower() in str(err.value).lower()


def test_client_id_defaults_to_the_name():
    assert validate({"name": "Lounge", "server": "127.0.0.1"})["client_id"] == "Lounge"


def test_an_explicit_client_id_wins():
    clean = validate({"name": "Lounge", "client_id": "DX5-Node", "server": "1.2.3.4"})
    assert clean["client_id"] == "DX5-Node"


def test_duplicate_names_are_rejected(supervisor):
    make(supervisor)
    with pytest.raises(PlayerError):
        make(supervisor)


def test_duplicate_client_ids_are_rejected(supervisor):
    """Two players registering as the same id would fight over one snapserver
    client, each disconnecting the other."""
    make(supervisor, name="A", client_id="Shared")
    with pytest.raises(PlayerError) as err:
        make(supervisor, name="B", client_id="Shared")
    assert "unique" in str(err.value).lower()


def test_names_are_passed_as_one_argument_not_through_a_shell(supervisor):
    """Shell metacharacters in a name are harmless: argv is a list.

    That is why the pattern only bars control characters -- rejecting
    punctuation would block "Kitchen · DX5" and Cyrillic names for no gain.
    """
    player = make(supervisor, name="rm -rf /; echo hi")
    player.start()
    assert wait_for(lambda: player.state == "running")
    argv = player._proc.args
    assert argv[argv.index("--hostID") + 1] == "rm -rf /; echo hi"
    player.stop()


def test_unicode_names_are_accepted():
    for name in ("Kitchen · DX5", "Кухня", "Küche"):
        assert validate({"name": name, "server": "127.0.0.1"})["name"] == name


@pytest.mark.parametrize("bad", ["with\nnewline", "with\x00null", "tab\there"])
def test_control_characters_in_names_are_rejected(bad):
    with pytest.raises(PlayerError):
        validate({"name": bad, "server": "127.0.0.1"})


# ---- launching --------------------------------------------------------------


def test_start_launches_snapclient_with_the_right_arguments(supervisor):
    player = make(
        supervisor, latency_ms=-120, pipewire_latency="2048/192000",
        client_id="DX5-Snapclient",
    )
    player.start()
    assert wait_for(lambda: player.state == "running"), player.state
    assert wait_for(lambda: any("snapclient args" in line for line in player.logs))

    logs = "\n".join(player.logs)
    assert "--hostID DX5-Snapclient" in logs
    assert "--player pipewire" in logs
    assert "--latency -120" in logs
    assert "tcp://192.168.111.50:1704" in logs
    assert "PIPEWIRE_NODE=alsa_output.usb-Topping_DX5-00.analog-stereo" in logs
    assert "PIPEWIRE_LATENCY=2048/192000" in logs

    player.stop()
    assert player.state == "stopped"


def test_alsa_output_addresses_the_device_directly(supervisor):
    """The escape hatch for a host where PipeWire is broken or absent."""
    player = make(supervisor, output_mode="alsa", alsa_device="hw:CARD=DX5,DEV=0", node="")
    player.start()
    assert wait_for(lambda: any("snapclient args" in line for line in player.logs))
    logs = "\n".join(player.logs)
    assert "--player alsa -s hw:CARD=DX5,DEV=0" in logs
    # PIPEWIRE_NODE/LATENCY steer the graph, which an ALSA output is not in.
    assert "PIPEWIRE_NODE=\n" in logs or "PIPEWIRE_NODE=" in logs.split("\n")[-3]
    player.stop()


def test_a_legacy_use_alsa_player_is_migrated(supervisor):
    """Players stored before the output picker carried use_alsa. They must not
    silently move back onto PipeWire the next time their config is written."""
    clean = validate({"name": "Old", "server": "127.0.0.1", "use_alsa": True})
    assert clean["output_mode"] == "alsa"
    assert clean["alsa_device"] == "default"
    assert "use_alsa" not in clean

    off = validate({"name": "Old", "server": "127.0.0.1", "use_alsa": False})
    assert off["output_mode"] == "pipewire"


def test_extra_arguments_are_appended(supervisor):
    player = make(supervisor, extra="--sampleformat 48000:24:2")
    player.start()
    assert wait_for(lambda: any("snapclient args" in line for line in player.logs))
    assert "--sampleformat 48000:24:2" in "\n".join(player.logs)
    player.stop()


def test_multiple_players_run_concurrently_with_distinct_nodes(supervisor):
    """The whole point of supervising in-process: one sink each, one container."""
    a = make(supervisor, name="Lounge", node="alsa_output.usb-Topping_DX5-00.analog-stereo")
    b = make(supervisor, name="Kitchen", node="alsa_output.usb-FiiO_K3-00.analog-stereo")
    a.start()
    b.start()
    assert wait_for(lambda: a.state == "running" and b.state == "running")

    assert wait_for(lambda: "PIPEWIRE_NODE=alsa_output.usb-Topping_DX5-00.analog-stereo"
                    in "\n".join(a.logs))
    assert wait_for(lambda: "PIPEWIRE_NODE=alsa_output.usb-FiiO_K3-00.analog-stereo"
                    in "\n".join(b.logs))
    assert a._proc.pid != b._proc.pid

    a.stop()
    assert a.state == "stopped"
    assert b.state == "running"  # stopping one must not disturb the other
    b.stop()


def test_a_crashing_player_is_restarted(supervisor, monkeypatch):
    monkeypatch.setenv("FAKE_SNAPCLIENT_MODE", "crash")
    player = make(supervisor)
    player.start()
    assert wait_for(lambda: player.restarts >= 2, timeout=10), player.restarts
    assert player.last_exit == 3
    player.stop()
    assert player.state == "stopped"


def test_stop_interrupts_the_backoff_promptly(supervisor, monkeypatch):
    """A stop must not wait out the retry delay."""
    monkeypatch.setenv("FAKE_SNAPCLIENT_MODE", "crash")
    monkeypatch.setattr(players_mod, "RETRY_START", 30.0)
    monkeypatch.setattr(players_mod, "RETRY_MAX", 30.0)
    player = make(supervisor)
    player.start()
    assert wait_for(lambda: player.state == "backoff", timeout=10)

    began = time.time()
    player.stop()
    assert time.time() - began < 5.0, "stop waited out the backoff"
    assert player.state == "stopped"


def test_backoff_resets_after_a_session_that_stayed_up(supervisor, monkeypatch):
    """An old outage must not leave a 60s penalty on the next failure.

    Same rule as entrypoint.sh: a session that lasted counts as healthy, so the
    next restart starts from 5s again rather than continuing the streak.
    """
    monkeypatch.setattr(players_mod, "HEALTHY_AFTER", 0.5)
    monkeypatch.setattr(players_mod, "RETRY_START", 0.2)
    monkeypatch.setattr(players_mod, "RETRY_MAX", 20.0)

    player = make(supervisor)
    player.start()
    assert wait_for(lambda: player.state == "running")

    # Kill it after it has been up long enough to count as healthy.
    time.sleep(0.8)
    player._proc.terminate()
    assert wait_for(lambda: player.restarts >= 1, timeout=10)
    assert wait_for(lambda: player.state in ("backoff", "running", "starting"), timeout=5)
    if player.state == "backoff":
        assert "restarting in 0s" in player.detail or "restarting in 1s" in player.detail
    player.stop()


def test_stop_immediately_after_start_leaves_no_orphan(supervisor):
    """stop() racing the launch must not strand a snapclient holding the sink.

    The supervisor thread assigns self._proc a moment after start() returns; a
    stop() that reads it too early terminates nothing and leaves both the child
    process and its thread running forever.
    """
    player = make(supervisor)
    player.start()
    player.stop()  # no sleep: land inside the launch window on purpose
    assert player.state == "stopped"
    assert player._proc is None
    assert not (player._thread and player._thread.is_alive())

    # And it can still be started again afterwards.
    player.start()
    assert wait_for(lambda: player.state == "running"), player.state
    player.stop()


def test_a_stopped_player_reports_no_leftover_detail(supervisor):
    """A stopped row must not keep showing what it was doing."""
    player = make(supervisor)
    player.start()
    assert wait_for(lambda: player.state == "running")
    player.stop()
    status = player.status()
    assert status["state"] == "stopped"
    assert status["detail"] == ""
    assert status["running"] is False
    assert status["uptime"] == 0


# ---- the sink watchdog ------------------------------------------------------


def test_player_restarts_when_its_sink_disappears(supervisor, monkeypatch):
    """Unplugging the DAC must not leave a green "running" player.

    snapclient keeps running when its output sink vanishes -- it quietly closes
    ALSA and sits there -- so without this watchdog the player reports healthy
    forever and never recovers.
    """
    monkeypatch.setattr(players_mod, "HEALTH_INTERVAL", 0.2)
    monkeypatch.setattr(players_mod, "SINK_GRACE", 0.6)
    monkeypatch.setattr(players_mod, "NODE_WAIT_SECONDS", 2.0)

    present = {"value": True}
    monkeypatch.setattr(players_mod, "sink_present", lambda node: present["value"])

    player = make(supervisor)
    player.start()
    assert wait_for(lambda: player.state == "running"), player.state
    first_pid = player._proc.pid

    present["value"] = False
    assert wait_for(lambda: player.state != "running", timeout=10), player.state
    assert any("has been gone" in line for line in player.logs)

    present["value"] = True
    assert wait_for(lambda: player.state == "running", timeout=15), player.state
    assert player._proc.pid != first_pid, "should be a fresh snapclient"
    player.stop()


def test_a_momentary_sink_blip_does_not_restart_the_player(supervisor, monkeypatch):
    """Only a sustained absence counts; sinks flicker during rate switches."""
    monkeypatch.setattr(players_mod, "HEALTH_INTERVAL", 0.1)
    monkeypatch.setattr(players_mod, "SINK_GRACE", 5.0)

    present = {"value": True}
    monkeypatch.setattr(players_mod, "sink_present", lambda node: present["value"])

    player = make(supervisor)
    player.start()
    assert wait_for(lambda: player.state == "running")
    pid = player._proc.pid

    present["value"] = False
    time.sleep(0.5)
    present["value"] = True
    time.sleep(0.5)

    assert player.state == "running"
    assert player._proc.pid == pid, "a brief blip must not restart snapclient"
    assert player.restarts == 0
    player.stop()


def test_a_player_with_no_node_is_not_watchdogged(supervisor, monkeypatch):
    """A player on the default sink has no node to watch; leave it alone."""
    monkeypatch.setattr(players_mod, "HEALTH_INTERVAL", 0.1)
    monkeypatch.setattr(players_mod, "SINK_GRACE", 0.3)
    monkeypatch.setattr(players_mod, "sink_present", lambda node: False)

    player = make(supervisor, name="Default sink", node="")
    player.start()
    assert wait_for(lambda: player.state == "running")
    time.sleep(1.0)
    assert player.state == "running"
    assert player.restarts == 0
    player.stop()


def test_a_missing_sink_holds_the_player_in_waiting(supervisor, monkeypatch):
    monkeypatch.setattr(players_mod, "sink_present", lambda node: False)
    monkeypatch.setattr(players_mod.shutil, "which", lambda name: "/usr/bin/" + name)
    player = make(supervisor)
    player.start()
    # It reports "waiting" as soon as it starts looking, then says the sink is
    # not present once NODE_WAIT_SECONDS is up.
    assert wait_for(lambda: player.state == "waiting", timeout=10), player.state
    assert wait_for(lambda: "not present" in player.detail, timeout=10), player.detail
    player.stop()


# ---- persistence ------------------------------------------------------------


def test_players_survive_a_reload(supervisor, tmp_path):
    make(supervisor, name="Kitchen", extra="--sampleformat 48000:24:2")
    saved = json.loads((tmp_path / "players.json").read_text())
    assert [p["name"] for p in saved["players"]] == ["Kitchen"]

    reloaded = Supervisor(config_path=str(tmp_path / "players.json"))
    listed = reloaded.list(with_snapcast=False)
    assert [p["name"] for p in listed] == ["Kitchen"]
    assert listed[0]["node"] == "alsa_output.usb-Topping_DX5-00.analog-stereo"
    assert listed[0]["extra"] == "--sampleformat 48000:24:2"


def test_every_field_of_every_player_is_persisted(supervisor, tmp_path):
    """The config file is the whole state: nothing about a player lives only in
    memory, so restarting the container brings every player back exactly as it
    was configured."""
    first = {
        "name": "Lounge · DX5", "client_id": "DX5-Snapclient",
        "node": "alsa_output.usb-Topping_DX5-00.analog-stereo",
        "server": "192.168.111.50", "port": 1804, "control_port": 1805,
        "output_mode": "pipewire", "alsa_device": "default",
        "pipewire_latency": "2048/192000", "latency_ms": -120,
        "volume": 0.55, "autostart": False, "extra": "--sampleformat 48000:24:2",
    }
    second = dict(first, name="Kitchen", client_id="Kitchen-K3", node="",
                  output_mode="alsa", alsa_device="hw:CARD=K3,DEV=0",
                  autostart=True, latency_ms=0, volume=1.0)
    supervisor.create(dict(first, autostart=False))
    supervisor.create(dict(second, autostart=False))

    stored = json.loads((tmp_path / "players.json").read_text())
    assert len(stored["players"]) == 2
    for entry in stored["players"]:
        assert entry["id"]  # the handle the API and the UI use

    reloaded = Supervisor(config_path=str(tmp_path / "players.json"))
    by_name = {p["name"]: p for p in reloaded.list(with_snapcast=False)}
    assert set(by_name) == {"Lounge · DX5", "Kitchen"}

    for expected in (first, second):
        got = by_name[expected["name"]]
        for key, value in expected.items():
            if key == "autostart":
                continue  # forced to False above so the reload does not launch
            assert got[key] == value, "%s did not survive the reload" % key


def test_the_config_is_written_atomically(supervisor, tmp_path):
    """os.replace, so a crash mid-write never leaves a half-written config."""
    make(supervisor, name="Kitchen")
    path = tmp_path / "players.json"
    assert json.loads(path.read_text())["players"]
    assert not (tmp_path / "players.json.tmp").exists()


def test_a_corrupt_config_does_not_stop_the_panel_booting(tmp_path):
    path = tmp_path / "players.json"
    path.write_text("{ this is not json")
    assert Supervisor(config_path=str(path)).list(with_snapcast=False) == []


def test_delete_stops_and_forgets(supervisor, tmp_path):
    player = make(supervisor)
    player.start()
    assert wait_for(lambda: player.state == "running")
    supervisor.delete(player.id)
    assert supervisor.list(with_snapcast=False) == []
    assert json.loads((tmp_path / "players.json").read_text())["players"] == []


def test_update_rebinds_a_running_player(supervisor):
    player = make(supervisor)
    player.start()
    assert wait_for(lambda: player.state == "running")

    supervisor.update(player.id, {"node": "alsa_output.usb-FiiO_K3-00.analog-stereo"})
    assert wait_for(lambda: player.state == "running", timeout=10)
    assert wait_for(
        lambda: "PIPEWIRE_NODE=alsa_output.usb-FiiO_K3-00.analog-stereo"
        in "\n".join(player.logs),
        timeout=10,
    )
    player.stop()


def test_update_leaves_a_stopped_player_stopped(supervisor):
    player = make(supervisor)
    supervisor.update(player.id, {"server": "10.0.0.9"})
    assert player.state == "stopped"
    assert player.config["server"] == "10.0.0.9"


def test_autostart_only_starts_the_flagged_ones(supervisor):
    quiet = make(supervisor, name="Quiet", autostart=False)
    loud = make(supervisor, name="Loud", autostart=True)
    loud.stop()

    supervisor.autostart()
    assert wait_for(lambda: loud.state == "running")
    assert quiet.state == "stopped"


# ---- panel settings ---------------------------------------------------------


def test_snapserver_settings_seed_new_players(supervisor):
    supervisor.update_settings(
        {"snapserver_host": "10.0.0.5", "snapserver_port": 1804,
         "snapserver_control_port": 1805, "snapserver_web_port": 1880}
    )
    assert supervisor.new_player_defaults() == {
        "server": "10.0.0.5", "port": 1804, "control_port": 1805}

    player = supervisor.create({"name": "Inherits", "autostart": False})
    assert player.config["server"] == "10.0.0.5"
    assert player.config["control_port"] == 1805

    explicit = supervisor.create(
        {"name": "Explicit", "server": "10.0.0.9", "autostart": False})
    assert explicit.config["server"] == "10.0.0.9"


def test_settings_persist(supervisor, tmp_path):
    supervisor.update_settings({"snapserver_host": "10.0.0.5"})
    reloaded = Supervisor(config_path=str(tmp_path / "players.json"))
    assert reloaded.settings["snapserver_host"] == "10.0.0.5"


@pytest.mark.parametrize(
    "bad,why",
    [
        ({"snapserver_host": "not a host"}, "invalid snapserver"),
        ({"snapserver_port": 0}, "port"),
        ({"snapserver_control_port": 99999}, "control port"),
        ({"snapserver_web_port": "abc"}, "web port"),
        ({"nonsense": 1}, "unknown setting"),
    ],
)
def test_bad_settings_are_rejected(supervisor, bad, why):
    with pytest.raises(players_mod.SettingsError) as err:
        supervisor.update_settings(bad)
    assert why.lower() in str(err.value).lower()


def test_an_empty_snapserver_host_is_allowed(supervisor):
    """Empty means "the default", not an error."""
    supervisor.update_settings({"snapserver_host": ""})
    assert supervisor.new_player_defaults()["server"] == "127.0.0.1"


# ---- ALSA enumeration -------------------------------------------------------


def test_alsa_devices_are_parsed_from_snapclient_l(monkeypatch):
    """snapclient prints "<index>: <name>" with the description on the next line."""
    monkeypatch.setattr(players_mod, "SNAPCLIENT", FAKE)
    monkeypatch.setenv("FAKE_ALSA_MODE", "hardware")
    devices = players_mod.list_alsa_devices(max_age=0)

    by_name = {d["device"]: d for d in devices}
    assert "hw:CARD=DX5,DEV=0" in by_name
    assert by_name["hw:CARD=DX5,DEV=0"]["description"].startswith("Topping DX5")
    # Hardware first: it is what somebody bypassing PipeWire is looking for.
    assert devices[0]["hardware"] is True
    assert by_name["lavrate"]["hardware"] is False


def test_only_plugins_means_dev_snd_was_not_passed_through(monkeypatch):
    monkeypatch.setattr(players_mod, "SNAPCLIENT", FAKE)
    monkeypatch.setenv("FAKE_ALSA_MODE", "plugins")
    devices = players_mod.list_alsa_devices(max_age=0)
    assert devices, "the plugin entries should still be listed"
    assert not any(d["hardware"] for d in devices)


def test_the_alsa_listing_is_cached(monkeypatch):
    """It costs a subprocess and the browser polls; the list only changes when
    hardware is plugged in."""
    monkeypatch.setattr(players_mod, "SNAPCLIENT", FAKE)
    players_mod.list_alsa_devices(max_age=0)
    calls = []
    monkeypatch.setattr(players_mod.subprocess, "run",
                        lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(OSError()))
    players_mod.list_alsa_devices()
    assert calls == [], "a cached listing still shelled out"


def test_a_missing_binary_offers_no_alsa_choices(monkeypatch):
    monkeypatch.setattr(players_mod, "SNAPCLIENT", "/nonexistent/snapclient")
    assert players_mod.list_alsa_devices(max_age=0) == []


@pytest.mark.parametrize("bad", ["", "has spaces", "semi;colon"])
def test_an_alsa_output_needs_a_valid_device(bad):
    with pytest.raises(PlayerError) as err:
        validate({"name": "X", "server": "127.0.0.1",
                  "output_mode": "alsa", "alsa_device": bad})
    assert "alsa" in str(err.value).lower() or "device" in str(err.value).lower()


def test_an_unknown_output_mode_is_rejected():
    with pytest.raises(PlayerError) as err:
        validate({"name": "X", "server": "127.0.0.1", "output_mode": "jack"})
    assert "output mode" in str(err.value).lower()


def test_an_alsa_player_is_not_watchdogged(supervisor, monkeypatch):
    """An ALSA device is not in the PipeWire graph: there is no sink to poll,
    and snapclient reports the device failing by itself."""
    monkeypatch.setattr(players_mod, "HEALTH_INTERVAL", 0.1)
    monkeypatch.setattr(players_mod, "SINK_GRACE", 0.3)
    monkeypatch.setattr(players_mod, "sink_present", lambda node: False)

    player = make(supervisor, name="Direct", output_mode="alsa",
                  alsa_device="hw:CARD=DX5,DEV=0", node="")
    player.start()
    assert wait_for(lambda: player.state == "running"), player.state
    time.sleep(1.0)
    assert player.state == "running"
    assert player.restarts == 0
    player.stop()
