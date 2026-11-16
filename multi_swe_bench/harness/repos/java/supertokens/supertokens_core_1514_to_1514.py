"""supertokens/supertokens-core -- JUnit tests run from a supertokens-root workspace, sqlite plugin (PR 1514).

CI (unit-test.yml) checks out supertokens-root (master), the core, and the plugin-interface branch the core's pluginInterfaceSupported.json names, runs loadModules + ./utils/setupTestEnv --local and `./gradlew test` with ST_PLUGIN_NAME=sqlite on JDK 21. Here /home is that workspace (the repo is /home/supertokens-core, so the tests' "../" paths reach the root); supertokens-root (c84e0ca02) and plugin-interface 11.2 (dafbb7953) / 11.3 (8a0abe4e5) are pinned to the last commits before the PR merged, and each stage uses the branch its own pluginInterfaceSupported.json names (the fix moves it from 11.2 to 11.3). The tests' services run in the stage container through TestServiceUtils' external mode: PostgreSQL (Ubuntu's, root/root) and Ory Hydra v2.2.0 with the CI image's hydra.yml (that image is amd64-only; the official oryd/hydra build is multi-arch and musl-linked, hence musl).

Architecture: PER-PR BASE. ImageBase.image_tag() is f"base-pr-{N}" and the base
inherits the harness default Image.dockerfile(), which clones "${REPO_URL}",
checks out ${BASE_COMMIT} and runs Image._HARDENING_BLOCK (pin + history scrub)
in the base layer. DockerfileEnhancer injects the build args / proxy / CA trust
into that base. The PR image (never enhanced) only COPYs the scripts and RUNs
prepare.sh once.

Toolchain: eclipse-temurin:21-jdk-noble (official multi-arch; CI's JDK 21), the Gradle wrappers of the workspace, postgresql, musl, Hydra v2.2.0.

Grading scope: /home/msb_stgradle.py rebuilds the workspace (setupTestEnv) for the stage's sources, compiles the core's tests and runs the src/test/java classes the test patch touches with --tests; ids are "<repo-relative test file>::<method>". Test sources that do not compile report the test methods the test patch adds or edits as FAIL.
"""

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "supertokens"
_REPO = "supertokens-core"
_REGISTER_KEY = "supertokens_core_1514_to_1514"

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
_BASE_IMAGE = "eclipse-temurin:21-jdk-noble"


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
export MSB_TIMEOUT_SECS=5400
export ST_PLUGIN_NAME=sqlite
export TEST_PG_HOST=localhost
export TEST_PG_PORT=5432
export TEST_OAUTH_HOST=localhost
export TEST_OAUTH_PUBLIC_PORT=4444
export TEST_OAUTH_ADMIN_PORT=4445
"""

_MSB_RUNNER = r"""#!/usr/bin/env python3
# supertokens-core driver for prepare.sh / run_tests.sh. CI (unit-test.yml) runs the
# core's tests from a supertokens-root workspace holding supertokens-core and the
# supertokens-plugin-interface branch the core's pluginInterfaceSupported.json names
# (sqlite plugin matrix entry): loadModules + ./utils/setupTestEnv --local + ./gradlew test.
# Here the workspace root is /home (the core repo is /home/@@REPO@@, as in CI's
# supertokens-root/supertokens-core, so the tests' "../" paths resolve to the root).
#   msb_stgradle.py setup <pi-version>  -- lay out the workspace with that plugin
#                                         interface and run setupTestEnv (provision)
#   msb_stgradle.py run                 -- setup with the stage's plugin interface, compile
#                                         the tests, run the touched test classes and print
#       MSB_TESTCASE <tab> <repo-relative test file> <tab> <[Nested::]method> <tab> PASS|FAIL|SKIP
# Test sources that do not compile at a stage (a gold test using an API the fix
# adds) report the test methods the test patch adds or edits as FAIL.
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = "/home"
CORE = "/home/@@REPO@@"
PI_STORE = "/opt/msb-plugin-interface"   # one checkout per supported branch, from the base layer
TIMEOUT = int(os.environ.get("MSB_TIMEOUT_SECS", "1500"))
GRADLE = ["./gradlew", "--no-daemon", "--console=plain"]
RESULTS = os.path.join(CORE, "build", "test-results", "test")
LIFECYCLE = {"setUp", "setup", "tearDown", "teardown", "before", "after", "beforeEach", "afterEach",
             "beforeAll", "afterAll", "init", "initMocks", "beforeClass", "afterClass"}


