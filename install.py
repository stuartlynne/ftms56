#!/usr/bin/env python3
"""Install ftms56 for the current user; optionally register a systemd user service."""

import argparse
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parent


def systemd_quote(value):
    value = str(value)
    if any(c in value for c in "\n\r\0"):
        raise ValueError("service paths cannot contain newlines or NUL")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def service_text(executable, config):
    return (ROOT / "deploy/ftms56.service.in").read_text().replace(
        "@EXECUTABLE@", systemd_quote(executable)).replace("@CONFIG@", systemd_quote(config))


def configuration(args):
    source = {"ble_address": args.ble_trainer} if args.ble_trainer else {"trainer_host": args.trainer or "192.0.2.10"}
    return dict(**source, physical_ratio=args.physical_ratio, shift_mode="circumference",
                shift_baseline_ratio=args.physical_ratio, shift_baseline_circumference=2.0,
                trainer_difficulty=50, zwift_difficulty=50, hide_kickr5=True)


def positive(value):
    number = float(value)
    if not 0 < number < float("inf"):
        raise argparse.ArgumentTypeError("must be finite and positive")
    return number


def main(argv=None):
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--prefix", type=Path, default=data_home / "ftms56", help="installation directory")
    p.add_argument("--bin-dir", type=Path, default=Path.home() / ".local/bin")
    p.add_argument("--config", type=Path, default=config_home / "ftms56/config.json")
    p.add_argument("--ble", action="store_true", help="install optional BLE dependencies")
    source = p.add_mutually_exclusive_group()
    source.add_argument("--trainer", help="Direct Connect hostname or IPv4 address")
    source.add_argument("--ble-trainer", help="Bluetooth MAC; also enables --ble")
    p.add_argument("--physical-ratio", type=positive, default=50 / 16, help="actual front teeth / rear teeth (default: 3.125)")
    p.add_argument("--systemd", action="store_true", help="install/reload a systemd user service; does not start it")
    p.add_argument("--start", action="store_true", help="with --systemd, enable and start the configured service")
    args = p.parse_args(argv)
    try:
        if sys.version_info < (3, 11):
            raise ValueError("Python 3.11 or newer is required")
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            raise ValueError("run this user-local installer as your desktop user, without sudo")
        if args.start and not args.systemd:
            raise ValueError("--start requires --systemd")
        if args.systemd and (sys.platform != "linux" or not shutil.which("systemctl")):
            raise ValueError("--systemd requires Linux with systemctl")
        prefix, bindir, config = (x.expanduser().resolve() for x in (args.prefix, args.bin_dir, args.config))
        executable = prefix / "venv/bin/ftms56"
        link = bindir / "ftms56"
        if (link.exists() or link.is_symlink()) and not (link.is_symlink() and link.resolve() == executable):
            raise ValueError(f"will not replace existing command: {link}")
        existing = json.loads(config.read_text()) if config.exists() else None
        if existing is not None and not isinstance(existing, dict):
            raise ValueError("existing config must be a JSON object")
        if args.start and existing is None and not (args.trainer or args.ble_trainer):
            raise ValueError("--start requires an existing configuration or --trainer/--ble-trainer")
        use_ble = args.ble or bool(args.ble_trainer) or bool(existing and existing.get("ble_address"))
        prefix.mkdir(parents=True, exist_ok=True)
        env = prefix / "venv"
        if not (env / "bin/python").exists():
            venv.EnvBuilder(with_pip=True).create(env)
        subprocess.run([str(env / "bin/python"), "-m", "pip", "install", "--upgrade", str(ROOT) + ("[ble]" if use_ble else "")], check=True)
        bindir.mkdir(parents=True, exist_ok=True)
        if not link.is_symlink():
            link.symlink_to(executable)
        config.parent.mkdir(parents=True, exist_ok=True)
        if config.exists():
            print(f"Preserved existing configuration: {config}")
            if args.trainer or args.ble_trainer:
                print("Device options only initialize a new config; edit the existing file to change upstream.")
        elif args.trainer or args.ble_trainer:
            # Exclusive creation avoids overwriting a file changed during installation.
            with config.open("x") as f:
                json.dump(configuration(args), f, indent=2)
                f.write("\n")
        else:
            example = config.with_suffix(".json.example")
            if not example.exists():
                with example.open("x") as f:
                    json.dump(configuration(args), f, indent=2)
                    f.write("\n")
            print(f"Configure your trainer before running: copy {example} to {config} and edit it.")
        if config.exists():
            subprocess.run([str(executable), "run", "--config", str(config), "--check-config"], check=True, stdout=subprocess.DEVNULL)
        if args.systemd:
            unit = config_home.expanduser().resolve() / "systemd/user/ftms56.service"
            unit.parent.mkdir(parents=True, exist_ok=True)
            # Do not replace a custom unit. Installer-generated units may be updated.
            if unit.exists() and "# Generated by ftms56 install.py" not in unit.read_text():
                raise ValueError(f"will not replace custom service: {unit}")
            temporary = unit.with_suffix(".service.tmp")
            temporary.write_text(service_text(executable, config))
            temporary.replace(unit)
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
            print(f"Installed user service: {unit}")
            if args.start:
                subprocess.run(["systemctl", "--user", "enable", "--now", "ftms56.service"], check=True)
        print(f"Installed command: {link}")
        print("Run: " + shlex.join([str(executable), "run", "--config", str(config)]))
        print("Re-run this installer to upgrade; existing configuration is preserved.")
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"install.py: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
