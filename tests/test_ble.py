"""BLE GATT-to-DirCon contract without a radio or worker threads."""

import asyncio
import socket
from types import SimpleNamespace
from uuid import UUID

from ftms56.ble import GattPeer, READY, properties, serve
from ftms56.protocol import (BIKE_DATA, CONTROL, FEATURES, FTMS, WAHOO_CONTROL,
    CHARACTERISTICS, DISCOVER, READ, WRITE, SUBSCRIBE, NOTIFY,
    NO_CHARACTERISTIC, NO_SERVICE, UNSUPPORTED, WRITE_FAILED,
    Decoder, Packet, characteristics)
from ftms56.bridge import Bridge, Config


class Client:
    def __init__(self):
        chars = [SimpleNamespace(uuid=str(UUID(bytes=u)), properties=p) for u, p in [
            (FEATURES, ["read"]), (CONTROL, ["write", "indicate"]),
            (BIKE_DATA, ["notify"]), (WAHOO_CONTROL, ["write", "indicate"])]]
        self.services = [SimpleNamespace(uuid=str(UUID(bytes=FTMS)), characteristics=chars)]
        self.callbacks = {}
        self.writes = []
        self.fail = False
        self.starts = 0

    async def read_gatt_char(self, char):
        if "read" not in char.properties:
            raise RuntimeError("read not permitted")
        return bytes.fromhex("034000000c600000")

    async def write_gatt_char(self, char, value, response):
        if self.fail:
            raise RuntimeError("GATT write failed")
        assert response is True
        self.writes.append((char.uuid, value))
        if char.uuid in self.callbacks:
            self.callbacks[char.uuid](char, bytes([0x80, value[0], 1]))

    async def start_notify(self, char, callback):
        self.starts += 1
        self.callbacks[char.uuid] = callback

    async def stop_notify(self, char):
        del self.callbacks[char.uuid]


def test_gatt_mapping_and_real_control_notification_before_write_reply():
    async def run():
        client, notifications = Client(), []
        peer = GattPeer(client, notifications.append)
        assert (await peer.request(Packet(DISCOVER))).body == FTMS
        chars = await peer.request(Packet(CHARACTERISTICS, body=FTMS))
        assert characteristics(chars.body)[CONTROL] == 6
        assert properties(["write-without-response", "notify"]) == 6
        assert (await peer.request(Packet(READ, body=FEATURES))).value == bytes.fromhex("034000000c600000")
        assert (await peer.request(Packet(READ, body=CONTROL))).code == UNSUPPORTED
        assert (await peer.request(Packet(READ, body=b"\xff" * 16))).code == NO_CHARACTERISTIC
        assert (await peer.request(Packet(CHARACTERISTICS, body=b"\xff" * 16))).code == NO_SERVICE
        sub = Packet(SUBSCRIBE, body=CONTROL + b"\x01")
        assert (await peer.request(sub)).code == 0
        assert (await peer.request(sub)).code == 0
        assert client.starts == 1
        reply = await peer.request(Packet(WRITE, 42, body=CONTROL + b"\x00"))
        assert reply.seq == 42 and reply.body == CONTROL and reply.code == 0
        assert notifications == [Packet(NOTIFY, body=CONTROL + b"\x80\x00\x01")]
        client.fail = True
        assert (await peer.request(Packet(WRITE, body=CONTROL + b"\x05\x00\x01"))).code == WRITE_FAILED
        assert (await peer.request(Packet(SUBSCRIBE, body=CONTROL + b"\x00"))).code == 0
        assert not client.callbacks
    asyncio.run(run())


def test_helper_stream_fragmentation_and_coalesced_reply_indication():
    async def run():
        a, b = socket.socketpair()
        reader, writer = await asyncio.open_connection(sock=a)
        peer_reader, peer_writer = await asyncio.open_connection(sock=b)
        task = asyncio.create_task(serve(peer_reader, peer_writer, Client()))
        decoder, packets = Decoder(), []

        async def receive(count):
            async with asyncio.timeout(1):
                while len(packets) < count:
                    data = await reader.read(4096)
                    assert data
                    packets.extend(decoder.feed(data))

        try:
            await receive(1)
            assert packets.pop() == Packet(0, body=READY)
            wire = Packet(SUBSCRIBE, 1, body=CONTROL + b"\x01").encode() + Packet(WRITE, 2, body=CONTROL + b"\x00").encode()
            writer.write(wire[:3])
            await writer.drain()
            await asyncio.sleep(0)
            assert not packets
            writer.write(wire[3:])
            await writer.drain()
            await receive(3)
            assert packets[0] == Packet(SUBSCRIBE, 1, body=CONTROL)
            assert packets[1] == Packet(NOTIFY, body=CONTROL + b"\x80\x00\x01")
            assert packets[2] == Packet(WRITE, 2, body=CONTROL)
        finally:
            writer.close()
            await writer.wait_closed()
            await asyncio.wait_for(task, 1)
            peer_writer.close()
            await peer_writer.wait_closed()
    asyncio.run(run())


def test_duplicate_characteristic_uuids_are_not_routed_to_arbitrary_handles():
    async def run():
        client = Client()
        client.services[0].characteristics.append(client.services[0].characteristics[0])
        assert (await GattPeer(client, lambda _: None).request(Packet(READ, body=FEATURES))).code == NO_CHARACTERISTIC
    asyncio.run(run())


def test_parent_waits_for_ble_ready_and_recovers_after_helper_disconnect(monkeypatch):
    helpers = []
    class Helper:
        def __init__(self, *args):
            self.sock, self.peer = socket.socketpair()
            self.peer.setblocking(False)
            self.closed = False
            helpers.append(self)
        def close(self):
            self.closed = True
            self.sock.close()
            self.peer.close()

    monkeypatch.setattr("ftms56.ble.BleWorker", Helper)
    bridge = Bridge(Config("127.0.0.1", ble_address="FD:CB:E7:3B:C2:2B", mdns=False))
    try:
        bridge._connect()
        helper = helpers[0]
        assert bridge.ble_waiting and not bridge.status()["connected"]
        assert bridge.current is None
        helper.peer.sendall(Packet(0, body=READY).encode())
        bridge.step(.001)
        bridge.step(.001)
        assert not bridge.ble_waiting
        assert bridge.status()["upstream_transport"] == "ble"
        assert Decoder().feed(helper.peer.recv(4096)) == [Packet(DISCOVER, 1)]
        helper.peer.close()
        bridge.step(.001)
        assert bridge.upstream is None and helper.closed
        assert not bridge.ready and bridge.current is None
    finally:
        bridge.close()
