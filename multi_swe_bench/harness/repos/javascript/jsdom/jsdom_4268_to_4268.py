"""jsdom/jsdom -- mocha API tests and the web-platform-tests runners (PRs 4268, 4417).

CI (jsdom-ci.yml) uses Node 22, `npm ci`, the web-platform-tests submodule, `wpt make-hosts-file` appended to /etc/hosts, and `npm test` (prepare, then mocha over test/api, run-tuwpts.js and run-wpts.js). The wpt tools need python (python-is-python3) and create their virtualenv on first use; it is created at provision so stages run offline. When a patch changes package-lock.json (PR 4417 swaps the CSS engine's dependencies), the npm cache is warmed at provision from the lockfile the patches produce, and a stage whose lockfile differs from the installed one reinstalls with `npm ci --offline`. `npm run prepare` (wireit code generation) runs at every stage, since fix patches edit the generators.

Architecture: PER-PR BASE. ImageBase.image_tag() is f"base-pr-{N}" and the base
inherits the harness default Image.dockerfile(), which clones "${REPO_URL}",
checks out ${BASE_COMMIT} and runs Image._HARDENING_BLOCK (pin + history scrub)
in the base layer. DockerfileEnhancer injects the build args / proxy / CA trust
into that base. The PR image (never enhanced) only COPYs the scripts and RUNs
prepare.sh once.

Toolchain: node:22-bookworm (official multi-arch; CI's node-version 22) with python-is-python3, python3-venv and python3-pip for the wpt tools.

Grading scope: /home/msb_js.py runs mocha (--reporter json) once per graded file: the test/api/*.js files the test patch touches, run-tuwpts.js (every to-upstream test), and run-wpts.js narrowed with --grep to the to-run.yaml DIR sections the test patch edits. Each WPT file is one mocha test whose title carries its expectation ("[expected fail] ..."); ids are "<repo-relative test file>::<describe > ... > test>"; a test file that fails to load at a stage reports one failing "<file load>" entry.
"""

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "jsdom"
_REPO = "jsdom"
_REGISTER_KEY = "jsdom_4268_to_4268"

# Single-PR routing (DS3 multi-arch set). Dataset rows carry no number_interval, so without
# this the row would resolve to the "{org}/{repo}" catch-all config. Only this exact PR
# number of this repo is tagged with this file's interval; every other PR is unaffected.
from multi_swe_bench.harness import pull_request as _pull_request

_INTERVAL = _REGISTER_KEY
_BOUNDS = re.search(r"_(\d+)_to_(\d+)$", _INTERVAL)
_PR_MIN = int(_BOUNDS.group(1))
_PR_MAX = int(_BOUNDS.group(2))
_ROUTING_FLAG = "_" + _INTERVAL + "_routed"

if not getattr(_pull_request.PullRequest, _ROUTING_FLAG, False):
    _orig_from_json = _pull_request.PullRequest.from_json.__func__

    def _routed_from_json(cls, json_str):
        pr = _orig_from_json(cls, json_str)
        if (
            pr.org == _ORG
            and pr.repo == _REPO
            and not pr.number_interval
            and _PR_MIN <= pr.number <= _PR_MAX
        ):
            pr.number_interval = _INTERVAL
        return pr

    _pull_request.PullRequest.from_json = classmethod(_routed_from_json)
    setattr(_pull_request.PullRequest, _ROUTING_FLAG, True)
_BASE_IMAGE = "node:22-bookworm"


_CHECK_GIT_CHANGES = r"""#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

_MSB_ENV = r"""# Sourced by prepare.sh and run_tests.sh -- one definition of the toolchain.
export CI=true
export MSB_TIMEOUT_SECS=3000
"""

_MSB_RUNNER = r"""#!/usr/bin/env python3
# JavaScript / TypeScript test driver for run_tests.sh.
#   msb_js.py run    -- runs the graded test files and prints one line per test:
#       MSB_TESTCASE <tab> <repo-relative test file> <tab> <describe > ... > test> <tab> PASS|FAIL|SKIP
#   msb_js.py list   -- prints the graded test files (provision / gate sanity).
#
# The graded files are the test files the test patch touches (CFG["test_re"],
# repo-relative) that exist at this stage, plus CFG["always"]. A snapshot file
# (__snapshots__/<name>.snap) grades the test file it belongs to.
# Frameworks (CFG["framework"]), each through its machine-readable reporter:
#   jest / vitest -- one invocation, --json / --reporter=json (the same schema)
#   mocha         -- one invocation per file, --reporter json
#   bun           -- one invocation, --reporter=junit
#   tsc           -- one `tsc --noEmit -p <tsconfig>` per CFG["tsconfigs"] entry
# A test file that fails to load (a gold test importing code the fix adds)
# has no test results; it is reported as one failing "<file load>" entry.
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

