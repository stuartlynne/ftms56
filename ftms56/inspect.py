"""Bounded, read-only trainer inspection; never writes a control characteristic."""

import socket
import time
from collections import deque
from uuid import UUID

from .protocol import (BIKE_DATA, CHARACTERISTICS, DISCOVER, FEATURES, FTMS,
                       NOTIFY, READ, SUBSCRIBE, Decoder, Packet, ProtocolError,
                       characteristics, service_uuids)
from .telemetry import Telemetry


def inspect_trainer(host: str, port: int = 36866, timeout: float = 5, sample_seconds: float = 0) -> dict:
    result = dict(host=host, port=port, services=[], packets=[])
    decoder, sequence = Decoder(), 0
    pending = deque()
    telemetry = Telemetry()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        def packets_until(deadline):
            while time.monotonic() < deadline:
                if not pending:
                    sock.settimeout(max(.01, deadline - time.monotonic()))
                    data = sock.recv(65536)
                    if not data:
                        raise ProtocolError("trainer closed connection during inspection")
                    pending.extend(decoder.feed(data))
                    continue
                packet = pending.popleft()
                result["packets"].append(dict(direction="in", hex=packet.encode().hex()))
                if packet.op == NOTIFY:
                    telemetry.receive(packet.uuid, packet.value)
                yield packet

        def request(op, body=b""):
            nonlocal sequence
            sequence = (sequence + 1) & 255
            packet = Packet(op, sequence, body=body)
            result["packets"].append(dict(direction="out", hex=packet.encode().hex()))
            sock.sendall(packet.encode())
            for response in packets_until(time.monotonic() + timeout):
                if response.op == NOTIFY:
                    continue
                if response.seq != sequence or response.op != op:
                    raise ProtocolError("unexpected response during inspection")
                return response
            raise TimeoutError("inspection request timed out")

        response = request(DISCOVER)
        if response.code:
            raise ProtocolError(f"service discovery failed: {response.code}")
        ftms_chars = {}
        for service in service_uuids(response.body):
            entry = dict(uuid=str(UUID(bytes=service)), characteristics=[])
            result["services"].append(entry)
            response = request(CHARACTERISTICS, service)
            if response.code:
                entry["error"] = response.code
                continue
            chars = characteristics(response.body)
            if service == FTMS:
                ftms_chars = chars
            for uuid, flags in chars.items():
                characteristic = dict(uuid=str(UUID(bytes=uuid)), properties=flags)
                entry["characteristics"].append(characteristic)
                if flags & 1:
                    value = request(READ, uuid)
                    characteristic["read_code"] = value.code
                    characteristic["value_hex"] = value.value.hex() if not value.code else None
                    if not value.code and uuid == FEATURES and len(value.value) == 8:
                        machine = int.from_bytes(value.value[:4], "little")
                        target = int.from_bytes(value.value[4:], "little")
                        result["ftms"] = dict(machine_features=machine, target_features=target,
                                               resistance=bool(target & (1 << 2)),
                                               target_power=bool(target & (1 << 3)),
                                               simulation=bool(target & (1 << 13)))
        if sample_seconds:
            if BIKE_DATA not in ftms_chars:
                raise ProtocolError("trainer has no indoor-bike-data characteristic")
            response = request(SUBSCRIBE, BIKE_DATA + b"\x01")
            if response.code:
                raise ProtocolError("telemetry subscription refused")
            try:
                for _ in packets_until(time.monotonic() + sample_seconds):
                    pass
            except socket.timeout:
                pass
            result["telemetry"] = dict(power=telemetry.power, cadence=telemetry.cadence,
                                       speed_mps=telemetry.speed_mps, resistance=telemetry.resistance,
                                       received=telemetry.updated is not None)
    return result
