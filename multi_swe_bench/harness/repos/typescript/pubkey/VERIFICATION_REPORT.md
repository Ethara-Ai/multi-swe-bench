# Verification Report — pubkey/rxdb

## Summary

| Phase | Result | Evidence |
|-------|--------|----------|
| **1. Import Chain** | ✅ PASS | Direct module load verifies all 3 era files register independently |
| **2. Image Collision** | ✅ FIXED | `image_tag()` unique per era: `base-era1`, `base-era2`, `base-era3` |
| **3. parse_log Correctness** | ✅ FIXED | State machine removed; line-by-line 3-pattern matching; dedup added |
| **4. Script Isolation** | ✅ PASS | All 12 scripts: `set -e`, `|| true`, `2>&1` — consistent with ref configs |
| **5. Era Coverage** | ✅ PASS | 180/180 PRs covered, 0 gaps, all `number_interval` match registry keys |

---

## Phase 1: Import Chain (PASS)

**Method**: Use `runpy.run_path()` to load each era module directly, bypassing the broken `__init__` chain (Python 3.9 incompatibility in upstream `curl/curl.py`).

```
Python 3.9.25, no syntax errors in any era file
All 3 modules import without error
All 3 register in Instance._registry
```

**Registry keys created**:
| Registry Key | Class |
|---|---|
| `pubkey/rxdb_4947_to_1445` | `Rxdb4947To1445` |
| `pubkey/rxdb_6905_to_4948` | `Rxdb6905To4948` |
| `pubkey/rxdb_8218_to_6906` | `Rxdb8218To6906` |

**`__init__.py` chain**:
- `typescript/__init__.py` → imports `pubkey.*`
- `pubkey/__init__.py` → imports all 3 era modules
- Each era module imports `multi_swe_bench.harness.image`, `instance`, `pull_request`

---

## Phase 2: Docker Image Configuration (PASS)

### Image Tag Collision (FIXED)

**Problem** (pre-fix): All 3 `ImageBase` classes returned `image_tag() -> "base"`, producing identical `image_full_name()` → only 1 of 3 Docker images would be built.

**Fix**: Unique tags per era.

**Verification**:

| File | `ImageBase.image_tag()` | `ImageBase.workdir()` |
|---|---|---|
| `rxdb_4947_to_1445.py` | `"base-era1"` | `"base-era1"` |
| `rxdb_6905_to_4948.py` | `"base-era2"` | `"base-era2"` |
| `rxdb_8218_to_6906.py` | `"base-era3"` | `"base-era3"` |

Resulting full image names:
- `mswebench/pubkey_m_rxdb:base-era1`
- `mswebench/pubkey_m_rxdb:base-era2`
- `mswebench/pubkey_m_rxdb:base-era3`

No collision. Each era builds independently.

### ImageDefault class (per-PR)

Each file has an `ImageDefault` class that:
- Depends on its era's `ImageBase`
- Uses `image_tag() -> f"pr-{self.pr.number}"` — unique per PR
- Uses `workdir() -> f"pr-{self.pr.number}"` — unique per PR

### Dockerfile Structure (all 3 eras)
```dockerfile
FROM {image_name}:{tag}
{global_env}
COPY fix.patch /home/
COPY test.patch /home/
COPY check_git_changes.sh /home/
COPY prepare.sh /home/
COPY run.sh /home/
COPY test-run.sh /home/
COPY fix-run.sh /home/
RUN bash /home/prepare.sh
{clear_env}
```

---

## Phase 3: parse_log Correctness (PASS)

### Bug Found & Fixed

**Original bug**: `in_failure_details` state machine flag was set to `True` on first failure match and skipped ALL subsequent lines (including other `N)` failure lines, passing test lines, and summary lines). Result: only 1 failure ever captured.

**Fix**: Removed state machine entirely. Each line is independently matched against all 3 patterns (fail/pass/skip) in order.

### Final parse_log logic (identical across all 3 files):

```python
def parse_log(self, test_log: str) -> TestResult:
    passed_tests = set()
    failed_tests = set()
    skipped_tests = set()

    ansi_re = re.compile(r"\x1b\[[0-9;]*m")
    re_fail = re.compile(r"^\s+(\d+)\)\s+(.*)")
    re_pass = re.compile(r"^\s*[✓✔]\s+(.*)")
    re_skip = re.compile(r"^\s*[-–]\s+(.*)")

    for line in test_log.splitlines():
        clean = ansi_re.sub("", line)
        if not clean.strip():
            continue
        m = re_fail.match(clean)
        if m:
            failed_tests.add(m.group(2).strip())
            continue
        m = re_pass.match(clean)
        if m:
            passed_tests.add(m.group(1).strip())
            continue
        m = re_skip.match(clean)
        if m:
            skipped_tests.add(m.group(1).strip())

    passed_tests -= failed_tests  # dedup

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )
```

### Mocha spec output format handled:
| Line | Pattern | Matched As |
|---|---|---|
| `  ✓ should return -1` | `re_pass` | passed_tests |
| `  ✔ should work` | `re_pass` | passed_tests |
| `  1) suite test name:` | `re_fail` | failed_tests |
| `  2) another failure` | `re_fail` | failed_tests |
| `  - pending test` | `re_skip` | skipped_tests |
| `  10 passing` | (no match) | ignored |
| `  2 failing` | (no match) | ignored |
| `  1 pending` | (no match) | ignored |
| ANSI color codes | stripped first | — |

