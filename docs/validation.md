# Validation and remaining work

## Verified on 2026-10-23

- Read-only service/characteristic inventory of the LAN KICKR via Direct Connect.
- FTMS resistance, target-power, simulation and wheel-circumference feature bits.
- Idle indoor-bike telemetry: power 0 W, cadence 0 rpm and speed 0 km/h.
- The same inventory and telemetry through the KICKR5 bridge profile.
- Direct and proxied inventories matched exactly, including readable values.
- Trace review confirmed zero upstream characteristic writes in those live checks.
- Both trainer profiles published and resolved through Avahi on the LAN, with
  distinct virtual MACs and correct service lists/ports; HTTP also advertised.
- HTTP JSON status and health via IPv4; health via IPv6 loopback.
- Desktop status page rendered and visually inspected in headless Chrome.
- Mobile status page rendered and visually inspected at 390 px width.
- Installed wheel launched from outside the checkout; bundled GUI and MIT
  license present and no third-party Python runtime dependencies declared.
- One Python thread observed while the live bridge, GUI and mDNS publication
  were active.
- A simulated Zwift client received handshake, device-information, gear-status
  and riding-data replies while using real trainer telemetry. No gear-changing
  commands were sent to the physical trainer.
- Three normal session releases/reconnects passed without request timeouts after
  adding the one-second trainer cleanup timer. The original immediate-reconnect
  timeout is documented in the protocol notes.
- Systemd unit syntax verification passed with the local installed Python path
  substituted for the deployment prefix. The service itself was not installed.

Private trainer inventory, raw traces and a status screenshot are retained in
the gitignored `.local/` directory. These contain local addresses and device
identifiers and are not intended as public protocol fixtures.

`python -m pytest -q` passed 84 tests. Automated checks use a separate simulated
trainer peer and real loopback sockets.
They cover fragmented/coalesced TCP frames, independent sequence numbers,
subscription sharing, exclusive profiles, both FTMS reply/indication orders,
Zwift handshake and ratio messages, SIM/ERG transitions, control refusal,
stale telemetry fallback, connection loss and silent-peer heartbeat timeout.
They also cover constructed device-info/gear-status replies, atomic combined
parameter updates, wind/rolling coefficients, and incomplete-request deadlines.
Socket checks include partial writes and upstream sequence-number wraparound.
Physics checks establish neutral-ratio invariance, gear-load ordering and limits;
they do not establish physical ride feel.

## Next session with the bicycle and Zwift

The first interactive check confirmed KICKR5 power delivery in Zwift. KICKR6
was discovered and selectable for power/cadence/resistance, but did not complete
initialization. Its trace contained repeated `RideOn 02 04` requests rejected by
the original strict handshake parser. The trace also showed nonzero power
notifications being forwarded to Zwift (up to 127 W), so loss was not on the
upstream telemetry path. The handshake variant is now covered by regression
checks. Zwift advanced to device-information queries after the fix. A second
correction distinguishes the wrapped legacy identity response from the direct
GET(0) device page. Current Zwift also probes optional object 16 (`00 08 10`);
the emulator returns a correlated empty status rather than rejecting its write.
The meaning of object 16 is still unknown. Actual KICKR6 power display and
shifting acceptance still need the next interactive retry. These changes pass
87 automated checks; the running interactive service was restarted with them.

### Successful interactive ride and grade display follow-up

The user confirmed that KICKR6 was immediately recognized on retry, Zwift offered
virtual shifting, Zwift Play gear buttons worked with confirmation feedback, and
the GUI virtual gear matched Zwift. The trace records successful FTMS load
commands during that ride. This establishes actual recognition/shifting, beyond
the earlier simulated-client checks; comparative ride feel and ERG remain to test.

The user also reported a mismatch between the grade display and Zwift's road
grade. The GUI previously called its received/translated pair “Road → trainer
grade”, which incorrectly suggested two measurements of the road. Those values
are now separately labeled Zwift simulation input and KICKR5 equivalent grade,
with two decimal places. The second is the last command acknowledged as
successful by the real trainer, rather than just the latest calculated target.

Recorded Zwift grade commands decode to -0.24..+0.23 percent in the documented
0.01 percent units. Trainer Difficulty was recalled as approximately 20 percent.
Zwift can scale simulation requests, including further downhill reduction;
neither a tenfold correction nor an exact reconstruction of the displayed road
grade is established by this trace. The next comparison needs an observed road
grade, exact difficulty setting and matching timestamp. Control scaling was not
changed on the basis of the display report. Private analysis is retained in
`.local/grade-review-2026-10-24.json`. The updated code passes 88 tests.

