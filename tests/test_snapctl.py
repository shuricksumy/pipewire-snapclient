"""Snapserver JSON-RPC: what the UI needs to draw a row, against a fake server."""
import os
import time

import pytest

import snapctl
from fake_snapserver import FakeSnapserver

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "panel", "static", "index.html")


@pytest.fixture
def server():
    snapctl.invalidate()
    srv = FakeSnapserver()
    yield srv
    srv.close()
    snapctl.invalidate()


def describe(server, client_id="DX5", **kw):
    return snapctl.describe("127.0.0.1", server.port, client_id, **kw)


# ---- what a row shows -------------------------------------------------------


def test_metadata_and_capabilities(server):
    info = describe(server)
    assert info["title"] == "O eterne Deus"
    assert info["artist"] == "Raphaela Gromes"  # the server sends a list
    assert info["connected"] is True
    assert info["can_control"] is True
    assert info["attached"] is True
    assert info["volume"] == 40


def test_an_unknown_client_is_none(server):
    assert describe(server, "NeverSeen") is None


def test_a_plain_pipe_stream_offers_no_control(server):
    """A stream with canControl=false must not get transport buttons."""
    info = describe(server, "Ghost", use_cache=False)
    assert info["can_control"] is False


# ---- pausing must not take the controls away --------------------------------


def test_pausing_keeps_the_controls_alive(server):
    """Music Assistant parks a paused group on a stream that cannot be
    controlled. Read naively that looks like "nothing to control here", the
    buttons vanish, and there is no way to resume from the panel. The real
    stream is still there, so the last controllable one is remembered.
    """
    first = describe(server, use_cache=False)
    assert first["can_control"] is True and first["attached"] is True

    # MA pauses: the client's group is moved onto the uncontrollable stream.
    server.groups["g1"] = "default"

    paused = describe(server, use_cache=False)
    assert paused["can_control"] is True, "controls disappeared on pause"
    assert paused["attached"] is False, "should be flagged as parked, for the label"
    assert paused["stream_id"] == "ma-dx5", "should still drive the real stream"
    # And the metadata is still there, so the row does not go blank.
    assert paused["title"] == "O eterne Deus"


def test_resuming_a_paused_player_targets_the_remembered_stream(server):
    describe(server, use_cache=False)          # sees ma-dx5 while controllable
    server.groups["g1"] = "default"            # MA parks it
    info = describe(server, use_cache=False)

    snapctl.control("127.0.0.1", server.port, info["stream_id"], "play")
    assert ("Stream.Control",
            {"id": "ma-dx5", "command": "play", "params": {}}) in server.calls


def test_forget_drops_the_remembered_stream(server):
    """Deleting a player must not leave its stream memory behind."""
    describe(server, use_cache=False)
    snapctl.forget("DX5")
    server.groups["g1"] = "default"
    info = describe(server, use_cache=False)
    assert info["can_control"] is False


# ---- caching ----------------------------------------------------------------


def test_status_is_cached_briefly(server):
    describe(server)
    before = len(server.calls)
    describe(server)
    assert len(server.calls) == before, "a second read inside the TTL hit the server"


def test_a_failing_server_is_cached_too(monkeypatch):
    """Without this, every player pointed at a down server pays a full connect
    timeout on every UI poll."""
    monkeypatch.setattr(snapctl, "STATUS_TIMEOUT", 0.3)
    snapctl.invalidate()

    began = time.time()
    # TEST-NET-3, reserved for documentation: guaranteed not to answer.
    for _ in range(4):
        with pytest.raises(snapctl.SnapcastError):
            snapctl.describe("203.0.113.1", 1705, "DX5")
    elapsed = time.time() - began
    assert elapsed < 2.0, "four polls took %.1fs -- the failure was not cached" % elapsed


def test_a_recovered_server_is_used_again(server, monkeypatch):
    monkeypatch.setattr(snapctl, "FAIL_TTL", 0.2)
    with pytest.raises(snapctl.SnapcastError):
        snapctl.describe("203.0.113.1", 1705, "DX5")
    time.sleep(0.3)
    assert describe(server)["title"] == "O eterne Deus"


# ---- the page draws what describe() reports ---------------------------------


def test_the_page_resets_a_stopped_row():
    page = open(INDEX).read()
    assert "player stopped" in page
    # ... and the paused label comes from the attached flag, not from guessing.
    assert "s.attached === false" in page
