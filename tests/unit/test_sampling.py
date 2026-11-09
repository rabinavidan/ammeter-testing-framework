"""
The sampling logic of run_test on a virtual clock and a fake transport: exact, instant and independent of machine
load. The wall-clock version of these checks is marked `timing` (tests/integration/test_sampling.py).
"""

import allure
import pytest

from helpers import TEST_CONFIG_PATH, FakeClock
from src.testing.test_framework import AmmeterTestFramework, resolve_sampling


def _subdir(tmp_path, n):
    """A separate folder per merged part, so parts never see each other's files."""
    path = tmp_path / f"part{n}"
    path.mkdir()
    return path


def _part_sampling_configuration():
    """Any two of count/duration/frequency determine the third; invalid settings say what is wrong."""
    valid = [
        ({"measurements_count": 10, "sampling_frequency_hz": 5}, (10, 2.0, 5.0)),
        ({"measurements_count": 20, "total_duration_seconds": 4}, (20, 4.0, 5.0)),
        ({"total_duration_seconds": 1.5, "sampling_frequency_hz": 4}, (6, 1.5, 4.0)),
        ({"measurements_count": 6, "total_duration_seconds": 3, "sampling_frequency_hz": 2}, (6, 3.0, 2.0)),
        ({"total_duration_seconds": 0.1, "sampling_frequency_hz": 1}, (1, 0.1, 1.0)),     # never 0 readings
    ]
    for config, expected in valid:
        with allure.step(f"{config} -> (count, duration, frequency) = {expected}"):
            assert resolve_sampling(config) == expected
    invalid = [
        ({"measurements_count": 5}, "at least two"),
        ({}, "at least two"),
        ({"measurements_count": 5, "total_duration_seconds": 1, "sampling_frequency_hz": 2}, "Inconsistent"),
        ({"measurements_count": -1, "sampling_frequency_hz": 2}, "positive"),
        ({"measurements_count": 0, "sampling_frequency_hz": 2}, "positive"),
        ({"measurements_count": 2.5, "sampling_frequency_hz": 2}, "whole number"),
        ({"measurements_count": True, "sampling_frequency_hz": 2}, "positive"),
        ({"measurements_count": "10", "sampling_frequency_hz": 2}, "positive"),
    ]
    for name in ("measurements_count", "total_duration_seconds", "sampling_frequency_hz"):
        for value in (float("nan"), float("inf"), float("-inf")):
            config = {"measurements_count": 10, "total_duration_seconds": 2, "sampling_frequency_hz": 5}
            config[name] = value
            invalid.append((config, "finite"))
    for config, message in invalid:
        with allure.step(f"{config} -> ValueError mentioning '{message}'"):
            with pytest.raises(ValueError, match=message):
                resolve_sampling(config)


def make_framework(tmp_path, monkeypatch, replies, latency=0.0, plausible=None, slow_request=None):
    """
    A framework on a FakeClock whose transport returns `replies` in turn (an Exception instance is raised instead),
    taking `latency` virtual seconds per request, plus 0.25 s on request number `slow_request` (1-based).
    """
    monkeypatch.chdir(tmp_path)
    clock = FakeClock()
    requested_at = []
    replies = iter(replies)

    def transport(port, command, timeout, host="localhost"):
        requested_at.append(clock.now())
        clock.time += latency + (0.25 if len(requested_at) == slow_request else 0)
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    framework = AmmeterTestFramework(TEST_CONFIG_PATH, clock=clock, transport=transport,
                                     overrides={"result_management": {"enabled": False}})
    framework.register_ammeter("virtual", 1, "MEASURE", plausible)
    return framework, requested_at


def sampling(count, frequency):
    return {"measurements_count": count, "sampling_frequency_hz": frequency, "total_duration_seconds": None}


