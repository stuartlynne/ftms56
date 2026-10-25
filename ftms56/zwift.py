"""Independent encoder/decoder for the observed Zwift trainer wire messages.

Only protocol facts are used from the reference implementations; no reference
implementation code or opaque device-identity replies are incorporated.
"""

from dataclasses import dataclass, field
import time

from .protocol import ProtocolError, Z_ASYNC, Z_TX
from .gears import GEAR_RATIOS, GearEstimate


def varint(number: int) -> bytes:
    if not 0 <= number < 1 << 64:
        raise ProtocolError("varint outside uint64 range")
    result = bytearray()
    while number >= 128:
        result.append((number & 127) | 128)
        number >>= 7
    result.append(number)
    return bytes(result)


def read_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    number = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ProtocolError("truncated varint")
        byte = data[offset]
        offset += 1
        if shift == 63 and byte > 1:
            raise ProtocolError("varint exceeds uint64")
        number |= (byte & 127) << shift
        if not byte & 128:
            return number, offset
    raise ProtocolError("unterminated varint")


def fields(data: bytes) -> dict[int, int | bytes]:
    """Decode flat protobuf fields; later occurrences replace earlier values."""
    result = {}
    offset = 0
    while offset < len(data):
        tag, offset = read_varint(data, offset)
        key, wire = tag >> 3, tag & 7
        if not key:
            raise ProtocolError("protobuf field zero")
        if wire == 0:
            value, offset = read_varint(data, offset)
        elif wire in (1, 2, 5):
            if wire == 2:
                size, offset = read_varint(data, offset)
            else:
                size = 8 if wire == 1 else 4
            end = offset + size
            if end > len(data):
                raise ProtocolError("truncated protobuf field")
            value, offset = data[offset:end], end
        else:
            raise ProtocolError(f"unsupported protobuf wire type {wire}")
        result[key] = value
    return result


def uint_field(key: int, value: int) -> bytes:
    return varint(key << 3) + varint(value)


def bytes_field(key: int, value: bytes) -> bytes:
    return varint((key << 3) | 2) + varint(len(value)) + value


def integer(values: dict, key: int, default: int | None = None) -> int | None:
    value = values.get(key, default)
    if value is not None and not isinstance(value, int):
        raise ProtocolError(f"field {key} must be an integer")
    return value


def nested(values: dict, key: int) -> dict:
    value = values[key]
    if not isinstance(value, bytes):
        raise ProtocolError(f"field {key} must be a submessage")
    return fields(value)


