"""Use Linux's existing Avahi daemon for mDNS, avoiding Python worker threads."""

import os
import re
import shlex
import shutil
import subprocess

SERVICE = "_wahoo-fitness-tnp._tcp"


def unescape(value: str) -> str:
    return re.sub(r"\\(\d{3})", lambda match: chr(int(match[1])), value)


def discover(timeout: float = 5) -> list[dict]:
    command = ["avahi-browse", "-rtp", SERVICE]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        output = result.stdout
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "avahi-browse failed")
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or b""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
    devices = {}
    for line in output.splitlines():
        parts = line.split(";", 9)
        if len(parts) != 10 or parts[0] != "=":
            continue
        _, interface, family, name, service, domain, host, address, port, txt = parts
        if family != "IPv4":
            continue
        records = dict(item.split("=", 1) for item in shlex.split(txt) if "=" in item)
        devices[(address, port)] = dict(name=unescape(name), host=unescape(host), address=address,
                                        port=int(port), interface=interface, txt=records)
    return list(devices.values())


class Publisher:
    def __init__(self, name: str, service: str, port: int, txt: list[str]):
        if not shutil.which("avahi-publish-service"):
            raise RuntimeError("avahi-publish-service missing; install avahi-utils or use --no-mdns")
        self.command = ["avahi-publish-service", name, service, str(port), *txt]
        self.process = None
        self.output = ""
        self.established = False
        self.retry_at = 0

    def tick(self, now: float) -> str | None:
        error = None
        if self.process is not None:
            try:
                data = os.read(self.process.stdout.fileno(), 4096)
                self.output = (self.output + data.decode(errors="replace"))[-4096:]
                self.established = "Established under name" in self.output
            except BlockingIOError:
                pass
            if self.process.poll() is not None:
                error = "mDNS publisher exited: " + self.output.strip()
                self.process.stdout.close()
                self.process = None
                self.established = False
                self.retry_at = now + 10
        if self.process is None and now >= self.retry_at:
            self.output = ""
            self.process = subprocess.Popen(self.command, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, env={**os.environ, "LC_ALL": "C"})
            os.set_blocking(self.process.stdout.fileno(), False)
        return error

    def close(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process.stdout.close()
            self.process = None
        self.established = False
