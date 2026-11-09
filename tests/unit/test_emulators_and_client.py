"""
The reply rules of the wire protocol, without a socket (Ammeters.client.parse_reply).
"""

import random

import allure
import pytest

from Ammeters.base_ammeter import AmmeterEmulatorBase
from Ammeters.Circutor_Ammeter import CircutorAmmeter
from Ammeters.client import OverRangeError, TruncatedReplyError, parse_reply
from Ammeters.Entes_Ammeter import EntesAmmeter
from Ammeters.Greenlee_Ammeter import GreenleeAmmeter
from Ammeters.serve import build

EMULATORS = [GreenleeAmmeter, EntesAmmeter, CircutorAmmeter]


def _part_emulators_print_safely_and_keep_their_randomness_to_themselves(capsys):
    """Regressions: Greenlee's 'Ω' crashed on cp1252 consoles; the base class reseeded the global `random`."""
    for cls in EMULATORS:
        with allure.step(f"{cls.__name__}: output encodes as cp1252; seed 42 repeats, seed 43 differs"):
            cls(0).measure_current()
            capsys.readouterr().out.encode("cp1252")
            first, second, other = ([cls(0, seed=s).measure_current() for _ in range(3)] for s in (42, 42, 43))
            assert first == second != other
    with allure.step("Creating and using an emulator leaves the global random generator's state untouched"):
        before = random.getstate()
        GreenleeAmmeter(0).measure_current()
        assert random.getstate() == before


def _part_the_emulator_contract_and_the_service_entrypoint():
    """The base class is abstract in both members; `serve.build` creates the named emulator on the given interface."""
    members = {"get_current_command": property(lambda self: b"MEASURE"), "measure_current": lambda self: 1.0}
    for missing in members:
        with allure.step(f"A subclass without {missing} can't be created (TypeError naming it)"):
            incomplete = type(f"Without_{missing}", (AmmeterEmulatorBase,),
                              {name: member for name, member in members.items() if name != missing})
            with pytest.raises(TypeError, match=missing):
                incomplete(0)
    with allure.step("A complete subclass works; an emulator can't take both a simulator and an instrument"):
        assert type("Complete", (AmmeterEmulatorBase,), members)(0).measure_current() == 1.0
        with pytest.raises(ValueError, match="not both"):
            GreenleeAmmeter(0, error_simulator=object(), instrument=object())
    with allure.step("serve.build('entes', 5001, '0.0.0.0', seed=7): an EntesAmmeter on 0.0.0.0:5001, seeded"):
        emulator = build("entes", 5001, "0.0.0.0", seed=7)
        assert isinstance(emulator, EntesAmmeter) and (emulator.host, emulator.port) == ("0.0.0.0", 5001)
        assert emulator.measure_current() == build("entes", 1, "x", seed=7).measure_current()
    with allure.step("An unknown type is rejected with the list of valid ones"):
        with pytest.raises(ValueError, match="circutor"):
            build("fluke", 5000, "0.0.0.0")


def _part_reply_rules():
    """A terminated number is a reading; nothing is None; cut-off, OL and garbage are distinct errors."""
    for data, expected in [(b"12.345\n", 12.345), (b"-0.5\n", -0.5), (b" 7 \n", 7.0), (b"1.5\nleftover", 1.5)]:
        with allure.step(f"parse_reply({data!r}) = {expected}"):
            assert parse_reply(data) == expected
    with allure.step("parse_reply(b'') is None (the meter closed without answering)"):
        assert parse_reply(b"") is None
    for data in (b"12.3", b"12.", b"OL"):
        with allure.step(f"parse_reply({data!r}) -> TruncatedReplyError (no terminator: never a shorter number)"):
            with pytest.raises(TruncatedReplyError):
                parse_reply(data)
    with allure.step("parse_reply(b'OL\\n') -> OverRangeError"):
        with pytest.raises(OverRangeError):
            parse_reply(b"OL\n")
    for data, error in [(b"ERR\n", ValueError), (b"12.3.4\n", ValueError), (b"\n", ValueError),
                        (b"\xff\xfe\n", UnicodeDecodeError)]:
        with allure.step(f"parse_reply({data!r}) -> {error.__name__}"):
            with pytest.raises(error):
                parse_reply(data)


@pytest.mark.bugfix
def test_the_emulators_and_the_reply_protocol(capsys):
    """Emulators print safely, keep their randomness private and honour their contract; replies follow the protocol."""
    with allure.step(
            "Regressions: Greenlee's 'Ω' crashed on cp1252 consoles; the base class reseeded the global `random`."):
        _part_emulators_print_safely_and_keep_their_randomness_to_themselves(capsys)
    with allure.step(
            "The base class is abstract in both members; `serve.build` creates the named emulator on the given "
            "interface."):
        _part_the_emulator_contract_and_the_service_entrypoint()
    with allure.step("A terminated number is a reading; nothing is None; cut-off, OL and garbage are distinct errors."):
        _part_reply_rules()
