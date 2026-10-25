# ftms56

A standalone Python application for Linux that bridges a KICKR5's Direct Connect
or Bluetooth LE connection to Zwift over Direct Connect, with an experimental KICKR6-style virtual shifting profile.
It translates Zwift's gear ratios and simulation parameters into FTMS commands.

The application uses one `selectors` loop, with nonblocking sockets, bounded
buffers, timeouts, TCP keepalives and a read-only application heartbeat. It uses
no Python threads or asyncio in the TCP bridge. Optional BLE mode uses a separate
Bleak helper process with its own asynchronous event loop. The existing Linux Avahi daemon handles mDNS;
small `avahi-publish-service` child processes register the advertisements.

| Port | Purpose |
| --- | --- |
| 36865 | Status GUI, JSON status and health |
| 36866 | Experimental virtual shifting profile, advertised as `ftms56 KICKR6` |
| 36867 | Pass-through test profile, advertised as `ftms56 KICKR5` |

Only one profile may be used at a time. Up to four connections from that profile
can share the trainer. Select the bridge instead of the original trainer in
Zwift. Shifting controllers continue to connect to Zwift normally; this program
does not connect to Click/Play/Ride controllers over Bluetooth.

## Current state

The pass-through path has been checked against a real KICKR: service inventories
and telemetry matched direct access, and power reached Zwift. An interactive
KICKR6-profile ride confirmed Zwift recognition, virtual shifting with Zwift Play,
and matching virtual gear display. Comparative load/ride feel and ERG handling
remain to be verified on the bicycle. The first translation
method is equivalent gradient; direct resistance-level translation is not yet
implemented because its scale needs trainer-specific calibration.

See [protocol findings](docs/protocol.md) and [validation / next ride](docs/validation.md).

## Run from this checkout

Requires Python 3.11+ and, for discovery/advertising, Linux `avahi-daemon` and
`avahi-utils`. There are no third-party Python runtime dependencies.
The optional BLE upstream additionally requires BlueZ and the `ble` dependency extra.

```sh
python -m ftms56 discover
python -m ftms56 inspect --trainer TRAINER_IP --sample-seconds 3 --output trainer.json
```

Inspection reads services, characteristics and optional telemetry; it does not
write control commands or change resistance. Use the discovered IPv4 address or
hostname with the following command:

```sh
python -m ftms56 run --trainer TRAINER_IP \
  --physical-ratio 3.125 --trainer-mass-kg 85
```

**Set `--physical-ratio` to the actual chainring teeth divided by sprocket teeth.**
The example is 50/16. Leave the bicycle in that physical gear while virtual
shifting. Set `--trainer-mass-kg` to the combined rider+bike mass assumed by the
trainer's simulation model. Zwift's transmitted rider/bike mass is used for the
virtual road model; matching the trainer's own mass assumption needs ride testing.

Open `http://localhost:36865/` or the Linux host's LAN address on port 36865.
The GUI shows profile, control mode, grade, gear, cadence, power, resistance when
reported, freshness, transfer counters and recent events. A live difficulty
slider controls the KICKR6 simulation profile. `/api/status` returns JSON and `/healthz` returns 200 when the trainer
is ready, otherwise 503. Default listeners support IPv4 and IPv6 where available.

The GUI guesses Mixed Terrain, Flat, All-Rounder or Climbing from incoming ratios
and revises its guess as shifts distinguish overlapping tables. It labels the
range as a guess, inferred, or approximate, and also estimates Sequential versus
SRAM-style from shift patterns. SRAM-style displays front/rear positions such as
`1-12` and the corresponding tooth combination. The estimate resets on connection
loss or disabled shifting; further shifts are needed to infer style again.
These estimates affect display only: load always uses the exact received ratio.

For a display-name recognition experiment, `--shifting-name KICKR5_54BD` changes
the shifting profile's DNS-SD and proprietary device-info names. `--hide-kickr5`
hides the pass-through advertisement. The shifting service, capabilities and
stable bridge MAC/serial identities remain unchanged; the physical trainer's
own advertisement remains visible.
Add `--shifting-id ftms56-kickr5-name-test` to give that profile a fresh virtual
MAC and matching DNS-SD/proprietary serial, avoiding reuse of a cached pairing
identity when testing display-name recognition.

