# Protocol notes

These are working notes from source review and read-only trainer inspection on
2026-10-23. They describe the implemented subset, not a complete specification
or a claim of Zwift certification.

## References and provenance

| Reference | Revision reviewed | Use |
| --- | --- | --- |
| [kvj/hass_Wahoo_Dircon](https://github.com/kvj/hass_Wahoo_Dircon/tree/f8a32301e9899cd2bbba60cfb7e236df684f82d2) | `f8a32301e9899cd2bbba60cfb7e236df684f82d2` | MIT Python reference for Direct Connect framing and discovery |
| [JuergenLeber/SHIFTR](https://github.com/JuergenLeber/SHIFTR/tree/058695971df496a00249af63e3a123af19c93830) | `058695971df496a00249af63e3a123af19c93830` | GPL-3.0 reference for observed Zwift messages and possible translation methods |
| [cagnulein/qdomyos-zwift](https://github.com/cagnulein/qdomyos-zwift/tree/47c6d22923b7ed74e9c18ece7ab0554d92b0a4f2) | `47c6d22923b7ed74e9c18ece7ab0554d92b0a4f2` | GPL-3.0 cross-check for Zwift service, ZigZag grade encoding and FTMS |
| [Berg0162/Kickr-Virtual-Shifting](https://github.com/Berg0162/Kickr-Virtual-Shifting/tree/509ac65dd01e651a2c39a19c626def87bcaddb3d) | `509ac65dd01e651a2c39a19c626def87bcaddb3d` | GPL-3.0 cross-check of effective-gradient approach, coefficient units and status requests |
| [dadepro/zwift-virtual-shifting](https://github.com/dadepro/zwift-virtual-shifting/tree/9a852ef3a1062e0618bf3522652376c6ff71e727) | `9a852ef3a1062e0618bf3522652376c6ff71e727` | MIT alternative using direct BLE button/keyboard input and fixed slope offsets; protocol defects noted below |
| [jvivenot/KickrShiftr](https://github.com/jvivenot/KickrShiftr/tree/f2ec892d761a7daa454f81b3c6dcdd48e896b8f4) | `f2ec892d761a7daa454f81b3c6dcdd48e896b8f4` (local review, 2026-10-24) | MIT iOS app using Wahoo proprietary wheel circumference changes; author reports Zwift testing with KICKR CORE |
| [elfrances/wahoo-fitness-tnp](https://github.com/elfrances/wahoo-fitness-tnp/tree/c227ec8e64793401b3b67e03ac009c0973934159) | `c227ec8e64793401b3b67e03ac009c0973934159` (local review, 2026-10-24) | GPL-3.0 reverse-engineered Direct Connect description |

All application source here was written independently. No reference repository
source was copied or translated into this project. The reviewed GPL sources
were used to identify interoperable wire facts; their implementations, prose,
device-identity blobs and resistance algorithms are not included. The physics
model below is derived from force and power relationships. The existing
`socketmonitor.py` was left untouched and is not imported by the application.

Relevant source locations in SHIFTR are `include/UUIDs.h` and
`src/DirConManager.cpp` (`processZwiftSyncRequest`, `getZwiftDataValues`,
`generateZwiftAsyncNotificationData`). In QZ, see
`src/characteristics/characteristicwriteprocessor0003.cpp` and
`src/devices/dircon/dirconmanager.cpp`, plus the message descriptions in
`src/android/src/main/proto/zwift_hub.proto`. Berg0162's `src/VirtualShifting.cpp`
confirms additional parameter/status details. SHIFTR's `VirtualShifting.md` explicitly
warns that parts of its explanation are outdated; the source was cross-checked.

### Local reference checkouts (2026-10-24)

The sibling checkouts were clean when inspected. SHIFTR, hass_Wahoo_Dircon and
dadepro's project match the revisions above. The local QZ checkout is newer than
the initial review: `32331ce613eec3204ad6604b337337304898aaf7`.

| Sibling directory | Most useful evidence | Limitation |
| --- | --- | --- |
| `../SHIFTR` | Zwift handshake, messages and load conversion in `src/DirConManager.cpp` and `lib/Calculations/src/Calculations.cpp` | Some documentation is outdated; empirical load adjustments are not trainer calibration |
| `../qdomyos-zwift` | Wahoo commands in `src/devices/wahookickrsnapbike/wahookickrsnapbike.cpp`; ratio conversion in `src/wheelcircumference.h` | Reviewed source behavior, not verified KICKR5 behavior over Direct Connect |
| `../hass_Wahoo_Dircon` | Small Python Direct Connect client | Treadmill-oriented; partial TCP reads, sequence wrapping and arbitrary UUID handling need care |
| `../wahoo-fitness-tnp` | Discovery, framing, UUID byte order and characteristic property bits | Reverse-engineered transport description, not a shifting or trainer physics specification |
| `../zwift-virtual-shifting` | Alternative local button/keyboard architecture | Fixed slope offsets and control-packet problems; detailed review below |
| `../KickrShiftr` | Minimal Wahoo circumference implementation and author-described packet capture | BLE KICKR CORE evidence; no Direct Connect test or configuration restoration |

The local `../doc/FTMS_v1.0.pdf` and `../doc/FTMS.TS.p4.pdf` provide specification
references; the test suite linked below is a later revision.

QZ is the strongest next lead for an alternative to equivalent-grade conversion.
Its Wahoo path supplies simulation mass and coefficients, and changes configured
wheel circumference in proportion to selected gear ratio divided by physical
gear ratio. This is relevant even to a wheel-off trainer: circumference is a
simulation parameter. It does not prove that changing it produces the desired
braking force on our trainer. A controlled comparison must check acknowledged
commands, power and ride feel, and restore the original configuration afterward.
QZ's destructor restores a fixed 2070 mm value; that is not evidence of the
trainer's original setting.

Before a hardware comparison, confirm the actual chainring/sprocket combination,
review Wahoo command responses and supported ranges, and establish how simulation
mass is set. Keep the working grade-conversion mode available for comparison.
No reference program was run against the trainer during this review.

### KickrShiftr circumference evidence (2026-10-24)

The clean local checkout at the revision above corroborates QZ's Wahoo command.
`KickrShiftr/BluetoothManager.swift` discovers Cycling Power service `1818` and
characteristic `a026e005-0a7d-4ab3-97fa-f1500f9feb8b`, then writes:

`48 <uint16 little-endian circumference in 0.1 mm>`

For example, its default 2.096 m circumference encodes as `48 e0 51`.
`ContentView.swift` multiplies circumference by 1.1 for an upshift and divides by
1.1 for a downshift, with a user-adjustable step ratio. It does not process Zwift
absolute gear ratios, road grade or difficulty, and sends no simulation-mass or
grade command. The README attributes the command to sniffing Wahoo's iPhone app
and reports testing with Zwift on a KICKR CORE. Those are author-reported hardware
results, not a test of our KICKR5 or our transport.

The implementation uses BLE writes with response. It does not subscribe to a
Wahoo command-result notification or read back circumference, so its success
callback establishes write completion rather than measured load behavior. There
is no circumference reset on exit; destruction only disconnects. The README says
the current value cannot be retrieved; the Direct Connect probe below qualifies
that claim. Its unsigned 16-bit encoding limits the
representable circumference to 6.5535 m (the code rejects that exact endpoint too).

For this bridge, the analogous candidate is an absolute setting
`C = C_reference * Zwift_ratio / physical_ratio`, rather than accumulated local
button steps. Our trainer inventory includes this same custom characteristic,
making a Direct Connect trial plausible, but command acceptance, load effect,
interaction with FTMS simulation writes and reliable restoration remain to be
verified before adopting it.

### Direct read probe on KICKR5 (2026-10-24)

Using the running bridge's KICKR5 pass-through connection, a Direct Connect READ
of `a026e005-0a7d-4ab3-97fa-f1500f9feb8b` succeeded (response code 0), despite
discovery advertising only write and notify/indicate. It returned
`01 48 01 00 20 4e`, exactly matching the earlier successful circumference result
for 2000 mm. A readable FTMS Features characteristic returned its expected value;
the standard Cycling Power control point also accepted READ but returned no value.
No probe control writes were sent. Raw request/response evidence is retained in
`.local/circumference-read-probe-2026-10-24.json`.

This establishes that Direct Connect permits this read and exposes a circumference
result when that is the latest result. It does not yet distinguish a cached last
command result from an independent current-configuration query, nor establish
whether a direct BLE read behaves the same way. The service recovered normally
after the probe and remains ready in circumference mode at the 2000 mm baseline.

## Captured Zwift gear presets (2026-10-24)

The user's four setting/shift tests produced complete sets of 24 ratios for
Flat, All-Rounder and Climbing, followed by SRAM-style All-Rounder. These are
observed wire values from this Zwift version, not a universal published table.
An initial Mixed Terrain sweep preceded the four tests. The three sequential
presets are the sorted ratios of the following virtual tooth combinations:

| Preset | Front rings | Rear sprockets, smallest to largest | Lowest ratio | Highest ratio |
| --- | --- | --- | --- | --- |
| Flat | 53, 39 | 10, 11, 12, 13, 14, 15, 16, 18, 20, 24, 26, 28 | 39/28 = 1.3928 on wire | 53/10 = 5.3 |
| All-Rounder | 48, 35 | 10, 11, 12, 13, 14, 15, 17, 19, 21, 24, 28, 33 | 35/33 = 1.0606 | 48/10 = 4.8 |
| Climbing | 43, 30 | 10, 11, 12, 13, 15, 17, 19, 21, 24, 28, 32, 36 | 30/36 = 0.8333 | 43/10 = 4.3 |

All 24 ratios from SRAM-style All-Rounder are also in sequential All-Rounder.
The difference is ordering: rear shifts follow a single front ring's 12 sprockets,
while changing the front ring changes the ratio at the same rear sprocket. For
example, the final captured front changes are 35/33 -> 48/33 -> 35/33. No separate
front-ring index, rear-gear index, gear number, preset or shift-style field was
present in these trainer commands: physical-parameter messages contain field 2
(ratio), plus rider/bike mass fields 4 and 5 in initialization messages only.
This does not establish what Zwift sends separately to its controllers.

Recorded sweep windows (local time) and first/last ratios:

| Test | Window | First ratio | Last ratio | Positions in sorted 24-ratio table |
| --- | --- | --- | --- | --- |
| Flat | 12:24:21–12:26:40 | 1.95 | 2.0384 | 5 -> 24 -> 1 -> 6 |
| All-Rounder | 12:27:05–12:28:57 | 1.6666 | 1.6666 | 5 -> 24 -> 1 -> 5 |
| Climbing | 12:29:37–12:30:42 | 1.3437 | 1.25 | 6 -> 24 -> 1 -> 5 |
| SRAM All-Rounder | 12:31:10–12:33:27 | 1.8421 | 1.0606 | Both rings; final ratio is 35/33 |

These positions are reconstructed from ratios, not directly reported gear labels.
Preset changes occurred within one connected Zwift session; they do not prove
the starting gear after a fresh ride or full application launch. The first
successful initialization in this later session sent ratio 1.68 (Mixed Terrain
gear 8) at 12:21:53. An earlier attempt at 12:19:43 sent ratio 0 (disabled).

The final SRAM small-ring sweep spans 35/33 -> 35/10 -> 35/33. The following
big-ring sweep reaches 48/12 before returning to 48/33 and then 35/33; the full
48/10 maximum had already been reached earlier in the SRAM test.

The load calculation uses the received ratio directly and handles these
presets without a lookup table. The GUI initially only mapped Mixed Terrain;
it now guesses the preset from matching tables, preserves a compatible guess,
and revises it when later ratios contradict it. Ambiguous matches remain marked
as guesses, and unknown values produce approximate displays. Several consistent
shift transitions provide a Sequential/SRAM-style estimate; transitions compatible
with both styles do not overturn the guess. These are display estimates, not
protocol fields. Trace extracts and derived mappings are retained in
`.local/gear-presets-ratios-2026-10-24.json` and
`.local/gear-presets-review-2026-10-24.json`. The running service was left connected
and its original gear-8 load anchor was not changed during analysis.

## Direct Connect

### Optional BLE upstream

SHIFTR's README explicitly describes forwarding a BLE trainer's services over
Direct Connect, with optional added shifting/FTMS emulation. QZ combines BLE
central connections (including its Wahoo driver) with its Direct Connect server.
Those projects establish the architecture; our BLE implementation uses the
public [Bleak client API](https://bleak.readthedocs.io/en/latest/api/client.html)
and [BlueZ backend](https://bleak.readthedocs.io/en/latest/backends/linux.html).

The TCP selector loop communicates with a separate BLE helper through a private
socketpair using the same packet format. Internal operation 0 carries helper
ready/error status and is never exposed downstream. Discovery is generated from
live GATT services, UUIDs are canonical 128-bit bytes, and BLE properties map to
DirCon's read/write/notify bits (indicate and notify both map to bit 4).
Subscriptions use real BLE notifications/indications. A GATT write completion
produces only the TCP write reply; the actual device result is a separate NOTIFY
packet. The existing parent-side FTMS/Wahoo transaction checks therefore apply.
Duplicate characteristic UUIDs cannot be disambiguated by this DirCon contract,
so operations on them are rejected rather than routed to an arbitrary handle.

BLE read errors are reported explicitly. In particular, the successful native
Direct Connect read of Wahoo's non-readable control point does not establish
that BlueZ permits the corresponding direct BLE read or that its cached result
is a general configuration query.

Advertise `_wahoo-fitness-tnp._tcp` using DNS-SD. TXT keys observed on the actual
trainer are `ble-service-uuids`, `mac-address`, and `serial-number`.
The bridge uses its own virtual identities for its two profiles; it does not
reuse the trainer's MAC or serial number.

Each TCP frame has a six-byte header:

| Offset | Size | Meaning |
| --- | --- | --- |
| 0 | 1 | Version, currently 1 |
| 1 | 1 | Message type |
| 2 | 1 | Sequence number |
| 3 | 1 | Response code, 0 for requests/success |
| 4 | 2 | Body length, big endian |

Message types 1..6 are service discovery, characteristic discovery, read, write,
notification subscription and unsolicited notification. UUIDs occupy 16 bytes
in canonical/network order; FTMS numeric values inside characteristics are
little endian. Service discovery responses contain consecutive UUIDs.
Characteristic discovery responses contain the service UUID, followed by
17-byte entries (characteristic UUID and properties byte). Property bits are
1=read, 2=write, 4=notify/indicate. A subscription request is UUID plus one byte
(0 disables; 1 enables); its response is the UUID alone.

The bridge maps downstream sequence numbers to one serialized upstream request
at a time. Multiple same-profile connections share the upstream connection and
the union of notification subscriptions. Up to four same-profile clients are
accepted; the other profile is rejected until all clients release the session.
Power/cadence notifications are decoded for status and forwarded unchanged.
Generated Zwift notifications use sequence zero, as QZ does. Real trainer
notifications were observed using their own increasing sequence numbers.
After the last downstream connection closes, the upstream session is closed and
a one-second timer allows the trainer to release it before reconnecting. A live
check found that an immediate TCP reconnect could succeed while the trainer
ignored discovery, causing a request timeout; ordinary retry recovered it.

## Observed trainer FTMS capabilities

Read-only inspection of the LAN KICKR returned these feature bytes:

```text
03 40 00 00  0c 60 00 00
```

Target features are `0x0000600c`: resistance, target power, indoor-bike simulation
parameters and wheel circumference. Supported resistance range bytes were
`00 00 64 00 01 00`; supported power range bytes were
`00 00 d0 07 01 00` (0..2000 W, 1 W increment). No native Zwift service was exposed.

Relevant UUIDs are service `0x1826`, feature `0x2acc`, control point `0x2ad9`,
indoor-bike data `0x2ad2`, and power range `0x2ad8`.

FTMS control is transactional: subscribing to the control point and writing a
command produces both a Direct Connect write response and an FTMS indication.
The latter is `80 <requested-opcode> <result>`, with result 1 meaning success.
The bridge waits for both before sending another control command and handles
either arrival order. Private translation/control responses are not exposed as
responses to Zwift's own FTMS commands. Control refusal aborts the internal
translation session; pending loads are not replayed after reconnect.

Control commands used:

| Opcode | Payload | Purpose |
| --- | --- | --- |
| `00` | none | Request control |
| `05` | signed 16-bit watts | ERG target |
| `11` | signed 16-bit wind, signed 16-bit grade, unsigned Crr byte, unsigned Cw byte | SIM parameters |

For `11`, wind uses 0.001 m/s, grade 0.01 percent, Crr 0.0001, Cw 0.01 kg/m.
Reference: Bluetooth SIG [FTMS test suite](https://files.bluetooth.com/wp-content/uploads/dlm_uploads/2024/10/FTMS.TS_.p6.pdf).

## Zwift trainer service

Additional primary protocol context is published in the appendix to Zwift's
[EP4478753A1 application](https://patents.google.com/patent/EP4478753A1/en).
This identifies the RideOn suffix as protocol major/minor version and describes
GET as a page/object query. It predates some messages requested by the current
Zwift client, so it does not establish the meaning of optional object 16.

UUID suffix: `-19ca-4651-86e5-fa29dcdd09d1`.

| UUID prefix | Role |
| --- | --- |
| `00000001` | Service |
| `00000002` | Trainer asynchronous notifications |
| `00000003` | Commands written by Zwift |
| `00000004` | Trainer synchronous response notifications |

The emulated profile adds this service to the real trainer's service list and
handles it locally. This exposes the observed shifting protocol; it does not
replace all device-information characteristics with a fictional KICKR6 identity.

Observed handshake is ASCII `RideOn` followed by `02` and optionally one
minor-version byte. The real Zwift client in the first interactive test sent
`02 04`; accepting only `02 00` prevented initialization. Both eight-byte forms
are now accepted, with the supported response negotiated as `02 00`.
The synchronous answer is `RideOn 02 00`. An asynchronous `0x2a` status message
is also constructed with protobuf fields rather than copied as an opaque blob.

Control messages start with `04`, followed by protobuf-style fields. Unsigned
integers use base-128 varints; signed grade uses ZigZag encoding. The decoder
checks bounds and nested lengths, and tolerates unrecognized fields of supported
wire types.

| Outer field | Type | Meaning |
| --- | --- | --- |
| 3 (`18`) | varint | ERG target watts |
| 4 (`22`) | nested message | Inner fields: 1 signed wind x100; 2 signed grade x100; 3 Cw x10000; 4 Crr x100000 |
| 5 (`2a`) | nested message | Inner field 2 is ratio x10000; 4 is bike mass x100; 5 is rider mass x100 |

For example, `04 2a 04 10 c0 bb 01` requests gear ratio 2.4. These are absolute
ratios, not up/down events. The conventional 24-ratio table is used only to label
the status display; calculations use the received ratio. Ratio zero disables
shifting and returns to ordinary SIM control.

Trainer riding notifications start with `03`. Fields 1 and 2 carry measured
power and cadence. Fields 3 and 4 represent speed and heart rate in QZ's message
description; the speed unit and heart-rate sourcing need verification here, so
these are emitted as zero. Fields 5 and 6 remain unknown and are also zero.
Negative measured power is preserved
in forwarded FTMS/CPS packets; the unsigned Zwift field is bounded at zero.

Status/device-info requests starting with `00` and `41` produce constructed
`3c` replies. Information ID 520 returns the current ratio x10000 in nested
field 8. General/device-info replies carry the bridge's own name, serial and
version. The observed discriminator value 512 and capability subfields 1=1,
2=20 are included for interoperability; their full meaning remains unverified.
These are protocol constants, not a copied trainer identity. Unsupported request
IDs receive a correlated empty status and are logged/traced, without inventing
object values. The legacy `41` identity query nests content under an extra
wrapper; GET(0) returns the device-information page directly. Current Zwift's
handshake now advances to these queries; full pairing acceptance remains under
interactive test.

## Translation model

The first implementation uses equivalent gradient, because FTMS resistance level
does not provide a portable calibrated force scale. Let `k` be virtual gear ratio
divided by actual mechanical ratio. With physical wheel speed `v`, virtual road
speed is `k*v`. Road force is calculated as gravity plus rolling resistance plus
signed aerodynamic drag. Power conservation gives required trainer force
`k * F_road(k*v)`.

Subtract aerodynamic drag at the physical speed, then solve the trainer's
gravity and rolling model for its required grade. Equal masses and ratio `k=1`
reproduce the original grade. A higher gear increases load on flat roads too,
because virtual air speed changes. This differs from adding a fixed slope per
gear, and is covered by mathematical invariance and monotonicity checks.

Actual mechanical ratio, circumference and the mass assumed by the trainer must
be configured. Zwift supplies rider and bike mass when available. The trainer's
own mass model has not been measured, so matching ride feel remains unverified.
By default, the bridge preserves the grade delivered by Zwift, which already
reflects its difficulty setting. The GUI difficulty control uses an explicit
baseline conversion: `effective_grade = received_grade * gui_percent / zwift_baseline_percent`.
Both percentages default to 50, so the default conversion is the identity.
The conversion occurs before calculating road force and virtual gearing; scaling
the final equivalent grade instead would also change flat-road gear response.
Zwift's downhill attenuation is already present in the received grade and is
preserved. The slider does not change measured telemetry, ERG targets or the
pass-through profile. Live settings survive session resets, but not process
restarts; the initial setting and assumed Zwift baseline are CLI options.
For a displayed -8% descent, Zwift's documented halving and a 50% baseline
produce a -2% input. A 20% GUI setting changes that to -0.8% before gearing,
preserving the same downhill rule. See [Zwift's explanation](https://forums.zwift.com/t/introducing-climb-portal-elevation-scaling-december-2023/616272).

SIM updates are limited to four per second and queued SIM updates are coalesced.
Default grade limits are -20..20 percent. Fresh wheel speed is preferred; fresh
cadence can estimate speed using the physical ratio and circumference. If both
become stale, one neutral SIM request replaces the translated load until fresh
telemetry returns. ERG cancels queued SIM translations; grade updates received
during ERG do not cancel ERG. There is no claim that this models flywheel inertia
or reproduces every aspect of native KICKR6 ride feel.

## Review of dadepro's alternative (2026-10-24)

The user-supplied sibling clone `../zwift-virtual-shifting` was reviewed at
`9a852ef3a1062e0618bf3522652376c6ff71e727`. Its MIT license is compatible with this
project, but its current implementation is not evidence of a validated KICKR
load model. No source from it was incorporated and no hardware test was run.

Its main, keyboard and gamepad entry points import `gear_controller.py`, which
maps a locally maintained gear number to a fixed gradient offset. It receives
neither Zwift's absolute virtual ratio nor the current route slope. Although
comments describe adding an offset to Zwift's grade, no such merge is implemented:
each shift writes a new absolute FTMS simulation command. The `base_gradient`
attribute is unused. `gear_controller_v3.py` contains a separate ratio-to-offset
experiment, but these entry points do not import it. Neither version configures
the actual physical chainring/cog ratio, and neither changes circumference.

An isolated fake-BLE-writer check of `KickrController` recorded these defects:

- `set_simulation_mode(grade=0.10)` emits `11 0000 0a00 2800 0000`, nine bytes.
  FTMS simulation uses opcode plus two signed 16-bit fields and two unsigned
  8-bit fields, seven bytes total. Its 16-bit Crr/Cw packing is incorrect.
- Its documented grade argument is a fraction (0.10 means 10%), but it encodes
  `grade * 100`. Raw grade 10 represents 0.10%, not 10%, in FTMS units. Correcting
  only the byte layout would therefore leave a factor-of-100 grade error.
- `set_resistance(10)` emits `04 0a`, whereas the standard target-resistance
  parameter is signed 16-bit. Its encoding is one byte short.
- The BLE connection subscribes to indoor-bike data but does not acquire FTMS
  control or subscribe/process control-point indications. `response=True` asks
  for an ATT write response, not an FTMS success indication. The bike-data
  callback itself is a stub.

The active gear model also reduces grade on an upshift, despite labeling it
harder. The unused v3 cassette indexing makes ratios decrease as gear numbers
rise within each chainring. Its fixed reference ratio and offset multiplier
do not provide a cadence/speed/mass-based simulation model.

The FTMS wire checks were compared with the Bluetooth SIG test suite's control
procedures and simulation parameter definitions. These findings establish source
and encoding defects, not the behavior of a trainer accepting those writes.