REPO = "/home/@@REPO@@"
CFG = json.loads(r'''{"framework": "mocha", "cmd": ["npx", "mocha"], "test_re": "^test/(api/[^/]+\\.js|web-platform-tests/run-(tu)?wpts\\.js)$", "always": ["test/web-platform-tests/run-tuwpts.js", "test/web-platform-tests/run-wpts.js"], "wpt_grep": {"test/web-platform-tests/run-wpts.js": {"root": "web-platform-tests", "to_run": "test/web-platform-tests/to-run.yaml"}}}''')
TIMEOUT = int(os.environ.get("MSB_TIMEOUT_SECS", "1500"))
FW = CFG["framework"]
CWD = os.path.join(REPO, CFG.get("cwd", "."))
TEST_RE = re.compile(CFG.get("test_re", r"\.(test|spec)\.[cm]?[jt]sx?$"))
SNAP_RE = re.compile(r"^(.*)/__snapshots__/([^/]+)\.snap$")
OUT = "/tmp/msb-js-report"


def patch_files(path):
    out = []
    if os.path.exists(path):
        for line in open(path, encoding="utf-8", errors="replace"):
            m = re.match(r"^diff --git a/.* b/(.*?)\s*$", line)
            if m:
                out.append(m.group(1))
    return out


def graded_files():
    files = []
    for f in patch_files("/home/test.patch") + CFG.get("always", []):
        m = SNAP_RE.match(f)
        if m:
            f = m.group(1) + "/" + m.group(2)
        if not TEST_RE.search(f):
            continue
        if any(re.search(x, f) for x in CFG.get("exclude", [])):
            continue
        if os.path.isfile(os.path.join(REPO, f)) and f not in files:
            files.append(f)
    return files


def rel_to_cwd(f):
    return os.path.relpath(os.path.join(REPO, f), CWD)


def repo_rel(p):
    p = p if os.path.isabs(p) else os.path.join(CWD, p)
    return os.path.relpath(os.path.normpath(p), REPO)


def call(cmd, **kw):
    print("+ " + " ".join(cmd), flush=True)
    try:
        return subprocess.run(cmd, cwd=CWD, timeout=TIMEOUT, **kw).returncode
    except subprocess.TimeoutExpired:
        print("MSB_TIMEOUT " + " ".join(cmd), flush=True)
        return 124


def put(results, key, st):
    if results.get(key) == "FAIL":
        return
    if results.get(key) == "PASS" and st == "SKIP":
        return
    results[key] = st


def jest_like(files, results):
    # jest --json and vitest --reporter=json share the schema.
    if os.path.exists(OUT):
        os.remove(OUT)
    args = [rel_to_cwd(f) for f in files]
    if FW == "jest":
        cmd = CFG["cmd"] + ["--ci", "--json", "--outputFile=" + OUT] + args
    else:
        cmd = CFG["cmd"] + ["--reporter=default", "--reporter=json", "--outputFile=" + OUT] + args
    rc = call(cmd)
    if not os.path.exists(OUT):
        print("MSB_TEST_RUNNER_DID_NOT_COMPLETE exit=%d" % rc)
        return rc or 1
    data = json.load(open(OUT))
    seen = set()
    for tr in data.get("testResults", []):
        f = repo_rel(tr["name"])
        seen.add(f)
        if not tr.get("assertionResults"):
            if tr.get("status") == "failed":
                put(results, (f, "<file load>"), "FAIL")
            continue
        for a in tr["assertionResults"]:
            name = " > ".join(list(a.get("ancestorTitles") or []) + [a.get("title", "")])
            st = {"passed": "PASS", "failed": "FAIL"}.get(a.get("status"), "SKIP")
            put(results, (f, name), st)
    for f in files:
        if f not in seen and rc != 0:
            put(results, (f, "<file load>"), "FAIL")
    return 0


