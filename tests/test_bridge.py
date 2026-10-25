"""Socket-level checks with an independent trainer peer; no test worker threads."""

import json
import select
import socket
import struct
import subprocess
import sys
import time

import pytest

from ftms56.bridge import Bridge, Config
from ftms56.physics import equivalent_grade
from ftms56.protocol import (BIKE_DATA, CHARACTERISTICS, CONTROL, DISCOVER, FEATURES,
                             CYCLING_POWER, WAHOO_CONTROL, wahoo_circumference,
                             FTMS, NOTIFY, POWER_RANGE, READ, SUBSCRIBE, WRITE, Z_ASYNC,
                             Z_RX, Z_TX, ZWIFT, Decoder, Packet, parse_simulation,
                             service_uuids, simulation)
from ftms56.zwift import bytes_field, uint_field
from ftms56.zwift import GEAR_RATIOS


@pytest.mark.parametrize("indication_first", [False, True])
def test_circumference_all_gears_absolute_updates_and_restoration(indication_first):
    h = Harness("circumference")
    try:
        h.trainer.indication_first = indication_first
        assert h.bridge.circumference_written == 2.0
        client = h.connect("kickr6")
        # Start at a nondefault gear, then skip around: no accumulated shift state.
        for seq, ratio in enumerate([GEAR_RATIOS[-1], *GEAR_RATIOS, 2.6099, 4.2399, 5.4899], 1):
            value = b"\x04" + bytes_field(5, uint_field(2, round(ratio * 10000)))
            assert h.request(client, Packet(WRITE, seq, body=Z_RX + value)).code == 0
            expected = round(2 * ratio / 1.68, 4)
            h.until(lambda: h.bridge.circumference_written == expected)
            assert h.bridge.ratio_matches_table is True
        circumference_writes = lambda: [c for c in h.trainer.commands if c[0] == 0x48]
        count = len(circumference_writes())
        h.request(client, Packet(WRITE, 30, body=Z_RX + value))
        for _ in range(20):
            h.tick()
        assert len(circumference_writes()) == count  # Unchanged ratio is not rewritten.
        # A flat remains flat: no equivalent-grade gearing is also applied.
        assert all(parse_simulation(c)[0] == 0 for c in h.trainer.commands if c[0] == 0x11)
        client.close()
        h.until(lambda: h.bridge.active_profile is None and circumference_writes()[-1] == wahoo_circumference(2))
    finally:
        h.close()


def test_circumference_unknown_ratio_range_and_erg():
    h = Harness("circumference")
    try:
        client = h.connect("kickr6")
        for seq, ratio in [(1, 2.1234), (2, 8.0)]:
            value = b"\x04" + bytes_field(5, uint_field(2, round(ratio * 10000)))
            h.request(client, Packet(WRITE, seq, body=Z_RX + value))
            if seq == 1:
                h.until(lambda: h.bridge.circumference_written == round(2 * ratio / 1.68, 4))
                assert h.bridge.ratio_matches_table is False
                previous = h.bridge.circumference_written
            else:
                assert h.bridge.circumference_error is not None
                assert h.bridge.circumference_written == previous
        erg = b"\x04" + uint_field(3, 250)
        h.request(client, Packet(WRITE, 3, body=Z_RX + erg))
        h.until(lambda: h.trainer.commands[-1] == b"\x05\xfa\x00" and h.bridge.current is None)
        assert h.bridge.circumference_written == 2
        assert h.bridge.zwift.mode == "ERG"
    finally:
        h.close()


def test_circumference_write_rejection_does_not_update_acknowledged_value():
    h = Harness("circumference")
    try:
        client = h.connect("kickr6")
        h.trainer.reject_wahoo = True
        value = b"\x04" + bytes_field(5, uint_field(2, 54900))
        h.request(client, Packet(WRITE, 1, body=Z_RX + value))
        h.until(lambda: not h.bridge.ready)
        assert h.bridge.circumference_written is None
        assert any("rejected circumference" in e["message"] for e in h.bridge.events)
    finally:
        h.close()


