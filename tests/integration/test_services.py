"""The pieces that run as Kubernetes services: an emulator served on its own, and the results collector."""
import io
import json
import os
import signal
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import allure

import Ammeters.serve as serve
from Ammeters.client import request_current_from_ammeter
from helpers import REPO, free_port

sys.path.insert(0, str(REPO / "tools"))
import results_server  # noqa: E402


def _subdir(tmp_path, n):
    """A separate folder per merged part, so parts never see each other's files."""
    path = tmp_path / f"part{n}"
    path.mkdir()
    return path


def _part_the_emulator_service_entrypoint(monkeypatch):
    """What an emulator pod runs: it answers on its host and port, and the stop signal ends it cleanly."""
    with allure.step("`python -m Ammeters.serve --type greenlee --port <free> --host 127.0.0.1` answers"):
        value = None
        # A free port can be taken by a parallel worker's emulator before the process binds it; the process then
        # exits (bind fails) and that other emulator ignores our command. Detect it and use another port.
        for _attempt in range(3):
            port = free_port()
            process = subprocess.Popen([sys.executable, "-m", "Ammeters.serve", "--type", "greenlee", "--port",
                                        str(port), "--host", "127.0.0.1"], cwd=REPO,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 10
                while value is None and process.poll() is None and time.monotonic() < deadline:
                    try:
                        value = request_current_from_ammeter(port, b"MEASURE_GREENLEE -get_measurement", 2,
                                                             "127.0.0.1")
                    except OSError:
                        time.sleep(0.1)
            finally:
                process.terminate()
                process.wait(timeout=10)
            if value is not None:
                break
        assert value is not None and 0.01 <= value <= 100
    with allure.step("serve.main in-process: a client reads once, then SIGINT (same handler as SIGTERM) -> exit 0"):
        port, built, replies = free_port(), {}, []
        original_build = serve.build

        def build_and_remember(*args, **kwargs):
            built["emulator"] = original_build(*args, **kwargs)
            return built["emulator"]

        monkeypatch.setattr(serve, "build", build_and_remember)

        def client():
            while "emulator" not in built:
                time.sleep(0.01)
            built["emulator"].ready.wait(5)
            replies.append(request_current_from_ammeter(port, b"MEASURE_ENTES -get_data", 2, "127.0.0.1"))
            signal.raise_signal(signal.SIGINT)      # delivered to the main thread, where main() installed its handler

        thread = threading.Thread(target=client)
        thread.start()
        exit_code = serve.main(["--type", "entes", "--port", str(port), "--host", "127.0.0.1"])
        thread.join(timeout=5)
        assert exit_code == 0 and len(replies) == 1 and 5 <= replies[0] <= 200


def upload(url, run, index, count, files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    request = urllib.request.Request(f"{url}/runs/{run}/shards/{index}?of={count}", data=buffer.getvalue(),
                                     method="PUT")
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def _part_the_results_collector(tmp_path, monkeypatch):
    """Shards merge into one report when the last arrives; escaping paths are refused; report failures are reported."""
    generated = []

    def fake_generate(run_dir):          # no Java needed: write a stand-in report
        generated.append(run_dir)
        open(os.path.join(run_dir, "report.html"), "w", encoding="utf-8").write("<html>merged</html>")

    monkeypatch.setattr(results_server.Collector, "_generate", staticmethod(fake_generate))
    results_server.Collector.data_dir = str(tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), results_server.Collector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with allure.step("Shard 0 of 2: received [0], no report; shard 1: both received, report built once"):
            assert upload(url, "run1", 0, 2, {"a-result.json": "{}"})["report_ready"] is False
            status = upload(url, "run1", 1, 2, {"b-result.json": "{}"})
            assert status["received"] == [0, 1] and status["report_ready"] and len(generated) == 1
            assert sorted(os.listdir(tmp_path / "runs" / "run1" / "allure-results")) == ["a-result.json",
                                                                                         "b-result.json"]
            with urllib.request.urlopen(f"{url}/runs/run1/report.html", timeout=10) as response:
                assert b"merged" in response.read()
        with allure.step("An archive containing '../../escaped.txt' writes only its safe file"):
            upload(url, "run2", 0, 1, {"../../escaped.txt": "x", "ok.json": "{}"})
            assert not (tmp_path / "escaped.txt").exists()
            assert os.listdir(tmp_path / "runs" / "run2" / "allure-results") == ["ok.json"]
        with allure.step("Report generation fails: the upload is still accepted and the status says why"):
            def broken_generate(run_dir):
                raise FileNotFoundError("allure")

            monkeypatch.setattr(results_server.Collector, "_generate", staticmethod(broken_generate))
            status = upload(url, "run3", 0, 1, {"a.json": "{}"})
            assert not status["report_ready"] and "FileNotFoundError" in status["report_error"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _part_shards_split_the_selected_tests_evenly():
    """What the test-run pods do: N shards of the `-m` selection, disjoint, complete and balanced."""
    def collect(selection, count=None, index=None):
        env = dict(os.environ)
        env.pop("JOB_COMPLETION_INDEX", None)
        env.update({"PYTEST_SHARD_COUNT": str(count), "PYTEST_SHARD_INDEX": str(index)} if count else
                   {"PYTEST_SHARD_COUNT": "1"})
        out = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
                              "-m", selection], cwd=REPO, env=env, capture_output=True, text=True, timeout=120).stdout
        return [line for line in out.splitlines() if "::" in line]

    for selection, count in (("sanity", 2), ("regression and not timing", 4)):
        everything = collect(selection)
        shards = [collect(selection, count, i) for i in range(count)]
        sizes = [len(s) for s in shards]
        with allure.step(f"-m '{selection}' over {count} shards: sizes {sizes} of {len(everything)}, "
                         f"disjoint, complete, differing by at most one"):
            assert sorted(t for s in shards for t in s) == sorted(everything)
            assert max(sizes) - min(sizes) <= 1


def test_the_kubernetes_services(monkeypatch, tmp_path):
    """What the pods run: an emulator served as a process, the results collector, and balanced test shards."""
    with allure.step(
            "What an emulator pod runs: it answers on its host and port, and the stop signal ends it cleanly."):
        _part_the_emulator_service_entrypoint(monkeypatch)
    with allure.step(
            "Shards merge into one report when the last arrives; escaping paths are refused; report failures are "
            "reported."):
        _part_the_results_collector(_subdir(tmp_path, 2), monkeypatch)
    with allure.step("What the test-run pods do: N shards of the `-m` selection, disjoint, complete and balanced."):
        _part_shards_split_the_selected_tests_evenly()