The two grade values are **Zwift simulation input** (the slope requested by
Zwift, which can already reflect Trainer Difficulty) and **KICKR5 equivalent
grade** (the last successfully accepted trainer load command after gearing and
physics translation). The intermediate **After GUI difficulty** value shows the
slope used by the gearing calculation. These are not measurements of the road
grade displayed by Zwift, and they need not match each other.

## Bluetooth LE upstream

SHIFTR's BLE-to-Direct-Connect pass-through and QZ's BLE inputs provide precedent
for this path. Our BLE transport uses BlueZ through Bleak and forwards GATT reads,
writes, subscriptions, notifications and indications through the same request
queue as the TCP upstream. The existing shifting and load calculations are shared.

```sh
python -m pip install '.[ble]'
python -m ftms56 ble-discover --timeout 8
python -m ftms56 run --ble-trainer TRAINER_BLUETOOTH_MAC \
  --physical-ratio 3.0588235294117645 --shift-mode circumference \
  --shift-baseline-circumference 2.0 --shift-baseline-ratio 3.0588235294117645
```

Use the address found by BLE discovery; it can differ from the trainer's Direct
Connect TXT MAC address. `--ble-adapter hci0` selects the local adapter and
`--ble-connect-timeout 35` bounds scan/connection/service discovery. The GUI shows
BLE plus the trainer address instead of a TCP address. `--trainer` and
`--ble-trainer` are mutually exclusive; the default TCP path has no BLE dependency.

A helper process bridges a private socketpair to BLE. The parent waits for actual
GATT readiness before beginning trainer transactions. On BLE loss the existing
session recovery closes downstream clients, clears pending commands, and starts
a fresh helper. FTMS and Wahoo writes still require their real control results;
a successful BLE write alone is not enough. Shutdown attempts the configured
circumference baseline restoration before disconnecting. Abrupt radio/process
failure cannot guarantee restoration.

The user running the program needs access to the system BlueZ service. The
provided dynamic-user service template is intended for TCP; adapt its user and
BlueZ permissions before deploying BLE as a system service. Do not have two apps
change trainer resistance simultaneously. The first live BLE tests use isolated
local ports with no advertisements, leaving the working TCP service available.

## Circumference shifting trial

The default shifting method remains equivalent-grade conversion. To try Wahoo's
proprietary circumference command instead:

```sh
python -m ftms56 run --trainer TRAINER_IP --shift-mode circumference \
  --shift-baseline-circumference 2.0 --shift-baseline-ratio 1.68 \
  --trace circumference-test.jsonl
```

This anchors community gear 8 (ratio 1.68) at **2000 mm**. Each received ratio
sets `2000 mm * ratio / 1.68`, without accumulating shift steps or requiring gear
8 to arrive first. All 24 community ratios fit the command's 6553.5 mm maximum
with this baseline: gear 1 is 892.9 mm and gear 24 is 6535.7 mm. A 2070 mm baseline
would exceed the limit in gear 24. Unexpected ratios are used directly and flagged;
out-of-range circumferences are rejected with a visible warning, retaining the
last setting. The displayed gear is only a community-table approximation.

The GUI shows requested and trainer-confirmed circumference. Confirmation requires
both the TCP write reply and a Wahoo result echoing the requested value. Wahoo control
notifications are captured in the trace and recent events. Confirmation
does not establish correct braking behavior. Grade is sent separately, adjusted
only by the GUI difficulty slider; equivalent-grade gearing is disabled in this
mode. Measured watts and cadence are forwarded normally.

This trial writes the configured test baseline during connection setup, when
shifting is disabled or ERG starts, after the last Zwift client disconnects, and
on a normal service stop. The original trainer value cannot currently be read:
**restoration means the configured test baseline, not the original Wahoo setting**.
After testing, set your usual circumference again in the Wahoo app. A connection
failure or abrupt shutdown cannot guarantee restoration; reconnecting this mode
writes the test baseline again.

