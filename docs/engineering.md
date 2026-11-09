# Engineering infrastructure

None of this is required by the specification. It is the delivery tooling around the framework: quality gates and
CI, a merge rule for `main`, a specification audit, Docker, Kubernetes deployment and an agent-evaluation harness.
The framework itself, and how to use it, is in the [README](../README.md).

**Status:** the GitHub Actions workflows, git hooks, pull-request diff coverage and branch protection are
configured but take effect only once the folder is a git repository pushed to GitHub; until then they have not
run. Everything else here was run: the quality gates locally on Windows and in the Linux Docker image, and the
Kubernetes overlays on a local 4-node cluster.

## Tests and the Allure report

The suite is 26 tests in three folders, one per layer: about one test per component, each walking through that
component's behaviours as named Allure steps. Wall-clock (`timing`) and statistical (`slow`) checks are separate
tests, so they can be selected on their own. Unit-layer coverage is 93 %, `Ammeters/` 100 %.

| Folder | Layer | What's in it | Time |
|---|---|---|---|
| `tests/unit/` | `unit` | Pure logic: statistics, control chart, sampling schedule on a virtual clock, protocol parsing, archive, framework API, instrument models, calibration sweeps, fault injection, config contract. **Sockets and threads are blocked** (`tests/unit/conftest.py`), so a test that needs the network fails and must move. | ~5 s |
| `tests/integration/` | `integration` | Real emulators on free ports, real sockets: protocol framing, fault injection end to end, the example script. | ~40 s (~18 s in parallel) |
| `tests/acceptance/` | `acceptance` | The specification's kind of test: each real ammeter run through the framework, the cross-type assessment, and the instrument and supply sweeps from the config. | ~10 s |

| Marker | Meaning |
|---|---|
| `regression` | Every test (assigned automatically): the full regression suite. |
| `sanity` | 6 hand-picked tests covering each core feature end to end, in ~10 s. Gates CI before the regression. |
| `bugfix` | Guards one of the [fixes to the provided code](../README.md#fixes-to-the-provided-code). |
| `timing` | Asserts on wall-clock time; excluded from pull-request runs and run nightly (the same logic is covered on a virtual clock). |
| `slow` | Statistical checks that take a few seconds. |

```sh
python -m pytest                                   # everything
python -m pytest -m unit                           # one layer (unit / integration / acceptance)
python -m pytest -m sanity                         # the smoke set
python -m pytest -m "regression and not timing" -n auto    # the CI regression, in parallel
python -m pytest tests/unit/test_sampling.py::test_sampling_and_its_schedule   # one test
```

Tests start their own emulators on free ports and write all output to temporary folders, so they need nothing
running and never touch `results/`. They use their own config (`tests/config/test_config.yaml`); the production
`config/config.yaml` is checked by a contract test, which also checks that the Kubernetes copy
(`deploy/k8s/base/config.yaml`) uses the same ports, commands and ranges. A session-end check fails the run if any
emulator thread is left running.

**Allure report.** The suite writes Allure results when given `--alluredir`; the Allure command-line tool turns
them into a report (or use `docker compose run --rm report`, which needs nothing installed):

```sh
python -m pytest --alluredir=allure-results --clean-alluredir
allure generate allure-results -o allure-report --clean --single-file   # allure-report/index.html, opens directly
allure serve allure-results                                              # or: build and open in a browser
```

The Allure command-line tool isn't a Python package: install it with `npm install -g allure-commandline`
(it needs Java 8 or later). In the report:

- **Behaviors** groups every test by the specification section it covers (epic) and area (feature). Each test
  module is mapped to its section in `MODULE_LABELS` (`tests/conftest.py`); a new module that isn't listed stops
  the run at collection, so nothing lands in an unlabelled group.
- **Every test shows its steps**, with the real values in each step's name: setup (emulator started on port N,
  ammeter registered, framework created), the action (e.g. "Run test on 'fixed' with 10 readings at 20 Hz", with
  the result attached), and each check. A failing test marks the exact step that failed. Each test's description
  says what it proves; `bugfix`, `acceptance` and `sanity` tests are marked *critical*.
