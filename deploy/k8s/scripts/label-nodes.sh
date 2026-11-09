#!/usr/bin/env sh
# Assign node pools. Workloads PREFER their pool (nodeAffinity), so they still schedule on a single-node cluster;
# on a real cluster make the pools dedicated with taints (see deploy/k8s/README.md, "Nodes and pools").
#   qa.ammeter/pool=sut           the system under test: emulator pods
#   qa.ammeter/pool=test-workers  sharded pytest pods
# Usage: deploy/k8s/scripts/label-nodes.sh <sut-node> <worker-node> [<worker-node> ...]
# Default (Docker Desktop multi-node): desktop-worker = sut, desktop-worker2 and desktop-worker3 = test-workers.
set -eu
SUT="${1:-desktop-worker}"
[ $# -gt 0 ] && shift
WORKERS="${*:-desktop-worker2 desktop-worker3}"
kubectl label node "$SUT" qa.ammeter/pool=sut --overwrite
for node in $WORKERS; do kubectl label node "$node" qa.ammeter/pool=test-workers --overwrite; done
kubectl get nodes -L qa.ammeter/pool
