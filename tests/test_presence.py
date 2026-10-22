import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest

from otodo_vav import HeatingMode, cli, presence

PHONE = "AA:BB:CC:DD:EE:FF"
# Monday 07:00: the schedule below is in comfort, Monday 10:00 in eco.
MORNING = datetime(2026, 10, 19, 7, 0, tzinfo=timezone.utc)
LATE_MORNING = datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc)
SCHEDULE = [{"d": 0, "mm": 2, "t": 360}, {"d": 0, "mm": 3, "t": 540}]


class FakeBbox(BaseHTTPRequestHandler):
    requests: ClassVar[list] = []
    needs_login: ClassVar[bool] = False

    def log_message(self, *args):
        pass

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else None
        self.requests.append((self.command, self.path, body))
        logged_in = "BBOX_ID=s1" in (self.headers.get("Cookie") or "")
        headers = {}
        if self.path == "/api/v1/login":
            code, out = (200, {}) if "password=secret" in body else (401, {})
            if code == 200:
                headers["Set-Cookie"] = "BBOX_ID=s1; Path=/"
        elif self.needs_login and not logged_in:
            code, out = 401, {}
        else:
            hosts = [
                {"hostname": "iphone", "macaddress": "aa:bb:cc:dd:ee:ff", "active": 1},
                {"hostname": "tv", "macaddress": "11:22:33:44:55:66", "active": 0},
            ]
            code, out = 200, [{"hosts": {"list": hosts}}]
        data = json.dumps(out).encode()
        self.send_response(code)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_PUT = _handle


@pytest.fixture
def bbox_url():
    FakeBbox.requests = []
    FakeBbox.needs_login = False
    srv = HTTPServer(("127.0.0.1", 0), FakeBbox)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()


def test_bbox_active_macs(bbox_url):
    assert presence.BboxClient(bbox_url).active_macs() == {"aa:bb:cc:dd:ee:ff"}


def test_bbox_logs_in_on_401(bbox_url):
    FakeBbox.needs_login = True
    assert presence.BboxClient(bbox_url, "secret").active_macs() == {"aa:bb:cc:dd:ee:ff"}
    assert [(m, p) for m, p, _ in FakeBbox.requests] == [
        ("GET", "/api/v1/hosts"),
        ("PUT", "/api/v1/login"),
        ("GET", "/api/v1/hosts"),
    ]


def test_bbox_without_password_reports_401(bbox_url):
    FakeBbox.needs_login = True
    with pytest.raises(presence.BboxError, match="BBOX_PASSWORD"):
        presence.BboxClient(bbox_url).hosts()


@pytest.mark.parametrize(
    ("service", "now", "expected"),
    [
        ({"workingMode": 1}, MORNING, True),
        ({"workingMode": 3}, MORNING, True),
        ({"workingMode": 4}, MORNING, False),
        ({"workingMode": 7}, MORNING, False),
        ({"workingMode": 1, "isActive": False}, MORNING, False),
        ({"workingMode": 0, "weeklyEvents": SCHEDULE}, MORNING, True),
        ({"workingMode": 0, "weeklyEvents": SCHEDULE}, LATE_MORNING, False),
    ],
)
def test_is_heating(service, now, expected):
    assert presence.is_heating(service, now) is expected


class FakeBboxClient:
    def __init__(self):
        self.macs = {"aa:bb:cc:dd:ee:ff"}

    def active_macs(self):
        return self.macs


class FakeVav:
    def __init__(self):
        self.services = [
            {"_id": 12, "name": "Salon", "workingMode": 1, "sensors": {"temp": 20.5}},
            {"_id": 13, "name": "Chambre", "workingMode": 4},
        ]
        self.mode_changes = []

    def list_thermostats(self, hub_id):
        return self.services

    def set_heating_mode(self, hub_id, service_id, mode):
        self.mode_changes.append((hub_id, service_id, mode))
        for service in self.services:
            if service["_id"] == service_id:
                service["workingMode"] = int(mode)


@pytest.fixture
def setup():
    clock = [0.0]
    messages = []
    bbox, vav = FakeBboxClient(), FakeVav()

    def make(**kwargs):
        return presence.PresenceWatcher(
            vav,
            bbox,
            [PHONE],
            ["hub1"],
            away_after=1800,
            notify=messages.append,
            clock=lambda: clock[0],
            now=lambda: MORNING,
            **kwargs,
        )

    return make, clock, messages, bbox, vav


def test_alerts_once_after_away_delay(setup):
    make, clock, messages, bbox, _ = setup
    watcher = make()
    watcher.tick()
    bbox.macs = set()
    clock[0] = 1700
    watcher.tick()
    assert messages == []
    clock[0] = 1800
    watcher.tick()
    clock[0] = 1900
    watcher.tick()
    assert messages == ["Personne à la maison et Salon chauffe (20.5 °C)"]


def test_alerts_again_after_coming_back(setup):
    make, clock, messages, bbox, _ = setup
    watcher = make()
    bbox.macs = set()
    clock[0] = 1800
    watcher.tick()
    bbox.macs = {"aa:bb:cc:dd:ee:ff"}
    clock[0] = 1900
    watcher.tick()
    bbox.macs = set()
    clock[0] = 3700
    watcher.tick()
    assert len(messages) == 2


def test_set_eco_switches_and_restores(setup):
    make, clock, messages, bbox, vav = setup
    watcher = make(set_eco=True)
    bbox.macs = set()
    clock[0] = 1800
    watcher.tick()
    assert vav.mode_changes == [("hub1", 12, HeatingMode.ECO)]
    assert messages[-1].endswith("passé en Eco")
    bbox.macs = {"aa:bb:cc:dd:ee:ff"}
    watcher.tick()
    assert vav.mode_changes[-1] == ("hub1", 12, HeatingMode.COMFORT)
    assert messages[-1] == "Retour à la maison : modes de chauffage restaurés"


def test_cli_bbox_hosts(bbox_url, capsys):
    cli.main(["bbox-hosts", "--bbox-url", bbox_url])
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("on  aa:bb:cc:dd:ee:ff")
    assert lines[1].startswith("off 11:22:33:44:55:66")
