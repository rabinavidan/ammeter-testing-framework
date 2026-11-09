"""
Run one ammeter emulator as a long-lived service (a container or Kubernetes pod):

    python -m Ammeters.serve --type entes --port 5001 --host 0.0.0.0 [--seed 7]

Binds all interfaces by default so other pods can reach it through a Service. SIGTERM (sent by Kubernetes
before it kills a pod) and SIGINT stop the server cleanly: the request in progress finishes, then it exits 0.
"""
import argparse
import signal
import sys
from typing import Dict, Type

from Ammeters.base_ammeter import AmmeterEmulatorBase
from Ammeters.Circutor_Ammeter import CircutorAmmeter
from Ammeters.Entes_Ammeter import EntesAmmeter
from Ammeters.Greenlee_Ammeter import GreenleeAmmeter

EMULATORS: Dict[str, Type[AmmeterEmulatorBase]] = {
    "greenlee": GreenleeAmmeter,
    "entes": EntesAmmeter,
    "circutor": CircutorAmmeter,
}


def build(ammeter_type: str, port: int, host: str, seed=None) -> AmmeterEmulatorBase:
    if ammeter_type not in EMULATORS:
        raise ValueError(f"Unknown ammeter type {ammeter_type!r}. Available: {sorted(EMULATORS)}")
    return EMULATORS[ammeter_type](port, seed=seed, host=host)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Serve one ammeter emulator.")
    parser.add_argument("--type", required=True, choices=sorted(EMULATORS))
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--host", default="0.0.0.0", help="interface to bind (default 0.0.0.0: all)")
    parser.add_argument("--seed", type=int, default=None, help="fixed random seed (default: fresh each start)")
    args = parser.parse_args(argv)

    emulator = build(args.type, args.port, args.host, args.seed)
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: emulator.stop())
    emulator.start_server()
    print(f"{args.type} emulator on {args.host}:{args.port} stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