def test_circumference_tcp_ack_alone_is_not_confirmation():
    h = Harness("circumference")
    try:
        client = h.connect("kickr6")
        h.trainer.suppress_wahoo_result = True
        value = b"\x04" + bytes_field(5, uint_field(2, 54900))
        h.request(client, Packet(WRITE, 1, body=Z_RX + value))
        h.until(lambda: h.bridge.current is not None and h.bridge.current.wahoo and h.bridge.current.reply is not None)
        assert h.bridge.circumference_written == 2
        assert h.bridge.circumference_requested == 6.5357
        h.trainer.notify(WAHOO_CONTROL, b"\x01\x48\x01\x00" + struct.pack("<H", 65357))
        h.until(lambda: h.bridge.circumference_written == 6.5357)
    finally:
        h.close()


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_rename_shifting_profile_and_hide_pass_through_advertisement(h, monkeypatch):
    class Advertisement:
        established = True
        def __init__(self, name, service, port, txt):
            self.command = ["avahi-publish-service", name, service, str(port), *txt]
        def close(self):
            pass

    monkeypatch.setattr("ftms56.bridge.Publisher", Advertisement)
    h.bridge.config.mdns = True
    h.bridge.config.hide_kickr5 = True
    h.bridge.config.shifting_name = "KICKR5_54BD"
    h.bridge._publish()
    assert len(h.bridge.publishers) == 2  # Shifting profile and HTTP status only.
    ad = h.bridge.publishers[0].command
    assert ad[1] == "KICKR5_54BD"
    assert ad[3] == str(h.bridge.config.kickr6_port)
    assert any("00000001-19ca-4651-86e5-fa29dcdd09d1" in v for v in ad)
    assert h.bridge.status()["links"]["kickr5"]["advertised"] is False
    old_mac = next(v for v in ad if v.startswith("mac-address="))
    h.bridge.config.shifting_id = "ftms56-kickr5-name-test"
    h.bridge._publish()
    fresh = h.bridge.publishers[0].command
    assert "serial-number=ftms56-kickr5-name-test" in fresh
    assert next(v for v in fresh if v.startswith("mac-address=")) != old_mac
    assert fresh[1] == "KICKR5_54BD"


class Trainer:
    """Fake only the trainer-side FTMS contract, not bridge implementation paths."""

    def __init__(self):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.listener.setblocking(False)
        self.port = self.listener.getsockname()[1]
        self.peers = {}
        self.commands = []
        self.requests = []
        self.silent = False
        self.indication_first = False
        self.deny_control = False
        self.features = bytes.fromhex("034000000c600000")
        self.wahoo = False
        self.reject_wahoo = False
        self.suppress_wahoo_result = False

    def tick(self):
        readable, _, _ = select.select([self.listener, *self.peers], [], [], 0)
        for sock in readable:
            if sock is self.listener:
                peer, _ = sock.accept()
                peer.setblocking(False)
                peer.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.peers[peer] = (Decoder(), set())
                continue
            data = sock.recv(65536)
            if not data:
                del self.peers[sock]
                sock.close()
                continue
            decoder, subscriptions = self.peers[sock]
            for packet in decoder.feed(data):
                self.requests.append(packet)
                if self.silent:
                    continue
                indication = b""
                if packet.op == DISCOVER:
                    body = FTMS + (CYCLING_POWER if self.wahoo else b"")
                elif packet.op == CHARACTERISTICS:
                    if packet.uuid == CYCLING_POWER:
                        body = CYCLING_POWER + WAHOO_CONTROL + b"\x06"
                    else:
                        body = FTMS + b"".join(u + bytes([p]) for u, p in [
                            (FEATURES, 1), (CONTROL, 6), (BIKE_DATA, 4), (POWER_RANGE, 1)])
                elif packet.op == READ:
                    value = self.features if packet.uuid == FEATURES else struct.pack("<hhH", 0, 2000, 1)
                    body = packet.uuid + value
                elif packet.op == SUBSCRIBE:
                    if packet.value == b"\x01":
                        subscriptions.add(packet.uuid)
                    else:
                        subscriptions.discard(packet.uuid)
                    body = packet.uuid
                elif packet.op == WRITE:
                    self.commands.append(packet.value)
                    body = packet.uuid
                    if packet.uuid == WAHOO_CONTROL:
                        if self.reject_wahoo:
                            sock.sendall(packet.reply(code=6).encode())
                            continue
                        assert WAHOO_CONTROL in subscriptions
                        indication = Packet(NOTIFY, 77, body=WAHOO_CONTROL + b"\x01\x48\x01\x00" + packet.value[1:]).encode()
                        if self.suppress_wahoo_result:
                            indication = b""
                    else:
                        assert CONTROL in subscriptions, "control indications must be enabled before writes"
                        result = 5 if self.deny_control and packet.value == b"\x00" else 1
                        indication = Packet(NOTIFY, 77, body=CONTROL + bytes([0x80, packet.value[0], result])).encode()
                else:
                    raise AssertionError(packet)
                reply = packet.reply(body).encode()
                wire = indication + reply if self.indication_first else reply + indication
                if packet.op == SUBSCRIBE and packet.uuid == BIKE_DATA and packet.value == b"\x01":
                    # Deliberately coalesce the write reply and first telemetry frame.
                    wire += Packet(NOTIFY, 78, body=BIKE_DATA + struct.pack("<HHHh", 4 | 64, 3000, 180, 250)).encode()
                sock.sendall(wire)

    def notify(self, uuid, value):
        for sock, (_, subscriptions) in self.peers.items():
            if uuid in subscriptions:
                sock.sendall(Packet(NOTIFY, 78, body=uuid + value).encode())

    def disconnect(self):
        for peer in self.peers:
            peer.close()
        self.peers.clear()

    def close(self):
        self.disconnect()
        self.listener.close()


