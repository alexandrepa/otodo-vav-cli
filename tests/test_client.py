import functools
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest

from vav_connect import HeatingMode, NotAuthenticatedError, VavClient, VavError, cli

THERMOSTAT = {
    "_id": 12,
    "hubId": "hub1",
    "type": "thermostat",
    "workingMode": 0,
    "comfortTemp": 19,
    "sensors": {"temp": 18.4},
    "modules": [{"capabilityId": 1, "deviceId": 7, "endpointIndex": 0}],
    "weeklyEvents": [{"d": 1, "mm": 3, "t": 1860}],
}
DEVICES = [
    {
        "_id": 7,
        "endpoints": [{"capabilities": [{"_id": 1, "value": 2}, {"_id": 4, "value": 2925}]}],
    },
    {"_id": 8, "endpoints": [{"capabilities": [{"_id": 1, "value": 0}]}]},
]


class FakeOtodo(BaseHTTPRequestHandler):
    requests: ClassVar[list] = []
    rejected_tokens: ClassVar[set] = set()
    expires_in: ClassVar[int] = 3600
    refresh_fails: ClassVar[bool] = False

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
                out = {"access_token": "A1", "refresh_token": "R1", "expires_in": self.expires_in}
                code = 200
        elif self.path == "/refreshToken":
            if self.refresh_fails:
                out, code = {"error": "unknown endpoint"}, 404
            else:
                out, code = {"access_token": "A2", "expires_in": 3600}, 200
        elif token in self.rejected_tokens:
            out, code = {"error": "expired"}, 401
        elif self.path == "/devices":
            out, code = DEVICES, 200
        elif self.path == "/hubs/hub1/local-services":
            out, code = [THERMOSTAT, {"_id": 13, "type": "water-heater"}], 200
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
    FakeOtodo.rejected_tokens = set()
    FakeOtodo.expires_in = 3600
    FakeOtodo.refresh_fails = False
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
    assert client.session.expires_at is not None


def test_login_failure_raises(server):
    url, _ = server
    with pytest.raises(VavError) as exc:
        VavClient(url).login("me@example.com", "wrong")
    assert exc.value.status == 401


def test_request_requires_login(server):
    url, _ = server
    with pytest.raises(NotAuthenticatedError):
        VavClient(url).list_devices()


def test_rejected_token_is_refreshed_once(server, client):
    _, log = server
    FakeOtodo.rejected_tokens = {"A1"}
    client.list_devices()
    assert [(m, p, t) for m, p, t, _ in log[1:]] == [
        ("GET", "/devices", "A1"),
        ("POST", "/refreshToken", "R1"),
        ("GET", "/devices", "A2"),
    ]
    assert client.session.refresh_token == "R1"


def test_token_is_refreshed_before_expiry(server):
    url, log = server
    FakeOtodo.expires_in = 60
    c = VavClient(url, refresh_margin=300)
    c.login("me@example.com", "pw")
    c.list_hubs()
    assert [(m, p, t) for m, p, t, _ in log[1:]] == [
        ("POST", "/refreshToken", "R1"),
        ("GET", "/hubs", "A2"),
    ]
    c.list_hubs()
    assert log[-1][1:3] == ("/hubs", "A2")
    assert len(log) == 4


def test_failed_early_refresh_keeps_current_token(server):
    url, log = server
    FakeOtodo.expires_in = 60
    FakeOtodo.refresh_fails = True
    c = VavClient(url, refresh_margin=300)
    c.login("me@example.com", "pw")
    c.list_hubs()
    assert log[-1][1:3] == ("/hubs", "A1")


def test_send_action_body(server, client):
    _, log = server
    client.send_action(7, 0, 4, 19.5)
    assert log[-1] == (
        "POST",
        "/devices/7/actions",
        "A1",
        {
            "endpoints": [
                {"index": 0, "capabilities": [{"_id": 4, "value": 19.5, "parameters": None}]}
            ]
        },
    )


def test_list_thermostats_filters_by_type(server, client):
    assert [s["_id"] for s in client.list_thermostats("hub1")] == [12]


def test_set_heating_mode_puts_full_service(server, client):
    _, log = server
    client.set_heating_mode("hub1", 12, HeatingMode.ECO)
    method, path, _, body = log[-1]
    assert (method, path) == ("PUT", "/hubs/hub1/local-services/12")
    assert body["workingMode"] == 4
    assert body["comfortTemp"] == 19
    assert body["weeklyEvents"] == THERMOSTAT["weeklyEvents"]
    assert body["updatedAt"].endswith("Z")


