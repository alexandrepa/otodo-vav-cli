"""Client for the Otodo cloud API used by the VAV mobile app."""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import IntEnum
from typing import Any

import requests

from .schedule import sort_events

BASE_URL = "https://api.gateway.otodo.io"
PARK_NAME = "vav"
APP_VERSION = "4.14.4"


class HeatingMode(IntEnum):
    """`workingMode` of a heating service."""

    AUTO = 0
    COMFORT = 1
    COMFORT_MINUS_1 = 2
    COMFORT_MINUS_2 = 3
    ECO = 4
    FROST_PROTECTION = 5
    OFF = 6
    HOLIDAYS = 7


# Pilot-wire value written to a device capability for each mode.
PILOT_WIRE_VALUE = {
    HeatingMode.COMFORT: 2,
    HeatingMode.COMFORT_MINUS_1: 9,
    HeatingMode.COMFORT_MINUS_2: 10,
    HeatingMode.ECO: 3,
    HeatingMode.FROST_PROTECTION: 8,
    HeatingMode.OFF: 0,
}

# Device capability holding the measured temperature, in tenths of a kelvin.
TEMPERATURE_CAPABILITY_ID = 4
# Capabilities of the room sensors holding the relative humidity and the battery level, in %.
HUMIDITY_CAPABILITY_ID = 15
BATTERY_CAPABILITY_ID = 20
_KELVIN_X10_AT_0C = 2731


def kelvin_x10_to_celsius(value: float) -> float:
    return round((value - _KELVIN_X10_AT_0C) / 10, 1)


class VavError(Exception):
    def __init__(self, status: int, method: str, path: str, body: Any):
        super().__init__(f"{method} {path} -> HTTP {status}: {body}")
        self.status = status
        self.method = method
        self.path = path
        self.body = body


class NotAuthenticatedError(RuntimeError):
    pass


@dataclass
class Session:
    access_token: str
    refresh_token: str | None = None
    user: dict[str, Any] | None = None
    expires_at: float | None = None
    """Access-token expiry as a Unix timestamp, from the server's `expires_in`."""

    def expires_within(self, seconds: float) -> bool:
        return self.expires_at is not None and time.time() >= self.expires_at - seconds


def _expires_at(data: dict[str, Any]) -> float | None:
    expires_in = data.get("expires_in")
    return time.time() + expires_in if isinstance(expires_in, (int, float)) else None