### Live difficulty control

A GUI slider now assumes a fixed 50% setting in Zwift and starts at 50% locally.
It scales only the simulation slope before virtual gearing. Additional checks
exercise live HTTP updates, fragmented request bodies, input validation,
nonmatching browser origins, scaling order, retained flat-road gearing at 0%,
unchanged measured power, pass-through and ERG behavior, downhill attenuation,
and setting retention across trainer reconnects. The suite passes 103 tests.
Interactive ride feel with the new slider remains to be checked on the bicycle.

1. Set the actual chainring/sprocket ratio and rider+bike mass assumption.
2. Pair Zwift to the bridge's KICKR5 profile as power, cadence and resistance.
   Verify a short normal SIM ride and a short ERG workout, capturing a trace.
3. Pair the KICKR6 profile and the normal Click/Play/Ride controller. Confirm
   Zwift enables virtual shifting. Verify its device-info and gear-status
   exchanges and inspect any unknown requests if it does not; acceptance by
   Zwift is the largest remaining interoperability uncertainty.
4. Test low-to-high gears on a flat road, then grades, coasting, starting from
   rest, and switching between SIM and ERG. Compare physical effort to expected
   load; adjust the trainer mass/physics assumptions if measurements require it.
5. Verify controller disconnect/reconnect behavior and mid-ride network recovery.
   Reconnection currently closes downstream sessions so Zwift must rediscover/
   reconnect and resubscribe; stale control commands are never replayed.
6. Install and enable the supplied systemd unit once interactive behavior is
   confirmed. The unit is supplied as an installation template, not a claim of
   an already-tested system service deployment.

No real resistance changes, Zwift pairing, shifting ride, ERG ride or physical
failure recovery were tested during the unattended work. The temporary live
bridge used for read-only verification was stopped after the checks.

## Circumference trial prepared, 2026-10-24

The interactive service was idle (zero clients) before switching to
`--shift-mode circumference --shift-baseline-circumference 2.0
--shift-baseline-ratio 1.68`. It remains running as `ftms56-test.service` at
the usual ports, logging to `.local/circumference-test-2026-10-24.jsonl`.

Live KICKR5 checks established Cycling Power discovery, Wahoo control subscription,
and acceptance of the baseline command `48 20 4e` (2000.0 mm). The trainer sent
`01 48 01 00 20 4e` on the custom characteristic. The bridge now requires both
the TCP response and that matching Wahoo result before recording confirmation.
The GUI was checked in Chrome: trainer ready, circumference method active,
requested/confirmed 2000.0 mm, difficulty 50%, no Zwift clients yet.

All **108 tests pass**. Added socket-level checks exercise every community ratio
in both TCP/result orderings, a nondefault first gear, skipped gears, unchanged
ratio suppression, unknown ratios, out-of-range rejection, TCP write failure,
TCP acknowledgement without a Wahoo result, flat-grade preservation, ERG baseline
restoration, and baseline restoration after the last client disconnects. The
GUI JavaScript passes Node's syntax check.

Only the baseline circumference was written to real hardware during preparation;
the all-gear sequence used the fake trainer. Physical load response, interaction
with FTMS simulation on the real trainer, ride-start initialization and multiple
rides within one Zwift launch remain for the user ride test. Restoration writes
the configured 2000 mm baseline, not the unreadable original setting; the user's
usual circumference should be restored in the Wahoo app after the trial.

### First circumference ride feedback and calibration review

The user reported a KICKR6 gear sweep followed by KICKR5 mechanical shifting,
with excessive effort above approximately virtual gear 20. The trace confirms
initial ratio 1.68 (community gear 8), progression to gear 24, down to gear 1,
then back to gear 8. Sent KICKR6 grades ranged from -0.79% to +0.12%; virtual
gearing was not also applied as equivalent grade. Confirmed circumference ranged
from 892.9 to 6535.6 mm. The live ratios include 2.6099, 4.2399 and 5.4899, one
wire unit below the community decimals. Table checks now tolerate that rounding
while circumference calculation continues to use the exact received ratio.

