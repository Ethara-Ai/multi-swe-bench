import json
import posixpath
import re
from collections import defaultdict
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_GO_MINOR = "1.14"

_MODULE_PATH = "github.com/GoogleContainerTools/container-structure-test"

_DECL_BEGIN = "===MSB_TEST_DECLS_BEGIN==="
_DECL_END = "===MSB_TEST_DECLS_END==="

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_RESULT_RE = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+)")
_BUILD_FAIL_RE = re.compile(r"^FAIL\s+(\S+)\s+\[(?:build|setup) failed\]")
_COMPILE_HDR_RE = re.compile(r"^#\s+(\S+)")


_DECL_MANIFEST = r"""
echo "===MSB_TEST_DECLS_BEGIN==="
find . -name '*_test.go' -not -path './vendor/*' -print0 \
  | xargs -0 -r grep -HoE '^func +Test[A-Za-z0-9_]*' 2>/dev/null \
  | sed -E 's#^\./##; s#:func +#|#' \
  | grep -v '|TestMain$' \
  | sort -u \
  || true
echo "===MSB_TEST_DECLS_END==="
"""

_RUN_SUITE = r"""
REPORT=/home/go_test.jsonl
FALLBACK=/home/go_test_v.log
rm -f "$REPORT" "$FALLBACK"


has_results() {
  [ -s "$1" ] || return 1
  grep -qE '^\{.*"Action":|^(ok |FAIL|\?)|^[[:space:]]*--- (PASS|FAIL|SKIP):|\[build failed\]|\[setup failed\]' "$1"
}

set +e
go test -json -vet=off -count=1 -timeout 900s ./... > "$REPORT" 2>&1
STATUS=$?
set -e
echo "exit status: $STATUS"
cat "$REPORT"

if ! has_results "$REPORT"; then
  echo "no recognisable results from the -json reporter; retrying with -v"
  set +e
  go test -v -vet=off -count=1 -timeout 900s ./... > "$FALLBACK" 2>&1
  FSTATUS=$?
  set -e
  echo "fallback exit status: $FSTATUS"
  cat "$FALLBACK"
  has_results "$FALLBACK" || {
    echo "FATAL: go test did not run"
    exit 1
  }
fi
"""


def _script(repo: str, patch_block: str) -> str:
    """Assemble a run script: identical preamble, manifest and suite for every
    stage; only `patch_block` (which patches get applied) differs."""
    return (
        "#!/bin/bash\n"
        "set -eo pipefail\n"
        "\n"
        "export CI=true\n"
        "cd /home/" + repo + "\n"
        + patch_block
        + _DECL_MANIFEST
        + _RUN_SUITE
    )


_APPLY_TEST = "git apply --whitespace=nowarn /home/test.patch\n"
_APPLY_TEST_AND_FIX = "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"


class ContainerStructureTestImageBase(Image):
    """Per-PR base. Intentionally minimal and WITHOUT a `# syntax` directive, so
    DockerfileEnhancer.enhance() runs and injects the shared infrastructure
    (TARGETARCH, proxy ARGs, the SSL_CERT_FILE/CA-cert farm, OCI labels) and
    rewrites the `git clone` below into the standardized parameterized fetch plus
    Image._HARDENING_BLOCK (detach onto ${BASE_COMMIT}, strip every ref/reflog,
    gc --prune, and the git rev-list dataset-leakage assertions)."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return f"golang:{_GO_MINOR}"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        repo = self.pr.repo
        org = self.pr.org

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{org}/{repo}.git /home/{repo}"
        else:
            code = f"COPY {repo} /home/{repo}"

        env = (
            "ENV CI=true \\\n"
            "    GO111MODULE=on \\\n"
            "    GOFLAGS=-mod=vendor \\\n"
            "    CGO_ENABLED=0 \\\n"
            "    GOTOOLCHAIN=local"
        )

        return f"""FROM {image_name}

{self.global_env}

{env}

WORKDIR /home/

{code}

{self.clear_env}
"""


class ContainerStructureTestImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return ContainerStructureTestImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
""",
            ),
            File(
                ".",
                "prepare.sh",

                "#!/bin/bash\n"
                "set -e\n"
                "\n"
                "cd /home/" + repo + "\n"
                "git reset --hard\n"
                "bash /home/check_git_changes.sh\n"
                "git checkout " + self.pr.base.sha + "\n"
                "bash /home/check_git_changes.sh\n"
                "\n"
                "go build ./... || true\n"
                "go test -run 'a^' -vet=off -count=1 ./... || true\n",
            ),
            File(".", "run.sh", _script(repo, "")),
            File(".", "test-run.sh", _script(repo, _APPLY_TEST)),
            File(".", "fix-run.sh", _script(repo, _APPLY_TEST_AND_FIX)),
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


