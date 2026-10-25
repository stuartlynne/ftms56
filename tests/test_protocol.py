import struct

import pytest

from ftms56.protocol import (BIKE_DATA, CHARACTERISTICS, CONTROL, DISCOVER, Decoder, Packet,
                             ProtocolError, characteristics, parse_simulation, simulation)
from ftms56.telemetry import Telemetry
from ftms56.zwift import State, bytes_field, fields, read_varint, uint_field, varint


def test_every_tcp_fragment_boundary_and_coalescing():
    packets = [Packet(DISCOVER, 255), Packet(CHARACTERISTICS, 0, body=CONTROL + b"\x06")]
    wire = b"".join(p.encode() for p in packets)
    for boundary in range(len(wire) + 1):
        decoder = Decoder()
        assert decoder.feed(wire[:boundary]) + decoder.feed(wire[boundary:]) == packets
        assert not decoder.buffer
    decoder = Decoder()
    received = []
    for byte in wire:
        received += decoder.feed(bytes([byte]))
    assert received == packets


def test_invalid_version_and_characteristic_lengths():
    with pytest.raises(ProtocolError):
        Decoder().feed(b"\x02\x01\x00\x00\x00\x00")
    with pytest.raises(ProtocolError):
        characteristics(CONTROL + b"\x00")


@pytest.mark.parametrize("number", [0, 127, 128, 7500, 24000, 54900, 2**64 - 1])
def test_varint_roundtrip(number):
    encoded = varint(number)
    assert read_varint(encoded) == (number, len(encoded))


@pytest.mark.parametrize("data", [b"\x80", b"\xff" * 10, b"\x80" * 9 + b"\x02"])
def test_reject_bad_varints(data):
    with pytest.raises(ProtocolError):
        read_varint(data)


def test_gear_wire_example_and_partial_parameter_update():
    state = State()
    # Ratio 24000, an observed wire format also described by the references.
    assert state.receive(bytes.fromhex("042a0410c0bb01")) == "sim"
    assert state.ratio == 2.4
    assert state.gear == 12
    state.receive(b"\x04" + bytes_field(5, uint_field(4, 950) + uint_field(5, 8100)))
    assert (state.ratio, state.bike_kg, state.rider_kg) == (2.4, 9.5, 81)
    state.receive(b"\x04" + bytes_field(5, uint_field(2, 0)))
    assert state.ratio is None


@pytest.mark.parametrize("grade", [-10000, -450, -1, 0, 1, 450, 10000])
def test_grade_uses_signed_zigzag(grade):
    encoded = (grade << 1) ^ (grade >> 63)
    state = State()
    assert state.receive(b"\x04" + bytes_field(4, uint_field(2, encoded))) == "sim"
    assert state.grade == grade / 100


def test_erg_grade_message_does_not_cancel_erg():
    state = State()
    assert state.receive(b"\x04" + uint_field(3, 250)) == "erg"
    assert state.receive(b"\x04" + bytes_field(4, uint_field(2, 100))) == "status"
    assert state.mode == "ERG" and state.target_power == 250


def test_bad_messages_do_not_mutate_state():
    state = State(ratio=2.4)
    invalid = b"\x04" + bytes_field(5, uint_field(2, 30000) + uint_field(5, 50000))
    with pytest.raises(ProtocolError):
        state.receive(invalid)
    assert state.ratio == 2.4
    for data in (b"\x00", b"\x12\x05xx", b"\x0b", b"\x10\x80"):
        with pytest.raises(ProtocolError):
            fields(data)


@pytest.mark.parametrize("handshake", [b"RideOn\x02", b"RideOn\x02\x00", b"RideOn\x02\x04"])
def test_handshake_is_constructed_with_typed_fields(handshake):
    state = State()
    assert state.receive(handshake) == "handshake"
    assert state.handshake
    assert state.notifications[0][1] == b"RideOn\x02\x00"
    message = state.notifications[1][1]
    assert fields(message[1:])[1] == 3


