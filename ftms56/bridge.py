"""One selector loop owns all TCP sockets and serialized trainer transactions."""

from collections import deque
from dataclasses import asdict, dataclass, field
import errno
import importlib.resources
import ipaddress
import json
import logging
import math
import selectors
import socket
import struct
import time
from uuid import UUID
from urllib.parse import urlsplit

from . import __version__
from .avahi import Publisher, SERVICE
from .physics import Translation, equivalent_grade
from .protocol import (
    BIKE_DATA, CHARACTERISTICS, CONTROL, DISCOVER, FEATURES, FTMS, NOTIFY, POWER_RANGE,
    READ, SUBSCRIBE, UNEXPECTED, UNSUPPORTED, WRITE, WRITE_FAILED, Z_ASYNC,
    Z_CHARACTERISTICS, Z_RX, Z_TX, ZWIFT, Decoder, Packet, ProtocolError,
    characteristics, parse_simulation, service_uuids, simulation,
    CYCLING_POWER, WAHOO_CONTROL, wahoo_circumference,
)
from .telemetry import Telemetry
from .zwift import State, riding_data

LOG = logging.getLogger(__name__)
MAX_OUTPUT = 1024 * 1024
MAX_QUEUE = 128


@dataclass
class Config:
    trainer_host: str
    trainer_port: int = 36866
    ble_address: str | None = None
    ble_adapter: str = "hci0"
    ble_connect_timeout: float = 35
    bind: str = "0.0.0.0"
    http_port: int = 36865
    kickr6_port: int = 36866
    kickr5_port: int = 36867
    name: str = "ftms56"
    shifting_name: str | None = None
    shifting_id: str | None = None
    hide_kickr5: bool = False
    mdns: bool = True
    physical_ratio: float = 50 / 16
    circumference: float = 2.105
    shift_mode: str = "grade"
    shift_baseline_circumference: float = 2.0
    shift_baseline_ratio: float = 1.68
    trainer_mass_kg: float = 85
    zwift_difficulty: float = 50
    trainer_difficulty: float = 50
    min_grade: float = -20
    max_grade: float = 20
    request_timeout: float = 5
    heartbeat_interval: float = 10
    trace: str | None = None

    def __post_init__(self):
        # Do hostname lookup once in the CLI, never inside the service loop.
        ipaddress.IPv4Address(self.trainer_host)
        ipaddress.IPv4Address(self.bind)
        if self.ble_address is not None:
            try:
                parts = self.ble_address.split(":")
                if len(parts) != 6 or any(len(part) != 2 for part in parts) or len(bytes.fromhex("".join(parts))) != 6:
                    raise ValueError
            except ValueError:
                raise ValueError("BLE trainer must be a Bluetooth MAC address") from None
        if not math.isfinite(self.ble_connect_timeout) or self.ble_connect_timeout <= 0:
            raise ValueError("BLE connection timeout must be positive and finite")
        if self.shift_mode not in ("grade", "circumference"):
            raise ValueError("shift mode must be grade or circumference")
        if not math.isfinite(self.shift_baseline_ratio) or self.shift_baseline_ratio <= 0:
            raise ValueError("shift baseline ratio must be positive and finite")
        wahoo_circumference(self.shift_baseline_circumference)
        if any(not math.isfinite(x) or x <= 0 for x in (
                self.physical_ratio, self.circumference, self.trainer_mass_kg)):
            raise ValueError("physical ratio, circumference and trainer mass must be positive")
        if not -100 <= self.min_grade <= self.max_grade <= 100:
            raise ValueError("grade limits must be ordered within -100..100")
        if not math.isfinite(self.zwift_difficulty) or not 0 < self.zwift_difficulty <= 100:
            raise ValueError("Zwift difficulty baseline must be greater than 0 and at most 100 percent")
        if not math.isfinite(self.trainer_difficulty) or not 0 <= self.trainer_difficulty <= 100:
            raise ValueError("trainer difficulty must be within 0..100 percent")
        if any(not math.isfinite(x) or x <= 0 for x in (self.request_timeout, self.heartbeat_interval)):
            raise ValueError("timeouts must be positive")
        ports = [self.http_port, self.kickr6_port, self.kickr5_port]
        if len(set(ports)) != 3 or not all(0 < x <= 65535 for x in [*ports, self.trainer_port]):
            raise ValueError("listener ports must be distinct and within 1..65535")
        if not self.ble_address and self.trainer_host in ("127.0.0.1", self.bind) and self.trainer_port in ports:
            raise ValueError("upstream address would connect back to this bridge")


@dataclass(eq=False)
class Channel:
    sock: socket.socket
    kind: str
    profile: str | None = None
    decoder: Decoder = field(default_factory=Decoder)
    out: bytearray = field(default_factory=bytearray)
    incoming: bytearray = field(default_factory=bytearray)
    subscriptions: set = field(default_factory=set)
    connected: bool = True
    closing: bool = False
    last_read: float = field(default_factory=time.monotonic)
    opened: float = field(default_factory=time.monotonic)
    partial_since: float | None = None


@dataclass
class Request:
    packet: Packet
    client: Channel | None = None
    callback: object = None
    tag: str = ""
    seq: int = 0
    deadline: float = 0
    reply: Packet | None = None
    indication: bytes | None = None

    @property
    def cp(self):
        return self.packet.op == WRITE and self.packet.uuid == CONTROL

    @property
    def wahoo(self):
        return (self.client is None and self.packet.op == WRITE and
                self.packet.uuid == WAHOO_CONTROL and self.packet.value[:1] == b"\x48")


