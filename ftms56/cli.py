import argparse
from dataclasses import fields, asdict
import ipaddress
import json
import logging
from pathlib import Path
import signal
import socket
import sys

from . import __version__
from .avahi import discover
from .bridge import Bridge, Config
from .inspect import inspect_trainer


def positive(value):
    number = float(value)
    if not 0 < number < float("inf"):
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return number


def parser():
    root = argparse.ArgumentParser(description="KICKR Direct Connect bridge with experimental virtual shifting")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    browse = commands.add_parser("discover", help="list trainers advertised through Avahi")
    browse.add_argument("--timeout", type=positive, default=5)
    ble_browse = commands.add_parser("ble-discover", help="scan for BLE trainers (requires the ble extra)")
    ble_browse.add_argument("--timeout", type=positive, default=8)
    ble_browse.add_argument("--ble-adapter", default="hci0")
    inspect = commands.add_parser("inspect", help="read services/features without changing trainer resistance")
    inspect.add_argument("--trainer", required=True)
    inspect.add_argument("--trainer-port", type=int, default=36866)
    inspect.add_argument("--timeout", type=positive, default=5)
    inspect.add_argument("--sample-seconds", type=positive, default=0, help="subscribe to indoor-bike telemetry")
    inspect.add_argument("--output", type=Path, help="save JSON inventory and raw request/response packets")
    run = commands.add_parser("run", help="run the bridge in the foreground")
    upstream = run.add_mutually_exclusive_group(required=True)
    upstream.add_argument("--trainer", help="trainer Direct Connect hostname/IP")
    upstream.add_argument("--ble-trainer", dest="ble_address", help="trainer Bluetooth MAC address")
    upstream.add_argument("--config", type=Path, help="JSON configuration file; use instead of individual run settings")
    run.add_argument("--check-config", action="store_true", help="validate settings and exit without connecting")
    run.add_argument("--ble-adapter", default="hci0")
    run.add_argument("--ble-connect-timeout", type=positive, default=35)
    run.add_argument("--trainer-port", type=int, default=36866)
    run.add_argument("--bind", default="0.0.0.0")
    run.add_argument("--http-port", type=int, default=36865)
    run.add_argument("--kickr5-port", type=int, default=36867)
    run.add_argument("--kickr6-port", type=int, default=36866)
    run.add_argument("--name", default="ftms56")
    run.add_argument("--shifting-name", help="override the shifting profile's advertised and Zwift device-info name")
    run.add_argument("--shifting-id", help="override shifting serial and virtual MAC seed for a fresh pairing identity")
    run.add_argument("--hide-kickr5", action="store_true", help="do not advertise the pass-through profile")
    run.add_argument("--no-mdns", action="store_true")
    run.add_argument("--physical-ratio", type=positive, default=50 / 16,
                     help="actual chainring teeth / sprocket teeth (default: 50/16)")
    run.add_argument("--circumference", type=positive, default=2.105, help="virtual wheel circumference in metres")
    run.add_argument("--shift-mode", choices=["grade", "circumference"], default="grade")
    run.add_argument("--shift-baseline-circumference", type=positive, default=2.0,
                     help="circumference in metres at the baseline ratio; also restored after the test (default: 2.0)")
    run.add_argument("--shift-baseline-ratio", type=positive, default=1.68,
                     help="virtual ratio at baseline circumference (default: 1.68, community gear 8)")
    run.add_argument("--trainer-mass-kg", type=positive, default=85,
                     help="combined rider+bike mass assumed by the trainer simulation")
    run.add_argument("--zwift-difficulty", type=float, default=50,
                     help="fixed Trainer Difficulty selected in Zwift, 1..100 percent (default: 50)")
    run.add_argument("--trainer-difficulty", type=float, default=50,
                     help="initial GUI difficulty, 0..100 percent (default: 50)")
    run.add_argument("--min-grade", type=float, default=-20)
    run.add_argument("--max-grade", type=float, default=20)
    run.add_argument("--request-timeout", type=positive, default=5)
    run.add_argument("--heartbeat-interval", type=positive, default=10)
    run.add_argument("--trace", help="append raw protocol packets to a JSONL file")
    run.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO")
    return root