@dataclass
class State:
    ratio: float | None = None
    grade: float = 0
    grade_updated: float | None = None
    rider_kg: float = 75
    bike_kg: float = 10
    mode: str = "SIM"
    target_power: int | None = None
    wind: float = 0
    crr: float = .004
    cw: float = .51
    identity: str = "ftms56"
    display_name: str | None = None
    serial_id: str | None = None
    handshake: bool = False
    unknown_messages: int = 0
    notifications: list[tuple[bytes, bytes]] = field(default_factory=list)
    gearing: GearEstimate = field(default_factory=GearEstimate)

    @property
    def gear(self) -> int | None:
        return self.gearing.gear(self.ratio)

    def receive(self, data: bytes) -> str:
        """Return handshake/status/sim/erg/unknown. Validate before changing state."""
        # The last bytes are protocol major/minor versions, not a terminator.
        # Live Zwift announces 2.4; respond with our supported version 2.0.
        if data == b"RideOn\x02" or (len(data) == 8 and data[:7] == b"RideOn\x02"):
            self.handshake = True
            self.notifications.extend([
                (Z_TX, b"RideOn\x02\x00"),
                (Z_ASYNC, b"\x2a" + uint_field(1, 3) +
                 bytes_field(2, bytes_field(4, b"RIDE_ON(0)\x00"))),
            ])
            return "handshake"
        if not data:
            raise ProtocolError("empty Zwift message")
        if data[0] in (0, 0x41):
            request = fields(data[1:])
            requested = integer(request, 1, 0)
            if data[0] == 0x41 or requested == 0:
                # Use this bridge's identity, never a captured trainer serial.
                content = (uint_field(1, 512) + bytes_field(2, b"".join(varint(n) for n in (0, 1, 0))) +
                           bytes_field(3, (self.display_name or self.identity + " KICKR6").encode()) +
                           bytes_field(6, (self.serial_id or self.identity + "-kickr6").encode()) +
                           bytes_field(7, b"1") + bytes_field(8, uint_field(1, 1) + uint_field(2, 20)))
                # The older 0x41 identity query uses a nested content wrapper;
                # GET(0) returns the device page directly (as in QZ/Berg0162).
                payload = bytes_field(1, content) if data[0] == 0x41 else content
                answer = b"\x3c" + uint_field(1, 0) + bytes_field(2, payload)
                self.notifications.append((Z_TX, answer))
            elif requested == 520:
                content = uint_field(8, round((self.ratio or 0) * 10000))
                answer = b"\x3c" + uint_field(1, 520) + bytes_field(2, bytes_field(1, content))
                self.notifications.append((Z_TX, answer))
            else:
                self.unknown_messages += 1
                # A newer client may probe optional objects unknown to this
                # emulated protocol version. Return an empty, correlated page
                # instead of rejecting the characteristic write itself.
                answer = b"\x3c" + uint_field(1, requested) + bytes_field(2, b"")
                self.notifications.append((Z_TX, answer))
            return "status"
        if data[0] != 4:
            self.unknown_messages += 1
            return "unknown"
        values = fields(data[1:])
        pending = {}
        if 3 in values:
            power = integer(values, 3)
            if not 0 <= power <= 2000:
                raise ProtocolError("target power outside 0..2000 W")
            pending.update(target_power=power, mode="ERG")
        if 4 in values:
            params = nested(values, 4)
            for key, name, scale, lower, upper in [
                (1, "wind", 100, -32, 32), (2, "grade", 100, -100, 100),
                (3, "cw", 10000, 0, 2.55), (4, "crr", 100000, 0, .0255),
            ]:
                number = integer(params, key)
                if number is not None:
                    if key in (1, 2):
                        number = (number >> 1) ^ -(number & 1)
                    number /= scale
                    if not lower <= number <= upper:
                        raise ProtocolError(f"simulation {name} outside supported range")
                    pending[name] = number
        if 5 in values:
            params = nested(values, 5)
            ratio = integer(params, 2)
            bike, rider = integer(params, 4), integer(params, 5)
            if ratio is not None and ratio != 0 and not 1000 <= ratio <= 100000:
                raise ProtocolError("gear ratio outside 0.1..10")
            if bike is not None and not 100 <= bike <= 10000:
                raise ProtocolError("bike mass outside 1..100 kg")
            if rider is not None and not 1000 <= rider <= 30000:
                raise ProtocolError("rider mass outside 10..300 kg")
            if ratio is not None:
                pending["ratio"] = ratio / 10000 if ratio else None
            if bike is not None:
                pending["bike_kg"] = bike / 100
            if rider is not None:
                pending["rider_kg"] = rider / 100
            if 3 not in values:
                pending["mode"] = "SIM"
        if pending:
            for key, value in pending.items():
                setattr(self, key, value)
            if "ratio" in pending:
                self.gearing.update(self.ratio)
            if "grade" in pending:
                self.grade_updated = time.monotonic()
            return "erg" if 3 in values else "sim" if self.mode == "SIM" else "status"
        self.unknown_messages += 1
        return "unknown"


def riding_data(power: float, cadence: float) -> bytes:
    # Fields 3..6 remain unknown; do not invent measurements for them.
    return b"\x03" + uint_field(1, max(0, round(power))) + uint_field(2, max(0, round(cadence))) + b"".join(
        uint_field(key, 0) for key in range(3, 7))
