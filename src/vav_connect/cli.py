from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path
from typing import Any

from .client import HeatingMode, Session, VavClient


def _token_file() -> Path:
    return Path(os.environ.get("VAV_TOKEN_FILE", Path.home() / ".vav_token.json"))


def _save_session(session: Session) -> None:
    path = _token_file()
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(
        json.dumps(
            {
                "access_token": session.access_token,
                "refresh_token": session.refresh_token,
                "expires_at": session.expires_at,
            }
        )
    )


def _client_from_env() -> VavClient:
    client = VavClient()
    path = _token_file()
    if path.exists():
        data = json.loads(path.read_text())
        client.session = Session(
            data["access_token"], data.get("refresh_token"), expires_at=data.get("expires_at")
        )
        return client
    email, password = os.environ.get("VAV_EMAIL"), os.environ.get("VAV_PASSWORD")
    if not email or not password:
        sys.exit("Run `vav-connect login EMAIL` or set VAV_EMAIL and VAV_PASSWORD")
    client.login(email, password)
    _save_session(client.session)
    return client


def _parse_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="vav-connect", description="Unofficial VAV / Otodo API client"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("login", help="Authenticate and store the session token")
    p.add_argument("email")

    sub.add_parser("devices", help="List devices")
    sub.add_parser("hubs", help="List hubs")
    sub.add_parser("rooms", help="List rooms")
    sub.add_parser("homes", help="List homes")

    sub.add_parser("temps", help="Measured temperature (°C) per device")

    p = sub.add_parser("device", help="Show one device")
    p.add_argument("device_id", type=int)

    p = sub.add_parser("services", help="List the heating services of a hub")
    p.add_argument("hub_id")

    p = sub.add_parser("thermostats", help="List the thermostat services of a hub")
    p.add_argument("hub_id")

    p = sub.add_parser("set-mode", help="Change a heating service mode")
    p.add_argument("hub_id")
    p.add_argument("service_id", type=int)
    p.add_argument("mode", choices=[m.name.lower() for m in HeatingMode])

    p = sub.add_parser("set-temp", help="Change a heating service comfort setpoint")
    p.add_argument("hub_id")
    p.add_argument("service_id", type=int)
    p.add_argument("temperature", type=float)

    p = sub.add_parser("action", help="Write a device capability value")
    p.add_argument("device_id", type=int)
    p.add_argument("endpoint_index", type=int)
    p.add_argument("capability_id", type=int)
    p.add_argument("value", help='JSON value, e.g. 19.5, true, "on"')

    p = sub.add_parser("raw", help="Send an arbitrary authenticated request")
    p.add_argument("method")
    p.add_argument("path")
    p.add_argument("--body", type=json.loads)

    args = parser.parse_args(argv)

    if args.cmd == "login":
        client = VavClient()
        password = os.environ.get("VAV_PASSWORD") or getpass.getpass("Password: ")
        client.login(args.email, password)
        _save_session(client.session)
        print(f"Session saved to {_token_file()}")
        return

    client = _client_from_env()
    commands = {
        "devices": lambda: client.list_devices(),
        "hubs": lambda: client.list_hubs(),
        "rooms": lambda: client.list_rooms(),
        "homes": lambda: client.list_homes(),
        "temps": lambda: client.measured_temperatures(),
        "device": lambda: client.get_device(args.device_id),
        "services": lambda: client.list_heating_services(args.hub_id),
        "thermostats": lambda: client.list_thermostats(args.hub_id),
        "set-mode": lambda: client.set_heating_mode(
            args.hub_id, args.service_id, HeatingMode[args.mode.upper()]
        ),
        "set-temp": lambda: client.set_comfort_temperature(
            args.hub_id, args.service_id, args.temperature
        ),
        "action": lambda: client.send_action(
            args.device_id, args.endpoint_index, args.capability_id, _parse_value(args.value)
        ),
        "raw": lambda: client.request(args.method.upper(), args.path, json_body=args.body),
    }
    try:
        result = commands[args.cmd]()
    finally:
        if client.session:
            _save_session(client.session)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