For a flat-road ride test, connect to the KICKR6 profile, leave Zwift and GUI
difficulty at 50%, and keep physical gearing fixed. Pause at each virtual gear
long enough to observe cadence, power, effort and acknowledged circumference.
Check both directions and gear 24. The physical ratio is not used to calibrate
this gear-8-anchored trial; matching absolute virtual drivetrain development is
a separate calibration.

## Live Trainer Difficulty

Set Trainer Difficulty **in Zwift to 50% and leave it there**. The GUI slider
defaults to 50%, preserving the received simulation slope. Selecting 20% in
the GUI multiplies the received slope by `20 / 50 = 0.4` before the virtual
gearing calculation. Existing downhill reduction is preserved, without being
applied a second time. At 0%, the slope component is zero but virtual gearing
still operates against rolling/aerodynamic load. Measured power is forwarded
unchanged; the slider does not rescale watts or Zwift's use of measured watts.

The slider affects the KICKR6 SIM profile. KICKR5 pass-through and ERG targets
are unchanged. Its setting survives trainer reconnects and profile handoffs,
but resets to the configured initial value when the process restarts.
`--zwift-difficulty 50` configures the assumed fixed baseline and
`--trainer-difficulty 50` configures the initial GUI value. The baseline must
be nonzero: a flat slope signal from 0% Zwift difficulty cannot be reversed.
Changing difficulty inside Zwift during a ride invalidates the baseline
assumption; the bridge cannot detect that from its current trainer messages.

`POST /api/difficulty` with JSON `{"percent":20}` updates the setting. The same
origin GUI submits it automatically on releasing the slider. Status and traces
include the GUI percentage and configured Zwift baseline for diagnosis.

Useful options:

```sh
python -m ftms56 run --help
python -m ftms56 run --trainer TRAINER_IP --trace session.jsonl
python -m ftms56 run --trainer TRAINER_IP --bind 127.0.0.1 --no-mdns
```

The trace records raw protocol messages for diagnosis. It can contain device
identifiers and session data. Keep local recordings in `.local/` (gitignored).
The default GUI and trainer listeners are available to the local network; there
is no authentication or public-Internet deployment component.

## Connection recovery

Requests and FTMS control indications have deadlines. Silent links are checked
with a read-only feature request every 10 seconds; failed connects retry with
backoff up to 30 seconds. A trainer failure closes the downstream session, clears
queued commands and rediscovers the trainer. Zwift must reconnect and resubscribe.
Normal profile handoff also allows one second for the trainer to release its
previous upstream session before reconnecting.
There is no instantaneous guarantee for an undetectable network black hole.

Shifting updates run at most four times a second, with pending updates coalesced.
Translated grades default to -20..20 percent. If speed and cadence become stale,
the bridge requests neutral simulation once and waits for telemetry recovery.
ERG mode forwards target watts and stops SIM translation. No measured power is
scaled to implement shifting.

## Install as a Linux service

Install the package into a virtual environment under `/opt/ftms56/.venv`, then
copy [the environment example](deploy/ftms56.env.example) to `/etc/ftms56.env`
and set the actual trainer address, physical ratio and mass. Copy
[the systemd unit](deploy/ftms56.service) to `/etc/systemd/system/ftms56.service`.

For example, from this checkout:

```sh
sudo mkdir -p /opt/ftms56
sudo python3 -m venv /opt/ftms56/.venv
sudo /opt/ftms56/.venv/bin/pip install .
sudo install -m 644 deploy/ftms56.env.example /etc/ftms56.env
# Edit /etc/ftms56.env before starting the service.
sudo install -m 644 deploy/ftms56.service /etc/systemd/system/ftms56.service
sudo systemctl daemon-reload
sudo systemctl enable --now ftms56
journalctl -u ftms56 -f
```

The unit uses a dynamic service user and requires no root access at runtime.
The system Avahi daemon must be running. Interactive ride validation should
precede enabling the service for normal use.

## Development and license

```sh
python -m pip install pytest
python -m pytest -q
```

MIT licensed. Reviewed projects and exact revisions are recorded in
[protocol notes](docs/protocol.md). The application was implemented independently;
GPL reference source was not copied or translated into this repository.
