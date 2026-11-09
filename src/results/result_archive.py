import functools
import hashlib
import json
import os
import platform
import re
import warnings
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

SCHEMA_VERSION = 1

# Test IDs become file names, so restrict them to characters that can't escape the archive directory
_TEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@functools.lru_cache(maxsize=1)
def code_version() -> str:
    """
    A fingerprint of the code that produced a run: the first 12 hex digits of a SHA-256 over every .py file in
    Ammeters/ and src/ (path and content, line endings normalised). Works without git; equal fingerprints mean
    identical emulator and framework code.
    """
    digest = hashlib.sha256()
    for package in ("Ammeters", "src"):
        for folder, dirs, files in sorted(os.walk(os.path.join(_ROOT, package))):
            dirs.sort()
            for name in sorted(f for f in files if f.endswith(".py")):
                path = os.path.join(folder, name)
                digest.update(os.path.relpath(path, _ROOT).replace(os.sep, "/").encode())
                with open(path, "rb") as f:
                    digest.update(f.read().replace(b"\r\n", b"\n"))
    return digest.hexdigest()[:12]


class ResultArchive:
    """
    Stores each test run as one JSON file, `<archive_dir>/<test_id>.json`.

    A record is the dict returned by `AmmeterTestFramework.run_test`, plus
    `schema_version` and a `metadata` block (archive time, host, platform,
    Python version, code fingerprint, and the config the run used).
    """

    def __init__(self, archive_dir: str = "results/archive"):
        self.archive_dir = os.path.normpath(archive_dir)

    def path_for(self, test_id: str) -> str:
        if not _TEST_ID_PATTERN.match(test_id or ""):
            raise ValueError(f"Invalid test ID {test_id!r}: use letters, digits, '_' and '-' only")
        return os.path.join(self.archive_dir, f"{test_id}.json")

    def exists(self, test_id: str) -> bool:
        return os.path.exists(self.path_for(test_id))

    def save(self, result: Dict[str, Any], config: Optional[Dict[str, Any]] = None,
             config_path: Optional[str] = None) -> str:
        """
        Archive a run result and return the file path.

        Raises FileExistsError rather than overwrite an existing record. If writing
        fails part-way the partial file is removed; `list_runs` skips any corrupt file
        left behind by a hard crash.
        """
        path = self.path_for(result["test_id"])
        os.makedirs(self.archive_dir, exist_ok=True)

        record = dict(result)
        record["schema_version"] = SCHEMA_VERSION
        record["metadata"] = {
            "archived_at": datetime.now().isoformat(),
            "hostname": platform.node(),
            "platform": platform.platform(),
            "python_version": platform.python_version(),
            "code_version": code_version(),
            "config_path": config_path,
            "config": config,
        }

        # Serialise first so an unserialisable result fails before any file is created
        content = json.dumps(record, indent=2, ensure_ascii=False)
        # "x" = exclusive create: fails if the file exists, so a record is never overwritten
        with open(path, "x", encoding="utf-8") as f:
            try:
                f.write(content)
            except BaseException:
                f.close()
                os.remove(path)
                raise
        return path

    def load(self, test_id: str) -> Dict[str, Any]:
        """Return the archived record for `test_id`; raises KeyError if there isn't one."""
        path = self.path_for(test_id)
        if not os.path.exists(path):
            raise KeyError(f"No archived result for test ID {test_id!r} in {self.archive_dir}")
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def list_runs(self, ammeter_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Summaries of archived runs, oldest first, optionally for one ammeter type.

        Unreadable or corrupt files are skipped with a warning so one bad file
        doesn't hide the rest of the history.
        """
        if not os.path.isdir(self.archive_dir):
            return []

        runs = []
        for name in sorted(os.listdir(self.archive_dir)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.archive_dir, name)
            try:
                with open(path, encoding="utf-8") as f:
                    record = json.load(f)
                summary = {
                    "test_id": record["test_id"],
                    "ammeter_type": record["ammeter_type"],
                    "start_time": record["start_time"],
                    "sampling": record.get("sampling"),
                    "error_count": len(record.get("errors", [])),
                    "statistics": record.get("statistics", {}),
                }
            except (OSError, ValueError, KeyError, TypeError) as e:
                warnings.warn(f"Skipping unreadable archive file {path}: {type(e).__name__}: {e}")
                continue
            if ammeter_type is None or summary["ammeter_type"] == ammeter_type:
                runs.append(summary)

        runs.sort(key=lambda run: run["start_time"])
        return runs

    def compare(self, test_ids: Sequence[str], metrics: Optional[Sequence[str]] = None) -> Dict[str, Dict[str, Any]]:
        """
        Side-by-side statistics for the given runs: {test_id: {ammeter_type, start_time, <metric>: value}}.

        `metrics` limits the statistics shown; by default every statistic each run recorded.
        A metric a run didn't record comes back as None.
        """
        comparison = {}
        for test_id in test_ids:
            record = self.load(test_id)
            stats = record.get("statistics", {})
            names = metrics if metrics is not None else list(stats)
            row = {"ammeter_type": record["ammeter_type"], "start_time": record["start_time"]}
            row.update({name: stats.get(name) for name in names})
            comparison[test_id] = row
        return comparison