def call(cmd, cwd=ROOT, log=None):
    print("+ " + " ".join(cmd), flush=True)
    try:
        if log:
            with open(log, "w") as fh:
                return subprocess.run(cmd, cwd=cwd, timeout=TIMEOUT, stdout=fh, stderr=subprocess.STDOUT).returncode
        return subprocess.run(cmd, cwd=cwd, timeout=TIMEOUT).returncode
    except subprocess.TimeoutExpired:
        print("MSB_TIMEOUT " + " ".join(cmd), flush=True)
        return 124


def tail(path, n=40):
    try:
        print("".join(open(path, errors="replace").readlines()[-n:]), flush=True)
    except OSError:
        pass


def stage_pi_version():
    data = json.load(open(os.path.join(CORE, "pluginInterfaceSupported.json")))
    return data["versions"][-1]


def setup(version):
    src = os.path.join(PI_STORE, version)
    if not os.path.isdir(src):
        print("MSB_PLUGIN_INTERFACE_NOT_PROVISIONED " + version)
        return 1
    dst = os.path.join(ROOT, "supertokens-plugin-interface")
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst, symlinks=True)
    with open(os.path.join(ROOT, "modules.txt"), "w") as fh:
        fh.write("core,master\nplugin-interface,master\nsqlite-plugin,master\n")
    with open(os.path.join(ROOT, "settings.gradle"), "w") as fh:
        # what loadModules writes for these modules
        fh.write("include 'supertokens-core:cli'\ninclude 'supertokens-core:downloader'\n"
                 "include 'supertokens-core:ee'\ninclude 'supertokens-core'\n"
                 "include 'supertokens-plugin-interface'\n")
    for d in ("core", "plugin-interface", "plugin", "cli", "downloader", "ee", "agent", "temp"):
        shutil.rmtree(os.path.join(ROOT, d), ignore_errors=True)
    for f in ("version.yaml", ".testEnvRunning", "config.yaml"):
        if os.path.exists(os.path.join(ROOT, f)):
            os.remove(os.path.join(ROOT, f))
    print("MSB_PLUGIN_INTERFACE=" + version, flush=True)
    rc = call(["./utils/setupTestEnv", "--local"], log="/tmp/msb-setup.log")
    if rc:
        print("MSB_SETUP_TEST_ENV_FAILED exit=%d" % rc)
        tail("/tmp/msb-setup.log")
    return rc


def patch_files():
    out = []
    for line in open("/home/test.patch", encoding="utf-8", errors="replace"):
        m = re.match(r"^diff --git a/.* b/(.*?)\s*$", line)
        if m:
            out.append(m.group(1))
    return out


def graded_classes():
    out = []
    for f in patch_files():
        if f.startswith("src/test/java/") and f.endswith(".java") and os.path.isfile(os.path.join(CORE, f)):
            src = open(os.path.join(CORE, f), encoding="utf-8", errors="replace").read()
            if re.search(r"@Test\b", src):
                out.append((f, f[len("src/test/java/"):-len(".java")].replace("/", ".")))
    return out


MNAME = re.compile(r"(?:^|[^\w$.])void\s+([A-Za-z_$][\w$]*)\s*\(")
ISTEST = re.compile(r"@(Test|ParameterizedTest|RepeatedTest|TestFactory|TestTemplate)\b")


def gold_methods(rel):
    # Added declarations credit only themselves; a hunk that only edits a body
    # credits the enclosing method its header names (as the Gradle/Maven configs).
    out, inside, added, ctx, annot = set(), False, [], "", False

    def flush():
        nonlocal added, ctx, annot
        out.update(added if added else ([ctx] if ctx else []))
        added, ctx, annot = [], "", False

    for line in open("/home/test.patch", encoding="utf-8", errors="replace"):
        line = line.rstrip("\n")
        if line.startswith("diff --git "):
            flush()
            inside = line.endswith(" b/" + rel)
            continue
        if not inside or line.startswith(("+++", "---")):
            continue
        if line.startswith("@@"):
            flush()
            m = MNAME.search(re.sub(r"^@@[^@]*@@\s*", "", line))
            ctx = m.group(1) if m else ""
            continue
        if line.startswith("+"):
            if ISTEST.search(line):
                annot = True
            m = MNAME.search(line)
            if m:
                if annot or m.group(1).startswith("test"):
                    added.append(m.group(1))
                annot = False
        elif line.startswith(" "):
            if ISTEST.search(line):
                annot = True
            elif MNAME.search(line):
                annot = False
    flush()
    return sorted(m for m in out if m and m not in LIFECYCLE)