### Dedup: `passed_tests -= failed_tests`
Ensures that if mocha reports a test as both passing and later failing (edge case), the failure status wins.

---

## Phase 4: Test Script Isolation (PASS)

### Script structure per era file

Each era file generates 6 scripts embedded in `ImageDefault.files()`:

| Script | Purpose |
|---|---|
| `prepare.sh` | Reset repo, checkout base, install deps |
| `run.sh` | Transpile + run tests (no patches) |
| `test-run.sh` | Apply test.patch, transpile, run tests |
| `fix-run.sh` | Apply both patches, transpile, run tests |
| `check_git_changes.sh` | Verify clean git state |

### Script safety patterns

All scripts use:
- `#!/bin/bash`
- `set -e` — fail on error
- `|| true` — on fallible commands (install, transpile, test)
- `2>&1` — capture stderr in test output

### Era-specific commands

| Aspect | Era 1 | Era 2 | Era 3 |
|---|---|---|---|
| Package manager | `npm install` | `npm install` | `npm install -g yarn@1.22.22 && yarn install` |
| Mocha config | `--config ./config/.mocharc.js` | `--config ./config/.mocharc.cjs` | `--config ./config/.mocharc.cjs` |
| Transpile | `npm run pretest` | `npm run transpile` | `npm run transpile` |
| Cross-env | (none) | `cross-env DEFAULT_STORAGE=lokijs` | `cross-env DEFAULT_STORAGE=memory` |
| expose-gc | (none) | `--expose-gc` | `--expose-gc` |
| Node version | 20 | 20 | 22 |
| Scripts match | `scripts/transpile.js` | `scripts/transpile.mjs` | `scripts/transpile.mjs` |

### Consistency check

Patterns match existing reference configs (`babel_classic_mocha.py`, `trpc/trpc.py`):
- `|| true` fallback on test commands
- `2>&1` redirect
- `set -e` shebangs

---

## Phase 5: Era Coverage (PASS)

### Dataset coverage

| Era | `number_interval` | File | PR Count | PR Range |
|---|---|---|---|---|
| 1 | `rxdb_4947_to_1445` | `rxdb_4947_to_1445.py` | 116 | #1445–#4947 |
| 2 | `rxdb_6905_to_4948` | `rxdb_6905_to_4948.py` | 52 | #4948–#6905 |
| 3 | `rxdb_8218_to_6906` | `rxdb_8218_to_6906.py` | 12 | #6906–#8218 |
| **Total** | | **3 eras** | **180** | **#1445–#8218** |

### Coverage verification

```
Dataset entries with number_interval: 180/180 (100%)
Era 1: 116 entries with number_interval = rxdb_4947_to_1445
Era 2: 52 entries with number_interval = rxdb_6905_to_4948
Era 3: 12 entries with number_interval = rxdb_8218_to_6906
No missing number_interval values
No PRs outside era boundaries
```

### Boundary verification (git analysis)

| Transition | PR# | SHA | Analysis |
|---|---|---|---|
| Earliest Era 1 | #1445 | e68a53e18a3e | `.mocharc.js`, npm, no cross-env |
| Latest Era 1 | #4947 | c9dde5d35ddd | `.mocharc.js`, npm, has cross-env |
| Earliest Era 2 | #5415 | ff5a4e8b9324 | `.mocharc.cjs`, npm, cross-env |
| Latest Era 2 | #6898 | 5732e3ed0369 | `.mocharc.cjs`, npm, no `packageManager` |
| Earliest Era 3 | #6918 | efc7933c6438 | `.mocharc.cjs`, `packageManager: yarn@1.22.22` |
| Latest Era 3 | #8218 | 36b5e7e9fe5f | `.mocharc.cjs`, `packageManager: yarn@1.22.22` |

### Registry dispatch

`Instance.create()` looks up by `pr.number_interval`:
```
rxdb_4947_to_1445 → Rxdb4947To1445
rxdb_6905_to_4948 → Rxdb6905To4948
rxdb_8218_to_6906 → Rxdb8218To6906
```

Fallback `pubkey/rxdb` (when `number_interval=""`) is NOT registered — all 180 entries have `number_interval` set, so no fallback needed.

---

## Files Modified/Created

| File | Action | Description |
|---|---|---|
| `pubkey/__init__.py` | CREATED | Imports all 3 era modules |
| `pubkey/rxdb_4947_to_1445.py` | CREATED | Era 1: npm, `.mocharc.js`, 116 PRs |
| `pubkey/rxdb_6905_to_4948.py` | CREATED | Era 2: npm, `.mocharc.cjs`, cross-env, 52 PRs |
| `pubkey/rxdb_8218_to_6906.py` | CREATED | Era 3: yarn, `.mocharc.cjs`, cross-env, 12 PRs |
| `typescript/__init__.py` | MODIFIED | Added `from multi_swe_bench.harness.repos.typescript.pubkey import *` |
| `dataset/pubkey__rxdb_lht_final.jsonl` | MODIFIED | Added `number_interval` field to all 180 entries |
| `pubkey/VERIFICATION_REPORT.md` | CREATED | This report |

## Issues Found & Fixed

| Issue | Severity | Found In | Fix |
|---|---|---|---|
| Image tag collision (all `"base"`) | **CRITICAL** | Phase 2 | Unique tags per era |
| `in_failure_details` state machine skips failures | **BUG** | Phase 3 | Replaced with line-by-line matching |
| Missing `passed_tests -= failed_tests` dedup | **WARN** | Phase 3 | Added before TestResult |