def test_fresh_shifting_identity_has_no_kickr6_name_or_serial():
    state = State(identity="ftms56", display_name="KICKR5_54BD", serial_id="ftms56-kickr5-name-test")
    state.receive(bytes.fromhex("000800"))
    page = fields(fields(state.notifications.pop()[1][1:])[2])
    assert page[3] == b"KICKR5_54BD"
    assert page[6] == b"ftms56-kickr5-name-test"
    assert fields(page[8]) == {1: 1, 2: 20}


def test_gear_status_reply_and_own_device_identity():
    state = State(ratio=2.4, identity="test-bridge")
    assert state.receive(bytes.fromhex("00088804")) == "status"
    assert state.notifications.pop()[1] == bytes.fromhex("3c08880412060a0440c0bb01")
    assert state.receive(bytes.fromhex("410805")) == "status"
    answer = state.notifications.pop()[1]
    wrapper = fields(answer[1:])
    content = fields(fields(wrapper[2])[1])
    assert content[3] == b"test-bridge KICKR6"
    assert content[6] == b"test-bridge-kickr6"
    assert fields(content[8]) == {1: 1, 2: 20}
    assert state.receive(bytes.fromhex("000800")) == "status"
    page = fields(fields(state.notifications.pop()[1][1:])[2])
    assert page[1] == 512
    assert page[6] == b"test-bridge-kickr6"


def test_shifting_display_name_can_change_without_protocol_identity_change():
    state = State(identity="ftms56", display_name="KICKR5_54BD")
    for request in [bytes.fromhex("000800"), bytes.fromhex("410805")]:
        state.receive(request)
        wrapper = fields(state.notifications.pop()[1][1:])
        content = fields(wrapper[2])
        if request[0] == 0x41:
            content = fields(content[1])
        assert content[3] == b"KICKR5_54BD"
        assert content[6] == b"ftms56-kickr6"
        assert fields(content[8]) == {1: 1, 2: 20}
    state.receive(b"RideOn\x02\x04")
    assert state.notifications[0][1] == b"RideOn\x02\x00"


def test_combined_command_and_simulation_coefficients_are_atomic():
    state = State()
    params = uint_field(1, 400) + uint_field(2, 900) + uint_field(3, 5100) + uint_field(4, 415)
    physical = uint_field(2, 24000) + uint_field(4, 800) + uint_field(5, 8000)
    assert state.receive(b"\x04" + bytes_field(4, params) + bytes_field(5, physical)) == "sim"
    assert (state.wind, state.grade, state.cw, state.crr, state.ratio) == (2, 4.5, .51, .00415, 2.4)
    bad = b"\x04" + uint_field(3, 300) + bytes_field(4, uint_field(3, 999999))
    with pytest.raises(ProtocolError):
        state.receive(bad)
    assert state.target_power is None and state.mode == "SIM"


def test_optional_get_from_live_zwift_receives_correlated_empty_status():
    state = State()
    assert state.receive(bytes.fromhex("000810")) == "status"
    assert state.unknown_messages == 1
    characteristic, answer = state.notifications.pop()
    assert fields(answer[1:]) == {1: 16, 2: b""}


def test_ftms_simulation_roundtrip():
    assert parse_simulation(simulation(-4.5, -1.25, .0041, .51)) == (-4.5, -1.25, .0041, .51)


def test_telemetry_optional_fields_and_no_partial_updates():
    telemetry = Telemetry()
    # Instantaneous speed, average speed, cadence, distance, resistance, power.
    flags = 2 | 4 | 16 | 32 | 64
    data = struct.pack("<HHHH", flags, 3600, 3000, 180) + b"\x01\x02\x03" + struct.pack("<hh", 123, 250)
    telemetry.receive(BIKE_DATA, data, now=10)
    assert telemetry.speed_mps == 10
    assert telemetry.cadence == 90
    assert telemetry.resistance == pytest.approx(12.3)
    assert telemetry.power == 250
    assert telemetry.speed(3, 2.1, 11) == 10
    with pytest.raises(ProtocolError):
        telemetry.receive(BIKE_DATA, data[:-1], now=12)
    assert telemetry.updated == 10
    assert telemetry.speed(3, 2.1, 16) is None
    # Cadence can arrive without a speed field (More Data flag).
    telemetry.receive(BIKE_DATA, struct.pack("<HH", 1 | 4, 120), now=20)
    assert telemetry.speed(3, 2, 20) == 6
