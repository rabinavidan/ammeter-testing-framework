"""
Upload one shard's Allure results to the results collector (tools/results_server.py). Stdlib only.

    python tools/upload_results.py --server http://results:8080 --run <run-id> --shard 2 --of 4 allure-results

Retries a few times (the collector may still be starting); exits non-zero if the upload never succeeds.
"""
import argparse
import io
import sys
import tarfile
import time
import urllib.request


def pack(folder: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        archive.add(folder, arcname=".")
    return buffer.getvalue()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Upload a shard's Allure results.")
    parser.add_argument("folder")
    parser.add_argument("--server", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--shard", type=int, required=True)
    parser.add_argument("--of", type=int, required=True)
    parser.add_argument("--attempts", type=int, default=10)
    args = parser.parse_args(argv)

    body = pack(args.folder)
    url = f"{args.server.rstrip('/')}/runs/{args.run}/shards/{args.shard}?of={args.of}"
    for attempt in range(1, args.attempts + 1):
        try:
            request = urllib.request.Request(url, data=body, method="PUT",
                                             headers={"Content-Type": "application/gzip"})
            with urllib.request.urlopen(request, timeout=60) as response:
                print(f"uploaded shard {args.shard}/{args.of} of run {args.run}: {response.read().decode()}")
                return 0
        except OSError as e:
            print(f"upload attempt {attempt} failed: {e}", flush=True)
            time.sleep(min(2 * attempt, 10))
    return 1


if __name__ == "__main__":
    sys.exit(main())