The gear-8 anchor intentionally preserves physical gearing at gear 8, but is
not calibrated to the absolute virtual sprocket ratios. Assuming the physical
gear was 52/17, it raises the whole effective gearing by
`(52/17)/1.68 = 1.8207`. Gear 24 therefore has the development of approximately
ratio 9.995 on a 2000 mm wheel, rather than the requested ratio 5.4899. This
provides a concrete explanation for the excessive upper gearing, without proving
every detail of the trainer load model. Confirm the physical cog used during the
virtual test before changing that calibration.

With 52/17 confirmed and the same 2000 mm wheel reference, absolute-ratio
calibration would set `C = 2000 mm * received_ratio / (52/17)`: approximately
1098.5 mm at gear 8 and 3589.6 mm at gear 24. The physical 52/11 top gear is ratio
4.7273; the virtual top would still be approximately 16% higher. Both methods
retain the same 7.32:1 virtual range; calibration shifts the range rather than
compressing it.

At virtual gear 20, samples with reported cadence above 20 rpm had median
91 W and 24 rpm. At gears 22-24 FTMS reported zero cadence despite nonzero
power, limiting cadence-based analysis of that low-speed effort. Do not interpret
zero reported cadence as proof the rider stopped pedalling. The KICKR5 comparison
contains no mechanical cog labels, so it cannot establish a matched-cadence,
matched-gear power comparison. Analysis artifact:
`.local/circumference-ride-review-2026-10-24.json`.

### Automatic range/style display inference

The estimator was replayed against all 341 captured ratio messages, including
the four user tests, the preceding Mixed Terrain sweeps and a ratio-0 disable.
It identified the observed preset changes, inferred SRAM-style during the
All-Rounder rear sweep, and ended with front/rear position 1-1 (35/33). The
estimates never replace or round the ratio used for load calculations.

All **115 tests pass**, including preset starts, shared-ratio disambiguation,
mid-session range/style changes, unknown ratios, reset and preservation of exact
incoming ratios. JavaScript syntax validation passes. After repeated zero-power,
zero-cadence checks, the service was restarted for this display update. Zwift
reconnected automatically, sending ratio 1.0606. Chrome showed All-Rounder inferred,
gear 1, 35/33 and Sequential marked as a guess pending further shifts; Wahoo
confirmed 1262.6 mm under the unchanged gear-8 anchor. Load calibration was not
changed in this update.

### Tiny Races calibration, 2026-10-24

The user confirmed physical 52/17 for the upcoming four 14:00/14:15/14:30/14:45
local starts. After confirming zero power and cadence, the transient service was
recreated with both `--physical-ratio` and `--shift-baseline-ratio` set to
`3.0588235294117645`, retaining the 2.0 m reference circumference. This replaces
the prior gear-8 anchor with `C = 2.0 m * received_ratio / (52/17)`.
Zwift reconnected automatically, sent All-Rounder ratio 1.0606, and the KICKR5
confirmed the resulting 693.5 mm setting. No ride-feel claim is made from that
command acknowledgement. The user can verify during warmup before racing.

Raw trace rows now include shift method, physical ratio, baseline ratio and
baseline circumference so the two calibrations can be separated during analysis.
The same append-only trace remains active. A deployment/status snapshot with the
planned race start times is in `.local/tiny-races-calibration-2026-10-24.json`.
All 115 tests still pass. The user plans SRAM All-Rounder, possibly Climbing for
the final race; style inference will settle again after several shifts.

### Four Tiny Races completed

The user reports SRAM-style in races 1–2 and Sequential in races 3–4. Trace
review from 13:45 to 15:10 local confirms All-Rounder throughout; no Climbing
ratios were observed. The calibration metadata in every reviewed packet uses
physical ratio and reference ratio 52/17. Every non-restoration circumference
write matched `2000 mm * latest_received_ratio / (52/17)` within 0.05 mm.

All 403 circumference writes in this warmup/race/cooldown interval received both
successful TCP replies and exact matching Wahoo results. Median full confirmation
latency was 11.1 ms and maximum 97.4 ms. All 14,971 FTMS simulation results were
success. No TCP write errors or unsupported ratios were found.

| Fixed start-time window | User-reported style | Ratio updates | Confirmed circumference writes | Maximum confirmation latency |
| --- | --- | ---: | ---: | ---: |
| 14:00–14:15 | SRAM-style | 37 | 37 | 93.0 ms |
| 14:15–14:30 | SRAM-style | 63 | 63 | 97.1 ms |
| 14:30–14:45 | Sequential | 62 | 61 | 97.1 ms |
| 14:45–15:00 | Sequential | 131 | 132 | 96.4 ms |

