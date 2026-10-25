# ftms56

An experimental Linux bridge that adds Zwift virtual shifting to a Wahoo KICKR5. It connects to
the trainer over Bluetooth LE or Wahoo Direct Connect and presents a shifting-capable
trainer to Zwift over the local network.

```text
Zwift + Play/Click/Ride controllers
                 |
          Direct Connect / TCP
                 |
               ftms56
                 |
      Bluetooth LE or Direct Connect
                 |
             Wahoo KICKR5
```

Zwift sends an absolute virtual gear ratio and simulation grade. The bridge applies
gearing using Wahoo's wheel-circumference command and forwards the simulation grade
separately. Power and cadence come from the trainer. Controllers pair with Zwift.

The KICKR5 has been tested with both upstream connections. Four short races have
been completed using the Direct Connect upstream. BLE has passed a short pedalling
and shifting test; longer rides and other trainer models remain to be validated.

## Requirements and dependencies

- Linux and Python **3.11 or newer**.
- `avahi-daemon` and `avahi-utils` for trainer discovery and advertising.
- For BLE: BlueZ, a working Bluetooth adapter, and access to the system Bluetooth service.

| Use | Python runtime dependencies |
| --- | --- |
| Direct Connect upstream | Standard library only |
| BLE upstream | Optional `bleak` extra, which installs `dbus-fast` on Linux |
| Development tests | `pytest` |

The TCP server and GUI use the standard library. BLE runs in a helper process so
its asynchronous I/O is isolated from the TCP selector loop. Installing builds the
package with setuptools; setuptools is a build dependency, not an application library.

On Debian/Ubuntu:

```sh
sudo apt install python3 python3-venv avahi-daemon avahi-utils
# For Bluetooth LE:
sudo apt install bluez
```

## Install

From this checkout, run the installer as your normal user, **without sudo**:

```sh
python3 install.py
# Or include optional BLE support:
python3 install.py --ble
```

It creates an isolated virtual environment and command:

| Item | Default location |
| --- | --- |
| Application environment | `~/.local/share/ftms56/venv` |
| Command | `~/.local/bin/ftms56` |
| Configuration | `~/.config/ftms56/config.json` |
| Optional systemd user service | `~/.config/systemd/user/ftms56.service` |

The installer respects `XDG_DATA_HOME` and `XDG_CONFIG_HOME`. `--prefix`, `--bin-dir`
and `--config` allow other locations. Add `~/.local/bin` to your PATH or use the
full command path below. Installation alone does not start a trainer connection.

### Find your trainer

```sh
# Trainers advertising Direct Connect:
~/.local/bin/ftms56 discover

# BLE trainers; requires installation with --ble:
~/.local/bin/ftms56 ble-discover --timeout 8
```

Use the address from the appropriate discovery command. The BLE address can differ
from the MAC in the trainer's network advertisement.

### Configure and run

Initialize a Direct Connect configuration, replacing the trainer address:

```sh
python3 install.py --trainer 192.168.1.100 --physical-ratio 3.0588235294117645
```

Or initialize a BLE configuration, replacing the Bluetooth address:

```sh
python3 install.py --ble-trainer AA:BB:CC:DD:EE:FF --physical-ratio 3.0588235294117645
```

**The example ratio is 52/17. Set it to your actual chainring teeth divided by
rear sprocket teeth, then leave the bike in that gear.** The installer uses the
same ratio as the circumference reference, with a 2.0 m reference circumference.
Without an explicit ratio, the installer's default is 50/16 = 3.125.

Device options initialize a new configuration. **Existing configuration files are
preserved on reinstall**, including when device options are supplied again. Edit
the JSON file to change an existing installation. If no trainer was specified,
the installer writes `config.json.example` instead; configure it before running.

Validate and start:

```sh
~/.local/bin/ftms56 run --config ~/.config/ftms56/config.json --check-config
~/.local/bin/ftms56 run --config ~/.config/ftms56/config.json
```

`--check-config` validates without connecting. With `--config`, run settings come
from the file; only `--check-config` and `--log-level` may be added. Complete
examples are in [deploy/config-tcp.example.json](deploy/config-tcp.example.json)
and [deploy/config-ble.example.json](deploy/config-ble.example.json).

### Optional systemd service

After configuring the trainer:

```sh
# Install, enable and start the user service:
python3 install.py --systemd --start

systemctl --user status ftms56
journalctl --user -u ftms56 -f
```

`--systemd` alone installs the service without starting it. It runs under your
normal account, supporting the same BlueZ permissions as the interactive command.
The installer preserves custom service units and updates its own generated unit.
Stop a foreground instance before starting the service; the default ports are shared.