def wpt_dirs(to_run):
    # The DIR: sections of the base to-run.yaml holding a line the test patch
    # removes or adds (jsdom): the upstream web-platform-tests run is narrowed
    # to those. Old-file line numbers are tracked through each hunk.
    base = subprocess.run(["git", "show", "HEAD:" + to_run], cwd=REPO, stdout=subprocess.PIPE,
                          text=True, errors="replace").stdout.split("\n")

    def dir_at(n):  # the DIR: section of old-file line n (1-based)
        cur = None
        for l in base[:n]:
            m = re.match(r"^DIR: (\S+)", l)
            if m:
                cur = m.group(1)
        return cur

    dirs, inside, old = [], False, 0
    lines = open("/home/test.patch", encoding="utf-8", errors="replace").read().split("\n") \
        if os.path.exists("/home/test.patch") else []
    for line in lines:
        if line.startswith("diff --git "):
            inside = line.rstrip().endswith(" b/" + to_run)
            continue
        if not inside or line.startswith(("--- ", "+++ ")):
            continue
        h = re.match(r"^@@ -(\d+)", line)
        if h:
            old = int(h.group(1))
            continue
        if line.startswith(" "):
            old += 1
        elif line.startswith("-") or line.startswith("+"):
            m = re.match(r"^[+-]DIR: (\S+)", line)
            d = m.group(1) if m else dir_at(old if line.startswith("-") else old - 1)
            if d and d not in dirs:
                dirs.append(d)
            if line.startswith("-"):
                old += 1
    return dirs


def mocha(files, results):
    for f in files:
        if os.path.exists(OUT):
            os.remove(OUT)
        extra = []
        wpt = CFG.get("wpt_grep", {}).get(f)
        if wpt:
            dirs = wpt_dirs(wpt["to_run"])
            print("MSB_WPT_DIRS=" + ",".join(dirs), flush=True)
            if not dirs:
                continue
            extra = ["--grep", "^" + re.escape(wpt["root"]) + " (" + "|".join(re.escape(d) for d in dirs) + ") "]
        cmd = CFG["cmd"] + extra + ["--reporter", "json", "--reporter-option", "output=" + OUT, rel_to_cwd(f)]
        rc = call(cmd)
        data = None
        if os.path.exists(OUT):
            try:
                data = json.load(open(OUT))
            except ValueError:
                data = None
        if not data or (not data.get("tests") and rc != 0):
            put(results, (f, "<file load>"), "FAIL")
            continue
        pending = {t.get("fullTitle") for t in data.get("pending", [])}
        for t in data.get("tests", []):
            name = t.get("fullTitle") or t.get("title", "")
            if name in pending:
                st = "SKIP"
            elif t.get("err") and t["err"] != {}:
                st = "FAIL"
            else:
                st = "PASS"
            put(results, (f, name), st)
        for t in data.get("failures", []):
            # hook failures ("before all" hook ...) surface only here
            put(results, (f, t.get("fullTitle") or t.get("title", "")), "FAIL")
    return 0


def bun(files, results):
    if os.path.exists(OUT):
        os.remove(OUT)
    cmd = CFG["cmd"] + ["--reporter=junit", "--reporter-outfile=" + OUT] + ["./" + rel_to_cwd(f) for f in files]
    rc = call(cmd)
    if not os.path.exists(OUT):
        print("MSB_TEST_RUNNER_DID_NOT_COMPLETE exit=%d" % rc)
        return rc or 1
    root = ET.parse(OUT).getroot()
    seen = set()

    def walk(node, f, titles):
        for ch in node:
            if ch.tag == "testsuite":
                nf = f
                name = ch.get("name", "")
                if f is None:
                    nf = repo_rel(ch.get("file") or name)
                    walk(ch, nf, [])
                else:
                    walk(ch, nf, titles + [name])
            elif ch.tag == "testcase":
                cf = f or repo_rel(ch.get("file") or ch.get("classname", ""))
                seen.add(cf)
                name = " > ".join(titles + [ch.get("name", "")])
                if ch.find("failure") is not None or ch.find("error") is not None:
                    st = "FAIL"
                elif ch.find("skipped") is not None:
                    st = "SKIP"
                else:
                    st = "PASS"
                put(results, (cf, name), st)

    walk(root, None, [])
    for f in files:
        if f not in seen:
            put(results, (f, "<file load>"), "FAIL")
    return 0