def test_set_comfort_temperature(server, client):
    _, log = server
    client.set_comfort_temperature("hub1", 12, 20.5)
    assert log[-1][3]["comfortTemp"] == 20.5


def test_unknown_service_raises(server, client):
    with pytest.raises(KeyError):
        client.set_heating_mode("hub1", 99, HeatingMode.OFF)


def test_measured_temperatures(server, client):
    assert client.measured_temperatures() == {7: 19.4}


def test_energy_query_omits_unset_params(server, client):
    _, log = server
    client.energy_consumption("hourly", 2026, 10, 21)
    client.energy_consumption("monthly", 2026)
    assert log[-2][1] == "/consumptions/energy?duration=hourly&year=2026&month=10&day=21"
    assert log[-1][1] == "/consumptions/energy?duration=monthly&year=2026"


@pytest.fixture
def cli_env(server, tmp_path, monkeypatch):
    url, log = server
    token_file = tmp_path / "token.json"
    token_file.write_text(json.dumps({"access_token": "A1", "refresh_token": "R1"}))
    monkeypatch.setenv("VAV_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(cli, "VavClient", functools.partial(VavClient, url))
    return log, token_file


def test_cli_set_mode_uses_numeric_service_id(cli_env, capsys):
    log, _ = cli_env
    cli.main(["set-mode", "hub1", "12", "eco"])
    assert log[-1][:2] == ("PUT", "/hubs/hub1/local-services/12")
    assert log[-1][3]["workingMode"] == 4


def test_cli_action_sends_numeric_capability_id(cli_env):
    log, _ = cli_env
    cli.main(["action", "7", "0", "4", "19.5"])
    assert log[-1][3]["endpoints"][0]["capabilities"][0]["_id"] == 4


def test_cli_persists_refreshed_token(cli_env):
    _, token_file = cli_env
    FakeOtodo.rejected_tokens = {"A1"}
    cli.main(["hubs"])
    saved = json.loads(token_file.read_text())
    assert saved["access_token"] == "A2"
    assert saved["expires_at"] is not None


def test_set_schedule_puts_sorted_events(server, client):
    _, log = server
    client.set_schedule("hub1", 12, [{"d": 2, "mm": 2, "t": 3240}, {"d": 0, "mm": 3, "t": 60}])
    method, path, _, body = log[-1]
    assert (method, path) == ("PUT", "/hubs/hub1/local-services/12")
    assert [e["t"] for e in body["weeklyEvents"]] == [60, 3240]
    assert body["workingMode"] == 0


def test_cli_schedule_prints_days(cli_env, capsys):
    cli.main(["schedule", "hub1", "12"])
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "sun  -"
    assert out[1] == "mon  07:00 eco"


def test_cli_set_schedule_replaces_days(cli_env, capsys):
    log, _ = cli_env
    cli.main(["set-schedule", "hub1", "12", "--day", "sat,sun", "08:00=comfort,22:00=eco"])
    method, path, _, body = log[-1]
    assert (method, path) == ("PUT", "/hubs/hub1/local-services/12")
    assert body["weeklyEvents"] == [
        {"d": 0, "mm": 2, "t": 480},
        {"d": 0, "mm": 3, "t": 1320},
        {"d": 1, "mm": 3, "t": 1860},
        {"d": 6, "mm": 2, "t": 9120},
        {"d": 6, "mm": 3, "t": 9960},
    ]
    assert "sat  08:00 comfort  22:00 eco" in capsys.readouterr().out


def test_cli_set_schedule_dry_run_sends_nothing(cli_env):
    log, _ = cli_env
    cli.main(["set-schedule", "hub1", "12", "--day", "all", "06:00=comfort", "--dry-run"])
    assert all(method == "GET" for method, *_ in log)


def test_cli_set_schedule_rejects_bad_program(cli_env):
    with pytest.raises(SystemExit):
        cli.main(["set-schedule", "hub1", "12", "--day", "mon", "06:00=hot"])


def test_cli_copy_schedule(cli_env):
    log, _ = cli_env
    cli.main(["copy-schedule", "hub1", "12", "12"])
    method, path, _, body = log[-1]
    assert (method, path) == ("PUT", "/hubs/hub1/local-services/12")
    assert body["weeklyEvents"] == THERMOSTAT["weeklyEvents"]
