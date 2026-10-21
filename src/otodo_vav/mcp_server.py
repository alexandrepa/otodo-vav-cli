"""MCP server exposing VAV radiators as tools, for Claude and other MCP clients.

Run `otodo-vav-mcp` (stdio) or `otodo-vav-mcp --http` (streamable HTTP). Credentials come
from `VAV_EMAIL` / `VAV_PASSWORD`, or from the token file written by `otodo-vav login`.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import unicodedata
from dataclasses import dataclass
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import schedule
from .client import HeatingMode, VavClient, VavError

MIN_COMFORT_TEMP = 7.0
MAX_COMFORT_TEMP = 28.0

MODES = [m.name.lower() for m in HeatingMode]

INSTRUCTIONS = f"""\
Controls the VAV pilot-wire radiators of a home.

Call list_radiators first: radiators are referred to by room name or by id, and "all"
targets every radiator. Modes: {", ".join(MODES)}. In "auto" a radiator follows its weekly
schedule (comfort/eco periods); the other modes are fixed until changed. The comfort
temperature is the setpoint used in comfort mode; eco and comfort_minus_* are offsets
handled by the radiator.
"""


@dataclass
class Radiator:
    hub_id: str
    service: dict[str, Any]
    name: str

    @property
    def id(self) -> int:
        return self.service["_id"]


def _normalize(text: str) -> str:
    stripped = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return " ".join(stripped.lower().split())


class RadiatorController:
    """Radiator-level operations on top of `VavClient`, shared by the MCP tools.

    Calls are serialized: the client and its token refresh are not thread-safe, and MCP
    tools run in worker threads.
    """

    def __init__(self, client: VavClient, email: str | None = None, password: str | None = None):
        self.client = client
        self.email = email
        self.password = password
        self._lock = threading.Lock()

    def _call(self, fn, *args):
        """Run `fn(*args)`, logging in again once if the session can no longer be refreshed."""
        if self.client.session is None:
            if not (self.email and self.password):
                raise ToolError(
                    "not logged in: set VAV_EMAIL and VAV_PASSWORD, or run `otodo-vav login`"
                )
            self.client.login(self.email, self.password)
        try:
            return fn(*args)
        except VavError as exc:
            if exc.status != 401 or not (self.email and self.password):
                raise
        self.client.login(self.email, self.password)
        return fn(*args)

    # --- lookups ---------------------------------------------------------

    def _radiators(self) -> list[Radiator]:
        rooms = {
            room.get("_id"): room.get("name")
            for room in self._call(self.client.list_rooms) or []
            if isinstance(room, dict)
        }
        radiators = []
        for hub in self._call(self.client.list_hubs) or []:
            hub_id = hub.get("_id") or hub.get("id")
            if not hub_id:
                continue
            for service in self._call(self.client.list_thermostats, hub_id):
                name = rooms.get(service.get("roomId")) or service.get("name")
                radiators.append(Radiator(hub_id, service, name or f"radiator {service['_id']}"))
        return radiators

    def _select(self, targets: list[str]) -> list[Radiator]:
        radiators = self._radiators()
        if not targets:
            raise ToolError("give at least one radiator name or id, or 'all'")
        if any(_normalize(t) == "all" for t in targets):
            return radiators
        selected: list[Radiator] = []
        for target in targets:
            wanted = _normalize(str(target))
            matches = [r for r in radiators if str(r.id) == wanted or _normalize(r.name) == wanted]
            if not matches:
                matches = [r for r in radiators if wanted and wanted in _normalize(r.name)]
            if not matches:
                known = ", ".join(f"{r.name} ({r.id})" for r in radiators) or "none"
                raise ToolError(f"no radiator matches {target!r}; known radiators: {known}")
            if len(matches) > 1:
                names = ", ".join(f"{r.name} ({r.id})" for r in matches)
                raise ToolError(f"{target!r} matches several radiators: {names}; use an id")
            if matches[0] not in selected:
                selected.append(matches[0])
        return selected

    def _summary(self, radiator: Radiator, measured: dict[int, float]) -> dict[str, Any]:
        service = radiator.service
        try:
            mode = HeatingMode(service.get("workingMode")).name.lower()
        except ValueError:
            mode = f"unknown ({service.get('workingMode')})"
        temperature = (service.get("sensors") or {}).get("temp")
        if not isinstance(temperature, (int, float)):
            temperature = next(
                (
                    measured[m["deviceId"]]
                    for m in service.get("modules") or []
                    if m.get("deviceId") in measured
                ),
                None,
            )
        return {
            "id": radiator.id,
            "name": radiator.name,
            "mode": mode,
            "comfort_temperature": service.get("comfortTemp"),
            "measured_temperature": temperature,
            "active": service.get("isActive", True),
        }

    # --- operations ------------------------------------------------------

    def list_radiators(self) -> list[dict[str, Any]]:
        with self._lock:
            radiators = self._radiators()
            needs_devices = any(
                not isinstance((r.service.get("sensors") or {}).get("temp"), (int, float))
                for r in radiators
            )
            measured = self._call(self.client.measured_temperatures) if needs_devices else {}
            return [self._summary(r, measured) for r in radiators]

    def set_mode(self, targets: list[str], mode: str) -> list[dict[str, Any]]:
        try:
            heating_mode = HeatingMode[mode.strip().upper().replace("-", "_")]
        except KeyError:
            raise ToolError(f"unknown mode {mode!r}; use one of {', '.join(MODES)}") from None
        with self._lock:
            done = []
            for radiator in self._select(targets):
                self._call(self.client.set_heating_mode, radiator.hub_id, radiator.id, heating_mode)
                done.append({"id": radiator.id, "name": radiator.name, "mode": mode})
            return done

    def set_comfort_temperature(
        self, targets: list[str], temperature: float
    ) -> list[dict[str, Any]]:
        if not MIN_COMFORT_TEMP <= temperature <= MAX_COMFORT_TEMP:
            raise ToolError(
                f"comfort temperature must be between {MIN_COMFORT_TEMP} and {MAX_COMFORT_TEMP} °C"
            )
        temperature = round(temperature * 2) / 2
        with self._lock:
            done = []
            for radiator in self._select(targets):
                self._call(
                    self.client.set_comfort_temperature, radiator.hub_id, radiator.id, temperature
                )
                done.append(
                    {"id": radiator.id, "name": radiator.name, "comfort_temperature": temperature}
                )
            return done

    def get_schedule(self, target: str) -> dict[str, Any]:
        with self._lock:
            (radiator,) = self._select([target])
            events = radiator.service.get("weeklyEvents") or []
            return {"id": radiator.id, "name": radiator.name, "schedule": schedule.describe(events)}

    def set_schedule(
        self, targets: list[str], days: str, program: str, dry_run: bool = False
    ) -> list[dict[str, Any]]:
        try:
            day_indexes = schedule.parse_days(days)
            day_program = schedule.parse_day_program(program)
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        with self._lock:
            done = []
            for radiator in self._select(targets):
                events = schedule.replace_days(
                    radiator.service.get("weeklyEvents") or [], day_indexes, day_program
                )
                if not dry_run:
                    self._call(self.client.set_schedule, radiator.hub_id, radiator.id, events)
                done.append(
                    {
                        "id": radiator.id,
                        "name": radiator.name,
                        "schedule": schedule.describe(events),
                        "applied": not dry_run,
                    }
                )
            return done


def build_server(controller: RadiatorController) -> MCPServer:
    server = MCPServer("otodo-vav", instructions=INSTRUCTIONS)

    @server.tool()
    def list_radiators() -> list[dict[str, Any]]:
        """List radiators with their room name, id, mode, comfort setpoint and measured °C."""
        return controller.list_radiators()

    @server.tool()
    def set_mode(radiators: list[str], mode: str) -> list[dict[str, Any]]:
        """Change the heating mode of radiators.

        radiators: room names or ids from list_radiators, or ["all"].
        mode: auto (follow the weekly schedule), comfort, comfort_minus_1, comfort_minus_2,
        eco, frost_protection, off or holidays.
        """
        return controller.set_mode(radiators, mode)

    @server.tool()
    def set_comfort_temperature(radiators: list[str], temperature: float) -> list[dict[str, Any]]:
        """Set the comfort setpoint (°C, 7-28, rounded to 0.5) of radiators.

        radiators: room names or ids from list_radiators, or ["all"].
        """
        return controller.set_comfort_temperature(radiators, temperature)

    @server.tool()
    def get_schedule(radiator: str) -> dict[str, Any]:
        """Weekly schedule a radiator follows in auto mode: switch times per day (mon-sun)."""
        return controller.get_schedule(radiator)

    @server.tool()
    def set_schedule(
        radiators: list[str], days: str, program: str, dry_run: bool = False
    ) -> list[dict[str, Any]]:
        """Replace the auto-mode schedule of some days; other days are kept.

        radiators: room names or ids from list_radiators, or ["all"].
        days: "mon", "mon-fri", "sat,sun" or "all".
        program: switch times for each of those days, e.g. "06:00=comfort,09:00=eco,
        18:00=comfort,23:00=eco". Modes are comfort or eco; a mode lasts until the next switch,
        across midnight.
        dry_run: return the resulting schedule without applying it.
        """
        return controller.set_schedule(radiators, days, program, dry_run)

    return server


def _controller_from_env() -> RadiatorController:
    from .cli import _client_from_env, _token_file

    email, password = os.environ.get("VAV_EMAIL"), os.environ.get("VAV_PASSWORD")
    if email and password:
        # Logged in on the first tool call, so the server starts even if the cloud is down
        client = VavClient()
    elif _token_file().exists():
        client = _client_from_env()
    else:
        client = VavClient()  # tools report how to configure credentials
    return RadiatorController(client, email, password)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="otodo-vav-mcp", description="VAV radiators MCP server")
    parser.add_argument(
        "--http", action="store_true", help="Serve streamable HTTP instead of stdio"
    )
    parser.add_argument("--host", default=os.environ.get("VAV_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("VAV_MCP_PORT", "8099")))
    parser.add_argument(
        "--secret",
        default=os.environ.get("VAV_MCP_SECRET"),
        help="Serve on /mcp/SECRET instead of /mcp (env VAV_MCP_SECRET)",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Allow serving on a non-loopback host without --secret",
    )
    args = parser.parse_args(argv)

    exposed = args.http and args.host not in ("127.0.0.1", "localhost", "::1")
    if exposed and not args.secret and not args.insecure:
        parser.error(f"--host {args.host} needs --secret (or --insecure)")
    if args.secret is not None and len(args.secret) < 24:
        parser.error("--secret must be at least 24 characters")

    server = build_server(_controller_from_env())
    if not args.http:
        server.run()
        return
    path = f"/mcp/{args.secret}" if args.secret else "/mcp"
    shown = "/mcp/<secret>" if args.secret else path
    print(f"Serving MCP on http://{args.host}:{args.port}{shown}", file=sys.stderr)
    # Stateless: no session to lose when the server restarts, and the tools need none
    server.run(
        "streamable-http",
        host=args.host,
        port=args.port,
        streamable_http_path=path,
        stateless_http=True,
    )


if __name__ == "__main__":
    main()