- The **acceptance** tests attach each run's time series, histogram, control chart, statistics and log, the
  comparison chart, the accuracy report and the sweep tables.
- Markers appear as tags; the overview page lists the Python, platform and library versions.

A generated report is kept as [`sample_results/allure-report.html`](../sample_results/allure-report.html).

## Docker

```sh
docker compose run --rm tests     # every quality gate: lint, unit, sanity, regression (~40 s after the first build)
docker compose run --rm demo      # the end-to-end demo; charts, archive and logs land in ./results
docker compose run --rm report    # full regression + Allure report in ./reports/allure-report.html
docker compose run --rm sanity    # the 6-test smoke set (~10 s)
```

The image (`Dockerfile`) is Python 3.14 on Linux with the exact dependency versions in `requirements-lock.txt`,
running as a non-root user. The whole suite passes in it (it was developed on Windows), so this is also the
cross-platform check.

## Continuous deployment

After `ci-gate`, on pushes to `main` only: `publish-images` pushes both images to GitHub Container Registry tagged
with the commit SHA, then `deploy-staging` → `deploy-regression` → `deploy-preprod` → `deploy-production` promote
them with `tools/deploy.sh`, which waits for each environment's test run and environment check and stops the chain
on a failure. Setup once the repository is on GitHub:

| Setting | Where | Value |
|---|---|---|
| `CD_ENABLED` | repository variable | `true` (until then the CD jobs are skipped) |
| `KUBECONFIG` | secret in each Environment `staging`, `regression`, `preprod`, `production` | that cluster's kubeconfig |
| required reviewers | Environments `preprod`, `production` | who approves a release |
| `CD_RUNNER` | repository variable (optional) | a self-hosted runner label, for a cluster GitHub's runners can't reach |
| image pull access | the cluster | make the GHCR packages public, or add an image pull secret |

Design and the verified local run: [deploy/k8s/README.md](../deploy/k8s/README.md). The delivery architecture is
also a browsable page in `site/`, published to GitHub Pages by `.github/workflows/pages.yml` (Settings > Pages >
Source: GitHub Actions).

## Quality gates: what runs before CI, and in CI

Every gate calls `tools/check.py`, so a local run, a git hook, CI and Docker all run exactly the same commands:

```sh
python tools/check.py lint          # ruff, mypy, knowledge-base check                (~5 s)
python tools/check.py unit          # unit layer + coverage floor of 90 %             (~8 s)
python tools/check.py sanity        # smoke set                                       (~10 s)
python tools/check.py regression --parallel    # all but `timing`, pytest-xdist       (~20 s)
python tools/check.py ci            # lint + unit + sanity + regression
python tools/check.py all           # ci + timing (nightly)
```

| When | What runs | Why there |
|---|---|---|
| **On commit** (git hook) | ruff, knowledge-base check | seconds; stops obvious mistakes before they're recorded |
| **On push** (git hook) | mypy, **unit layer** + coverage floor | unit tests are deterministic, need no network and take seconds: the right gate before sharing work |
| **CI, stage 1** (once pushed) | lint (ruff, mypy, kb) | cheapest; nothing else runs if it fails |
| **CI, stage 2** (in parallel) | unit + coverage report; sanity | fast feedback that the logic and each feature work |
| **CI, stage 3** | regression in parallel on Linux (Python 3.11–3.14), Windows and macOS (3.14) | the expensive, cross-platform run, only after the cheap gates pass |
| **CI, stage 4** | Allure report (single HTML artifact); Docker image builds and passes sanity | what a reviewer downloads or runs |
| **Nightly** | everything incl. `timing`, Allure report kept 30 days | wall-clock checks on quiet runners |

Install the hooks once with `pre-commit install --install-hooks --hook-type pre-commit --hook-type pre-push`
(`.pre-commit-config.yaml`). The workflows are `.github/workflows/ci.yml` and `.github/workflows/nightly.yml`.