def tsc(results):
    for tc in CFG["tsconfigs"]:
        if not os.path.isfile(os.path.join(REPO, tc)):
            continue
        rc = call(CFG["cmd"] + ["--noEmit", "-p", rel_to_cwd(tc)])
        put(results, (tc, "tsc --noEmit"), "PASS" if rc == 0 else "FAIL")
    return 0


def main(mode):
    os.chdir(CWD)
    files = graded_files() if FW != "tsc" else []
    print("MSB_TEST_FILES=" + ",".join(files), flush=True)
    if mode == "list":
        return 0
    results = {}
    if FW == "tsc":
        rc = tsc(results)
    elif not files:
        print("MSB_NO_TEST_TARGETS_AT_THIS_STAGE")
        return 0
    elif FW in ("jest", "vitest"):
        rc = jest_like(files, results)
    elif FW == "mocha":
        rc = mocha(files, results)
    elif FW == "bun":
        rc = bun(files, results)
    else:
        raise SystemExit("unknown framework " + FW)
    for (f, name), st in sorted(results.items()):
        print("MSB_TESTCASE\t%s\t%s\t%s" % (f, name.replace("\t", " ").replace("\n", " "), st))
    # Test failures are data; only a runner that did not complete is an error.
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "run"))
"""

_PREPARE = r"""#!/bin/bash
set -e

# ---- Section 1: PIN ---------------------------------------------------------
cd /home/@@REPO@@
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach @@SHA@@
bash /home/check_git_changes.sh

# ---- Section 2: PROVISION (the tree may get dirty from here on) -------------
source /home/msb_env.sh
git submodule update --init --recursive --depth 1
npm ci --no-audit --no-fund
sha256sum package-lock.json > /home/.msb-lock.sha
# A patch that changes package-lock.json: warm the npm cache with the lockfile
# the stages will see, so a stage can reinstall offline.
if grep -q '^diff --git a/package-lock.json ' /home/test.patch /home/fix.patch; then
    rm -rf /tmp/msb-deps && mkdir /tmp/msb-deps && cp package.json package-lock.json /tmp/msb-deps/
    (cd /tmp/msb-deps \
        && for p in /home/test.patch /home/fix.patch; do
               git apply --whitespace=nowarn --include=package.json --include=package-lock.json "$p"
           done \
        && npm ci --ignore-scripts --no-audit --no-fund >/dev/null)
    rm -rf /tmp/msb-deps
fi
# the wpt tools' virtualenv (lint + serve requirements)
(cd test/web-platform-tests/tests && ./wpt lint --help >/dev/null && ./wpt serve --help >/dev/null)

# ---- Section 3: GATE (hard, never tolerated) --------------------------------
test -x test/web-platform-tests/tests/_venv3/bin/python
test -f test/web-platform-tests/tests/wpt
npx --no-install mocha --version
python3 /home/msb_js.py list
echo "DEPS_OK"
"""

_RUN_TESTS = r"""#!/bin/bash
# The single test command shared verbatim by run.sh, test-run.sh and fix-run.sh.
# Test failures are data (reported per test); a runner that does not complete
# exits non-zero.
set -o pipefail
cd /home/@@REPO@@
source /home/msb_env.sh
# web-platform-tests hosts (the container's /etc/hosts is per run)
grep -q web-platform.test /etc/hosts || ./test/web-platform-tests/tests/wpt make-hosts-file >> /etc/hosts
# a patch changed the lockfile: reinstall from the cache warmed at provision
if ! sha256sum -c --status /home/.msb-lock.sha; then
    npm ci --offline --no-audit --no-fund > /tmp/msb-npm.log 2>&1 || { echo "MSB_NPM_CI_FAILED"; tail -30 /tmp/msb-npm.log; }
