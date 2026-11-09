#!/usr/bin/env sh
# Local clusters only (Docker Desktop multi-node / kind): their nodes run their own containerd and can't see images
# built with `docker build`. Import the images into every node. On a real cluster, push to a registry instead and
# set the image in the overlay (kustomize `images:`).
# Usage: deploy/k8s/scripts/load-images.sh [image ...]      (default: ammeter-qa:test ammeter-qa-report:latest)
set -eu
IMAGES="${*:-ammeter-qa:test ammeter-qa-report:latest}"
for node in $(kubectl get nodes -o jsonpath='{.items[*].metadata.name}'); do
  for image in $IMAGES; do
    echo "loading ${image} into ${node}"
    docker save "${image}" | docker exec -i "${node}" ctr -n k8s.io images import --all-platforms - >/dev/null
  done
done
