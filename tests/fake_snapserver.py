"""A minimal Snapserver control port for tests.

Speaks the same newline-delimited JSON-RPC as the real thing on 1705, with a
status payload shaped like one captured from a live Music Assistant snapserver:
per-stream capability flags that differ between streams, metadata with an
artist *list*, and clients whose config.name starts empty.
"""
import json
import socket
import threading


class FakeSnapserver:
    def __init__(self):
        self.calls = []
        self.clients = {
            "DX5": {"name": "", "connected": True, "host": "ac8c319d5021",
                    "group": "g1", "volume": 40, "muted": False},
            "Kitchen": {"name": "", "connected": True, "host": "ac8c319d5021",
                          "group": "g2", "volume": 8, "muted": False},
            "Ghost": {"name": "", "connected": False, "host": "878955be3733",
                      "group": "g3", "volume": 25, "muted": False},
        }
        self.groups = {"g1": "ma-dx5", "g2": "ma-kitchen", "g3": "default"}
        self.streams = {
            "ma-dx5": {
                "status": "idle", "canControl": True, "canPause": True,
                "canGoNext": True, "canGoPrevious": False,
                "playbackStatus": "unknown",
                "metadata": {"title": "O eterne Deus", "artist": ["Raphaela Gromes"],
                             "album": "O eterne Deus", "artUrl": "http://x/art.png"},
            },
            "ma-kitchen": {
                "status": "playing", "canControl": True, "canPause": True,
                "canGoNext": True, "canGoPrevious": True,
                "playbackStatus": "playing",
                "metadata": {"title": "Baby Don't Hurt Me",
                             "artist": ["David Guetta", "Anne-Marie"],
                             "album": "Baby Don't Hurt Me", "artUrl": "http://x/b.png"},
            },
            # A plain pipe stream: no control, no metadata. The UI must not
            # offer buttons for this one.
            "default": {"status": "idle", "canControl": False, "canPause": False,
                        "canGoNext": False, "canGoPrevious": False},
        }
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(8)
        self.port = self._server.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    # ---- payloads ----------------------------------------------------------

    def status(self):
        groups = []
        for gid, stream_id in self.groups.items():
            members = [
                {"id": cid,
                 "connected": c["connected"],
                 "config": {"name": c["name"],
                            "volume": {"percent": c["volume"], "muted": c["muted"]}},
                 "host": {"name": c["host"]}}
                for cid, c in self.clients.items() if c["group"] == gid
            ]
            groups.append({"id": gid, "stream_id": stream_id, "muted": False,
                           "clients": members})
        streams = [{"id": sid, "status": s["status"],
                    "properties": {k: v for k, v in s.items() if k != "status"}}
                   for sid, s in self.streams.items()]
        return {"server": {"groups": groups, "streams": streams,
                           "server": {"snapserver": {"version": "0.34.0"}}}}

    def handle(self, request):
        method = request.get("method")
        params = request.get("params") or {}
        self.calls.append((method, params))

        if method == "Server.GetStatus":
            return self.status()
        if method == "Client.SetName":
            self.clients[params["id"]]["name"] = params["name"]
            return {"name": params["name"]}
        if method == "Client.SetVolume":
            client = self.clients[params["id"]]
            volume = params["volume"]
            if "percent" in volume:
                client["volume"] = volume["percent"]
            if "muted" in volume:
                client["muted"] = volume["muted"]
            return {"volume": volume}
        if method == "Stream.Control":
            stream = self.streams.get(params["id"])
            if stream is None:
                return {"__error__": "unknown stream"}
            if not stream.get("canControl"):
                return {"__error__": "stream cannot be controlled"}
            stream["playbackStatus"] = (
                "paused" if params["command"] == "pause" else "playing")
            return "ok"
        if method == "Server.DeleteClient":
            self.clients.pop(params["id"], None)
            return {"server": self.status()["server"]}
        return {"__error__": "unknown method %s" % method}

    # ---- plumbing ----------------------------------------------------------

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._session, args=(conn,), daemon=True).start()

    def _session(self, conn):
        with conn:
            buffer = b""
            conn.settimeout(5)
            try:
                while not self._stop.is_set():
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        if not line.strip():
                            continue
                        request = json.loads(line)
                        result = self.handle(request)
                        if isinstance(result, dict) and "__error__" in result:
                            reply = {"id": request.get("id"), "jsonrpc": "2.0",
                                     "error": {"code": -32603,
                                               "message": result["__error__"]}}
                        else:
                            reply = {"id": request.get("id"), "jsonrpc": "2.0",
                                     "result": result}
                        # A real server also pushes notifications; emit one so
                        # the client has to skip past it to find its reply.
                        conn.sendall((json.dumps(
                            {"jsonrpc": "2.0", "method": "Server.OnUpdate",
                             "params": {}}) + "\r\n").encode())
                        conn.sendall((json.dumps(reply) + "\r\n").encode())
            except (OSError, ValueError):
                return

    def close(self):
        self._stop.set()
        try:
            self._server.close()
        except OSError:
            pass
