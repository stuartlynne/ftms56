"""Optional BlueZ/Bleak upstream, isolated from the TCP selector loop.

The helper presents the existing DirCon request/reply contract over a private
socketpair. BLE notifications remain separate packets, including control results.
"""

import asyncio
import logging
import signal
import socket
import subprocess
import sys
from uuid import UUID

from .protocol import (CHARACTERISTICS, DISCOVER, NOTIFY, READ, SUBSCRIBE, WRITE,
                       NO_CHARACTERISTIC, NO_SERVICE, UNSUPPORTED, WRITE_FAILED,
                       Decoder, Packet, ProtocolError)

LOG = logging.getLogger(__name__)
READY = b"BLE ready"


def properties(flags):
    return ((1 if "read" in flags else 0) |
            (2 if "write" in flags or "write-without-response" in flags else 0) |
            (4 if "notify" in flags or "indicate" in flags else 0))


class GattPeer:
    def __init__(self, client, emit):
        self.client, self.emit = client, emit
        self.services = {UUID(s.uuid).bytes: s for s in client.services}
        self.chars = {}
        for service in client.services:
            for char in service.characteristics:
                uuid = UUID(char.uuid).bytes
                if uuid in self.chars:
                    # DirCon requests identify characteristics by UUID only.
                    # Refuse ambiguous operations rather than guessing a handle.
                    self.chars[uuid] = None
                else:
                    self.chars[uuid] = char
        self.subscribed = set()

    async def request(self, p):
        if p.code or p.op not in (DISCOVER, CHARACTERISTICS, READ, WRITE, SUBSCRIBE):
            return p.reply(b"", code=UNSUPPORTED)
        if p.op == DISCOVER:
            if p.body:
                return p.reply(b"", code=UNSUPPORTED)
            return p.reply(b"".join(self.services))
        uuid = p.uuid
        if p.op == CHARACTERISTICS:
            service = self.services.get(uuid)
            if service is None:
                return p.reply(code=NO_SERVICE)
            return p.reply(uuid + b"".join(UUID(c.uuid).bytes + bytes([properties(c.properties)])
                                           for c in service.characteristics))
        char = self.chars.get(uuid)
        if char is None:
            return p.reply(code=NO_CHARACTERISTIC)
        try:
            if p.op == READ:
                # Let BlueZ report read permissions; do not pretend a cached
                # indication is a fresh read of a non-readable characteristic.
                value = await self.client.read_gatt_char(char)
                return p.reply(uuid + bytes(value))
            if p.op == WRITE:
                if not p.value or not properties(char.properties) & 2:
                    return p.reply(code=UNSUPPORTED)
                await self.client.write_gatt_char(char, p.value, response="write" in char.properties)
                return p.reply()
            if len(p.value) != 1 or p.value[0] not in (0, 1) or not properties(char.properties) & 4:
                return p.reply(code=UNSUPPORTED)
            if p.value[0] and uuid not in self.subscribed:
                def changed(_, data):
                    self.emit(Packet(NOTIFY, body=uuid + bytes(data)))
                await self.client.start_notify(char, changed)
                self.subscribed.add(uuid)
            elif not p.value[0] and uuid in self.subscribed:
                await self.client.stop_notify(char)
                self.subscribed.remove(uuid)
            return p.reply()
        except Exception as exc:
            LOG.warning("BLE %s %s failed: %s", p.op, str(UUID(bytes=uuid)), exc)
            return p.reply(code=WRITE_FAILED if p.op == WRITE else UNSUPPORTED)


async def serve(reader, writer, client):
    """Bounded notification queue with one serialized BLE operation at a time."""
    output = asyncio.Queue(maxsize=256)

    def emit(packet):
        try:
            output.put_nowait(packet.encode())
        except asyncio.QueueFull:
            writer.close()  # Parent tears down the session; never drop control results silently.

    async def send():
        while True:
            data = await output.get()
            writer.write(data)
            await writer.drain()

    async def receive():
        peer, decoder = GattPeer(client, emit), Decoder()
        emit(Packet(0, body=READY))
        while data := await reader.read(65536):
            for packet in decoder.feed(data):
                emit(await peer.request(packet))

    tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def worker(sock, address, adapter, timeout):
    from bleak import BleakClient, BleakScanner
    sock.setblocking(False)
    reader, writer = await asyncio.open_connection(sock=sock)
    client = None
    try:
        async with asyncio.timeout(timeout):
            device = await BleakScanner.find_device_by_address(address, timeout=timeout,
                                                               bluez={"adapter": adapter})
            if device is None:
                raise RuntimeError("BLE trainer not found during scan")
            client = BleakClient(device, disconnected_callback=lambda _: writer.close(),
                                 bluez={"adapter": adapter}, timeout=timeout)
            await client.connect()
        await serve(reader, writer, client)
    except Exception as exc:
        LOG.error("BLE upstream: %s: %s", type(exc).__name__, exc)
        if not writer.is_closing():
            writer.write(Packet(0, code=1, body=str(exc).encode()[:1024]).encode())
            try:
                await writer.drain()
            except (OSError, ConnectionError):
                pass
    finally:
        writer.close()
        if client is not None and client.is_connected:
            try:
                await asyncio.wait_for(client.disconnect(), 2)
            except Exception as exc:
                LOG.warning("BLE disconnect: %s", exc)


class BleWorker:
    def __init__(self, address, adapter="hci0", timeout=35):
        parent, child = socket.socketpair()
        try:
            self.process = subprocess.Popen([
                sys.executable, "-m", "ftms56.ble", str(child.fileno()), address, adapter, str(timeout)
            ], pass_fds=(child.fileno(),))
        except Exception:
            parent.close()
            raise
        finally:
            child.close()
        self.sock = parent

    def close(self):
        self.sock.close()
        try:
            self.process.wait(timeout=.25)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=1)


async def discover(timeout=8, adapter="hci0"):
    from bleak import BleakScanner
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True,
        service_uuids=["00001818-0000-1000-8000-00805f9b34fb", "00001826-0000-1000-8000-00805f9b34fb"],
        bluez={"adapter": adapter})
    return [dict(name=a.local_name or d.name, address=d.address, rssi=a.rssi,
                 services=a.service_uuids) for d, a in devices.values()]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # systemd may signal the entire process group before the parent has restored
    # the baseline. IPC EOF owns graceful shutdown; SIGKILL remains a fallback.
    signal.signal(signal.SIGTERM, lambda *_: None)
    signal.signal(signal.SIGINT, lambda *_: None)
    asyncio.run(worker(socket.socket(fileno=int(sys.argv[1])), sys.argv[2], sys.argv[3], float(sys.argv[4])))