fi
npm run prepare > /tmp/msb-prepare.log 2>&1 || { echo "MSB_PREPARE_FAILED"; tail -40 /tmp/msb-prepare.log; }
python3 /home/msb_js.py run
"""

_APPLY_FN = r"""# Apply one patch file. Each file gets its own context ladder: full context
# first; only if the base commit moved under the patch's context (the PR
# branch was cut from an older parent than base.sha) retry with 2, then 1,
# lines. git apply is atomic per file, so a retry never meets half-applied
# hunks of the same file; if no attempt applies, the stage fails loudly.
# Entries of the form "Binary files A and B differ" (the patch was exported
# without --binary) carry no bytes at all and can never apply; only those
# paths are excluded, every other hunk is applied as-is.
_msb_apply() {
    local _ex=() _f
    while IFS= read -r _f; do
        [ -n "$_f" ] && _ex+=("--exclude=$_f")
    done < <(sed -nE 's#^Binary files (.*) and (.*) differ$#\1\n\2#p' "$1" \
                 | grep -v '^/dev/null$' | sed -E 's#^[ab]/##' | sort -u)
    git apply --whitespace=nowarn "${_ex[@]}" "$1" \
        || git apply --whitespace=nowarn -C2 "${_ex[@]}" "$1" \
        || git apply --whitespace=nowarn -C1 "${_ex[@]}" "$1"
}
"""


def _apply(patches: str) -> str:
    return "\n".join(f"_msb_apply {p}" for p in patches.split())


def _stage(apply_line: str) -> str:
    return (
        "#!/bin/bash\n"
        "set -eo pipefail\n"
        "export CI=true\n"
        "\n"
        + (_APPLY_FN + "\n" if apply_line else "")
        + "cd /home/@@REPO@@\n"
        + (apply_line + "\n" if apply_line else "")
        + "bash /home/run_tests.sh\n"
    )


_RUN = _stage("")
_TEST_RUN = _stage(_apply("/home/test.patch"))
_FIX_RUN = _stage(_apply("/home/test.patch /home/fix.patch"))


def _render(body: str, pr: PullRequest) -> str:
    return body.replace("@@REPO@@", pr.repo).replace("@@SHA@@", pr.base.sha)


class JsdomImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return _BASE_IMAGE

    def image_tag(self) -> str:
        # PR-scoped: the default dockerfile() pins ${BASE_COMMIT} and scrubs
        # history in this layer, so it must never be shared across PRs.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return ["python-is-python3", "python3-venv", "python3-pip"]

    def extra_setup(self) -> str:
        return ""


class JsdomImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image:
        return JsdomImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        pr = self.pr
        return [
            File(".", "fix.patch", f"{pr.fix_patch}"),
            File(".", "test.patch", f"{pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "msb_env.sh", _render(_MSB_ENV, pr)),
            File(".", "msb_js.py", _render(_MSB_RUNNER, pr)),
            File(".", "prepare.sh", _render(_PREPARE, pr)),
            File(".", "run_tests.sh", _render(_RUN_TESTS, pr)),
            File(".", "run.sh", _render(_RUN, pr)),
            File(".", "test-run.sh", _render(_TEST_RUN, pr)),
            File(".", "fix-run.sh", _render(_FIX_RUN, pr)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}

"""


_CASE_RE = re.compile(r"^MSB_TESTCASE\t([^\t\n]+)\t([^\t\n]+)\t(PASS|FAIL|SKIP)[ \t\r]*$", re.MULTILINE)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_DIFF_B_RE = re.compile(r"^diff --git a/\S+ b/(\S+)$", re.MULTILINE)


@Instance.register(_ORG, _REGISTER_KEY)
class Jsdom(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return JsdomImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        return "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        return "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        return "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        clean = _ANSI_RE.sub("", test_log)

        # Tests defined in a file the FIX patch writes (Rust keeps unit tests in
        # the source file they cover) are the fix's own tests, not the gold
        # test patch's: crediting them would grade the fix against itself, so
        # they are not reported at any stage. Gold test files are never in the
        # fix patch.
        fix_files = set(_DIFF_B_RE.findall(self.pr.fix_patch or "")) - set(
            _DIFF_B_RE.findall(self.pr.test_patch or "")
        )

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # "<repo-relative source file>::<test path>": report.py splits on the
        # first "::" to map a credited test back to the file that defines it.
        for path, name, status in _CASE_RE.findall(clean):
            if path in fix_files:
                continue
            test_id = f"{path}::{name}"
            if status == "FAIL":
                failed_tests.add(test_id)
            elif status == "SKIP":
                skipped_tests.add(test_id)
            else:
                passed_tests.add(test_id)

        # Failure wins over pass, pass wins over skip; the buckets never overlap.
        passed_tests -= failed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