**Merging to `main` only when CI is green.** On pull requests CI also runs a **diff-coverage** job (changed lines
in `Ammeters/` must be 100 % covered, in `src/` 90 %, `tools/changed_coverage.py`). A final job, **`ci-gate`**,
passes only if lint, unit, sanity, every regression cell, Docker and diff coverage passed; it is the single
required status check. Branch protection (`.github/branch-protection.json`) makes `main` accept changes only
through a pull request whose `ci-gate` is green and whose branch is up to date, with no force pushes, no
deletion and no exception for admins. Apply it once after pushing the repository to GitHub:

```sh
gh auth login
tools/setup_branch_protection.sh                 # current repo, branch main
```

A local pre-push hook (`tools/guard_main.py`) refuses a direct `git push` to `main` before it reaches the network.
`.github/pull_request_template.md` lists what a reviewer expects.

## Specification audit

```sh
python tools/spec_audit.py
```

Checks every requirement of `Exam/ammeter-test-specification.pdf` against the running code (26 checks), each with
what was measured. Checks compare behaviour with independently known answers rather than the presence of files:
statistics against hand-computed values, accuracy ranking against data with a known true current, the
config-driven claim by changing only the config and checking the run follows (and that 7 kinds of invalid setting
fail when the framework is created), result shape against a run where every reading failed, timing as seen by a
device in another process at 50 Hz, README sections for content and every README/docs link and anchor for a
target, sample results against the fields the current code produces, dependencies against what the code imports and what the lock pins, portability by
static rules (encodings, OS-only modules, line endings). A check that can't be run here is reported as SKIP, never
as PASS. Runs live on free ports in a temporary folder; exit code 1 if any requirement fails.

## Kubernetes: staging, regression, preprod, production

`deploy/k8s/` deploys the emulators, a sharded test run and an environment check to four environments (Kustomize
base + overlays), each its own namespace with quotas, restricted Pod Security and default-deny network policies.
The test run is an Indexed Job: each pod runs one shard of the suite (balanced round-robin split in
`tests/conftest.py`) on the `test-workers` node pool and uploads its Allure results to a collector that builds one
merged report. Production points the same Services at the real ammeters and checks them every 15 minutes.

```sh
deploy/k8s/scripts/label-nodes.sh && deploy/k8s/scripts/load-images.sh    # local cluster setup
kubectl apply -k deploy/k8s/overlays/regression                            # staging | regression | preprod | production
```

Design, environment matrix, node pools, sharding and security: [`deploy/k8s/README.md`](../deploy/k8s/README.md).

## Agent evaluation

The agents are prompts, so a change to one is measured on known defects rather than trusted:

```sh
python evals/agent_evals.py plant <dir>     # copy the repo to <dir> and plant every defect in evals/defects.json
# run spec_gap_auditor (then spec_gap_fixer) in a session opened at <dir>, with the printed prompts
python evals/agent_evals.py audit <dir>     # recall: which planted defects the report's findings name
python evals/agent_evals.py fix <dir>       # which defects are gone, and are `check.py ci` and `spec_audit` green
```

There are six defects, each an exact text substitution, so planting is reproducible and a fix can be checked
without judgement:
- a wrong median;
- a spec-audit check hard-coded to PASS;
- a missing `encoding=`;
- a false default in the README;
- a broken documentation link;
- a sample record missing a field.

Planting them already showed which layer catches what:

| Defect | Caught by scripts alone |
|---|---|
| wrong median | `spec_audit` (the unit tests missed it: their only median check used an odd count, so an even-count step was added) |
| stale sample | `spec_audit` |
| hard-coded PASS | no, and it hides the missing encoding (the check it disables would have found it) |
| missing encoding | no, while the PASS above is hard-coded |
| false README default, broken link | no: these need the auditor's judgement |

`tests/unit/test_ai_tooling.py` checks that every defect's anchor text still exists in the code, so the harness
can't silently stop planting anything, and that the scoring counts what it claims. The agents themselves have not
been scored yet: that needs one Claude Code run per agent, about 200k tokens for the auditor.
