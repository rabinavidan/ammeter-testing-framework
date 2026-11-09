"""
Allure results collector for sharded test runs (stdlib only; runs in the Docker `report` image).

Shards on different nodes can't share a ReadWriteOnce volume, so each uploads its results here instead:

    PUT  /runs/<run>/shards/<index>?of=<count>   body: tar.gz of an allure-results folder
    GET  /runs/<run>                             JSON status: shards received, expected, report ready
    GET  /runs/<run>/report.html                 the merged single-file Allure report
    GET  /healthz                                liveness/readiness probe

When all <count> shards of a run have arrived, the collector runs `allure generate` over their merged results
and serves the single-file report. Data lives under --data (a PersistentVolume in Kubernetes).

    python tools/results_server.py --data /data --port 8080
"""
import argparse
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
_lock = threading.Lock()


def safe_extract(archive: tarfile.TarFile, destination: str) -> None:
    """Extract regular files only, refusing paths that would escape `destination`."""
    root = os.path.realpath(destination)
    for member in archive.getmembers():
        target = os.path.realpath(os.path.join(destination, member.name))
        if not member.isfile() or not target.startswith(root + os.sep):
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        source = archive.extractfile(member)
        if source is not None:
            with source, open(target, "wb") as out:
                shutil.copyfileobj(source, out)


class Collector(BaseHTTPRequestHandler):
    data_dir = "/data"

    def _json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _run_dir(self, run: str) -> str:
        return os.path.join(self.data_dir, "runs", run)

    def _status(self, run: str) -> dict:
        run_dir = self._run_dir(run)
        meta_path = os.path.join(run_dir, "meta.json")
        if not os.path.exists(meta_path):
            return {"expected": None, "received": [], "report_ready": False}
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        meta["report_ready"] = os.path.exists(os.path.join(run_dir, "report.html"))
        return meta

    def do_GET(self):  # noqa: N802 (http.server naming)
        path = urlparse(self.path).path
        if path == "/healthz":
            return self._json(200, {"ok": True})
        match = re.fullmatch(r"/runs/([^/]+)(/report\.html)?", path)
        if not match or not NAME.match(match.group(1)):
            return self._json(404, {"error": "not found"})
        run = match.group(1)
        if not match.group(2):
            return self._json(200, {"run": run, **self._status(run)})
        report = os.path.join(self._run_dir(run), "report.html")
        if not os.path.exists(report):
            return self._json(404, {"error": "report not ready", **self._status(run)})
        with open(report, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self):  # noqa: N802
        url = urlparse(self.path)
        match = re.fullmatch(r"/runs/([^/]+)/shards/(\d+)", url.path)
        count = parse_qs(url.query).get("of", [""])[0]
        if not match or not NAME.match(match.group(1)) or not count.isdigit() or int(count) < 1:
            return self._json(400, {"error": "use PUT /runs/<run>/shards/<index>?of=<count>"})
        run, index, expected = match.group(1), int(match.group(2)), int(count)
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= MAX_UPLOAD_BYTES or index >= expected:
            return self._json(400, {"error": "bad upload size or shard index"})
        body = self.rfile.read(length)

        run_dir = self._run_dir(run)
        try:
            with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
                safe_extract(archive, os.path.join(run_dir, "allure-results"))
        except tarfile.TarError as e:
            return self._json(400, {"error": f"not a tar.gz: {e}"})

        with _lock:
            meta = self._status(run)
            received = sorted(set(meta.get("received", [])) | {index})
            meta = {"expected": expected, "received": received}
            os.makedirs(run_dir, exist_ok=True)
            json.dump(meta, open(os.path.join(run_dir, "meta.json"), "w", encoding="utf-8"))
            complete = len(received) == expected
        if complete:
            # The upload itself succeeded; a report failure is recorded in the run's status, never sent back as a
            # dropped connection (which would make every shard retry the same failing generation)
            try:
                error = self._generate(run_dir)
            except Exception as e:  # noqa: BLE001 - any failure here belongs in the status, not the upload
                error = f"{type(e).__name__}: {e}"
            if error:
                with _lock:
                    meta = json.load(open(os.path.join(run_dir, "meta.json"), encoding="utf-8"))
                    meta["report_error"] = error
                    json.dump(meta, open(os.path.join(run_dir, "meta.json"), "w", encoding="utf-8"))
        return self._json(201, {"run": run, **self._status(run)})

    @staticmethod
    def _generate(run_dir: str):
        """Build the merged report; returns None on success, else a description of what failed."""
        output = os.path.join(run_dir, "allure-report")
        code = subprocess.call(["allure", "generate", os.path.join(run_dir, "allure-results"), "-o", output,
                                "--clean", "--single-file"])
        if code != 0:
            return f"allure generate exited {code}"
        shutil.copy(os.path.join(output, "index.html"), os.path.join(run_dir, "report.html"))
        return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Collect sharded Allure results and build one report.")
    parser.add_argument("--data", default="/data")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    Collector.data_dir = args.data
    os.makedirs(os.path.join(args.data, "runs"), exist_ok=True)
    print(f"results collector on :{args.port}, data in {args.data}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", args.port), Collector).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
