# vav-connect-cli

Unofficial Python client and CLI for the Otodo cloud API behind the **VAV** heating app
(`com.otodo.vav`). Use it to read and control your own radiators from scripts or home
automation.

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
services = client.list_heating_services(hub_id)

client.set_heating_mode(hub_id, service_id, HeatingMode.ECO)
client.set_comfort_temperature(hub_id, service_id, 20.5)

# Direct capability write on a device
client.send_action(device_id, endpoint_index=0, capability_id="...", value=19.5)
```

`VavClient.request(method, path, json_body=..., params=...)` sends any authenticated request.
An expired access token is refreshed automatically once.

## CLI

```bash
vav-connect login you@example.com          # prompts for the password, stores ~/.vav_token.json (0600)
vav-connect hubs
vav-connect devices
vav-connect services <hubId>
vav-connect set-mode <hubId> <serviceId> eco
vav-connect set-temp <hubId> <serviceId> 20.5
vav-connect action <deviceId> <endpointIndex> <capabilityId> 19.5
vav-connect raw GET /rooms
```

Instead of `vav-connect login`, you can set `VAV_EMAIL` and `VAV_PASSWORD`. `VAV_TOKEN_FILE` changes
where the session token is stored.

## How the API works

| Item | Value |
| --- | --- |
| Base URL | `https://api.gateway.otodo.io` |
| Login | `POST /authenticate` with `{"email", "password", "parkName": "vav"}` |
| Refresh | `POST /refreshToken` with the refresh token in the `token` header |
| Auth header | `token: <access_token>` (not `Authorization: Bearer`) |
| Extra headers | `language`, `mobileVersion`, `timezoneid`, `userAgent` |

### Heating control

Heating modes and the comfort setpoint live on **heating services** attached to a hub:

- `GET /hubs/{hubId}/local-services` lists them.
- `PUT /hubs/{hubId}/local-services/{serviceId}` replaces one; the client re-sends the full
  service with `workingMode` or `comfortTemp` changed and a fresh `updatedAt`.

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
{"endpoints": [{"index": 0, "capabilities": [{"_id": "<capabilityId>", "value": 19.5, "parameters": null}]}]}
```

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
