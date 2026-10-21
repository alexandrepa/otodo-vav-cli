import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest

from pyvav import HeatingMode, NotAuthenticatedError, VavClient, VavError


class FakeOtodo(BaseHTTPRequestHandler):
    requests: ClassVar[list] = []

    def log_message(self, *args):
        pass

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        token = self.headers.get("token")
        self.requests.append((self.command, self.path, token, body))

        if self.path == "/authenticate":
            if body["password"] != "pw":
                out, code = {"error": "bad credentials"}, 401
            else:
                out, code = {"access_token": "A1", "refresh_token": "R1"}, 200
        elif self.path == "/refreshToken":
            out, code = {"access_token": "A2"}, 200
        elif token == "A1" and self.path == "/devices":
            out, code = {"error": "expired"}, 401
        elif self.path == "/hubs/hub1/local-services":
            out, code = (
                [{"_id": "s1", "workingMode": 0, "comfortTemp": 19, "weeklyEvents": []}],
                200,
            )
        else:
            out, code = {"ok": True}, 200

        data = json.dumps(out).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle


@pytest.fixture
def server():
    FakeOtodo.requests = []
    srv = HTTPServer(("127.0.0.1", 0), FakeOtodo)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_port}", FakeOtodo.requests
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def client(server):
    url, _ = server
    c = VavClient(url)
    c.login("me@example.com", "pw")
    return c


def test_login_sends_park_name(server, client):
    _, log = server
    assert log[0] == (
        "POST",
        "/authenticate",
        None,
        {"email": "me@example.com", "password": "pw", "parkName": "vav"},
    )
    assert client.session.access_token == "A1"


def test_login_failure_raises(server):
    url, _ = server
    with pytest.raises(VavError) as exc:
        VavClient(url).login("me@example.com", "wrong")
    assert exc.value.status == 401


def test_request_requires_login(server):
    url, _ = server
    with pytest.raises(NotAuthenticatedError):
        VavClient(url).list_devices()


def test_expired_token_is_refreshed_once(server, client):
    _, log = server
    client.list_devices()
    assert [(m, p, t) for m, p, t, _ in log[1:]] == [
        ("GET", "/devices", "A1"),
        ("POST", "/refreshToken", "R1"),
        ("GET", "/devices", "A2"),
    ]
    assert client.session.refresh_token == "R1"


def test_send_action_body(server, client):
    _, log = server
    client.send_action("d1", 0, "cap1", 19.5)
    assert log[-1] == (
        "POST",
        "/devices/d1/actions",
        "A1",
        {
            "endpoints": [
                {"index": 0, "capabilities": [{"_id": "cap1", "value": 19.5, "parameters": None}]}
            ]
        },
    )


def test_set_heating_mode_puts_full_service(server, client):
    _, log = server
    client.set_heating_mode("hub1", "s1", HeatingMode.ECO)
    method, path, _, body = log[-1]
    assert (method, path) == ("PUT", "/hubs/hub1/local-services/s1")
    assert body["workingMode"] == 4
    assert body["comfortTemp"] == 19
    assert body["updatedAt"].endswith("Z")


def test_set_comfort_temperature(server, client):
    _, log = server
    client.set_comfort_temperature("hub1", "s1", 20.5)
    assert log[-1][3]["comfortTemp"] == 20.5


def test_unknown_service_raises(server, client):
    with pytest.raises(KeyError):
        client.set_heating_mode("hub1", "missing", HeatingMode.OFF)


def test_energy_query_omits_unset_params(server, client):
    _, log = server
    client.energy_consumption("hourly", 2026, 10, 21)
    client.energy_consumption("monthly", 2026)
    assert log[-2][1] == "/consumptions/energy?duration=hourly&year=2026&month=10&day=21"
    assert log[-1][1] == "/consumptions/energy?duration=monthly&year=2026"
