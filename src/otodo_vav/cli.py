from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests

from . import presence, schedule
from .client import HeatingMode, Session, VavClient, VavError


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
        sys.exit("Run `otodo-vav login EMAIL` or set VAV_EMAIL and VAV_PASSWORD")
    client.login(email, password)
    _save_session(client.session)
    return client


def _parse_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _print_schedule(events: list[dict[str, Any]]) -> None:
    for day, entries in schedule.describe(events).items():
        print(f"{day}  {'  '.join(entries) or '-'}")


def _new_schedule(client: VavClient, args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.file:
        events = json.loads(Path(args.file).read_text())
    else:
        events = client.get_schedule(args.hub_id, args.service_id)
    for days, program in args.day or []:
        events = schedule.replace_days(events, days, program)
    return schedule.sort_events(events)


def _bbox_from_args(args: argparse.Namespace) -> presence.BboxClient:
    return presence.BboxClient(args.bbox_url, os.environ.get("BBOX_PASSWORD"))


def _hub_ids(client: VavClient, args: argparse.Namespace) -> list[str]:
    if args.hub:
        return args.hub
    return [hub.get("_id") or hub["id"] for hub in client.list_hubs() or []]


def _presence_watch(client: VavClient, args: argparse.Namespace) -> None:
    notify = presence.ntfy_notifier(args.ntfy_topic, args.ntfy_server) if args.ntfy_topic else print
    watcher = presence.PresenceWatcher(
        client,
        _bbox_from_args(args),
        args.phone,
        _hub_ids(client, args),
        away_after=args.away_after * 60,
        notify=notify,
        set_eco=args.set_eco,
    )
    print(f"Watching {len(watcher.phones)} phone(s) on hubs {', '.join(watcher.hub_ids)}")
    while True:
        try:
            watcher.tick()
        except (presence.BboxError, VavError, requests.RequestException) as exc:
            print(f"warning: {exc}", file=sys.stderr, flush=True)
        finally:
            if client.session:
                _save_session(client.session)
        time.sleep(args.interval)


class _DayAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        try:
            days, program = values
            item = schedule.parse_days(days), schedule.parse_day_program(program)
        except ValueError as exc:
            parser.error(str(exc))
        getattr(namespace, self.dest).append(item)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="otodo-vav", description="Unofficial VAV / Otodo API client"
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

    p = sub.add_parser("schedule", help="Show the weekly schedule a service follows in Auto mode")
    p.add_argument("hub_id")
    p.add_argument("service_id", type=int)
    p.add_argument("--json", action="store_true", help="Print the raw weeklyEvents")

    p = sub.add_parser("set-schedule", help="Change the weekly schedule of a service")
    p.add_argument("hub_id")
    p.add_argument("service_id", type=int)
    p.add_argument("--file", help="JSON weeklyEvents to start from, e.g. from `schedule --json`")
    p.add_argument(
        "--day",
        nargs=2,
        action=_DayAction,
        default=[],
        metavar=("DAYS", "PROGRAM"),
        help="Replace DAYS (mon, mon-fri, sat,sun, all) with PROGRAM "
        "(06:00=comfort,09:00=eco); repeatable",
    )
    p.add_argument("--dry-run", action="store_true", help="Print the new schedule, change nothing")

    p = sub.add_parser("copy-schedule", help="Copy a service's weekly schedule to other services")
    p.add_argument("hub_id")
    p.add_argument("source_id", type=int)
    p.add_argument("target_ids", type=int, nargs="+")
    p.add_argument("--dry-run", action="store_true", help="Print the schedule, change nothing")

    p = sub.add_parser("action", help="Write a device capability value")
    p.add_argument("device_id", type=int)
    p.add_argument("endpoint_index", type=int)
    p.add_argument("capability_id", type=int)
    p.add_argument("value", help='JSON value, e.g. 19.5, true, "on"')

    p = sub.add_parser(
        "bbox-hosts", help="List the devices known to the Bbox (to find a phone MAC)"
    )
    p.add_argument("--bbox-url", default=presence.BBOX_URL)

    p = sub.add_parser(
        "presence-watch", help="Alert when no phone is on the Wi-Fi while radiators heat"
    )
    p.add_argument(
        "--phone", action="append", required=True, metavar="MAC", help="Phone MAC; repeatable"
    )
    p.add_argument("--hub", action="append", help="Hub to watch; repeatable (default: all hubs)")
    p.add_argument("--bbox-url", default=presence.BBOX_URL)
    p.add_argument(
        "--away-after",
        type=float,
        default=30,
        metavar="MINUTES",
        help="Minutes without any phone before nobody is home (default 30)",
    )
    p.add_argument(
        "--interval", type=float, default=60, metavar="SECONDS", help="Poll period (default 60)"
    )
    p.add_argument("--ntfy-topic", help="Send alerts to this ntfy topic (default: print only)")
    p.add_argument("--ntfy-server", default="https://ntfy.sh")
    p.add_argument(
        "--set-eco",
        action="store_true",
        help="Also switch heating radiators to Eco, and restore their mode on return",
    )

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

    if args.cmd == "bbox-hosts":
        for host in _bbox_from_args(args).hosts():
            state = "on " if presence.is_active(host.get("active")) else "off"
            print(
                f"{state} {host.get('macaddress', '-'):17}  {host.get('ipaddress') or '-':15}  "
                f"{host.get('link') or '-':10}  {host.get('hostname') or '-'}"
            )
        return

    if args.cmd == "set-schedule" and not args.file and not args.day:
        parser.error("set-schedule needs --file or --day")

    client = _client_from_env()
    try:
        if args.cmd == "schedule":
            events = client.get_schedule(args.hub_id, args.service_id)
            if args.json:
                print(json.dumps(events, indent=2))
            else:
                _print_schedule(events)
            return
        if args.cmd == "set-schedule":
            events = _new_schedule(client, args)
            _print_schedule(events)
            if not args.dry_run:
                client.set_schedule(args.hub_id, args.service_id, events)
            return
        if args.cmd == "copy-schedule":
            events = client.get_schedule(args.hub_id, args.source_id)
            _print_schedule(events)
            if not args.dry_run:
                for target_id in args.target_ids:
                    client.set_schedule(args.hub_id, target_id, events)
            return
        if args.cmd == "presence-watch":
            _presence_watch(client, args)
            return
    finally:
        if client.session:
            _save_session(client.session)

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
