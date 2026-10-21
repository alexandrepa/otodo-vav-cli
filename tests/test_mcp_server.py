import copy

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from otodo_vav import HeatingMode, VavError, mcp_server
from otodo_vav.mcp_server import RadiatorController, build_server

SERVICES = [
    {
        "_id": 12,
        "type": "thermostat",
        "roomId": "r1",
        "workingMode": 0,
        "comfortTemp": 19,
        "sensors": {"temp": 18.4},
        "weeklyEvents": [{"d": 0, "mm": 2, "t": 360}, {"d": 0, "mm": 3, "t": 540}],
    },
    {
        "_id": 14,
        "type": "thermostat",
        "roomId": "r2",
        "workingMode": 4,
        "comfortTemp": 20,
        "modules": [{"deviceId": 9, "endpointIndex": 0, "capabilityId": 1}],
    },
    {
        "_id": 15,
        "type": "thermostat",
        "roomId": "r3",
        "workingMode": 0,
        "comfortTemp": 19,
        "sensors": {"temp": {"capabilityId": 4, "deviceId": 20, "endpointIndex": 0}},
        "modules": [{"deviceId": 21, "endpointIndex": 0, "capabilityId": 6}],
    },
    {"_id": 13, "type": "water-heater"},
]


class FakeClient:
    def __init__(self):
        self.services = copy.deepcopy(SERVICES)
        self.calls = []
        self.expired = False
        self.session = "logged in"

    def _check(self):
        if self.expired:
            raise VavError(401, "GET", "/x", "expired")

    def login(self, email, password):
        self.calls.append(("login", email))
        self.expired = False
        self.session = "logged in"

    def list_rooms(self):
        self._check()
        return [
            {"_id": "r1", "name": "Salon"},
            {"_id": "r2", "name": "Chambre Élise"},
            {"_id": "r3", "name": "Bureau "},
        ]

    def list_hubs(self):
        return [{"_id": "hub1"}]

    def list_thermostats(self, hub_id):
        return [s for s in self.services if s["type"] == "thermostat"]

    def sensor_readings(self):
        return {
            9: {"temperature": 17.5},
            20: {"temperature": 19.0, "humidity": 60.85, "battery": 100},
        }

    def _service(self, service_id):
        return next(s for s in self.services if s["_id"] == service_id)

    def set_heating_mode(self, hub_id, service_id, mode):
        self.calls.append(("mode", hub_id, service_id, mode))
        self._service(service_id)["workingMode"] = int(mode)

    def set_comfort_temperature(self, hub_id, service_id, temperature):
        self.calls.append(("temp", hub_id, service_id, temperature))

    def set_schedule(self, hub_id, service_id, events):
        self.calls.append(("schedule", hub_id, service_id, events))


@pytest.fixture
def fake():
    return FakeClient()


@pytest.fixture
def controller(fake):
    return RadiatorController(fake, "me@example.com", "pw")


def test_list_radiators_uses_room_names_and_device_temperature(controller):
    assert controller.list_radiators() == [
        {
            "id": 12,
            "name": "Salon",
            "mode": "auto",
            "comfort_temperature": 19,
            "measured_temperature": 18.4,
            "humidity": None,
            "battery": None,
            "active": True,
        },
        {
            "id": 14,
            "name": "Chambre Élise",
            "mode": "eco",
            "comfort_temperature": 20,
            "measured_temperature": 17.5,
            "humidity": None,
            "battery": None,
            "active": True,
        },
        {
            "id": 15,
            "name": "Bureau",
            "mode": "auto",
            "comfort_temperature": 19,
            "measured_temperature": 19.0,
            "humidity": 60.85,
            "battery": 100,
            "active": True,
        },
    ]


def test_set_mode_matches_names_loosely(controller, fake):
    controller.set_mode(["salon", "chambre elise"], "comfort")
    assert [c[2:] for c in fake.calls] == [(12, HeatingMode.COMFORT), (14, HeatingMode.COMFORT)]


def test_set_mode_all_and_ids(controller, fake):
    controller.set_mode(["all"], "frost-protection")
    controller.set_mode(["14"], "off")
    assert [c[2] for c in fake.calls] == [12, 14, 15, 14]
    assert fake.calls[-1][3] is HeatingMode.OFF


