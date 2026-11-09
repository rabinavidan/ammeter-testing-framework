#!/usr/bin/env bash
# Deploy one environment and wait until it has proved itself. Used by CI (the deploy-* jobs in ci.yml) and by hand.
#
#   tools/deploy.sh <staging|regression|preprod|production> <test-image> <report-image>
#   tools/deploy.sh staging ghcr.io/acme/ammeter-qa:3f2a9c0 ghcr.io/acme/ammeter-qa-report:3f2a9c0
#   tools/deploy.sh staging ammeter-qa:test ammeter-qa-report:latest       # local cluster (images loaded into nodes)
#
# 1. Pins both images with a throwaway Kustomize layer on top of the overlay (the overlays stay untouched).
# 2. Deletes the previous run's Jobs: a Job's spec is immutable, so `apply` alone can't start a new run.
# 3. Applies, waits for the emulators to be ready, then for the test-run and env-check Jobs to complete.
# 4. On failure prints each failed pod's last log lines and exits 1, so CI stops the promotion there.
# Production has no emulators and no test Jobs: the deploy is verified by the device-check CronJob existing.
set -euo pipefail

ENV="${1:?usage: deploy.sh <env> <test-image> <report-image>}"
IMAGE="${2:?test image required}"
REPORT_IMAGE="${3:?report image required}"
TIMEOUT="${DEPLOY_TIMEOUT:-600s}"
NS="ammeter-${ENV}"
K8S="$(cd "$(dirname "$0")/../deploy/k8s" && pwd)"
[ -d "$K8S/overlays/$ENV" ] || { echo "No overlay for environment '$ENV'"; exit 2; }

split() {  # "registry/name:tag" -> name and tag (a digest ref "name@sha256:..." is kept whole as the name)
  local ref="$1"
  if [[ "$ref" == *@* ]]; then echo "$ref" ""; return; fi
  local tag="${ref##*:}"; local name="${ref%:*}"
  if [[ "$name" == "$ref" || "$tag" == */* ]]; then echo "$ref" "latest"; else echo "$name" "$tag"; fi
}
read -r IMG_NAME IMG_TAG <<<"$(split "$IMAGE")"
read -r REP_NAME REP_TAG <<<"$(split "$REPORT_IMAGE")"

LAYER="$K8S/.deploy-$ENV"                  # inside deploy/k8s so the overlay is a relative path (git-ignored)
rm -rf "$LAYER"; mkdir -p "$LAYER"
trap 'rm -rf "$LAYER"' EXIT
image_entry() {  # name, newName, newTag (empty tag = digest already in newName)
  if [ -n "$3" ]; then echo "  - {name: $1, newName: $2, newTag: \"$3\"}"; else echo "  - {name: $1, newName: \"$2\"}"; fi
}
{
  echo "apiVersion: kustomize.config.k8s.io/v1beta1"
  echo "kind: Kustomization"
  echo "resources: [../overlays/$ENV]"
  echo "images:"
  image_entry ammeter-qa "$IMG_NAME" "$IMG_TAG"
  image_entry ammeter-qa-report "$REP_NAME" "$REP_TAG"
} >"$LAYER/kustomization.yaml"

echo "== deploy $ENV: $IMAGE (report: $REPORT_IMAGE)"
kubectl kustomize "$LAYER" >/dev/null                       # fails fast on a broken overlay, before touching the cluster
kubectl delete job test-run env-check -n "$NS" --ignore-not-found --wait=true
kubectl apply -k "$LAYER"

fail() {
  echo "!! $1"
  for pod in $(kubectl get pods -n "$NS" --field-selector=status.phase=Failed -o name 2>/dev/null); do
    echo "--- $pod"; kubectl logs -n "$NS" "$pod" --tail=30 || true
  done
  exit 1
}

if [ "$ENV" = "production" ]; then
  kubectl get cronjob device-check -n "$NS" >/dev/null || fail "device-check CronJob missing"
  echo "== production deployed: real devices are checked every 15 minutes by the device-check CronJob"
  exit 0
fi

echo "== waiting for the emulators"
for name in greenlee entes circutor; do
  kubectl rollout status "deployment/$name" -n "$NS" --timeout="$TIMEOUT" || fail "emulator $name did not become ready"
done

echo "== waiting for the test run and the environment check"
for job in test-run env-check; do
  # Complete or Failed, whichever comes first; a Job with failed indexes ends as Failed
  if ! kubectl wait "job/$job" -n "$NS" --for=condition=Complete --timeout="$TIMEOUT" 2>/dev/null; then
    fail "job $job did not complete ($(kubectl get job "$job" -n "$NS" -o jsonpath='{.status.conditions[*].type}' 2>/dev/null))"
  fi
done

for pod in $(kubectl get pods -n "$NS" -l app.kubernetes.io/name=test-run -o name); do
  echo "$pod: $(kubectl logs -n "$NS" "$pod" --tail=200 | grep -E '[0-9]+ (passed|failed)' | tail -1)"
done
echo "== $ENV deployed and verified"