def run():
    rc = setup(stage_pi_version())
    if rc:
        return rc
    classes = graded_classes()
    print("MSB_TEST_CLASSES=" + ",".join(c for _, c in classes), flush=True)
    if not classes:
        print("MSB_NO_TEST_TARGETS_AT_THIS_STAGE")
        return 0
    rc = call(GRADLE + ["--offline", ":supertokens-core:testClasses"], log="/tmp/msb-compile.log")
    if rc:
        print("MSB_TEST_COMPILE_FAILED exit=%d" % rc)
        tail("/tmp/msb-compile.log", 60)
        for f, _ in classes:
            for m in gold_methods(f):
                print("MSB_TESTCASE\t%s\t%s\tFAIL" % (f, m))
        return 0
    shutil.rmtree(RESULTS, ignore_errors=True)
    args = []
    for _, c in classes:
        args += ["--tests", c]
    call(GRADLE + ["--offline", ":supertokens-core:test", "--continue"] + args, log="/tmp/msb-test.log")
    tail("/tmp/msb-test.log", 30)
    by_class = {c: f for f, c in classes}
    results = {}
    for x in glob.glob(os.path.join(RESULTS, "TEST-*.xml")):
        for tc in ET.parse(x).getroot().iter("testcase"):
            cls = tc.get("classname", "")
            outer, _, nested = cls.partition("$")
            f = by_class.get(outer)
            if f is None:
                continue
            name = tc.get("name", "")
            name = name[:-2] if name.endswith("()") else name
            key = (f, (nested + "::" + name) if nested else name)
            if tc.find("failure") is not None or tc.find("error") is not None:
                st = "FAIL"
            elif tc.find("skipped") is not None:
                st = "SKIP"
            else:
                st = "PASS"
            if results.get(key) != "FAIL":
                results[key] = st
    if not results:
        print("MSB_TEST_RUNNER_DID_NOT_COMPLETE")
        return 1
    for (f, n), st in sorted(results.items()):
        print("MSB_TESTCASE\t%s\t%s\t%s" % (f, n, st))
    return 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    os.chdir(ROOT)
    sys.exit(setup(sys.argv[2]) if mode == "setup" else run())
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
# the supertokens-root workspace around the repo
cp -a /opt/msb-supertokens-root/. /home/
# utils/project-prefix derives the module prefix from the workspace folder's name
# (supertokens-root -> supertokens); this workspace is /home, so it is stated
printf '#!/bin/bash\necho supertokens\n' > /home/utils/project-prefix
# PostgreSQL role the tests connect as (CI's postgres container: root/root)
pgv=$(ls /usr/lib/postgresql | sort -n | tail -1)
pg_ctlcluster "$pgv" main start
for _ in $(seq 60); do pg_isready -q && break; sleep 1; done
su postgres -c "psql -qc \"CREATE ROLE root SUPERUSER LOGIN PASSWORD 'root'\""
su postgres -c "psql -qc \"CREATE DATABASE root OWNER root\""
pg_ctlcluster "$pgv" main stop
# both plugin-interface branches built once, so every stage resolves offline
for v in 11.3 11.2; do python3 /home/msb_stgradle.py setup "$v"; done
(cd /home && ./gradlew --no-daemon --console=plain :supertokens-core:testClasses)
# the test task's runtime classpath too (webauthn4j-test's runtime dependencies), so
# the stages' offline `test` resolves
cat > /home/msb-resolve.gradle <<'EOF'
allprojects { p ->
    if (p.path == ':supertokens-core') {
        p.tasks.register('msbResolveTestRuntime') {
            doLast { println 'MSB_TEST_RUNTIME_JARS=' + p.configurations.testRuntimeClasspath.files.size() }
        }
    }
}
EOF
(cd /home && ./gradlew --no-daemon --console=plain -I /home/msb-resolve.gradle :supertokens-core:msbResolveTestRuntime)