class Bridge:
    def __init__(self, config: Config):
        self.config = config
        self.selector = selectors.DefaultSelector()
        self.channels = set()
        self.clients = set()
        self.upstream = None
        self.ble_worker = None
        self.ble_waiting = False
        self.active_profile = None
        self.queue = deque()
        self.current = None
        self.sequence = 0
        self.ready = False
        self.shifting_available = False
        self.services = []
        self.ftms_chars = {}
        self.wahoo_chars = {}
        self.circumference_requested = None
        self.circumference_written = None
        self.circumference_updated = None
        self.circumference_error = None
        self.ratio_matches_table = None
        self.last_checked_ratio = None
        self.feature_value = b""
        self.power_range = (0, 2000, 1)
        self.internal_subscriptions = set()
        self.control_ready = False
        self.control_requested = False
        self.zwift = State(identity=config.name, display_name=config.shifting_name, serial_id=config.shifting_id)
        self.telemetry = Telemetry()
        self.wind, self.crr, self.cw = 0, .004, .51
        self.difficulty_percent = config.trainer_difficulty
        self.translation = None
        self.trainer_grade = None
        self.trainer_grade_updated = None
        self.next_connect = 0
        self.backoff = 1
        self.connect_deadline = 0
        self.next_heartbeat = 0
        self.last_translation = 0
        self.last_riding_data = 0
        self.stale_telemetry = False
        self.started = time.monotonic()
        self.stopping = False
        self.events = deque(maxlen=30)
        self.counters = dict(trainer_rx_bytes=0, trainer_tx_bytes=0, client_rx_bytes=0,
                             client_tx_bytes=0, trainer_rx_packets=0, client_rx_packets=0,
                             reconnects=0, rejected_clients=0, translated_commands=0)
        self.publishers = []
        self.trace = open(config.trace, "a", buffering=1) if config.trace else None
        self.html = importlib.resources.files("ftms56").joinpath("status.html").read_bytes()

    def event(self, message: str, level: int = logging.INFO):
        self.events.append(dict(time=time.strftime("%Y-%m-%d %H:%M:%S%z"), message=message,
                                level=logging.getLevelName(level)))
        LOG.log(level, message)

    def _trace(self, direction: str, packet: Packet, profile=None):
        if self.trace:
            self.trace.write(json.dumps(dict(time=time.time(), direction=direction, profile=profile,
                                            difficulty_percent=self.difficulty_percent,
                                            zwift_baseline_percent=self.config.zwift_difficulty,
                                            shift_method=self.config.shift_mode,
                                            shift_baseline_circumference_m=self.config.shift_baseline_circumference,
                                            shift_baseline_ratio=self.config.shift_baseline_ratio,
                                            physical_ratio=self.config.physical_ratio,
                                            upstream_transport="ble" if self.config.ble_address else "tcp",
                                            packet_hex=packet.encode().hex())) + "\n")

    def _register(self, sock, kind, profile=None, connected=True):
        sock.setblocking(False)
        ch = Channel(sock, kind, profile, connected=connected)
        self.channels.add(ch)
        self.selector.register(sock, selectors.EVENT_READ | (0 if connected else selectors.EVENT_WRITE), ch)
        return ch

    def _interest(self, ch):
        if ch not in self.channels:
            return
        events = selectors.EVENT_READ
        if ch.out or not ch.connected:
            events |= selectors.EVENT_WRITE
        self.selector.modify(ch.sock, events, ch)

    def _send(self, ch, data):
        if ch not in self.channels:
            return
        if isinstance(data, Packet):
            self._trace("trainer-out" if ch is self.upstream else "client-out", data, ch.profile)
            data = data.encode()
        if len(ch.out) + len(data) > MAX_OUTPUT:
            raise ProtocolError("slow peer exceeded 1 MiB output limit")
        ch.out.extend(data)
        self._interest(ch)

    @staticmethod
    def _tcp_options(sock):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for name, value in [("TCP_KEEPIDLE", 10), ("TCP_KEEPINTVL", 3), ("TCP_KEEPCNT", 3),
                            ("TCP_USER_TIMEOUT", 15000)]:
            option = getattr(socket, name, None)
            if option is not None:
                sock.setsockopt(socket.IPPROTO_TCP, option, value)

    def start(self):
        try:
            for kind, profile, port in [("http-listener", None, self.config.http_port),
                                         ("trainer-listener", "kickr5", self.config.kickr5_port),
                                         ("trainer-listener", "kickr6", self.config.kickr6_port)]:
                dual_stack = self.config.bind == "0.0.0.0" and socket.has_dualstack_ipv6()
                sock = socket.socket(socket.AF_INET6 if dual_stack else socket.AF_INET)
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    if dual_stack:
                        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                    sock.bind(("::" if dual_stack else self.config.bind, port))
                    sock.listen(16)
                    self._register(sock, kind, profile)
                except Exception:
                    sock.close()
                    raise
            self.event(f"HTTP on {self.config.bind}:{self.config.http_port}; connecting to trainer")
            self._connect()
        except Exception:
            self.close()
            raise

    def _connect(self):
        if self.config.ble_address:
            from .ble import BleWorker
            self.ble_waiting = True
            self.ble_worker = BleWorker(self.config.ble_address, self.config.ble_adapter, self.config.ble_connect_timeout)
            self.upstream = self._register(self.ble_worker.sock, "upstream")
            self.connect_deadline = time.monotonic() + self.config.ble_connect_timeout + 2
            self.event(f"Connecting to BLE trainer {self.config.ble_address} on {self.config.ble_adapter}")
            return
        sock = socket.socket()
        try:
            self._tcp_options(sock)
            sock.setblocking(False)
            result = sock.connect_ex((self.config.trainer_host, self.config.trainer_port))
            if result not in (0, errno.EINPROGRESS, errno.EWOULDBLOCK):
                raise OSError(result, "trainer connect")
            self.upstream = self._register(sock, "upstream", connected=False)
            self.connect_deadline = time.monotonic() + self.config.request_timeout
        except OSError as exc:
            sock.close()
            self._lost(f"trainer connection failed: {exc}")

    def _drop(self, ch):
        if ch not in self.channels:
            return
        self.selector.unregister(ch.sock)
        self.channels.remove(ch)
        ch.sock.close()
        if ch in self.clients:
            self.clients.remove(ch)
            self.queue = deque(r for r in self.queue if r.client is not ch)
            if not self.clients:
                self.active_profile = None
                if self.config.shift_mode == "circumference" and self.circumference_written is not None:
                    self.queue.clear()
                    self._set_circumference(self.config.shift_baseline_circumference,
                        after=lambda: self._lost("last Zwift connection closed; baseline restored", session_reset=True))
                else:
                    self._lost("last Zwift connection closed; resetting trainer session", session_reset=True)
            else:
                for uuid in ch.subscriptions - self.internal_subscriptions:
                    if not any(uuid in c.subscriptions for c in self.clients):
                        self._enqueue(Packet(SUBSCRIBE, body=uuid + b"\x00"))

    def _lost(self, reason, session_reset=False):
        if self.stopping:
            return
        self.event(reason, logging.INFO if session_reset else logging.WARNING)
        # Detach clients before closing so _drop cannot recursively reset.
        clients, self.clients = self.clients, set()
        for ch in clients:
            self._drop(ch)
        if self.upstream:
            self._drop(self.upstream)
        if self.ble_worker:
            self.ble_worker.close()
            self.ble_worker = None
        self.ble_waiting = False
        self.upstream = None
        self.ready = self.shifting_available = self.control_ready = self.control_requested = False
        self.active_profile = None
        self.queue.clear()
        self.current = None
        self.internal_subscriptions.clear()
        self.services = []
        self.ftms_chars = {}
        self.wahoo_chars = {}
        # A lost TCP link does not establish that the trainer reset its setting.
        if (self.circumference_written is not None and
                self.circumference_written != self.config.shift_baseline_circumference):
            self.event("Circumference restoration unconfirmed after connection loss; baseline will be written on reconnect", logging.WARNING)
        self.circumference_written = None
        self.circumference_requested = None
        self.circumference_updated = None
        self.last_checked_ratio = None
        self.ratio_matches_table = None
        self.feature_value = b""
        self.power_range = (0, 2000, 1)
        self.telemetry = Telemetry()
        self.zwift = State(identity=self.config.name, display_name=self.config.shifting_name, serial_id=self.config.shifting_id)
        self.translation = None
        self.trainer_grade = None
        self.trainer_grade_updated = None
        self.stale_telemetry = False
        self.wind, self.crr, self.cw = 0, .004, .51
        self._unpublish()
        now = time.monotonic()
        # A live KICKR accepted an immediate reconnect but then ignored its
        # discovery request. Allow its previous connection to drain/release.
        self.next_connect = now + (1 if session_reset else self.backoff)
        self.backoff = min(self.backoff * 2, 30)

    def _enqueue(self, packet, client=None, callback=None, tag=""):
        request = Request(packet, client, callback, tag)
        if tag in ("translation", "circumference"):
            self.queue = deque(r for r in self.queue if r.tag != tag)
        if len(self.queue) >= MAX_QUEUE:
            raise ProtocolError("trainer request queue full")
        self.queue.append(request)

    def _pump(self, now):
        if self.current is not None or not self.queue or not self.upstream or not self.upstream.connected or self.ble_waiting:
            return
        self.current = self.queue.popleft()
        self.sequence = (self.sequence + 1) & 255
        self.current.seq = self.sequence
        self.current.deadline = now + self.config.request_timeout
        p = self.current.packet
        self._send(self.upstream, Packet(p.op, self.sequence, p.code, p.body))

    def _finish(self):
        request, self.current = self.current, None
        reply = request.reply
        if (not reply.code and request.cp and request.indication is not None and
                request.indication[2] == 1 and request.packet.value[0] == 0x11):
            self.trainer_grade = parse_simulation(request.packet.value)[0]
            self.trainer_grade_updated = time.monotonic()
        if request.client in self.clients:
            self._send(request.client, Packet(reply.op, request.packet.seq, reply.code, reply.body))
        if request.callback:
            request.callback(reply, request.indication)

    def _bootstrap(self):
        def got_services(packet, _):
            if packet.code:
                raise ProtocolError("trainer service discovery failed")
            self.services = service_uuids(packet.body)
            if ZWIFT in self.services:
                raise ProtocolError("upstream already exposes Zwift shifting; select the KICKR5, not another bridge")
            if self.config.shift_mode == "circumference":
                if CYCLING_POWER not in self.services:
                    raise ProtocolError("circumference mode requires Cycling Power service")
                self._enqueue(Packet(CHARACTERISTICS, body=CYCLING_POWER), callback=got_wahoo_chars)
            if FTMS in self.services:
                self._enqueue(Packet(CHARACTERISTICS, body=FTMS), callback=got_chars)
            else:
                self._ready()

        def got_wahoo_chars(packet, _):
            if packet.code:
                raise ProtocolError("trainer Wahoo characteristic discovery failed")
            self.wahoo_chars = characteristics(packet.body)
            if self.wahoo_chars.get(WAHOO_CONTROL, 0) & 6 != 6:
                raise ProtocolError("trainer does not expose Wahoo circumference write/result control")
            if self.wahoo_chars[WAHOO_CONTROL] & 4:
                self.internal_subscriptions.add(WAHOO_CONTROL)
                def subscribed(reply, _):
                    if reply.code:
                        raise ProtocolError("trainer rejected Wahoo result subscription")
                self._enqueue(Packet(SUBSCRIBE, body=WAHOO_CONTROL + b"\x01"), callback=subscribed)
            # Write a known baseline before advertising a ready test session.
            self._set_circumference(self.config.shift_baseline_circumference)

        def got_chars(packet, _):
            if packet.code:
                raise ProtocolError("trainer FTMS discovery failed")
            self.ftms_chars = characteristics(packet.body)
            if FEATURES in self.ftms_chars:
                self._enqueue(Packet(READ, body=FEATURES), callback=got_features)
            else:
                self._ready()

        def got_features(packet, _):
            if packet.code or packet.uuid != FEATURES or len(packet.value) != 8:
                raise ProtocolError("invalid FTMS feature response")
            self.feature_value = packet.value
            target = int.from_bytes(packet.value[4:], "little")
            self.shifting_available = bool(target & (1 << 13) and
                                            self.ftms_chars.get(CONTROL, 0) & 2 and
                                            self.ftms_chars.get(CONTROL, 0) & 4 and
                                            self.ftms_chars.get(BIKE_DATA, 0) & 4)
            if POWER_RANGE in self.ftms_chars:
                self._enqueue(Packet(READ, body=POWER_RANGE), callback=got_power_range)
            else:
                self._ready()

        def got_power_range(packet, _):
            if not packet.code and len(packet.value) == 6:
                self.power_range = struct.unpack("<hhH", packet.value)
            self._ready()

        self._enqueue(Packet(DISCOVER), callback=got_services)

    def _ready(self):
        if self.config.shift_mode == "circumference" and self.circumference_written is None:
            raise ProtocolError("circumference baseline was not acknowledged during discovery")
        self.ready = True
        self.backoff = 1
        self.next_heartbeat = time.monotonic() + self.config.heartbeat_interval
        self.event("Trainer ready; virtual shifting " + ("available (experimental)" if self.shifting_available else "unavailable"))
        self._publish()

    def _publish(self):
        if not self.config.mdns:
            return
        self._unpublish()
        try:
            for profile, port in [("kickr5", self.config.kickr5_port), ("kickr6", self.config.kickr6_port)]:
                if profile == "kickr5" and self.config.hide_kickr5:
                    continue
                if profile == "kickr6" and not self.shifting_available:
                    continue
                services = [u for u in self.services if u in (FTMS, bytes.fromhex("0000181800001000800000805f9b34fb"))]
                if profile == "kickr6":
                    services.append(ZWIFT)
                uuids = ",".join("0x" + UUID(bytes=u).hex[4:8] if u[4:] == FTMS[4:] else str(UUID(bytes=u)) for u in services)
                # Stable locally administered virtual MACs, distinct from trainer and each other.
                import hashlib
                identity_override = self.config.shifting_id if profile == "kickr6" else None
                suffix = hashlib.sha256((identity_override or self.config.name + profile).encode()).digest()[:5]
                mac = "-".join(f"{x:02X}" for x in b"\x02" + suffix)
                display_name = (self.config.shifting_name if profile == "kickr6" else None) or f"{self.config.name} {profile.upper()}"
                self.publishers.append(Publisher(display_name, SERVICE, port,
                                                  [f"serial-number={identity_override or self.config.name + '-' + profile}",
                                                   f"mac-address={mac}", f"ble-service-uuids={uuids}"]))
            self.publishers.append(Publisher(f"{self.config.name} status", "_http._tcp", self.config.http_port, ["path=/"]))
        except RuntimeError as exc:
            self.event(str(exc), logging.ERROR)

    def _unpublish(self):
        for publisher in self.publishers:
            publisher.close()
        self.publishers.clear()

    def _accept(self, listener):
        sock, address = listener.sock.accept()
        if listener.kind == "http-listener":
            if sum(ch.kind == "http" for ch in self.channels) >= 16:
                sock.close()
            else:
                self._register(sock, "http")
            return
        if not self.ready or (listener.profile == "kickr6" and not self.shifting_available) or (
                self.active_profile is not None and self.active_profile != listener.profile) or len(self.clients) >= 4:
            self.counters["rejected_clients"] += 1
            sock.close()
            return
        self._tcp_options(sock)
        client = self._register(sock, "client", listener.profile)
        self.clients.add(client)
        self.active_profile = listener.profile
        if listener.profile == "kickr6":
            self._ensure_subscriptions((BIKE_DATA,))
        self.event(f"{listener.profile} client connected from {address[0]}")

    def _ensure_subscriptions(self, uuids):
        def subscribed(reply, _):
            if reply.code:
                raise ProtocolError("trainer rejected internal telemetry/control subscription")

        for uuid in uuids:
            if uuid not in self.ftms_chars:
                raise ProtocolError("required FTMS characteristic missing")
            if uuid not in self.internal_subscriptions:
                self.internal_subscriptions.add(uuid)
                self._enqueue(Packet(SUBSCRIBE, body=uuid + b"\x01"), callback=subscribed)

    def _ensure_control(self):
        self._ensure_subscriptions((CONTROL, BIKE_DATA))
        if self.control_ready or self.control_requested:
            return
        self.control_requested = True

        def controlled(reply, indication):
            if reply.code or indication is None or indication[2] != 1:
                raise ProtocolError("trainer denied FTMS control")
            self.control_ready = True

        self._enqueue(Packet(WRITE, body=CONTROL + b"\x00"), callback=controlled, tag="request-control")

    def _translate(self, now):
        if self.active_profile != "kickr6" or self.zwift.mode != "SIM":
            return
        if now - self.last_translation < .25:
            return
        if self.config.shift_mode == "circumference":
            self._ensure_control()
            if self.zwift.ratio is not None:
                self._apply_ratio()
            grade = max(self.config.min_grade, min(self.config.max_grade, self._effective_grade()))
            self.translation = None
            self._enqueue(Packet(WRITE, body=CONTROL + simulation(grade, self.wind, self.crr, self.cw)),
                          callback=self._translated, tag="translation")
            self.last_translation = now
            return
        speed = self.telemetry.speed(self.config.physical_ratio, self.config.circumference, now)
        if speed is None and self.zwift.ratio is not None:
            if self.translation is not None and not self.stale_telemetry and self.control_ready:
                self.event("Speed/cadence telemetry stale; requesting neutral simulation until it returns", logging.WARNING)
                self._enqueue(Packet(WRITE, body=CONTROL + simulation(0, self.wind, self.crr, self.cw)),
                              callback=self._translated, tag="translation")
                self.stale_telemetry = True
                self.translation = None
            return
        if self.stale_telemetry:
            self.event("Speed/cadence telemetry recovered")
            self.stale_telemetry = False
        self._ensure_control()
        self.translation = self._calculate_translation(speed or 0)
        self._enqueue(Packet(WRITE, body=CONTROL + simulation(self.translation.grade, self.wind, self.crr, self.cw)),
                      callback=self._translated, tag="translation")
        self.last_translation = now

    def _effective_grade(self):
        return self.zwift.grade * self.difficulty_percent / self.config.zwift_difficulty

    def _calculate_translation(self, speed):
        grade = self._effective_grade()
        if self.config.shift_mode == "circumference":
            output = max(self.config.min_grade, min(self.config.max_grade, grade))
            return Translation(output, grade, output != grade, 1)
        if self.zwift.ratio is None:
            output = max(self.config.min_grade, min(self.config.max_grade, grade))
            return Translation(output, grade, output != grade, 1)
        return equivalent_grade(
            grade, self.zwift.ratio, self.config.physical_ratio, speed,
            self.zwift.rider_kg + self.zwift.bike_kg, self.config.trainer_mass_kg,
            self.wind, self.crr, self.cw, self.config.min_grade, self.config.max_grade)

    def set_difficulty(self, percent):
        if (isinstance(percent, bool) or not isinstance(percent, (int, float)) or
                not math.isfinite(percent) or not 0 <= percent <= 100):
            raise ValueError("percent must be a finite number within 0..100")
        if percent != self.difficulty_percent:
            self.difficulty_percent = float(percent)
            self.last_translation = 0
            self.queue = deque(r for r in self.queue if r.tag != "translation")
            self.event(f"GUI trainer difficulty set to {percent:g}% (Zwift baseline {self.config.zwift_difficulty:g}%)")

    def _set_circumference(self, metres, after=None):
        value = wahoo_circumference(metres)
        metres = struct.unpack("<H", value[1:])[0] / 10000
        self.circumference_requested = metres

        def written(reply, _):
            if reply.code:
                self.circumference_error = "Trainer rejected circumference write"
                raise ProtocolError(self.circumference_error)
            self.circumference_written = metres
            self.circumference_updated = time.monotonic()
            self.circumference_error = None
            self.event(f"Wahoo circumference write acknowledged: {metres * 1000:.1f} mm")
            if after:
                after()

        self._enqueue(Packet(WRITE, body=WAHOO_CONTROL + value), callback=written, tag="circumference")

    def _apply_ratio(self):
        ratio = self.zwift.ratio
        if ratio != self.last_checked_ratio:
            self.last_checked_ratio = ratio
            estimate = self.zwift.gearing.status(ratio)
            self.ratio_matches_table = estimate["ratio_matches"]
            self.event(f"Virtual ratio {ratio:g}; community table " +
                       (f"{estimate['preset']} gear {estimate['gear_label']} ({estimate['confidence']})"
                        if self.ratio_matches_table else "MISMATCH"),
                       logging.INFO if self.ratio_matches_table else logging.WARNING)
        metres = self.config.shift_baseline_circumference * ratio / self.config.shift_baseline_ratio
        try:
            value = wahoo_circumference(metres)
        except ProtocolError as exc:
            message = f"Ratio {ratio:g}: {exc}; retaining last circumference"
            if message != self.circumference_error:
                self.event(message, logging.WARNING)
            self.circumference_error = message
            return
        metres = struct.unpack("<H", value[1:])[0] / 10000
        if metres != self.circumference_requested:
            self._set_circumference(metres)

    def _restore_baseline(self):
        if self.config.shift_mode == "circumference" and self.circumference_requested is not None:
            self.queue = deque(r for r in self.queue if r.tag != "circumference")
            self._set_circumference(self.config.shift_baseline_circumference)

    def _translated(self, reply, indication):
        if reply.code or indication is None or indication[2] != 1:
            raise ProtocolError("trainer rejected translated simulation command")
        self.counters["translated_commands"] += 1

    def _client_packet(self, client, packet):
        self.counters["client_rx_packets"] += 1
        self._trace("client-in", packet, client.profile)
        if packet.code or packet.op not in range(1, 6):
            self._send(client, packet.reply(b"", code=UNSUPPORTED))
            return
        if packet.op == DISCOVER:
            if packet.body:
                raise ProtocolError("discovery request must have no body")
        else:
            uuid = packet.uuid
            if packet.op in (CHARACTERISTICS, READ) and len(packet.body) != 16:
                raise ProtocolError("invalid discovery/read request length")
            if packet.op == SUBSCRIBE and (len(packet.value) != 1 or packet.value[0] not in (0, 1)):
                raise ProtocolError("invalid subscription request")
            if packet.op == WRITE and not packet.value:
                raise ProtocolError("empty characteristic write")
            if client.profile == "kickr6" and (uuid == ZWIFT or uuid in Z_CHARACTERISTICS):
                self._local(client, packet)
                return
        if packet.op == SUBSCRIBE:
            uuid = packet.uuid
            previous = uuid in client.subscriptions
            if packet.value == b"\x01":
                client.subscriptions.add(uuid)
            else:
                client.subscriptions.discard(uuid)
            enabled = uuid in self.internal_subscriptions or any(uuid in c.subscriptions for c in self.clients)

            def subscription_done(reply, _):
                if reply.code and client in self.clients:
                    if previous:
                        client.subscriptions.add(uuid)
                    else:
                        client.subscriptions.discard(uuid)

            self._enqueue(Packet(SUBSCRIBE, packet.seq, body=uuid + bytes([enabled])), client, subscription_done)
            return
        if packet.op == WRITE and packet.uuid == CONTROL:
            value = packet.value
            # Indications must be enabled before any FTMS control transaction.
            if CONTROL not in self.internal_subscriptions:
                self.internal_subscriptions.add(CONTROL)
                self._enqueue(Packet(SUBSCRIBE, body=CONTROL + b"\x01"))
            if client.profile == "kickr6":
                if value[0] == 0x11:
                    grade, self.wind, self.crr, self.cw = parse_simulation(value)
                    self.zwift.grade, self.zwift.mode = grade, "SIM"
                    self.zwift.grade_updated = time.monotonic()
                    self.zwift.wind, self.zwift.crr, self.zwift.cw = self.wind, self.crr, self.cw
                    speed = 0
                    if self.config.shift_mode == "circumference" and self.zwift.ratio is not None:
                        self._apply_ratio()
                    elif self.zwift.ratio is not None:
                        speed = self.telemetry.speed(self.config.physical_ratio, self.config.circumference, time.monotonic())
                        if speed is None:
                            self._send(client, packet.reply(code=WRITE_FAILED))
                            return
                    self.translation = self._calculate_translation(speed)
                    packet = Packet(WRITE, packet.seq, body=CONTROL + simulation(
                        self.translation.grade, self.wind, self.crr, self.cw))
                elif value[0] == 5:
                    self.zwift.mode = "ERG"
                    if len(value) == 3:
                        self.zwift.target_power = struct.unpack("<h", value[1:])[0]
                    self.queue = deque(r for r in self.queue if r.tag != "translation")
                    self._restore_baseline()
                elif value[0] in (1, 4, 8):
                    self.zwift.ratio = None
                    self.zwift.mode = "MANUAL"
                    self.queue = deque(r for r in self.queue if r.tag != "translation")
                    self._restore_baseline()
        self._enqueue(packet, client)

    def _local(self, client, packet):
        uuid = packet.uuid
        if packet.op == CHARACTERISTICS and uuid == ZWIFT:
            body = ZWIFT + b"".join(u + bytes([flags]) for u, flags in Z_CHARACTERISTICS.items())
            self._send(client, packet.reply(body))
        elif packet.op == SUBSCRIBE and uuid in (Z_ASYNC, Z_TX):
            if packet.value == b"\x01":
                client.subscriptions.add(uuid)
            else:
                client.subscriptions.discard(uuid)
            self._send(client, packet.reply())
        elif packet.op == WRITE and uuid == Z_RX:
            try:
                previous_unknown = self.zwift.unknown_messages
                action = self.zwift.receive(packet.value)
                if action == "status" and self.zwift.unknown_messages > previous_unknown:
                    self.event("Unsupported optional Zwift GET; returned empty status: " + packet.value.hex(), logging.WARNING)
                self.wind, self.crr, self.cw = self.zwift.wind, self.zwift.crr, self.zwift.cw
                if action == "unknown":
                    self.event("Unrecognized Zwift command: " + packet.value.hex(), logging.WARNING)
                    self._send(client, packet.reply(code=UNSUPPORTED))
                    return
                if action in ("sim", "erg"):
                    self._ensure_control()
                if action == "sim":
                    if self.config.shift_mode == "circumference" and self.zwift.ratio is not None:
                        self._apply_ratio()
                    elif (self.config.shift_mode == "circumference" and
                          self.circumference_requested != round(self.config.shift_baseline_circumference, 4)):
                        self._restore_baseline()
                    self._translate(time.monotonic())
                elif action == "erg":
                    self.queue = deque(r for r in self.queue if r.tag != "translation")
                    self._restore_baseline()
                    low, high, increment = self.power_range
                    target = self.zwift.target_power
                    if not low <= target <= high:
                        raise ProtocolError("ERG target outside trainer supported power range")
                    self._enqueue(Packet(WRITE, body=CONTROL + b"\x05" + struct.pack("<h", target)),
                                  callback=self._translated, tag="erg")
                self._send(client, packet.reply())
                for characteristic, value in self.zwift.notifications:
                    self._notify(characteristic, value)
                self.zwift.notifications.clear()
            except ProtocolError as exc:
                self.event(str(exc), logging.WARNING)
                self._send(client, packet.reply(code=WRITE_FAILED))
        else:
            self._send(client, packet.reply(code=UNSUPPORTED))

    def _notify(self, uuid, value):
        for client in list(self.clients):
            if uuid in client.subscriptions:
                try:
                    self._send(client, Packet(NOTIFY, body=uuid + value))
                except ProtocolError as exc:
                    self.event(f"Dropping slow client: {exc}", logging.WARNING)
                    self._drop(client)

    def _trainer_packet(self, packet):
        self.counters["trainer_rx_packets"] += 1
        self._trace("trainer-in", packet)
        if packet.op == NOTIFY:
            uuid = packet.uuid
            if uuid == WAHOO_CONTROL and self.config.shift_mode == "circumference":
                self.event("Wahoo control notification: " + packet.value.hex())
            try:
                self.telemetry.receive(uuid, packet.value)
            except ProtocolError as exc:
                self.event(str(exc), logging.WARNING)
            r = self.current
            private = False
            if uuid == CONTROL and r is not None and r.cp:
                if len(packet.value) != 3 or packet.value[:2] != bytes([0x80, r.packet.value[0]]):
                    raise ProtocolError("unexpected FTMS control indication")
                r.indication = packet.value
                private = r.client is None
                if packet.value[2] == 1 and r.packet.value[0] == 0:
                    self.control_ready = True
                if packet.value[2] == 5:
                    self.control_ready = self.control_requested = False
                if r.reply is not None:
                    self._finish()
            elif uuid == WAHOO_CONTROL and r is not None and r.wahoo:
                # Observed KICKR5 response: 01 48 01 00 <uint16 circumference>.
                expected = b"\x01\x48\x01\x00" + r.packet.value[1:]
                if packet.value != expected:
                    raise ProtocolError("Wahoo circumference result rejected or did not echo requested value")
                r.indication = packet.value
                private = r.client is None
                if r.reply is not None:
                    self._finish()
            if not private:
                self._notify(uuid, packet.value)
            return
        r = self.current
        if r is None or packet.seq != r.seq or packet.op != r.packet.op:
            raise ProtocolError("trainer response does not match outstanding request")
        if packet.code == 0 and packet.op != DISCOVER and packet.uuid != r.packet.uuid:
            raise ProtocolError("trainer response UUID does not match request")
        if r.client is not None and r.client.profile == "kickr6" and packet.op == DISCOVER and not packet.code:
            packet = Packet(packet.op, packet.seq, body=packet.body + ZWIFT)
        r.reply = packet
        if packet.code or not (r.cp or r.wahoo) or r.indication is not None:
            self._finish()

    def status(self):
        now = time.monotonic()
        telemetry = asdict(self.telemetry)
        for key in ("updated", "speed_updated", "cadence_updated"):
            value = telemetry.pop(key)
            telemetry[key + "_age_s"] = None if value is None else round(now - value, 2)
        profile_connections = {profile: sum(c.profile == profile for c in self.clients)
                               for profile in ("kickr5", "kickr6")}
        upstream_connected = bool(self.upstream and self.upstream.connected and not self.ble_waiting)
        upstream_address = (f"BLE {self.config.ble_address} ({self.config.ble_adapter})" if self.config.ble_address else
                            f"{self.config.trainer_host}:{self.config.trainer_port}")
        upstream_state = ("Connected" if self.ready else "Discovering services" if upstream_connected
                          else "Connecting" if self.upstream else "Disconnected; retrying")
        return dict(version=__version__, uptime_s=round(now - self.started),
                    trainer=upstream_address, upstream_transport="ble" if self.config.ble_address else "tcp",
                    ready=self.ready, connected=upstream_connected,
                    active_profile=self.active_profile, clients=len(self.clients),
                    links=dict(wahoo=dict(state=upstream_state, connected=upstream_connected,
                                          ready=self.ready, address=upstream_address),
                               **{profile: dict(connected=count > 0, clients=count,
                                                port=getattr(self.config, profile + "_port"),
                                                advertised=self.config.mdns and self.ready and
                                                    (not self.config.hide_kickr5 if profile == "kickr5" else self.shifting_available),
                                                name=(self.config.shifting_name if profile == "kickr6" else None) or
                                                     f"{self.config.name} {profile.upper()}")
                                  for profile, count in profile_connections.items()}),
                    shifting_available=self.shifting_available, shifting_experimental=True,
                    control_ready=self.control_ready, mode=self.zwift.mode,
                    gear=self.zwift.gear, ratio=self.zwift.ratio, requested_grade=self.zwift.grade,
                    gearing=self.zwift.gearing.status(self.zwift.ratio),
                    zwift_simulation_grade=self.zwift.grade if self.zwift.grade_updated is not None else None,
                    difficulty=dict(percent=self.difficulty_percent, zwift_baseline_percent=self.config.zwift_difficulty),
                    effective_simulation_grade=self._effective_grade() if self.zwift.grade_updated is not None else None,
                    zwift_grade_age_s=None if self.zwift.grade_updated is None else round(now - self.zwift.grade_updated, 2),
                    trainer_grade=self.trainer_grade,
                    trainer_grade_age_s=None if self.trainer_grade_updated is None else round(now - self.trainer_grade_updated, 2),
                    target_power=self.zwift.target_power, unknown_zwift_messages=self.zwift.unknown_messages,
                    simulation=dict(wind_mps=self.wind, crr=self.crr, cw=self.cw),
                    translation=asdict(self.translation) if self.translation else None,
                    stale_telemetry=self.stale_telemetry,
                    physical_ratio=self.config.physical_ratio, trainer_mass_kg=self.config.trainer_mass_kg,
                    shifting=dict(method=self.config.shift_mode,
                                  baseline_circumference_mm=self.config.shift_baseline_circumference * 1000,
                                  baseline_ratio=self.config.shift_baseline_ratio,
                                  requested_circumference_mm=None if self.circumference_requested is None else self.circumference_requested * 1000,
                                  written_circumference_mm=None if self.circumference_written is None else self.circumference_written * 1000,
                                  write_age_s=None if self.circumference_updated is None else round(now - self.circumference_updated, 2),
                                  ratio_matches_table=self.ratio_matches_table,
                                  error=self.circumference_error),
                    telemetry=telemetry, queue_depth=len(self.queue), counters=self.counters,
                    services=[str(UUID(bytes=u)) for u in self.services],
                    feature_hex=self.feature_value.hex(), events=list(self.events),
                    mdns=[dict(name=p.command[1], established=p.established) for p in self.publishers])

    def _http(self, ch, data):
        ch.incoming.extend(data)
        if len(ch.incoming) > 8192:
            self._drop(ch)
            return
        if b"\r\n\r\n" not in ch.incoming:
            return
        raw_headers, raw_body = bytes(ch.incoming).split(b"\r\n\r\n", 1)
        line = raw_headers.split(b"\r\n", 1)[0].split()
        if len(line) != 3:
            self._drop(ch)
            return
        method, path, _ = line
        headers = {}
        for row in raw_headers.split(b"\r\n")[1:]:
            name, sep, value = row.partition(b":")
            if not sep or name.lower() in headers:
                self._http_reply(ch, "400 Bad Request", "text/plain", b"Invalid headers\n")
                return
            headers[name.lower()] = value.strip().decode("latin-1")
        try:
            length = int(headers.get(b"content-length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > 1024 or b"transfer-encoding" in headers:
            self._http_reply(ch, "400 Bad Request", "text/plain", b"Invalid body length\n")
            return
        if len(raw_body) < length:
            return
        code, mime = "200 OK", "text/html; charset=utf-8"
        if method == b"POST" and path == b"/api/difficulty":
            mime = "application/json"
            origin = headers.get(b"origin")
            if origin and urlsplit(origin).netloc.lower() != headers.get(b"host", "").lower():
                code, body = "403 Forbidden", b'{"error":"Origin does not match host"}'
            elif headers.get(b"content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                code, body = "415 Unsupported Media Type", b'{"error":"Use application/json"}'
            else:
                try:
                    settings = json.loads(raw_body[:length])
                    if not isinstance(settings, dict) or set(settings) != {"percent"}:
                        raise ValueError("Expected an object containing percent")
                    self.set_difficulty(settings["percent"])
                    body = json.dumps(self.status()["difficulty"]).encode()
                except (ValueError, UnicodeDecodeError) as exc:
                    code, body = "400 Bad Request", json.dumps(dict(error=str(exc))).encode()
        elif method != b"GET":
            code, body = "405 Method Not Allowed", b"Method not allowed\n"
        elif path == b"/":
            body = self.html
        elif path == b"/api/status":
            mime, body = "application/json", json.dumps(self.status()).encode()
        elif path == b"/healthz":
            code = "200 OK" if self.ready else "503 Service Unavailable"
            mime, body = "text/plain", b"ready\n" if self.ready else b"trainer unavailable\n"
        else:
            code, body = "404 Not Found", b"Not found\n"
        self._http_reply(ch, code, mime, body)

    def _http_reply(self, ch, code, mime, body):
        header = f"HTTP/1.1 {code}\r\nContent-Type: {mime}\r\nContent-Length: {len(body)}\r\nConnection: close\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n\r\n"
        self._send(ch, header.encode() + body)
        ch.closing = True

    def _read(self, ch):
        data = ch.sock.recv(65536)
        if not data:
            if ch is self.upstream:
                self._lost("trainer closed the connection")
            else:
                self._drop(ch)
            return
        ch.last_read = time.monotonic()
        if ch.kind == "http":
            if not ch.closing:
                self._http(ch, data)
            return
        counter = "trainer_rx_bytes" if ch is self.upstream else "client_rx_bytes"
        self.counters[counter] += len(data)
        was_partial = bool(ch.decoder.buffer)
        packets = ch.decoder.feed(data)
        if ch.decoder.buffer:
            if not was_partial or packets:
                ch.partial_since = time.monotonic()
        else:
            ch.partial_since = None
        for packet in packets:
            if ch not in self.channels:
                break
            if ch is self.upstream:
                if self.config.ble_address and packet.op == 0:
                    from .ble import READY
                    if packet.code or packet.body != READY or not self.ble_waiting:
                        raise ProtocolError("BLE helper: " + packet.body.decode(errors="replace"))
                    self.ble_waiting = False
                    self.counters["reconnects"] += 1
                    self.event("Connected to BLE trainer; discovering GATT services")
                    self._bootstrap()
                    continue
                self._trainer_packet(packet)
            else:
                self._client_packet(ch, packet)

    def _write(self, ch):
        if not ch.connected:
            error = ch.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if error:
                raise OSError(error, "trainer connect")
            ch.connected = True
            self.counters["reconnects"] += 1
            self.event("Connected to trainer; discovering FTMS capabilities")
            self._bootstrap()
        if ch.out:
            size = ch.sock.send(ch.out)
            if not size:
                raise OSError("socket write returned zero")
            del ch.out[:size]
            if ch.kind != "http":
                counter = "trainer_tx_bytes" if ch is self.upstream else "client_tx_bytes"
                self.counters[counter] += size
        if not ch.out and ch.closing:
            self._drop(ch)
        else:
            self._interest(ch)

    def step(self, timeout=.25):
        for key, mask in self.selector.select(timeout):
            if self.stopping:
                return
            ch = key.data
            if ch not in self.channels:
                continue
            try:
                if ch.kind.endswith("listener"):
                    self._accept(ch)
                    continue
                # Complete nonblocking connect before attempting recv().
                if mask & selectors.EVENT_WRITE:
                    self._write(ch)
                if ch in self.channels and ch.connected and mask & selectors.EVENT_READ:
                    self._read(ch)
            except (BlockingIOError, InterruptedError):
                pass
            except (OSError, ProtocolError, ValueError) as exc:
                if ch is self.upstream:
                    self._lost(f"trainer error: {exc}")
                else:
                    self.event(f"{ch.kind} error: {exc}", logging.WARNING)
                    self._drop(ch)
        if self.stopping:
            return
        now = time.monotonic()
        try:
            if self.upstream is None and now >= self.next_connect:
                self._connect()
            if self.upstream and (not self.upstream.connected or self.ble_waiting) and now >= self.connect_deadline:
                self._lost("trainer connection timed out")
            if self.current and now >= self.current.deadline:
                self._lost("trainer request/FTMS indication timed out")
            if self.ready and now >= self.next_heartbeat:
                if not any(r.tag == "heartbeat" for r in self.queue) and not (self.current and self.current.tag == "heartbeat"):
                    def heartbeat_done(reply, _):
                        if reply.code:
                            raise ProtocolError("trainer rejected heartbeat request")

                    self._enqueue(Packet(READ, body=FEATURES) if self.feature_value else Packet(DISCOVER),
                                  callback=heartbeat_done, tag="heartbeat")
                self.next_heartbeat = now + self.config.heartbeat_interval
            if self.active_profile == "kickr6":
                if self.control_ready:
                    self._translate(now)
                if now - self.last_riding_data >= 1 and self.telemetry.updated is not None and now - self.telemetry.updated <= 5:
                    self._notify(Z_ASYNC, riding_data(self.telemetry.power or 0, self.telemetry.cadence or 0))
                    self.last_riding_data = now
            for publisher in self.publishers:
                error = publisher.tick(now)
                if error:
                    self.event(error, logging.WARNING)
            for ch in list(self.channels):
                if ch.kind == "http" and now - ch.opened > 5:
                    self._drop(ch)
                elif ch.kind == "client" and ch.partial_since is not None and now - ch.partial_since > 5:
                    self._drop(ch)
            self._pump(now)
        except (OSError, ProtocolError, ValueError) as exc:
            self._lost(f"session error: {exc}")

    def run(self):
        try:
            self.start()
            while not self.stopping:
                self.step()
        finally:
            # On a normal service stop, drain a baseline write before closing.
            # A network failure or abrupt process death cannot guarantee restoration.
            if (self.config.shift_mode == "circumference" and self.ready and
                    self.circumference_requested is not None):
                self.active_profile = None
                self._unpublish()
                clients, self.clients = self.clients, set()
                for ch in clients:
                    self._drop(ch)
                for ch in list(self.channels):
                    if ch.kind.endswith("-listener"):
                        self._drop(ch)
                self.queue.clear()
                self._restore_baseline()
                self.stopping = False
                deadline = time.monotonic() + min(self.config.request_timeout, 3)
                while self.upstream and (self.queue or self.current) and time.monotonic() < deadline:
                    self.step(.01)
                if (self.queue or self.current or
                        self.circumference_written != round(self.config.shift_baseline_circumference, 4)):
                    self.event("Baseline restoration not confirmed on shutdown", logging.WARNING)
            self.close()

    def close(self):
        self.stopping = True
        self._unpublish()
        self.clients.clear()
        for ch in list(self.channels):
            self._drop(ch)
        if self.ble_worker:
            self.ble_worker.close()
            self.ble_worker = None
        self.selector.close()
        if self.trace:
            self.trace.close()
            self.trace = None
