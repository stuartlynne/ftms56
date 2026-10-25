"""Installation boundaries: config validation, service paths, and preservation."""

import importlib.util
import json
from pathlib import Path

import pytest

from ftms56.cli import load_config, main

spec = importlib.util.spec_from_file_location("ftms56_installer", Path(__file__).parents[1] / "install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def test_json_config_is_checked_offline_and_does_not_accept_silent_overrides(tmp_path, capsys):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"ble_address": "AA:BB:CC:DD:EE:FF", "physical_ratio": 52/17,
                                  "shift_baseline_ratio": 52/17, "trace": "ride.jsonl"}))
    assert main(["run", "--config", str(config), "--check-config"]) == 0
    values = json.loads(capsys.readouterr().out)
    assert values["ble_address"] == "AA:BB:CC:DD:EE:FF"
    assert values["trace"] == str(tmp_path / "ride.jsonl")
    assert main(["run", "--config", str(config), "--physical-ratio", "3"]) == 1
    assert "instead of" in capsys.readouterr().err


@pytest.mark.parametrize("values", [[], {}, {"trainer_host":"192.0.2.10","typo":5},
    {"trainer_host":"192.0.2.10","ble_address":"AA:BB:CC:DD:EE:FF"},
    {"trainer_host":"192.0.2.10","trace":3}, {"trainer_host":"192.0.2.10","http_port":36866}])
def test_bad_configs_are_rejected(tmp_path, values):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(values))
    with pytest.raises(ValueError):
        load_config(path)


def test_installer_preserves_existing_config_and_creates_optional_service_without_starting(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config home"))
    monkeypatch.setattr(installer.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(installer.shutil, "which", lambda _: "/usr/bin/systemctl")
    prefix = tmp_path / "app"
    (prefix / "venv/bin").mkdir(parents=True)
    (prefix / "venv/bin/python").touch()
    config = tmp_path / "config.json"
    original = json.dumps({"ble_address":"AA:BB:CC:DD:EE:FF","physical_ratio":52/17})
    config.write_text(original)
    commands = []
    monkeypatch.setattr(installer.subprocess, "run", lambda argv, **kwargs: commands.append(argv))
    assert installer.main(["--prefix",str(prefix),"--bin-dir",str(tmp_path / "bin"),
                           "--config",str(config),"--systemd"]) == 0
    assert config.read_text() == original
    assert commands[0][-1].endswith("[ble]")  # Existing BLE config retains optional dependencies.
    assert ["systemctl","--user","daemon-reload"] in commands
    assert not any("enable" in cmd for cmd in commands)
    unit = tmp_path / "config home/systemd/user/ftms56.service"
    assert "KillMode=mixed" in unit.read_text()
    assert str(config) in unit.read_text()
    unit.write_text("# custom service\n")
    assert installer.main(["--prefix",str(prefix),"--bin-dir",str(tmp_path / "bin"),
                           "--config",str(config),"--systemd"]) == 1
    assert unit.read_text() == "# custom service\n"


def test_installer_rejects_replacing_an_unrelated_command(tmp_path, monkeypatch):
    monkeypatch.setattr(installer.os, "geteuid", lambda: 1000)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    command = bindir / "ftms56"
    command.write_text("existing command")
    assert installer.main(["--prefix",str(tmp_path / "app"),"--bin-dir",str(bindir)]) == 1
    assert command.read_text() == "existing command"
    assert not (tmp_path / "app").exists()