class Harness:
    def __init__(self, shift_mode="grade"):
        self.trainer = Trainer()
        self.trainer.wahoo = shift_mode == "circumference"
        self.bridge = Bridge(Config("127.0.0.1", trainer_port=self.trainer.port, bind="127.0.0.1",
                                    http_port=free_port(), kickr5_port=free_port(), kickr6_port=free_port(),
                                    mdns=False, request_timeout=.2, heartbeat_interval=.3, shift_mode=shift_mode))
        self.sockets = []
        self.buffers = {}
        self.bridge.start()
        self.until(lambda: self.bridge.ready)

    def tick(self):
        self.trainer.tick()
        self.bridge.step(.001)

    def until(self, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while not predicate():
            self.tick()
            if time.monotonic() > deadline:
                raise AssertionError("timed out: " + repr(self.bridge.status()))

    def connect(self, profile="kickr5"):
        port = self.bridge.config.kickr5_port if profile == "kickr5" else self.bridge.config.kickr6_port
        sock = socket.create_connection(("127.0.0.1", port), timeout=1)
        sock.setblocking(False)
        self.sockets.append(sock)
        self.buffers[sock] = (Decoder(), [])
        self.tick()
        return sock

    def received(self, sock):
        decoder, packets = self.buffers[sock]
        while True:
            try:
                data = sock.recv(65536)
                if not data:
                    return packets
                packets.extend(decoder.feed(data))
            except BlockingIOError:
                return packets

    def request(self, sock, packet):
        sock.sendall(packet.encode())
        self.until(lambda: any(p.op == packet.op and p.seq == packet.seq for p in self.received(sock)))
        packets = self.buffers[sock][1]
        reply = next(p for p in packets if p.op == packet.op and p.seq == packet.seq)
        packets.remove(reply)
        return reply

    def close(self):
        for sock in self.sockets:
            sock.close()
        self.bridge.close()
        self.trainer.close()


@pytest.fixture
def h():
    harness = Harness()
    try:
        yield harness
    finally:
        harness.close()


def test_pass_through_sequence_mapping_and_fragmented_request(h):
    client = h.connect()
    assert h.request(client, Packet(DISCOVER, 255)).body == FTMS
    encoded = Packet(READ, 0, body=FEATURES).encode()
    client.sendall(encoded[:3])
    for _ in range(3):
        h.tick()
    assert not h.received(client)
    client.sendall(encoded[3:])
    h.until(lambda: bool(h.received(client)))
    assert h.received(client)[0] == Packet(READ, 0, body=FEATURES + h.trainer.features)


def test_upstream_sequence_wrap_is_independent_of_client(h):
    client = h.connect()
    h.bridge.sequence = 254
    assert h.request(client, Packet(READ, 17, body=FEATURES)).seq == 17
    assert h.request(client, Packet(READ, 19, body=FEATURES)).seq == 19
    assert [p.seq for p in h.trainer.requests[-2:]] == [255, 0]


def test_partial_socket_writes_preserve_entire_frame(h):
    client = h.connect()
    channel = next(iter(h.bridge.clients))
    channel.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    packet = Packet(NOTIFY, body=BIKE_DATA + b"x" * 60000)
    h.bridge._send(channel, packet)
    h.tick()
    assert channel.out  # Kernel accepted only part of the queued frame.
    h.until(lambda: bool(h.received(client)))
    assert h.received(client) == [packet]
    assert not channel.out


def test_profiles_exclusive_but_same_profile_multiple_connections(h):
    first = h.connect()
    second = h.connect()
    assert h.request(first, Packet(DISCOVER, 1)).code == 0
    assert h.request(second, Packet(DISCOVER, 1)).code == 0
    rejected = h.connect("kickr6")
    h.until(lambda: h.bridge.counters["rejected_clients"] == 1)
    assert rejected.recv(1) == b""
    assert h.bridge.active_profile == "kickr5"


def test_notification_subscription_union(h):
    first, second = h.connect(), h.connect()
    h.request(first, Packet(SUBSCRIBE, 1, body=BIKE_DATA + b"\x01"))
    h.request(second, Packet(SUBSCRIBE, 2, body=BIKE_DATA + b"\x01"))
    h.request(first, Packet(SUBSCRIBE, 3, body=BIKE_DATA + b"\x00"))
    h.received(first).clear()
    h.received(second).clear()
    h.trainer.notify(BIKE_DATA, bytes.fromhex("4400b80bb400fa00"))
    h.until(lambda: any(p.op == NOTIFY for p in h.received(second)))
    assert not h.received(first)


@pytest.mark.parametrize("indication_first", [False, True])
def test_control_ack_and_indication_order(h, indication_first):
    h.trainer.indication_first = indication_first
    client = h.connect()
    h.request(client, Packet(SUBSCRIBE, 1, body=CONTROL + b"\x01"))
    reply = h.request(client, Packet(WRITE, 2, body=CONTROL + b"\x00"))
    assert reply.code == 0
    h.until(lambda: any(p.op == NOTIFY for p in h.received(client)))
    assert next(p for p in h.received(client) if p.op == NOTIFY).value == b"\x80\x00\x01"
    assert h.bridge.control_ready


def test_virtual_service_handshake_and_simulation_translation(h):
    client = h.connect("kickr6")
    assert service_uuids(h.request(client, Packet(DISCOVER, 1)).body) == [FTMS, ZWIFT]
    reply = h.request(client, Packet(CHARACTERISTICS, 2, body=ZWIFT))
    assert len(reply.body) == 16 + 3 * 17
    for seq, uuid in [(3, Z_TX), (4, Z_ASYNC)]:
        h.request(client, Packet(SUBSCRIBE, seq, body=uuid + b"\x01"))
    # Actual Zwift's negotiation flags from the interactive LAN trace.
    assert h.request(client, Packet(WRITE, 5, body=Z_RX + b"RideOn\x02\x04")).code == 0
    h.until(lambda: any(p.op == NOTIFY and p.value == b"RideOn\x02\x00" for p in h.received(client)))
    gear = b"\x04" + bytes_field(5, uint_field(2, 54900))
    assert h.request(client, Packet(WRITE, 6, body=Z_RX + gear)).code == 0
    h.until(lambda: h.bridge.counters["translated_commands"] >= 1)
    assert h.trainer.commands[0] == b"\x00"
    translated = next(c for c in h.trainer.commands if c[0] == 0x11)
    assert parse_simulation(translated)[0] > 0
    assert h.bridge.telemetry.power == 250
    assert h.bridge.zwift.gear == 24
    assert h.bridge.status()["trainer_grade"] == parse_simulation(translated)[0]


def test_control_already_acquired_still_subscribes_to_telemetry(h):
    client = h.connect("kickr6")
    h.request(client, Packet(WRITE, 1, body=CONTROL + b"\x00"))
    assert h.bridge.control_ready
    h.request(client, Packet(WRITE, 2, body=Z_RX + bytes.fromhex("042a0410c0bb01")))
    h.until(lambda: h.bridge.counters["translated_commands"] > 0)
    assert BIKE_DATA in h.bridge.internal_subscriptions


def test_erg_cancels_queued_simulation(h):
    client = h.connect("kickr6")
    h.request(client, Packet(WRITE, 1, body=Z_RX + bytes.fromhex("042a0410c0bb01")))
    h.until(lambda: h.bridge.counters["translated_commands"] > 0)
    h.request(client, Packet(WRITE, 2, body=Z_RX + b"\x04" + uint_field(3, 250)))
    h.until(lambda: h.trainer.commands[-1] == b"\x05\xfa\x00")
    for _ in range(20):
        h.tick()
    assert h.trainer.commands[-1] == b"\x05\xfa\x00"
    assert h.bridge.zwift.mode == "ERG"


def test_denied_control_never_executes_translated_load(h):
    h.trainer.deny_control = True
    client = h.connect("kickr6")
    h.request(client, Packet(WRITE, 1, body=Z_RX + bytes.fromhex("042a0410c0bb01")))
    h.until(lambda: not h.bridge.clients)
    assert h.trainer.commands == [b"\x00"]
    assert not h.bridge.control_ready


def test_stale_telemetry_requests_neutral_once(h):
    client = h.connect("kickr6")
    h.request(client, Packet(WRITE, 1, body=Z_RX + bytes.fromhex("042a0410c0bb01")))
    h.until(lambda: h.bridge.counters["translated_commands"] > 0)
    h.bridge.telemetry.speed_updated -= 10
    h.bridge.telemetry.cadence_updated -= 10
    h.bridge.last_translation = 0
    h.until(lambda: h.bridge.stale_telemetry and h.bridge.current is None and not h.bridge.queue)
    assert parse_simulation(h.trainer.commands[-1])[0] == 0
    count = len(h.trainer.commands)
    for _ in range(10):
        h.tick()
    assert len(h.trainer.commands) == count


def test_disconnect_clears_commands_and_reconnects_without_replay(h):
    client = h.connect("kickr6")
    h.request(client, Packet(DISCOVER, 1))
    h.trainer.disconnect()
    h.until(lambda: not h.bridge.ready)
    assert not h.bridge.clients
    assert not h.bridge.queue
    h.bridge.next_connect = 0
    h.until(lambda: h.bridge.ready)
    assert h.bridge.zwift.ratio is None
    assert not h.trainer.commands


def test_last_client_release_allows_trainer_cleanup_without_blocking(h):
    client = h.connect()
    h.request(client, Packet(DISCOVER, 1))
    client.close()
    h.until(lambda: not h.bridge.clients)
    assert not h.bridge.ready and h.bridge.upstream is None
    assert h.bridge.next_connect > time.monotonic() + .5
    # No sleep in the application: the selector is still available during cooldown.
    h.bridge.next_connect = 0
    h.until(lambda: h.bridge.ready)
    assert not h.trainer.commands


def test_silent_trainer_heartbeat_timeout(h):
    h.trainer.silent = True
    h.bridge.next_heartbeat = 0
    h.until(lambda: not h.bridge.ready)
    assert any("timed out" in e["message"] for e in h.bridge.events)


def test_http_status_health_and_slow_header_deadline(h):
    sock = socket.create_connection(("127.0.0.1", h.bridge.config.http_port), timeout=1)
    sock.setblocking(False)
    h.sockets.append(sock)
    sock.sendall(b"GET /api/status HTTP/1.1\r\nHost: test\r\n\r\n")
    wire = bytearray()

    def got_http():
        try:
            wire.extend(sock.recv(65536))
        except BlockingIOError:
            pass
        return b"\r\n\r\n" in wire and wire.endswith(b"}")

    h.until(got_http)
    status = json.loads(wire.split(b"\r\n\r\n", 1)[1])
    assert status["ready"] and status["shifting_experimental"]
    assert status["clients"] == 0


def test_slow_http_and_incomplete_frame_deadlines(h):
    sock = socket.create_connection(("127.0.0.1", h.bridge.config.http_port), timeout=1)
    h.sockets.append(sock)
    h.tick()
    channel = next(c for c in h.bridge.channels if c.kind == "http")
    channel.opened -= 6
    h.tick()
    assert sock.recv(1) == b""
    client = h.connect()
    client.sendall(b"\x01\x03")
    h.until(lambda: any(c.partial_since is not None for c in h.bridge.clients))
    channel = next(iter(h.bridge.clients))
    channel.partial_since -= 6
    h.tick()
    assert not h.bridge.clients


def test_virtual_telemetry_and_handshake_do_not_acquire_control(h):
    client = h.connect("kickr6")
    h.request(client, Packet(SUBSCRIBE, 1, body=Z_ASYNC + b"\x01"))
    h.request(client, Packet(SUBSCRIBE, 2, body=Z_TX + b"\x01"))
    h.request(client, Packet(WRITE, 3, body=Z_RX + b"RideOn\x02\x00"))
    h.bridge.last_riding_data = 0
    h.until(lambda: any(p.op == NOTIFY and p.uuid == Z_ASYNC and p.value.startswith(b"\x03") for p in h.received(client)))
    assert h.trainer.commands == []
    assert not h.bridge.control_ready
    assert h.bridge.telemetry.power == 250


def test_inspector_cli_reads_coalesced_telemetry_without_control_writes(h):
    process = subprocess.Popen([sys.executable, "-m", "ftms56", "inspect", "--trainer", "127.0.0.1",
                                "--trainer-port", str(h.trainer.port), "--sample-seconds", ".03"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        h.until(lambda: process.poll() is not None)
        stdout, stderr = process.communicate()
        assert process.returncode == 0, stderr.decode()
        result = json.loads(stdout)
        assert result["telemetry"]["received"]
        assert result["telemetry"]["power"] == 250
        assert not h.trainer.commands
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_display_distinguishes_received_grade_from_accepted_trainer_command(h):
    assert h.bridge.status()["zwift_simulation_grade"] is None
    assert h.bridge.status()["trainer_grade"] is None
    client = h.connect("kickr6")
    # Real downhill wire sample: signed value -24, units 0.01 percent.
    h.request(client, Packet(WRITE, 1, body=Z_RX + bytes.fromhex("042202102f")))
    h.until(lambda: h.bridge.status()["trainer_grade"] is not None)
    status = h.bridge.status()
    assert status["zwift_simulation_grade"] == -.24
    assert status["trainer_grade"] == -.24  # Shifting inactive: direct SIM command.
    assert status["zwift_grade_age_s"] >= 0
    assert status["trainer_grade_age_s"] >= 0


def http_exchange(h, wire, remainder=b""):
    sock = socket.create_connection(("127.0.0.1", h.bridge.config.http_port), timeout=1)
    sock.setblocking(False)
    h.sockets.append(sock)
    sock.sendall(wire)
    if remainder:
        for _ in range(3):
            h.tick()
        assert h.bridge.difficulty_percent == 50
        sock.sendall(remainder)
    received = bytearray()

    def complete():
        try:
            received.extend(sock.recv(65536))
        except BlockingIOError:
            pass
        if b"\r\n\r\n" not in received:
            return False
        header, body = bytes(received).split(b"\r\n\r\n", 1)
        length = next(int(row.split(b":", 1)[1]) for row in header.split(b"\r\n") if row.lower().startswith(b"content-length:"))
        return len(body) == length

    h.until(complete)
    header, body = bytes(received).split(b"\r\n\r\n", 1)
    return int(header.split()[1]), json.loads(body)


def post_difficulty(h, value, fragment=False, origin=None):
    body = json.dumps({"percent": value}).encode()
    header = (b"POST /api/difficulty HTTP/1.1\r\nHost: test\r\nContent-Type: application/json\r\n" +
              f"Content-Length: {len(body)}\r\n".encode() +
              (f"Origin: {origin}\r\n".encode() if origin else b"") + b"\r\n")
    return http_exchange(h, header + body[:3], body[3:]) if fragment else http_exchange(h, header + body)


def test_difficulty_api_waits_for_complete_body_and_rejects_cross_origin(h):
    code, response = post_difficulty(h, 20, fragment=True)
    assert code == 200 and response == {"percent": 20, "zwift_baseline_percent": 50}
    assert not h.trainer.commands  # Changing idle preferences does not acquire trainer control.
    assert post_difficulty(h, 100, origin="https://different-host.example")[0] == 403
    assert h.bridge.difficulty_percent == 20


@pytest.mark.parametrize("value", [-1, 101, True, "50", None, float("nan"), float("inf")])
def test_invalid_difficulty_does_not_change_load(h, value):
    assert post_difficulty(h, value)[0] == 400
    assert h.bridge.difficulty_percent == 50
    assert not h.trainer.commands


def test_live_difficulty_scales_slope_before_gearing_and_preserves_power(h):
    client = h.connect("kickr6")
    gear = b"\x04" + bytes_field(5, uint_field(2, 45000))
    grade = b"\x04" + bytes_field(4, uint_field(2, 600))  # Received +3% at a 50% baseline.
    h.request(client, Packet(WRITE, 1, body=Z_RX + gear))
    h.request(client, Packet(WRITE, 2, body=Z_RX + grade))
    h.until(lambda: h.bridge.counters["translated_commands"] > 0)
    assert post_difficulty(h, 20)[0] == 200
    expected = round(equivalent_grade(1.2, 4.5, 3.125, 3000 / 360).grade, 2)
    h.until(lambda: h.bridge.trainer_grade == expected)
    status = h.bridge.status()
    assert status["zwift_simulation_grade"] == 3
    assert status["effective_simulation_grade"] == pytest.approx(1.2)
    assert status["telemetry"]["power"] == 250
    assert post_difficulty(h, 0)[0] == 200
    flat = round(equivalent_grade(0, 4.5, 3.125, 3000 / 360).grade, 2)
    h.until(lambda: h.bridge.trainer_grade == flat)
    assert flat > 0  # Flat-road virtual gearing remains present at 0% difficulty.
    assert status["telemetry"]["power"] == 250


def test_difficulty_preserves_existing_downhill_reduction(h):
    h.bridge.zwift.grade = -2  # -8% road, halved downhill and then 50% Zwift difficulty.
    h.bridge.set_difficulty(20)
    assert h.bridge._effective_grade() == pytest.approx(-.8)


def test_difficulty_does_not_modify_pass_through_or_erg(h):
    client = h.connect("kickr5")
    post_difficulty(h, 0)
    h.request(client, Packet(WRITE, 1, body=CONTROL + simulation(3)))
    assert parse_simulation(h.trainer.commands[-1])[0] == 3
    client.close()
    h.until(lambda: not h.bridge.clients)
    h.bridge.next_connect = 0
    h.until(lambda: h.bridge.ready)
    assert h.bridge.difficulty_percent == 0  # Retained across trainer reconnects.
    client = h.connect("kickr6")
    h.request(client, Packet(WRITE, 2, body=Z_RX + b"\x04" + uint_field(3, 250)))
    h.until(lambda: h.trainer.commands[-1] == b"\x05\xfa\x00" and h.bridge.current is None)
    before = len(h.trainer.commands)
    post_difficulty(h, 100)
    for _ in range(10):
        h.tick()
    assert len(h.trainer.commands) == before
    assert h.bridge.zwift.mode == "ERG"


@pytest.mark.parametrize("baseline", [0, -1, 101, float("nan")])
def test_invalid_zwift_baseline_is_rejected(baseline):
    with pytest.raises(ValueError):
        Config("192.0.2.1", zwift_difficulty=baseline)