def load_config(path):
    path = Path(path).expanduser().resolve()
    values = json.loads(path.read_text())
    if not isinstance(values, dict):
        raise ValueError("configuration must be a JSON object")
    unknown = set(values) - {f.name for f in fields(Config)}
    if unknown:
        raise ValueError("unknown configuration fields: " + ", ".join(sorted(unknown)))
    if bool(values.get("trainer_host")) == bool(values.get("ble_address")):
        raise ValueError("set exactly one of trainer_host or ble_address")
    if values.get("ble_address"):
        if not isinstance(values["ble_address"], str):
            raise ValueError("ble_address must be a Bluetooth address string")
        values["trainer_host"] = "127.0.0.1"
    else:
        if not isinstance(values["trainer_host"], str):
            raise ValueError("trainer_host must be a hostname or IPv4 address")
        values["trainer_host"] = socket.gethostbyname(values["trainer_host"])
    if values.get("trace"):
        if not isinstance(values["trace"], str):
            raise ValueError("trace must be a filename string")
        trace = Path(values["trace"]).expanduser()
        values["trace"] = str(trace if trace.is_absolute() else path.parent / trace)
    try:
        return Config(**values)
    except TypeError as exc:
        raise ValueError(f"invalid configuration value: {exc}") from None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    args = parser().parse_args(argv)
    try:
        config = None
        if args.command == "run" and args.config:
            allowed = {"--config", "--check-config", "--log-level"}
            extra = [v.split("=", 1)[0] for v in argv[1:] if v.startswith("--") and v.split("=", 1)[0] not in allowed]
            if extra:
                raise ValueError("put run settings in the configuration file instead of: " + ", ".join(extra))
            config = load_config(args.config)
            args.ble_address = config.ble_address
            if args.check_config:
                print(json.dumps(asdict(config), indent=2))
                return 0
        if args.command == "discover":
            print(json.dumps(discover(args.timeout), indent=2))
            return 0
        if args.command == "ble-discover" or getattr(args, "ble_address", None):
            from importlib.metadata import PackageNotFoundError, version
            try:
                if int(version("bleak").split(".")[0]) < 3:
                    raise PackageNotFoundError
            except PackageNotFoundError:
                raise RuntimeError("BLE mode requires: python -m pip install '.[ble]'") from None
        if args.command == "ble-discover":
            import asyncio
            from .ble import discover as discover_ble
            print(json.dumps(asyncio.run(discover_ble(args.timeout, args.ble_adapter)), indent=2))
            return 0
        # Resolve names once before entering the selector loop.
        host = config.trainer_host if config else socket.gethostbyname(args.trainer) if args.trainer else "127.0.0.1"
        ipaddress.IPv4Address(host)
        if args.command == "inspect":
            result = inspect_trainer(host, args.trainer_port, args.timeout, args.sample_seconds)
            rendered = json.dumps(result, indent=2) + "\n"
            if args.output:
                args.output.write_text(rendered)
                print(f"Saved read-only trainer inventory to {args.output}")
            else:
                print(rendered, end="")
            return 0
        logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")
        values = vars(args).copy()
        for key in ("command", "trainer", "no_mdns", "log_level", "config", "check_config"):
            values.pop(key)
        config = config or Config(trainer_host=host, mdns=not args.no_mdns, **values)
        if args.check_config:
            print(json.dumps(asdict(config), indent=2))
            return 0
        # Prevent accidental self-proxying via one of this host's LAN addresses.
        if not config.ble_address:
            route = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                route.connect((host, config.trainer_port))
                if route.getsockname()[0] == host and config.trainer_port in (
                        config.http_port, config.kickr5_port, config.kickr6_port):
                    raise ValueError("upstream points to this bridge's own address/port")
            finally:
                route.close()
        bridge = Bridge(config)

        def stop(signum, frame):
            bridge.stopping = True

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        bridge.run()
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ftms56: {exc}", file=sys.stderr)
        return 1