These are fixed analysis windows from the planned starts, not inferred official
race boundaries. They include intermissions, and racing appears to continue beyond
15:00 in the power trace. Command and ratio-update counts can differ through
deduplication and transactions crossing a window boundary. They are not evidence
of missed shifts. No per-race average power is asserted from these windows.

The inferred style was SRAM-style throughout the first window, mostly SRAM-style
in the second with four Sequential estimates near the transition, and Sequential
throughout the third and fourth windows. Maximum All-Rounder circumference was
3138.5 mm, versus the previous uncalibrated 5714.3 mm for the same maximum ratio.

Journal review shows one uninterrupted client session from 13:52:12 until
15:07:29, covering all four races. Discovery timed out after a warmup disconnect
at 13:48 and again after the final disconnect at 15:07; both recovered before
another ride, with no trainer timeout logged during the race block. The trainer
is now ready, no clients attached, with the configured 2000 mm baseline restored.
This establishes transport/control continuity, not a calibrated force measurement
or proof of correct subjective load. User ride-feel feedback remains separate.

Raw analysis and per-second/minute power evidence are retained in
`.local/tiny-races-review-2026-10-24.json`; reproduce using
`PYTHONPATH=. python .local/review_tiny_races.py`. No service restart or hardware
changes were made during this post-ride analysis.

### KICKR5 display-name experiment prepared

At the user's request, the idle service was recreated with
`--shifting-name KICKR5_54BD --hide-kickr5`. The shifting DNS-SD instance and
proprietary device-info field 3 now use `KICKR5_54BD`. The pass-through profile
is no longer advertised (its listener remains available). The shifting service,
characteristics, protocol responses, capability fields, virtual MAC and serial
identity remain unchanged. In particular, serial `ftms56-kickr6` is retained:
this experiment changes display names, not every identity string. The 52/17
circumference calibration is unchanged.

Live DNS-SD browsing verified only the renamed shifting instance from the bridge,
plus the physical trainer's separate `Wahoo KICKR 54BD` instance. Service status
is ready and the trainer confirmed the 2000 mm baseline. Raw traffic is recorded
separately in `.local/kickr5-name-experiment-2026-10-24.jsonl`; the setup snapshot
is `.local/kickr5-name-experiment-ready-2026-10-24.json`. Both forms of device-info
response and hidden advertisement behavior are tested; all 117 tests pass.
Zwift acceptance with the renamed profile awaits the user's pairing test, and
any positive result must account for possible cached identity/capability state.

The user then reported the working entry still displayed `ftms56 KICKR6`, with
the physical `Wahoo KICKR 54BD` entry not offering shifting. The renamed bridge
trace confirms a connection at 17:47:15 and ratio commands at 17:48, including
All-Rounder gears 5–7. The bridge was advertising `KICKR5_54BD` and returning
that device-info name, so the displayed old label is consistent with cached
identity metadata; this first experiment does not settle name-based recognition.

For a clean follow-up, the idle service was recreated with the same names,
capabilities and 52/17 calibration plus `--shifting-id ftms56-kickr5-name-test`.
This changes the virtual MAC seed and both proprietary/DNS-SD serial values,
removing the old identity and its literal `kickr6` serial suffix. The physical
trainer's own advertisement remains separate and cannot be withdrawn by hiding
our bridge's pass-through profile. Live DNS-SD verifies the fresh identity under
`KICKR5_54BD`; trace is `.local/kickr5-fresh-identity-experiment-2026-10-24.jsonl`.
All 118 tests pass. User pairing of this fresh identity remains to be verified.

### BLE upstream implementation and live validation

The user authorized trying BLE from the current host, with a laptop closer to
the trainer as a fallback. The powered `hci0` adapter discovered `Wahoo KICKR
54BD` at BLE address `FD:CB:E7:3B:C2:2B`, distinct from the Direct Connect TXT MAC.
Scan RSSI ranged approximately -96 to -86 dBm. An initial probe using the system
Bleak 1.0.1 connected/read features but errored on disconnect. The application
extra uses Bleak 3.0.2/dbus-fast 5.2.0 in the project virtual environment.

An isolated non-advertised BLE-backed bridge on ports 36965–36967 established:

- Live GATT discovery: nine services; FTMS/CPS and Wahoo proprietary control.
- FTMS Features `034000000c600000`, matching the native Direct Connect inspection.
- Complete read-only inspection via our TCP pass-through, with actual incoming
  Indoor Bike Data notifications (zero power/cadence/speed with the bike idle).