# ---- Section 3: GATE (hard, never tolerated) --------------------------------
(cd /home && ./gradlew --no-daemon --console=plain --offline -q :supertokens-core:testClasses)
(cd /home && ./gradlew --no-daemon --console=plain --offline -I /home/msb-resolve.gradle :supertokens-core:msbResolveTestRuntime)
test -x /usr/local/bin/hydra && hydra version >/dev/null
test -f /home/temp/config.yaml
echo "DEPS_OK"
"""

_RUN_TESTS = r"""#!/bin/bash
# The single test command shared verbatim by run.sh, test-run.sh and fix-run.sh.
# Test failures are data (reported per test); a runner that does not complete
# exits non-zero.
set -o pipefail
cd /home/@@REPO@@
source /home/msb_env.sh
# CI's test services, started in the stage container: PostgreSQL (TestServiceUtils' external
# mode, TEST_PG_*) and the OAuth provider (TEST_OAUTH_*)
pgv=$(ls /usr/lib/postgresql | sort -n | tail -1)
pg_ctlcluster "$pgv" main start 2>/dev/null || true
for _ in $(seq 60); do pg_isready -q && break; sleep 1; done
if ! curl -sf localhost:4445/health/ready >/dev/null; then
    (cd / && DSN='sqlite:///var/lib/sqlite/db.sqlite?_fk=true' nohup hydra serve -c /hydra.yml all --dev > /tmp/hydra.log 2>&1 &)
    for _ in $(seq 60); do curl -sf localhost:4445/health/ready >/dev/null && break; sleep 1; done
fi
python3 /home/msb_stgradle.py run
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


class SupertokensCoreImageBase(Image):
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
        return ["postgresql", "musl", "python3"]

    def extra_setup(self) -> str:
        return "# Ory Hydra v2.2.0 -- the build inside CI's rishabhpoddar/oauth-server-cicd image (amd64-only),\n# from Ory's official multi-arch image; that image's /hydra.yml and SQLite DSN\nCOPY --from=oryd/hydra:v2.2.0 /usr/bin/hydra /usr/local/bin/hydra\nRUN echo 'c2VydmU6CiAgICBjb29raWVzOgogICAgICAgIHNhbWVfc2l0ZV9tb2RlOiBMYXgKCnVybHM6CiAgICBzZWxmOgogICAgICAgIGlzc3VlcjogaHR0cDovL2xvY2FsaG9zdDo0NDQ0CiAgICBjb25zZW50OiBodHRwOi8vbG9jYWxob3N0OjMwMDEvYXV0aC9vYXV0aC9jb25zZW50CiAgICBsb2dpbjogaHR0cDovL2xvY2FsaG9zdDozMDAxL2F1dGgvb2F1dGgvbG9naW4KICAgIGxvZ291dDogaHR0cDovL2xvY2FsaG9zdDozMDAxL2F1dGgvb2F1dGgvbG9nb3V0CgpzZWNyZXRzOgogICAgc3lzdGVtOgogICAgICAgIC0gc2VjcmV0c2VjcmV0c2VjcmV0c2VjcmV0CgpvaWRjOgogICAgc3ViamVjdF9pZGVudGlmaWVyczoKICAgICAgICBzdXBwb3J0ZWRfdHlwZXM6CiAgICAgICAgICAgIC0gcGFpcndpc2UKICAgICAgICAgICAgLSBwdWJsaWMKICAgICAgICBwYWlyd2lzZToKICAgICAgICAgICAgc2FsdDogc2VjcmV0c2VjcmV0c2VjcmV0c2VjcmV0Cg==' | base64 -d > /hydra.yml \\\n    && mkdir -p /var/lib/sqlite \\\n    && DSN='sqlite:///var/lib/sqlite/db.sqlite?_fk=true' hydra migrate -c /hydra.yml sql -e --yes\n# supertokens-root and the plugin-interface branches, pinned to the PR's merge time\nRUN git clone -q https://github.com/supertokens/supertokens-root.git /opt/msb-supertokens-root \\\n    && git -C /opt/msb-supertokens-root checkout -q --detach c84e0ca0258c750a41118cdad664e59b55e3734c \\\n    && rm -rf /opt/msb-supertokens-root/.git \\\n    && git clone -q https://github.com/supertokens/supertokens-plugin-interface.git /opt/msb-plugin-interface/11.2 \\\n    && git -C /opt/msb-plugin-interface/11.2 checkout -q --detach dafbb7953422f63ea0680003e60b132c4cc29c10 \\\n    && rm -rf /opt/msb-plugin-interface/11.2/.git \\\n    && git clone -q https://github.com/supertokens/supertokens-plugin-interface.git /opt/msb-plugin-interface/11.3 \\\n    && git -C /opt/msb-plugin-interface/11.3 checkout -q --detach 8a0abe4e5b50f484780ab4be3f760b60127fc55b \\\n    && rm -rf /opt/msb-plugin-interface/11.3/.git \\\n    && true"


class SupertokensCoreImageDefault(Image):
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
        return SupertokensCoreImageBase(self.pr, self._config)

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
            File(".", "msb_stgradle.py", _render(_MSB_RUNNER, pr)),
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
class SupertokensCore(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SupertokensCoreImageDefault(self.pr, self._config)

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