class VavClient:
    def __init__(
        self,
        base_url: str = BASE_URL,
        *,
        language: str = "fr",
        timezone_id: str = "Europe/Paris",
        timeout: float = 30.0,
        session: Session | None = None,
        http: requests.Session | None = None,
        refresh_margin: float = 300.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.language = language
        self.timezone_id = timezone_id
        self.timeout = timeout
        self.session = session
        self.http = http or requests.Session()
        self.refresh_margin = refresh_margin

    # --- transport -------------------------------------------------------

    def _headers(self, token: str | None) -> dict[str, str]:
        headers = {
            "Content-type": "application/json",
            "userAgent": f"otodo-vav-cli/{APP_VERSION}",
            "language": self.language,
            "mobileVersion": APP_VERSION,
            "timezoneid": self.timezone_id,
        }
        if token:
            headers["token"] = token
        return headers

    def _send(
        self,
        method: str,
        path: str,
        *,
        token: str | None,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
    ) -> requests.Response:
        return self.http.request(
            method,
            f"{self.base_url}{path}",
            headers=self._headers(token),
            json=json_body,
            params={k: v for k, v in (params or {}).items() if v is not None} or None,
            timeout=self.timeout,
        )

    @staticmethod
    def _decode(resp: requests.Response) -> Any:
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    def request(
        self, method: str, path: str, *, json_body: Any = None, params: dict[str, Any] | None = None
    ) -> Any:
        """Authenticated request.

        The token is refreshed ahead of its expiry, and once more on HTTP 401.
        """
        if self.session is None:
            raise NotAuthenticatedError("call login() first")
        if self.session.refresh_token and self.session.expires_within(self.refresh_margin):
            try:
                self.refresh()
            except VavError:
                pass  # the current token may still be valid; a 401 below retries the refresh
        resp = self._send(
            method, path, token=self.session.access_token, json_body=json_body, params=params
        )
        if resp.status_code == 401 and self.session.refresh_token:
            self.refresh()
            resp = self._send(
                method, path, token=self.session.access_token, json_body=json_body, params=params
            )
        if not resp.ok:
            raise VavError(resp.status_code, method, path, self._decode(resp))
        return self._decode(resp)

    # --- authentication --------------------------------------------------

    def login(self, email: str, password: str) -> dict[str, Any]:
        resp = self._send(
            "POST",
            "/authenticate",
            token=None,
            json_body={"email": email, "password": password, "parkName": PARK_NAME},
        )
        user = self._decode(resp)
        if not resp.ok or not isinstance(user, dict) or "access_token" not in user:
            raise VavError(resp.status_code, "POST", "/authenticate", user)
        self.session = Session(
            user["access_token"], user.get("refresh_token"), user, _expires_at(user)
        )
        return user

    def refresh(self) -> dict[str, Any]:
        if self.session is None or not self.session.refresh_token:
            raise NotAuthenticatedError("no refresh token available")
        resp = self._send("POST", "/refreshToken", token=self.session.refresh_token)
        data = self._decode(resp)
        if not resp.ok or not isinstance(data, dict) or "access_token" not in data:
            raise VavError(resp.status_code, "POST", "/refreshToken", data)
        self.session.access_token = data["access_token"]
        self.session.refresh_token = data.get("refresh_token", self.session.refresh_token)
        self.session.expires_at = _expires_at(data)
        return data

    def recover_password(self, email: str) -> Any:
        resp = self._send(
            "POST",
            "/recover/initiate",
            token=None,
            json_body={"email": email, "parkName": PARK_NAME},
        )
        if not resp.ok:
            raise VavError(resp.status_code, "POST", "/recover/initiate", self._decode(resp))
        return self._decode(resp)

    def configurations(self) -> Any:
        return self.request("GET", "/configurations")

    # --- devices ---------------------------------------------------------

    def list_devices(self, brand: str | None = None) -> Any:
        return self.request("GET", "/devices", params={"brand": brand})

    def get_device(self, device_id: int) -> Any:
        return self.request("GET", f"/devices/{device_id}")

    def patch_device(self, device_id: int, changes: dict[str, Any]) -> Any:
        return self.request("PATCH", f"/devices/{device_id}", json_body=changes)

    def set_endpoint_name(self, device_id: int, endpoint_index: int, name: str) -> Any:
        return self.request(
            "PATCH", f"/devices/{device_id}/endpoints/{endpoint_index}", json_body={"name": name}
        )

    def send_action(
        self,
        device_id: int,
        endpoint_index: int,
        capability_id: int,
        value: Any,
        parameters: Any = None,
    ) -> Any:
        """Write a device capability value (setpoint, pilot wire, on/off...)."""
        body = {
            "endpoints": [
                {
                    "index": endpoint_index,
                    "capabilities": [
                        {"_id": capability_id, "value": value, "parameters": parameters}
                    ],
                }
            ]
        }
        return self.request("POST", f"/devices/{device_id}/actions", json_body=body)

    def set_pilot_wire_mode(
        self, device_id: int, endpoint_index: int, capability_id: int, mode: HeatingMode
    ) -> Any:
        if mode not in PILOT_WIRE_VALUE:
            raise ValueError(f"{mode.name} has no direct pilot-wire value")
        return self.send_action(device_id, endpoint_index, capability_id, PILOT_WIRE_VALUE[mode])

    def capability_history(
        self,
        device_id: int,
        start: str,
        end: str,
        aggregation_minutes: int | None = None,
        aggregable_capabilities: list[int] | None = None,
    ) -> Any:
        params = {
            "startDate": start,
            "endDate": end,
            "aggregationMinutes": aggregation_minutes,
            "aggregableCapas": ",".join(map(str, aggregable_capabilities))
            if aggregable_capabilities
            else None,
        }
        return self.request("GET", f"/devices/{device_id}/capabilities/values", params=params)

    def add_device_favorite(self, device_id: int) -> Any:
        return self.request("POST", f"/devices/{device_id}/add-favorite")

    def remove_device_favorite(self, device_id: int) -> Any:
        return self.request("POST", f"/devices/{device_id}/remove-favorite")

    def sensor_readings(self) -> dict[int, dict[str, float]]:
        """Readings per device id: "temperature" (°C), "humidity" and "battery" (%).

        Only the readings a device reports are included; devices with none are left out.
        """
        names = {
            TEMPERATURE_CAPABILITY_ID: "temperature",
            HUMIDITY_CAPABILITY_ID: "humidity",
            BATTERY_CAPABILITY_ID: "battery",
        }
        readings: dict[int, dict[str, float]] = {}
        for device in self.list_devices() or []:
            found: dict[str, float] = {}
            for endpoint in device.get("endpoints") or []:
                for capability in endpoint.get("capabilities") or []:
                    name, value = names.get(capability.get("_id")), capability.get("value")
                    if name and name not in found and isinstance(value, (int, float)):
                        if name == "temperature":
                            value = kelvin_x10_to_celsius(value)
                        found[name] = value
            if found:
                readings[device["_id"]] = found
        return readings

    def measured_temperatures(self) -> dict[int, float]:
        """Measured temperature in °C per device id, for devices that report one."""
        return {
            device_id: values["temperature"]
            for device_id, values in self.sensor_readings().items()
            if "temperature" in values
        }

    # --- hubs ------------------------------------------------------------

    def list_hubs(self) -> Any:
        return self.request("GET", "/hubs")

    def patch_hub(self, hub_id: str, changes: dict[str, Any]) -> Any:
        return self.request("PATCH", f"/hubs/{hub_id}", json_body=changes)

    def identify_hub(self, mac: str) -> Any:
        return self.request("GET", "/hubs/identify", params={"mac": mac})

    # --- heating services (mode, comfort setpoint, schedule) -------------

    def list_heating_services(self, hub_id: str) -> Any:
        return self.request("GET", f"/hubs/{hub_id}/local-services")

    def list_thermostats(self, hub_id: str) -> list[dict[str, Any]]:
        return [
            s for s in self.list_heating_services(hub_id) or [] if s.get("type") == "thermostat"
        ]

    def get_heating_service(self, hub_id: str, service_id: int) -> dict[str, Any]:
        for service in self.list_heating_services(hub_id) or []:
            if service.get("_id") == service_id:
                return service
        raise KeyError(f"service {service_id} not found on hub {hub_id}")

    def update_heating_service(self, hub_id: str, service: dict[str, Any]) -> Any:
        """Replace a whole heating service (PUT), as the app does after an edit."""
        body = copy.deepcopy(service)
        body["updatedAt"] = (
            datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )
        return self.request(
            "PUT", f"/hubs/{hub_id}/local-services/{service['_id']}", json_body=body
        )

    def set_heating_mode(self, hub_id: str, service_id: int, mode: HeatingMode) -> Any:
        service = self.get_heating_service(hub_id, service_id)
        service["workingMode"] = int(mode)
        return self.update_heating_service(hub_id, service)

    def set_comfort_temperature(self, hub_id: str, service_id: int, temperature: float) -> Any:
        service = self.get_heating_service(hub_id, service_id)
        service["comfortTemp"] = temperature
        return self.update_heating_service(hub_id, service)

    def get_schedule(self, hub_id: str, service_id: int) -> list[dict[str, Any]]:
        """The `weeklyEvents` a service follows in Auto mode (see `otodo_vav.schedule`)."""
        return self.get_heating_service(hub_id, service_id).get("weeklyEvents") or []

    def set_schedule(self, hub_id: str, service_id: int, events: list[dict[str, Any]]) -> Any:
        service = self.get_heating_service(hub_id, service_id)
        service["weeklyEvents"] = sort_events(events)
        return self.update_heating_service(hub_id, service)

    def list_services(self) -> Any:
        return self.request("GET", "/services")

    def service_action(self, service_id: int | str, body: dict[str, Any]) -> Any:
        return self.request("POST", f"/services/{service_id}/actions", json_body=body)

    # --- homes, rooms, settings ------------------------------------------

    def list_homes(self) -> Any:
        return self.request("GET", "/homes")

    def patch_home(self, home_id: str, changes: dict[str, Any]) -> Any:
        return self.request("PATCH", f"/homes/{home_id}", json_body=changes)

    def list_rooms(self) -> Any:
        return self.request("GET", "/rooms")

    def update_room(self, room_id: str, changes: dict[str, Any]) -> Any:
        return self.request("PATCH", f"/rooms/{room_id}", json_body=changes)

    def get_settings(self) -> Any:
        return self.request("GET", "/settings")

    def update_settings(self, settings: dict[str, Any]) -> Any:
        return self.request("PUT", "/settings", json_body=settings)

    def update_energy_profile(self, profile: dict[str, Any]) -> Any:
        return self.request("PUT", "/homes/energy-profile", json_body=profile)

    # --- energy coaching -------------------------------------------------

    def feeling_cold(self, room_ids: list[str]) -> Any:
        return self.request("POST", "/homes/coaching/feeling/cold", json_body={"roomIds": room_ids})

    def feeling_warm(self, room_ids: list[str]) -> Any:
        return self.request("POST", "/homes/coaching/feeling/warm", json_body={"roomIds": room_ids})

    def start_coaching(self) -> Any:
        return self.request("POST", "/homes/coaching/start-learning")

    def suspend_coaching(self, until: str | None = None) -> Any:
        body = {"unsuspendExpectedDate": until} if until else None
        return self.request("POST", "/homes/coaching/suspend", json_body=body)

    # --- consumption, weather, alerts ------------------------------------

    def energy_consumption(
        self, duration: str, year: int, month: int | None = None, day: int | None = None
    ) -> Any:
        """duration: 'hourly', 'daily', 'monthly' or 'currentMonth'."""
        return self.request(
            "GET",
            "/consumptions/energy",
            params={"duration": duration, "year": year, "month": month, "day": day},
        )

    def device_energy_consumption(
        self,
        device_id: int,
        duration: str,
        year: int,
        month: int | None = None,
        day: int | None = None,
    ) -> Any:
        return self.request(
            "GET",
            "/consumptions/energy",
            params={
                "deviceId": device_id,
                "duration": duration,
                "year": year,
                "month": month,
                "day": day,
            },
        )

    def heating_cumulative(self) -> Any:
        return self.request("GET", "/consumptions/energy/cumulative/heating")

    def energy_comparison(self, home_id: str) -> Any:
        return self.request("GET", f"/consumptions/energy/comparison/{home_id}")

    def energy_saved(self, year: int, month: int | None = None) -> Any:
        return self.request("GET", "/consumptions/saved", params={"year": year, "month": month})

    def energy_signals(self) -> Any:
        return self.request("GET", "/consumptions/signals")

    def weather(
        self, kind: str, duration: str, year: int, month: int | None = None, day: int | None = None
    ) -> Any:
        """kind: 'temperature' or 'humidity'."""
        return self.request(
            "GET",
            f"/weather/{kind}",
            params={"duration": duration, "year": year, "month": month, "day": day},
        )

    def alerts(self, start: str | None = None, end: str | None = None) -> Any:
        return self.request("GET", "/alerts/events", params={"start": start, "end": end})
