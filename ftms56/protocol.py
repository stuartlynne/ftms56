"""Direct Connect framing and the small subset of GATT used by the bridge.

Wire facts are documented in docs/protocol.md. UUIDs use network byte order;
characteristic values retain their Bluetooth-defined byte order.
"""

from dataclasses import dataclass
import struct
from uuid import UUID


def bluetooth_uuid(short: int) -> bytes:
    return UUID(f"{short:08x}-0000-1000-8000-00805f9b34fb").bytes


FTMS = bluetooth_uuid(0x1826)
FEATURES = bluetooth_uuid(0x2ACC)
CONTROL = bluetooth_uuid(0x2AD9)
BIKE_DATA = bluetooth_uuid(0x2AD2)
POWER_DATA = bluetooth_uuid(0x2A63)
POWER_RANGE = bluetooth_uuid(0x2AD8)
CYCLING_POWER = bluetooth_uuid(0x1818)
WAHOO_CONTROL = UUID("a026e005-0a7d-4ab3-97fa-f1500f9feb8b").bytes
ZWIFT = UUID("00000001-19ca-4651-86e5-fa29dcdd09d1").bytes
Z_ASYNC = UUID("00000002-19ca-4651-86e5-fa29dcdd09d1").bytes
Z_RX = UUID("00000003-19ca-4651-86e5-fa29dcdd09d1").bytes
Z_TX = UUID("00000004-19ca-4651-86e5-fa29dcdd09d1").bytes
Z_CHARACTERISTICS = {Z_ASYNC: 4, Z_RX: 2, Z_TX: 4}

DISCOVER, CHARACTERISTICS, READ, WRITE, SUBSCRIBE, NOTIFY = range(1, 7)
SUCCESS, UNKNOWN_MESSAGE, UNEXPECTED, NO_SERVICE, NO_CHARACTERISTIC, UNSUPPORTED, WRITE_FAILED, UNKNOWN_PROTOCOL = range(8)
HEADER = struct.Struct("!BBBBH")


class ProtocolError(ValueError):
    pass


def wahoo_circumference(metres: float) -> bytes:
    """Wahoo proprietary 0x48, unsigned little-endian tenths of millimetres."""
    if not 0.0001 <= metres <= 6.5535:
        raise ProtocolError("Wahoo circumference must be within 0.1..6553.5 mm")
    return b"\x48" + struct.pack("<H", round(metres * 10000))


@dataclass(frozen=True)
class Packet:
    op: int
    seq: int = 0
    code: int = 0
    body: bytes = b""
    version: int = 1

    def encode(self) -> bytes:
        return HEADER.pack(self.version, self.op, self.seq, self.code, len(self.body)) + self.body

    @property
    def uuid(self) -> bytes:
        if len(self.body) < 16:
            raise ProtocolError("characteristic/service UUID missing")
        return self.body[:16]

    @property
    def value(self) -> bytes:
        return self.body[16:]

    def reply(self, body: bytes | None = None, code: int = SUCCESS) -> "Packet":
        return Packet(self.op, self.seq, code, self.body[:16] if body is None else body)


class Decoder:
    """Incremental stream framing; handles arbitrary TCP fragmentation/coalescing."""

    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data: bytes) -> list[Packet]:
        self.buffer.extend(data)
        result = []
        while len(self.buffer) >= HEADER.size:
            version, op, seq, code, size = HEADER.unpack_from(self.buffer)
            if version != 1:
                raise ProtocolError(f"unsupported Direct Connect version {version}")
            end = HEADER.size + size
            if len(self.buffer) < end:
                break
            result.append(Packet(op, seq, code, bytes(self.buffer[HEADER.size:end]), version))
            del self.buffer[:end]
        return result


def service_uuids(body: bytes) -> list[bytes]:
    if len(body) % 16:
        raise ProtocolError("invalid service list length")
    return [body[i:i + 16] for i in range(0, len(body), 16)]


def characteristics(body: bytes) -> dict[bytes, int]:
    if len(body) < 16 or (len(body) - 16) % 17:
        raise ProtocolError("invalid characteristic list length")
    return {body[i:i + 16]: body[i + 16] for i in range(16, len(body), 17)}


def simulation(grade: float, wind: float = 0, crr: float = .004, cw: float = .51) -> bytes:
    """FTMS 0x11: wind m/s, grade percent, rolling coefficient, drag kg/m."""
    return b"\x11" + struct.pack("<hhBB", round(wind * 1000), round(grade * 100),
                                  round(crr * 10000), round(cw * 100))


def parse_simulation(value: bytes) -> tuple[float, float, float, float]:
    if len(value) != 7 or value[0] != 0x11:
        raise ProtocolError("invalid FTMS simulation command")
    wind, grade, crr, cw = struct.unpack("<hhBB", value[1:])
    return grade / 100, wind / 1000, crr / 10000, cw / 100