```sh
systemctl --user stop ftms56
systemctl --user restart ftms56   # after editing configuration or upgrading
systemctl --user disable --now ftms56
```

User services normally run with your login session. To run before login and after
logout, an administrator can enable lingering:

```sh
sudo loginctl enable-linger "$USER"
```

### Upgrade or remove

Re-run the installer from the updated checkout using the same installation paths.
It preserves your configuration and installs BLE dependencies automatically when
an existing configuration uses BLE. Restart the service to load the new version.

To remove a default installation, first disable/stop the user service, then remove
the command symlink, installation directory and generated service file. Keep or
remove `~/.config/ftms56` as desired, then run `systemctl --user daemon-reload`.

### Manual virtual environment installation

The installer is optional:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
# For BLE instead:
.venv/bin/python -m pip install '.[ble]'
.venv/bin/ftms56 --help
```

## Pair with Zwift

1. Run ftms56 and open `http://localhost:36865/`, or the host's LAN address on port 36865.
2. Wait for the upstream trainer to show **Connected** and the bridge to show **Trainer ready**.
3. Select **ftms56 KICKR6** in Zwift for power, cadence and resistance. This is the
   bridge's default shifting-profile name, even when the physical trainer is a KICKR5.
4. Pair Play/Click/Ride controllers with Zwift and enable virtual shifting.
5. Keep Trainer Difficulty in Zwift at **50%**. The GUI slider provides further slope adjustment.

The physical trainer may also appear directly in Zwift. Select the bridge's entry.
A native Direct Connect connection accepts only one app at a time; the bridge owns
that connection when using TCP upstream. Discovery caching may require reselecting
the bridge or restarting Zwift after an identity/name change.

The GUI shows each connection, measured power/cadence, received and accepted grades,
requested/confirmed circumference, and an inferred gear range and shift style.
Gear labels are estimates; the actual received ratio always controls the load.
Mixed Terrain, Flat, All-Rounder and Climbing are supported by the display mapping.

| Default TCP port | Purpose |
| --- | --- |
| 36865 | GUI, `/api/status`, `/healthz` |
| 36866 | Shifting trainer profile |
| 36867 | Optional pass-through profile |

The installer hides the pass-through advertisement. Set `hide_kickr5` to `false`
to advertise it for comparison. Only one profile can be active at a time, with up
to four clients sharing it. The listeners bind to the local network by default;
configure `bind` and the ports in JSON if needed.

## Configuration notes

- **Upstream:** set exactly one of `trainer_host` or `ble_address`. BLE optionally
  takes `ble_adapter` (default `hci0`) and `ble_connect_timeout` (default 35 seconds).
- **Gearing:** the installed circumference mode uses
  `circumference = shift_baseline_circumference × Zwift ratio / shift_baseline_ratio`.
  Set `physical_ratio` and `shift_baseline_ratio` to the actual drivetrain ratio.
- **Reference circumference:** `shift_baseline_circumference` is in metres. The
  installer uses 2.0. On normal stop/disconnect the bridge restores this configured
  value. It does not reliably retrieve the original trainer setting, and abrupt
  connection loss cannot guarantee restoration. Set your usual size in Wahoo's app
  when returning to a different setup.
- **Difficulty:** `zwift_difficulty` records Zwift's fixed setting; `trainer_difficulty`
  is the initial GUI percentage. Both default to 50. Received downhill treatment is
  preserved. Changes to the GUI slider affect slope, not measured watts, and reset
  to the configured value when the process restarts.
- **Alternative load method:** `shift_mode: "grade"` selects the earlier equivalent-grade
  model. It additionally uses `circumference` and `trainer_mass_kg`; see the protocol notes.
- **Names:** `shifting_name` overrides the displayed name. `shifting_id` supplies a
  fresh bridge identity for pairing experiments.
- **Diagnostics:** set `trace` to a JSONL filename to capture raw traffic. Relative
  paths resolve beside the configuration file. Traces can grow quickly; recording
  is off by default. Service messages go to the journal.

All configuration keys are the fields of `Config` in [ftms56/bridge.py](ftms56/bridge.py).
Unknown keys are rejected. Direct command-line settings remain available with
`ftms56 run --help`; these retain the original grade-mode defaults, so use the
installer's JSON configuration for the circumference setup described here.

## Development and references

```sh
python3 -m pip install pytest
python3 -m pytest -q
```

- [Protocol and source references](docs/protocol.md)
- [Validation and ride results](docs/validation.md)
- [Gear-ratio CSV](docs/zwift-gear-ratios.csv) and [preset mappings](docs/zwift-gear-mappings.csv)
- [Archived planning/working README](README-plan.md)

MIT licensed. Reference-project provenance and interoperability findings are
recorded in the protocol notes.
