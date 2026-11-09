"""
The production config/config.yaml is read only here: the rest of the suite uses its own test config, so this is
the contract that the shipped configuration is valid and consistent with the code and the README.
"""
import allure
import pytest

import main
from Ammeters.Circutor_Ammeter import CircutorAmmeter
from Ammeters.Entes_Ammeter import EntesAmmeter
from Ammeters.error_simulation import ErrorSimulator
from Ammeters.Greenlee_Ammeter import GreenleeAmmeter
from Ammeters.instrument_model import ReferenceSource
from helpers import K8S_CONFIG_PATH, PRODUCTION_CONFIG_PATH, attach_json
from src.testing.calibration import build_instruments
from src.testing.test_framework import AmmeterTestFramework, deep_merge, resolve_sampling
from src.utils.config import load_config


def _subdir(tmp_path, n):
    """A separate folder per merged part, so parts never see each other's files."""
    path = tmp_path / f"part{n}"
    path.mkdir()
    return path


def _part_the_production_config_is_valid_and_consistent(tmp_path, monkeypatch):
    """Regression (ports/commands): the shipped config builds a framework and agrees with main.py and the code."""
    monkeypatch.chdir(tmp_path)
    config = load_config(PRODUCTION_CONFIG_PATH)
    attach_json("ammeters", config["ammeters"])
    with allure.step("It builds a framework; every ammeter validates; the sampling resolves"):
        framework = AmmeterTestFramework(PRODUCTION_CONFIG_PATH)
        for name in framework.config["ammeters"]:
            framework.ammeter_config(name)
        resolve_sampling(framework.config["testing"]["sampling"])
    expected = {"greenlee": (5000, GreenleeAmmeter, [0.01, 100]), "entes": (5001, EntesAmmeter, [5, 200]),
                "circutor": (5002, CircutorAmmeter, [0.001, 0.1])}
    with allure.step("Ports are the README's (5000-5002) and main.py's; commands are the emulators'"):
        assert (main.GREENLEE_PORT, main.ENTES_PORT, main.CIRCUTOR_PORT) == (5000, 5001, 5002)
        for name, (port, cls, _) in expected.items():
            assert config["ammeters"][name]["port"] == port
            assert config["ammeters"][name]["command"].encode() == cls(0).get_current_command
    with allure.step("Plausible ranges are exactly what each formula can produce"):
        assert {n: config["ammeters"][n]["plausible_range_a"] for n in expected} == {
            n: r for n, (_, _, r) in expected.items()}
    with allure.step("The Kubernetes config uses the same ports, commands and ranges, with Service names as hosts"):
        k8s = load_config(K8S_CONFIG_PATH)["ammeters"]
        for name in expected:
            shipped = dict(config["ammeters"][name], host=name)
            assert k8s[name] == shipped, name
    with allure.step("Error simulation is off, and valid when switched on; the instrument datasheets build"):
        assert ErrorSimulator.from_config(config, "entes") is None
        enabled = deep_merge(config, {"error_simulation": {"enabled": True}})
        assert all(ErrorSimulator.from_config(enabled, n) is not None for n in expected)
        instruments = build_instruments(config, ReferenceSource())
        assert sorted(instruments) == sorted(expected) and all(m.has_spec() for m in instruments.values())


def _part_loading_a_config_reports_problems_clearly(tmp_path):
    """Missing file, bad YAML and a non-mapping fail with messages naming the file; an empty file is {}."""
    broken, listing, empty = tmp_path / "broken.yaml", tmp_path / "list.yaml", tmp_path / "empty.yaml"
    broken.write_text("testing: [unclosed\n", encoding="utf-8")
    listing.write_text("- a\n- b\n", encoding="utf-8")
    empty.write_text("", encoding="utf-8")
    with allure.step("Missing file -> FileNotFoundError naming it"):
        with pytest.raises(FileNotFoundError, match="nowhere.yaml"):
            load_config(str(tmp_path / "nowhere.yaml"))
    with allure.step("YAML syntax error -> ValueError naming the file"):
        with pytest.raises(ValueError, match="Invalid YAML in .*broken.yaml"):
            load_config(str(broken))
    with allure.step("Top level is a list -> ValueError; empty file -> {}"):
        with pytest.raises(ValueError, match="mapping"):
            load_config(str(listing))
        assert load_config(str(empty)) == {}


@pytest.mark.sanity
@pytest.mark.bugfix
def test_the_configuration(tmp_path, monkeypatch):
    """The shipped configs are valid and consistent with the code; loading a bad config reports it clearly."""
    with allure.step(
            "Regression (ports/commands): the shipped config builds a framework and agrees with main.py and the "
            "code."):
        _part_the_production_config_is_valid_and_consistent(_subdir(tmp_path, 1), monkeypatch)
    with allure.step(
            "Missing file, bad YAML and a non-mapping fail with messages naming the file; an empty file is {}."):
        _part_loading_a_config_reports_problems_clearly(_subdir(tmp_path, 2))
