## What and why

<!-- What changes, and the reason. -->

## Checklist (CI enforces these; `ci-gate` must be green before this can merge)

- [ ] `python tools/check.py ci` passes locally (lint, unit, sanity, regression)
- [ ] Changed lines in `Ammeters/` are fully covered (`python tools/changed_coverage.py --base origin/main`)
- [ ] New behaviour has tests in the right layer (`tests/unit` unless it needs a real socket)
- [ ] README and docs updated if behaviour or commands changed
- [ ] A fix to provided code is listed in README "Fixes to the provided code" and has a `bugfix` test