def test_partial_name_match(controller, fake):
    controller.set_mode(["chambre"], "eco")
    assert fake.calls == [("mode", "hub1", 14, HeatingMode.ECO)]


def test_unknown_radiator_lists_known_ones(controller, fake):
    with pytest.raises(ToolError, match="Salon \\(12\\)"):
        controller.set_mode(["cuisine"], "eco")
    assert fake.calls == []


def test_unknown_mode(controller):
    with pytest.raises(ToolError, match="unknown mode"):
        controller.set_mode(["all"], "turbo")


def test_comfort_temperature_bounds_and_rounding(controller, fake):
    with pytest.raises(ToolError):
        controller.set_comfort_temperature(["all"], 35)
    controller.set_comfort_temperature(["salon"], 20.3)
    assert fake.calls == [("temp", "hub1", 12, 20.5)]


def test_get_schedule(controller):
    out = controller.get_schedule("Salon")
    assert out["schedule"]["mon"] == ["06:00 comfort", "09:00 eco"]


def test_set_schedule_keeps_other_days(controller, fake):
    controller.set_schedule(["salon"], "sat,sun", "08:00=comfort,23:00=eco")
    (_, _, service_id, events) = fake.calls[0]
    assert service_id == 12
    assert {e["d"] for e in events} == {0, 5, 6}


def test_set_schedule_dry_run_and_bad_program(controller, fake):
    out = controller.set_schedule(["salon"], "mon", "07:00=comfort", dry_run=True)
    assert out[0]["applied"] is False and out[0]["schedule"]["mon"] == ["07:00 comfort"]
    with pytest.raises(ToolError):
        controller.set_schedule(["salon"], "mon", "07:00=hot")
    assert fake.calls == []


def test_relogin_when_session_expired(controller, fake):
    fake.expired = True
    controller.list_radiators()
    assert fake.calls == [("login", "me@example.com")]


def test_logs_in_on_first_call(controller, fake):
    fake.session = None
    controller.list_radiators()
    assert fake.calls == [("login", "me@example.com")]


def test_reports_missing_credentials(fake):
    fake.session = None
    with pytest.raises(ToolError, match="VAV_EMAIL"):
        RadiatorController(fake).list_radiators()


def test_server_starts_without_logging_in(monkeypatch, tmp_path):
    monkeypatch.setenv("VAV_TOKEN_FILE", str(tmp_path / "token.json"))
    monkeypatch.setenv("VAV_EMAIL", "me@example.com")
    monkeypatch.setenv("VAV_PASSWORD", "pw")
    controller = mcp_server._controller_from_env()
    assert controller.client.session is None and controller.email == "me@example.com"
    monkeypatch.delenv("VAV_EMAIL")
    assert mcp_server._controller_from_env().client.session is None


def test_http_is_stateless_on_the_secret_path(monkeypatch):
    runs = []
    monkeypatch.setattr(mcp_server, "_controller_from_env", lambda: None)
    monkeypatch.setattr(
        mcp_server,
        "build_server",
        lambda c: type("S", (), {"run": lambda *a, **k: runs.append(k)})(),
    )
    mcp_server.main(["--http", "--host", "0.0.0.0", "--secret", "s" * 24])
    assert runs == [
        {
            "host": "0.0.0.0",
            "port": 8099,
            "streamable_http_path": "/mcp/" + "s" * 24,
            "stateless_http": True,
        }
    ]


def test_tools_are_exposed(controller, fake):
    server = build_server(controller)

    async def run():
        names = {tool.name for tool in await server.list_tools()}
        result = await server.call_tool("set_mode", {"radiators": ["Salon"], "mode": "eco"})
        return names, result

    names, result = anyio.run(run)
    assert names == {
        "list_radiators",
        "set_mode",
        "set_comfort_temperature",
        "get_schedule",
        "set_schedule",
    }
    assert not result.is_error
    assert fake.calls == [("mode", "hub1", 12, HeatingMode.ECO)]


def test_set_mode_schema_lists_the_modes(controller):
    tools = anyio.run(build_server(controller).list_tools)
    schema = next(t for t in tools if t.name == "set_mode").input_schema
    assert schema["properties"]["mode"]["enum"] == mcp_server.MODES