- Successful custom circumference command and matching Wahoo indication at 2000 mm.
- Zwift 2.4 handshake/device-info responses through the shifting TCP profile.
- FTMS Request Control and a neutral SIM command, accepted by the trainer over BLE.
  The ratio used for this check preserved the existing 2000 mm circumference.
- Baseline restoration and automatic BLE reconnect after the test client closed.

After checking the working bridge had no clients, both temporary services were
stopped and the main `ftms56-test.service` was recreated with BLE upstream on
the usual ports. It retains `KICKR5_54BD`, the fresh experiment identity,
hidden pass-through advertisement, and 52/17 circumference calibration. Status
reports ready, BLE address/adapter, and confirmed 2000 mm baseline. No further
control command was sent on cutover beyond that same known baseline. Original
TCP service configuration is saved in `.local/before-ble-cutover-unit-2026-10-24.txt`.
The isolated BLE test service is stopped, avoiding two live controlling bridges.

All **122 tests pass**. Added tests cover GATT operations/property mapping,
subscription deduplication, real result forwarding separately from write replies,
fragmented/coalesced IPC traffic, errors, ambiguous UUIDs, waiting for BLE readiness,
and helper-loss cleanup. These are transport/control checks, not a BLE ride test.
No pedalling power, long-duration radio reliability, or force/load response over
BLE has yet been verified. Weak RF may require a closer adapter or laptop.

Artifacts: `.local/ble-kickr-inventory-2026-10-24.json`,
`.local/ble-through-tcp-inspection-2026-10-24.json`,
`.local/ble-shifting-verification-2026-10-24.json`,
`.local/ble-main-ready-2026-10-24.json`, and
`.local/ble-upstream-main-2026-10-24.jsonl` (active trace).

### User BLE-to-Direct-Connect test succeeded

The user reports Zwift initially did not find the bridge while the physical
trainer was still network-connected. After exiting Zwift, unplugging that
connection and restarting, Zwift showed our advertisement alongside the physical
trainer's BLE entry, and operation through our bridge worked correctly. Because
both discovery conditions and the Zwift session changed, this does not establish
that native Ethernet must be disconnected for BLE operation. Earlier simultaneous
BLE and native TCP bench access succeeded. Discovery caching/selection remains a
possible explanation for the first attempt, not a confirmed cause.

The actual Zwift client connected at 18:13:41 and disconnected at 18:14:59.
During 18:13–18:15, the BLE-backed main trace contains 26 ratio commands spanning
1.6666–3.6923, 19 circumference writes and 19 successful Wahoo results. Pending
rapid ratio changes can coalesce, so those counts need not be equal. FTMS results
include successful Request Control and 134 successful SIM commands, with no
nonzero TCP result codes. Incoming BLE telemetry includes pedalling up to 77 W
and 35 rpm. Thus this test extends the idle bench check to a real user pedalling
and shifting through the BLE-to-TCP path, under the fresh KICKR5 display identity.

Baseline restoration and reconnect succeeded after the user disconnected; the
main service remains BLE-backed, ready and idle at 2000 mm. Sustained ride/race
reliability at this radio signal level remains untested. Summary artifact:
`.local/ble-user-test-2026-10-24.json`. No service changes were made during review.

## Installation and documentation packaging

The pre-rewrite README was preserved verbatim as `README-plan.md` (SHA-256
`390e0a9f858d3c7bd2cd25626e6d6d9939c383da06420ba3a980e114409214c1`). The new
README describes the project, dependencies, user-local installation, JSON settings,
optional systemd user service, pairing and operation. Legacy TCP system-service
templates remain for links in the archived README; the installer uses the new
`deploy/ftms56.service.in` user-service template.

`install.py` was executed in temporary paths under `.local/install-validation`,
including a virtual environment path containing a space. The package built and
installed with no third-party TCP runtime dependencies. The installed command,
JSON validation and packaged GUI resource worked from `/tmp`, outside the source
checkout. A second install preserved the existing configuration. A foreground
installed-process smoke test against the fake trainer reached ready and served
the GUI without sending control writes. No physical trainer was used in that test.

The generated user unit passed `systemd-analyze --user verify`. Unit registration,
explicit start semantics, custom-unit preservation and config validation have
automated coverage; no new user service was enabled or started on this host.
All **131 tests pass**. The existing interactive BLE service was left running;
the installation checks did not reconfigure or restart it. Default install paths
were not populated during validation.
