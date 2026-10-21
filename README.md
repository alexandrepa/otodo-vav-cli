# vav-connect-cli

Unofficial Python client and CLI for the Otodo cloud API behind the **VAV** heating app
(`com.otodo.vav`). Use it to read and control your own radiators from scripts or home
automation.

> **Disclaimer.** This project is not affiliated with or endorsed by Otodo or VAV. The API
> is undocumented; endpoints and payloads were inferred from the public Android app
> (version 4.14.4) for interoperability with the user's own devices. It may break at any
> time. Use it only with your own account and devices.

## Status

The request formats are covered by tests against a local fake server and agree with the
independent [homebridge-otodo-vav](https://github.com/Surfysamy/homebridge-otodo-vav) plugin
(login, hubs, rooms, devices, thermostat services and mode/setpoint updates). This client has
**not yet been run against the real Otodo service**, and `POST /refreshToken` is used by the
app but not confirmed by any other client. Start with read-only commands (`hubs`, `devices`,
`thermostats`, `temps`) and report what you find.

## Install

```bash
pip install git+https://github.com/alexandrepa/vav-connect-cli
```

## Library

```python
from vav_connect import HeatingMode, VavClient

client = VavClient()
client.login("you@example.com", "password")

hubs = client.list_hubs()
thermostats = client.list_thermostats(hub_id)  # service ids are integers

client.set_heating_mode(hub_id, 12, HeatingMode.ECO)
client.set_comfort_temperature(hub_id, 12, 20.5)

client.measured_temperatures()  # {device_id: °C}

# Weekly schedule followed in Auto mode
from vav_connect import schedule

events = client.get_schedule(hub_id, 12)
events = schedule.replace_days(
    events, schedule.parse_days("sat,sun"), schedule.parse_day_program("08:00=comfort,23:00=eco")
)
client.set_schedule(hub_id, 12, events)

# Direct capability write on a device
client.send_action(device_id=7, endpoint_index=0, capability_id=4, value=19.5)
```

`VavClient.request(method, path, json_body=..., params=...)` sends any authenticated request.
The access token is refreshed shortly before the expiry announced by the server (`expires_in`,
margin set by `refresh_margin`, 5 minutes by default) and once more if a request gets HTTP 401.

## CLI

```bash
vav-connect login you@example.com          # prompts for the password, stores ~/.vav_token.json (0600)
vav-connect hubs
vav-connect devices
vav-connect temps                          # measured temperature per device
vav-connect services <hubId>
vav-connect thermostats <hubId>            # services of type "thermostat" only
vav-connect set-mode <hubId> <serviceId> eco
vav-connect set-temp <hubId> <serviceId> 20.5
vav-connect schedule <hubId> <serviceId>  # weekly schedule followed in Auto mode
vav-connect set-schedule <hubId> <serviceId> --day mon-fri 06:00=comfort,09:00=eco,18:00=comfort,23:00=eco
vav-connect set-schedule <hubId> <serviceId> --day sat,sun 08:00=comfort,23:00=eco --dry-run
vav-connect copy-schedule <hubId> <sourceServiceId> <targetServiceId>...
vav-connect action <deviceId> <endpointIndex> <capabilityId> 19.5
vav-connect raw GET /rooms
```

`set-schedule` starts from the current schedule (or from `--file`, a JSON list such as the
output of `schedule --json`) and replaces each day given with `--day`; other days are kept.
`--dry-run` prints the result without sending it. The schedule only applies in `auto` mode
(`set-mode <hubId> <serviceId> auto`).

Instead of `vav-connect login`, you can set `VAV_EMAIL` and `VAV_PASSWORD`. `VAV_TOKEN_FILE` changes
where the session token is stored.

## How the API works

| Item | Value |
| --- | --- |
| Base URL | `https://api.gateway.otodo.io` |
| Login | `POST /authenticate` with `{"email", "password", "parkName": "vav"}` |
| Login response | `access_token`, `refresh_token`, `token_type`, `expires_in` (seconds), `userId`, `homeId`, `parkId`, `isAdmin`, `isSuspended`, `language` |
| Refresh | `POST /refreshToken` with the refresh token in the `token` header |
| Auth header | `token: <access_token>` (not `Authorization: Bearer`) |
| Extra headers | `language`, `mobileVersion`, `timezoneid`, `userAgent` |

### Heating control

Heating modes and the comfort setpoint live on **heating services** attached to a hub:

- `GET /hubs/{hubId}/local-services` lists them.
- `PUT /hubs/{hubId}/local-services/{serviceId}` replaces one; the client re-sends the full
  service with `workingMode` or `comfortTemp` changed and a fresh `updatedAt`.

Service, device and capability ids are **integers**; hub ids are strings. A thermostat service
looks like this (abridged, illustrative values):

```json
{
  "_id": 12,
  "hubId": "…",
  "roomId": "…",
  "type": "thermostat",
  "isActive": true,
  "workingMode": 0,
  "comfortTemp": 19,
  "sensors": {"temp": 18.4},
  "modules": [{"deviceId": 7, "endpointIndex": 0, "capabilityId": 1}],
  "weeklyEvents": [{"d": 1, "t": 1860, "mm": 3}],
  "hysteresis": 0.5,
  "limitTemp": 30,
  "updatedAt": "…"
}
```

`modules` links the service to the radiators it drives. `weeklyEvents` is the schedule used in
Auto mode: each event switches the service to mode `mm` at minute `t` of the week, on day `d`
(`t = d × 1440 + minute of the day`). Day 0 is **Monday** and `mm` is the pilot-wire value,
2 = comfort and 3 = eco; both were checked by editing a schedule in the app (homebridge-otodo-vav
documents Sunday and the opposite modes). The app's default schedule is comfort 06:00–09:00 and
18:00–23:00, eco otherwise.

| Mode | `workingMode` | Pilot-wire device value |
| --- | --- | --- |
| Auto | 0 | — |
| Comfort | 1 | 2 |
| Comfort −1 | 2 | 9 |
| Comfort −2 | 3 | 10 |
| Eco | 4 | 3 |
| Frost protection | 5 | 8 |
| Off | 6 | 0 |
| Holidays | 7 | — |

Device capabilities can also be written directly with
`POST /devices/{deviceId}/actions`:

```json
{"endpoints": [{"index": 0, "capabilities": [{"_id": 4, "value": 19.5, "parameters": null}]}]}
```

`GET /devices` returns each device with its endpoints and capability values. Capability `4`
holds the measured temperature in tenths of a kelvin: °C = (value − 2731) / 10.

### Other endpoints wrapped

Devices, hubs, homes, rooms, settings, energy coaching (`/homes/coaching/...`), energy
consumption (`/consumptions/...`), weather history (`/weather/...`) and alerts. See
`src/vav_connect/client.py`.

### Not supported

- Local mode (direct HTTP calls to the hub on port 1045).
- Real-time updates over the `wss://api.gateway.otodo.io/` WebSocket.
- Pairing and hub installation flows.

## Development

```bash
pip install -e '.[dev]'
pytest
ruff check .
```

## License

MIT