def _part_readings_follow_an_absolute_schedule(tmp_path, monkeypatch):
    """Reading i is requested at i / frequency; latency doesn't accumulate; an overrun only delays what it passes."""
    with allure.step("5 readings at 10 Hz: requested at exactly 0, 0.1, 0.2, 0.3, 0.4 s; the run lasts 0.5 s"):
        framework, requested_at = make_framework(tmp_path, monkeypatch, [10.0] * 5)
        result = framework.run_test("virtual", sampling(5, 10))
        assert requested_at == pytest.approx([0.0, 0.1, 0.2, 0.3, 0.4])
        assert result["actual_duration_seconds"] == pytest.approx(0.5)
        assert result["timing"] == {"max_lateness_s": pytest.approx(0.0), "achieved_frequency_hz": pytest.approx(10)}
        assert not any("behind schedule" in w for w in result["warnings"])
    with allure.step("30 ms latency per request: each request still starts on its slot (i × 0.1 s)"):
        framework, requested_at = make_framework(tmp_path, monkeypatch, [10.0] * 10, latency=0.03)
        framework.run_test("virtual", sampling(10, 10))
        assert requested_at == pytest.approx([i / 10 for i in range(10)])
    with allure.step("The 2nd request takes 0.25 s: the 3rd and 4th (slots 0.2, 0.3 s) go at once at 0.35 s, "
                     "the 5th is back on schedule at 0.4 s"):
        framework, requested_at = make_framework(tmp_path, monkeypatch, [10.0] * 5, slow_request=2)
        result = framework.run_test("virtual", sampling(5, 10))
        assert requested_at == pytest.approx([0.0, 0.1, 0.35, 0.35, 0.4])
    with allure.step("That overrun (150 ms late, over half the 100 ms interval) is recorded and warned about"):
        assert result["timing"]["max_lateness_s"] == pytest.approx(0.15)
        assert any("behind schedule" in w and "150 ms late" in w for w in result["warnings"])
    with allure.step("Requests slower than the interval (500 Hz asked, 10 ms per request): the run overruns "
                     "its window and the warning gives the achieved frequency, 100 Hz"):
        framework, _ = make_framework(tmp_path, monkeypatch, [10.0] * 20, latency=0.01)
        result = framework.run_test("virtual", sampling(20, 500))
        assert result["actual_duration_seconds"] == pytest.approx(0.2)
        assert result["timing"]["achieved_frequency_hz"] == pytest.approx(100)
        assert any("behind schedule" in w and "achieved 100 Hz of 500 Hz" in w for w in result["warnings"])


def _part_bad_readings_are_recorded_and_sampling_continues(tmp_path, monkeypatch):
    """Regression (NaN/inf accepted): timeouts, NaN, no reply and implausible values become errors; the rest is kept."""
    replies = [10.0, TimeoutError("timed out"), float("nan"), None, 500.0, float("inf"), 11.0]
    framework, _ = make_framework(tmp_path, monkeypatch, replies, plausible=[0, 100])
    with allure.step("Replies: 10, timeout, NaN, nothing, 500 (outside 0..100), inf, 11"):
        result = framework.run_test("virtual", sampling(7, 10))
    with allure.step("Kept 10 and 11; recorded TimeoutError, 2 ValueError (NaN, inf), NoReplyError (nothing), "
                     "ImplausibleReadingError: each failure has its own type"):
        assert result["values"] == [10.0, 11.0]
        assert result["error_counts"] == {"TimeoutError": 1, "ValueError": 2, "NoReplyError": 1,
                                          "ImplausibleReadingError": 1}
        assert [e["index"] for e in result["errors"]] == [1, 2, 3, 4, 5]
        assert result["statistics"]["success_rate"] == pytest.approx(2 / 7)


@pytest.mark.sanity
@pytest.mark.bugfix
def test_sampling_and_its_schedule(tmp_path, monkeypatch):
    """Two of count, duration, frequency set the third; an absolute schedule; bad readings recorded, run goes on."""
    with allure.step("Any two of count/duration/frequency determine the third; invalid settings say what is wrong."):
        _part_sampling_configuration()
    with allure.step(
            "Reading i is requested at i / frequency; latency doesn't accumulate; an overrun only delays what it "
            "passes."):
        _part_readings_follow_an_absolute_schedule(_subdir(tmp_path, 2), monkeypatch)
    with allure.step(
            "Regression (NaN/inf accepted): timeouts, NaN, no reply and implausible values become errors; the "
            "rest is kept."):
        _part_bad_readings_are_recorded_and_sampling_continues(_subdir(tmp_path, 3), monkeypatch)
