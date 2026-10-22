"""Alert when nobody is home while radiators heat.

Presence comes from the phones connected to a Bouygues Bbox: its local API
(`GET /api/v1/hosts`, reachable at https://mabbox.bytel.fr from the home network)
lists every device the box knows with its MAC address and an `active` flag.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

import requests

from . import schedule
from .client import HeatingMode, VavClient

BBOX_URL = "https://mabbox.bytel.fr"

# Modes in which a radiator follows the comfort setpoint, so it may heat.
HEATING_MODES = {HeatingMode.COMFORT, HeatingMode.COMFORT_MINUS_1, HeatingMode.COMFORT_MINUS_2}


class BboxError(RuntimeError):
    pass


def normalize_mac(mac: str) -> str:
    return mac.strip().lower().replace("-", ":")


def is_active(value: Any) -> bool:
    # The box reports `active` as 1/0 or true/false depending on the firmware.
    return value in (True, 1, "1", "true")


class BboxClient:
    """Read the devices connected to a Bbox.

    `password` is the box admin password. It is only sent if the box answers
    HTTP 401, i.e. when reading the hosts needs a session (`BBOX_ID` cookie).
    """

    def __init__(
        self,
        base_url: str = BBOX_URL,
        password: str | None = None,
        *,
        timeout: float = 10.0,
        http: requests.Session | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.password = password
        self.timeout = timeout
        self.http = http or requests.Session()

    def login(self) -> None:
        resp = self.http.put(
            f"{self.base_url}/api/v1/login",
            data={"password": self.password, "remember": 1},
            timeout=self.timeout,
        )
        if resp.status_code == 429:
            raise BboxError("Bbox login rate-limited; wait 15 minutes before retrying")
        if not resp.ok:
            raise BboxError(f"Bbox login failed: HTTP {resp.status_code}")

    def hosts(self) -> list[dict[str, Any]]:
        url = f"{self.base_url}/api/v1/hosts"
        resp = self.http.get(url, timeout=self.timeout)
        if resp.status_code == 401 and self.password:
            self.login()
            resp = self.http.get(url, timeout=self.timeout)
        if not resp.ok:
            hint = " (set BBOX_PASSWORD)" if resp.status_code == 401 else ""
            raise BboxError(f"GET /api/v1/hosts -> HTTP {resp.status_code}{hint}")
        data = resp.json()
        # The box wraps every answer in a one-item list: [{"hosts": {"list": [...]}}]
        if isinstance(data, list):
            data = data[0] if data else {}
        return (data.get("hosts") or {}).get("list") or []

    def active_macs(self) -> set[str]:
        return {
            normalize_mac(h["macaddress"])
            for h in self.hosts()
            if h.get("macaddress") and is_active(h.get("active"))
        }


def minute_of_week(now: datetime) -> int:
    return now.weekday() * schedule.MINUTES_PER_DAY + now.hour * 60 + now.minute


def is_heating(service: dict[str, Any], now: datetime) -> bool:
    """Whether a thermostat service is set to comfort right now (directly or by its schedule)."""
    if service.get("isActive") is False:
        return False
    mode = service.get("workingMode")
    if mode in HEATING_MODES:
        return True
    if mode == HeatingMode.AUTO:
        scheduled = schedule.mode_at(service.get("weeklyEvents") or [], minute_of_week(now))
        return scheduled == schedule.SCHEDULE_MODES["comfort"]
    return False


class PresenceWatcher:
    """Track presence and decide when to alert.

    Nobody is home once none of `phones` has been seen for `away_after` seconds.
    While nobody is home, each heating service is reported once; reports start
    again after someone comes back. With `set_eco`, the reported services are
    switched to Eco and put back in their previous mode on return.
    """

    def __init__(
        self,
        client: VavClient,
        bbox: BboxClient,
        phones: Iterable[str],
        hub_ids: list[str],
        *,
        away_after: float = 1800,
        notify: Callable[[str], None] = print,
        set_eco: bool = False,
        clock: Callable[[], float] = time.time,
        now: Callable[[], datetime] = datetime.now,
    ):
        self.client = client
        self.bbox = bbox
        self.phones = {normalize_mac(p) for p in phones}
        self.hub_ids = hub_ids
        self.away_after = away_after
        self.notify = notify
        self.set_eco = set_eco
        self.clock = clock
        self.now = now
        self.last_seen = clock()  # assume someone is home at start
        self.reported: set[tuple[str, int]] = set()
        self.previous_modes: dict[tuple[str, int], int] = {}

    @property
    def away(self) -> bool:
        return self.clock() - self.last_seen >= self.away_after

    def tick(self) -> None:
        if self.phones & self.bbox.active_macs():
            was_away = self.away
            self.last_seen = self.clock()
            if was_away:
                self._welcome_back()
            return
        if not self.away:
            return
        now = self.now()
        for hub_id in self.hub_ids:
            for service in self.client.list_thermostats(hub_id):
                key = (hub_id, service["_id"])
                if key in self.reported or not is_heating(service, now):
                    continue
                # Marked first so a failed notification never re-saves Eco as the mode to restore
                self.reported.add(key)
                self._report(hub_id, service)

    def _report(self, hub_id: str, service: dict[str, Any]) -> None:
        name = service.get("name") or f"service {service['_id']}"
        temp = (service.get("sensors") or {}).get("temp")
        message = f"Personne à la maison et {name} chauffe"
        if temp is not None:
            message += f" ({temp} °C)"
        if self.set_eco:
            self.previous_modes[(hub_id, service["_id"])] = service["workingMode"]
            self.client.set_heating_mode(hub_id, service["_id"], HeatingMode.ECO)
            message += " : passé en Eco"
        self.notify(message)

    def _welcome_back(self) -> None:
        for (hub_id, service_id), mode in self.previous_modes.items():
            self.client.set_heating_mode(hub_id, service_id, HeatingMode(mode))
        if self.previous_modes:
            self.notify("Retour à la maison : modes de chauffage restaurés")
        self.previous_modes.clear()
        self.reported.clear()


def ntfy_notifier(topic: str, server: str = "https://ntfy.sh") -> Callable[[str], None]:
    """Push notifications through ntfy (install the ntfy app and subscribe to `topic`)."""

    def notify(message: str) -> None:
        print(message, flush=True)
        requests.post(
            f"{server.rstrip('/')}/{topic}",
            data=message.encode(),
            headers={"Title": "Chauffage", "Tags": "fire"},
            timeout=10,
        ).raise_for_status()

    return notify
