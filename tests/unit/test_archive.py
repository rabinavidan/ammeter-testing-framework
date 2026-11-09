import os
import re
import uuid
from types import SimpleNamespace

import allure
import pytest

from src.results.result_archive import ResultArchive
from src.testing import test_framework as test_framework_module


def _subdir(tmp_path, n):
    """A separate folder per merged part, so parts never see each other's files."""
    path = tmp_path / f"part{n}"
    path.mkdir()
    return path


def record(test_id, ammeter_type="entes", start="2026-01-01T00:00:00", mean=10.0):
    return {"test_id": test_id, "ammeter_type": ammeter_type, "start_time": start, "values": [mean],
            "sampling": {"measurements_count": 1}, "errors": [], "statistics": {"mean": mean}}


def _part_records_round_trip_and_are_never_overwritten(tmp_path):
    """Save/load keeps every field plus metadata; an ID is never reused; bad input leaves nothing behind."""
    archive = ResultArchive(str(tmp_path))
    with allure.step("Save and load: fields, schema version 1 and the config snapshot come back"):
        archive.save(record("entes_aaaaaaaa", mean=1.0), config={"k": 1}, config_path="cfg.yaml")
        loaded = archive.load("entes_aaaaaaaa")
        assert loaded["statistics"] == {"mean": 1.0} and loaded["schema_version"] == 1
        assert loaded["metadata"]["config"] == {"k": 1} and loaded["metadata"]["config_path"] == "cfg.yaml"
        assert re.fullmatch(r"[0-9a-f]{12}", loaded["metadata"]["code_version"])
    with allure.step("Saving the same ID again raises FileExistsError and the first record is unchanged"):
        with pytest.raises(FileExistsError):
            archive.save(record("entes_aaaaaaaa", mean=2.0))
        assert archive.load("entes_aaaaaaaa")["statistics"]["mean"] == 1.0
    for bad in ("../config", "a/b", "a\\b", "", "x.json"):
        with allure.step(f"Unsafe ID {bad!r} is rejected"):
            with pytest.raises(ValueError, match="Invalid test ID"):
                archive.load(bad)
    with allure.step("A missing ID raises KeyError; an unserialisable record leaves no partial file"):
        with pytest.raises(KeyError):
            archive.load("entes_ffffffff")
        with pytest.raises(TypeError):
            archive.save({"test_id": "entes_badbad00", "x": object()})
        assert not archive.exists("entes_badbad00")


def _part_listing_and_comparing_history(tmp_path):
    """Runs list oldest first and filter by type; a corrupt file is skipped with a warning; compare lines them up."""
    archive = ResultArchive(str(tmp_path))
    with allure.step("A folder that doesn't exist yet lists as empty"):
        assert ResultArchive(str(tmp_path / "nowhere")).list_runs() == []
    archive.save(record("entes_bbbbbbbb", start="2026-01-02T00:00:00", mean=2.0))
    archive.save(record("entes_aaaaaaaa", start="2026-01-01T00:00:00", mean=1.0))
    archive.save(record("greenlee_cccccccc", "greenlee", start="2026-01-03T00:00:00"))
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    with allure.step("All runs oldest first, the corrupt file skipped with a warning; filtering by type works"):
        with pytest.warns(UserWarning, match="broken.json"):
            assert [r["test_id"] for r in archive.list_runs()] == [
                "entes_aaaaaaaa", "entes_bbbbbbbb", "greenlee_cccccccc"]
        with pytest.warns(UserWarning):
            assert [r["test_id"] for r in archive.list_runs("greenlee")] == ["greenlee_cccccccc"]
    with allure.step("compare: means 1.0 and 2.0 side by side; an unrecorded metric is None"):
        comparison = archive.compare(["entes_aaaaaaaa", "entes_bbbbbbbb"], ["mean", "median"])
        assert comparison["entes_aaaaaaaa"]["mean"] == 1.0 and comparison["entes_bbbbbbbb"]["mean"] == 2.0
        assert comparison["entes_aaaaaaaa"]["median"] is None


def _part_new_test_ids_skip_ones_already_archived(framework, monkeypatch):
    """If a new random ID is already in the archive, another is drawn."""
    os.makedirs(framework.archive.archive_dir)
    open(framework.archive.path_for("fixed_00000000"), "w", encoding="utf-8").write("{}")
    ids = iter([uuid.UUID(int=0), uuid.UUID(int=0), uuid.UUID(int=1 << 124)])
    # Patch only the framework's view of uuid: other code (e.g. Allure) needs the real uuid4
    monkeypatch.setattr(test_framework_module, "uuid", SimpleNamespace(uuid4=lambda: next(ids)))
    with allure.step("With 'fixed_00000000' taken and the random source repeating it, the new ID is 'fixed_10000000'"):
        assert framework.new_test_id("fixed") == "fixed_10000000"


def test_the_archive(tmp_path, framework, monkeypatch):
    """Records round-trip and are never overwritten; history is listed and compared; IDs are never reused."""
    with allure.step(
            "Save/load keeps every field plus metadata; an ID is never reused; bad input leaves nothing behind."):
        _part_records_round_trip_and_are_never_overwritten(_subdir(tmp_path, 1))
    with allure.step(
            "Runs list oldest first and filter by type; a corrupt file is skipped with a warning; compare lines "
            "them up."):
        _part_listing_and_comparing_history(_subdir(tmp_path, 2))
    with allure.step("If a new random ID is already in the archive, another is drawn."):
        _part_new_test_ids_skip_ones_already_archived(framework, monkeypatch)
