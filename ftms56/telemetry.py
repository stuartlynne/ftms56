"""Read observed telemetry without rewriting measured power/cadence."""

from dataclasses import dataclass
import struct
import time

from .protocol import BIKE_DATA, POWER_DATA, ProtocolError


@dataclass
class Telemetry:
    power: float | None = None
    cadence: float | None = None
    speed_mps: float | None = None
    resistance: float | None = None
    updated: float | None = None
    speed_updated: float | None = None
    cadence_updated: float | None = None

    def receive(self, uuid: bytes, data: bytes, now: float | None = None):
        now = time.monotonic() if now is None else now
        if uuid == BIKE_DATA:
            if len(data) < 2:
                raise ProtocolError("short indoor bike data")
            flags, = struct.unpack_from("<H", data)
            offset = 2
            readings = {}
            # FTMS fields in wire order. Energy is a 5-byte group.
            layout = [(not flags & 1, "speed_mps", 2, "<H", 1 / 360),
                      (flags & 2, None, 2, None, 1),
                      (flags & 4, "cadence", 2, "<H", .5),
                      (flags & 8, None, 2, None, 1),
                      (flags & 16, None, 3, None, 1),
                      (flags & 32, "resistance", 2, "<h", .1),
                      (flags & 64, "power", 2, "<h", 1),
                      (flags & 128, None, 2, None, 1),
                      (flags & 256, None, 5, None, 1),
                      (flags & 512, None, 1, None, 1),
                      (flags & 1024, None, 1, None, 1),
                      (flags & 2048, None, 2, None, 1),
                      (flags & 4096, None, 2, None, 1)]
            for present, name, size, fmt, scale in layout:
                if present:
                    if offset + size > len(data):
                        raise ProtocolError("truncated indoor bike data field")
                    if name:
                        readings[name] = struct.unpack_from(fmt, data, offset)[0] * scale
                    offset += size
            for name, value in readings.items():
                setattr(self, name, value)
                if name in ("speed_mps", "cadence"):
                    setattr(self, name.removesuffix("_mps") + "_updated", now)
            self.updated = now
        elif uuid == POWER_DATA:
            if len(data) < 4:
                raise ProtocolError("short cycling power measurement")
            self.power = struct.unpack_from("<h", data, 2)[0]
            self.updated = now

    def speed(self, physical_ratio: float, circumference: float, now: float, max_age: float = 5) -> float | None:
        if self.speed_updated is not None and now - self.speed_updated <= max_age:
            return max(0, self.speed_mps)
        if self.cadence_updated is not None and now - self.cadence_updated <= max_age:
            return max(0, self.cadence) * physical_ratio * circumference / 60
        return None