@Instance.register("GoogleContainerTools", "container-structure-test")
class ContainerStructureTest(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ContainerStructureTestImageDefault(self.pr, self._config)

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

    @staticmethod
    def _rel_pkg(pkg: str) -> str:
        """`go test` Package import path -> repo-relative directory."""
        pkg = pkg.strip()
        if pkg == _MODULE_PATH:
            return ""
        if pkg.startswith(_MODULE_PATH + "/"):
            return pkg[len(_MODULE_PATH) + 1 :]
        if pkg.startswith("./"):
            return pkg[2:]
        return pkg

    def parse_log(self, test_log: str) -> TestResult:
        log = _ANSI_RE.sub("", test_log)
        lines = log.splitlines()

        name_to_files: dict[str, list[str]] = defaultdict(list)
        dir_tests: dict[str, list[tuple[str, str]]] = defaultdict(list)
        in_decl = False
        for line in lines:
            s = line.strip()
            if s == _DECL_BEGIN:
                in_decl = True
                continue
            if s == _DECL_END:
                in_decl = False
                continue
            if not in_decl or "|" not in s:
                continue
            path, _, name = s.rpartition("|")
            path, name = path.strip(), name.strip()
            if not path.endswith("_test.go") or not name.startswith("Test"):
                continue
            if path not in name_to_files[name]:
                name_to_files[name].append(path)
            entry = (path, name)
            d = posixpath.dirname(path)
            if entry not in dir_tests[d]:
                dir_tests[d].append(entry)

        def key(test: str, pkg_dir: Optional[str]) -> str:
            """`<repo-relative _test.go path>::<test identifier>`.

            The manifest resolves the declaring FILE, which `go test` never
            reports. Subtests (`TestFoo/sub`) resolve through their root name so
            parent and subtest share one file prefix. Falls back to the package
            DIRECTORY only if the manifest has no entry — which cannot happen for
            a test that actually ran, since the manifest is built from the very
            tree that was compiled."""
            root = test.split("/", 1)[0]
            candidates = name_to_files.get(root, [])
            path: Optional[str] = None
            if len(candidates) == 1:
                path = candidates[0]
            elif len(candidates) > 1:
                same = [c for c in candidates if posixpath.dirname(c) == pkg_dir]
                path = same[0] if len(same) == 1 else pkg_dir
            elif pkg_dir:
                path = pkg_dir
            return f"{path}::{test}" if path else test

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        build_failed_dirs: set[str] = set()

        def record(status: str, test: str, pkg_dir: Optional[str]) -> None:
            name = key(test, pkg_dir)
            if status == "pass":
                passed_tests.add(name)
            elif status == "fail":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        def scan_plain(text: str, pkg_dir: Optional[str]) -> None:
            """Signals that appear as plain text — both on their own lines (the
            -v fallback, and go's build errors, which are not test2json events)
            and nested inside a -json event's Output string."""
            for raw_line in text.splitlines():
                m = _RESULT_RE.match(raw_line)
                if m:
                    record(m.group(1).lower(), m.group(2), pkg_dir)
                    continue
                stripped = raw_line.strip()
                m = _BUILD_FAIL_RE.match(stripped)
                if m:
                    build_failed_dirs.add(self._rel_pkg(m.group(1)))
                    continue
                m = _COMPILE_HDR_RE.match(stripped)
                if m and m.group(1).startswith(_MODULE_PATH):
                    build_failed_dirs.add(self._rel_pkg(m.group(1)))

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("{"):
                try:
                    ev = json.loads(stripped)
                except Exception:
                    ev = None
                if isinstance(ev, dict) and "Action" in ev:
                    action = ev.get("Action")
                    test = ev.get("Test")
                    pkg = ev.get("Package") or ""
                    pkg_dir = self._rel_pkg(pkg) if pkg else None
                    if test and action in ("pass", "fail", "skip"):
                        record(action, test, pkg_dir)
                    output = ev.get("Output") or ""
                    if output:
                        if "[build failed]" in output or "[setup failed]" in output:
                            if pkg_dir is not None:
                                build_failed_dirs.add(pkg_dir)
                        scan_plain(output, pkg_dir)
                    continue
            scan_plain(line, None)

        for d in build_failed_dirs:
            for path, name in dir_tests.get(d, []):
                failed_tests.add(f"{path}::{name}")

        passed_tests -= failed_tests | skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
