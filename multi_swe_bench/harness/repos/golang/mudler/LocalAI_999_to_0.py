import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_GO_IMAGE = "golang:1.25-bookworm"
_TAG = "era1"


class _ImageBase(Image):
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
        return _GO_IMAGE

    def image_tag(self) -> str:
        return f"base-{_TAG}"

    def workdir(self) -> str:
        return f"base-{_TAG}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}
ENV GOTOOLCHAIN=auto

RUN apt-get update && apt-get install -y --no-install-recommends \\
    build-essential \\
    ca-certificates \\
    cmake \\
    curl \\
    git \\
    pkg-config \\
    protobuf-compiler \\
    unzip \\
    && rm -rf /var/lib/apt/lists/*

RUN go install google.golang.org/protobuf/cmd/protoc-gen-go@v1.31.0 && \\
    go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@v1.3.0

WORKDIR /home/

{code}

{self.clear_env}

"""


class _ImageDefault(Image):
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
        return _ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
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
                "extract_packages.sh",
                """#!/bin/bash
_extract_go_packages() {
    local PKGS
    PKGS=$(grep -h '^diff --git a/' "$@" 2>/dev/null \\
        | sed 's|diff --git a/||;s| b/.*||' \\
        | grep '\\.go$' \\
        | grep -v '^vendor/' \\
        | xargs -I{} dirname {} \\
        | sort -u)

    local result=""
    for pkg in $PKGS; do
        if [ -d "$pkg" ] && compgen -G "$pkg/*.go" > /dev/null 2>&1; then
            result="$result ./$pkg"
        fi
    done
    echo "$result"
}

_extract_test_names_in_pkg() {
    local pkg_path="$1"
    shift
    for patch in "$@"; do
        awk -v pkg="$pkg_path" '
            /^diff --git / {
                in_pkg = ($3 ~ ("^a/" pkg "/[^/]+_test\\.go$"))
            }
            in_pkg && /^\\+func +Test[A-Z]/ {
                if (match($0, /Test[A-Z][A-Za-z0-9_]*/)) {
                    print substr($0, RSTART, RLENGTH)
                }
            }
        ' "$patch" 2>/dev/null
    done | sort -u
}

GO_TEST_PKGS=$(_extract_go_packages "$@")
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git fetch --depth 1 origin {pr.base.sha} || true
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

export PATH="$PATH:/go/bin"
make prepare-sources 2>&1 | tail -20 || true
make get-sources 2>&1 | tail -20 || true
make protogen-go 2>&1 | tail -30 || true
make assets 2>&1 | tail -10 || true

go test -v -count=1 -timeout 3m ./pkg/utils/... 2>&1 | tail -5 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set +e

cd /home/{pr.repo}
source /home/extract_packages.sh /home/test.patch /home/fix.patch
if [ -z "$GO_TEST_PKGS" ]; then
    echo "No buildable packages derived from patches; exiting cleanly."
    exit 0
fi
_synth_patches="${{SYNTH_PATCHES:-}}"
echo "Testing packages: $GO_TEST_PKGS"
SECONDS=0
BUDGET=450
for pkg in $GO_TEST_PKGS; do
    if [ "$SECONDS" -gt "$BUDGET" ]; then
        echo "Budget ${{BUDGET}}s exceeded (elapsed=${{SECONDS}}s); skipping remaining"
        break
    fi
    echo "==> $pkg (elapsed=${{SECONDS}}s)"
    pkg_out=$(timeout 90 go test -v -count=1 -timeout 60s "$pkg" 2>&1)
    echo "$pkg_out"
    if [ -n "$_synth_patches" ] && echo "$pkg_out" | grep -q '\[build failed\]'; then
        pkg_path="${{pkg#./}}"
        for tn in $(_extract_test_names_in_pkg "$pkg_path" $_synth_patches); do
            echo "--- FAIL: $tn (synthetic: build failed in $pkg)"
        done
    fi
done
echo "Total elapsed: ${{SECONDS}}s"
exit 0
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set +e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch 2>/dev/null || \
    git apply --3way --whitespace=nowarn /home/test.patch 2>/dev/null || \
    git apply --reject --whitespace=nowarn /home/test.patch 2>/dev/null || true
go mod tidy 2>/dev/null || true
source /home/extract_packages.sh /home/test.patch /home/fix.patch
if [ -z "$GO_TEST_PKGS" ]; then
    echo "No buildable packages derived from patches; exiting cleanly."
    exit 0
fi
_synth_patches="/home/test.patch"
echo "Testing packages: $GO_TEST_PKGS"
SECONDS=0
BUDGET=450
for pkg in $GO_TEST_PKGS; do
    if [ "$SECONDS" -gt "$BUDGET" ]; then
        echo "Budget ${{BUDGET}}s exceeded (elapsed=${{SECONDS}}s); skipping remaining"
        break
    fi
    echo "==> $pkg (elapsed=${{SECONDS}}s)"
    pkg_out=$(timeout 90 go test -v -count=1 -timeout 60s "$pkg" 2>&1)
    echo "$pkg_out"
    if [ -n "$_synth_patches" ] && echo "$pkg_out" | grep -q '\[build failed\]'; then
        pkg_path="${{pkg#./}}"
        for tn in $(_extract_test_names_in_pkg "$pkg_path" $_synth_patches); do
            echo "--- FAIL: $tn (synthetic: build failed in $pkg)"
        done
    fi
done
echo "Total elapsed: ${{SECONDS}}s"
exit 0
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set +e

cd /home/{pr.repo}
(git apply --whitespace=nowarn /home/test.patch /home/fix.patch 2>/dev/null || \
    (git apply --3way --whitespace=nowarn /home/test.patch 2>/dev/null && \
     git apply --3way --whitespace=nowarn /home/fix.patch 2>/dev/null) || \
    (git apply --reject --whitespace=nowarn /home/test.patch 2>/dev/null; \
     git apply --reject --whitespace=nowarn /home/fix.patch 2>/dev/null) || true)
go mod tidy 2>/dev/null || true
source /home/extract_packages.sh /home/test.patch /home/fix.patch
if [ -z "$GO_TEST_PKGS" ]; then
    echo "No buildable packages derived from patches; exiting cleanly."
    exit 0
fi
_synth_patches="/home/test.patch /home/fix.patch"
echo "Testing packages: $GO_TEST_PKGS"
SECONDS=0
BUDGET=450
for pkg in $GO_TEST_PKGS; do
    if [ "$SECONDS" -gt "$BUDGET" ]; then
        echo "Budget ${{BUDGET}}s exceeded (elapsed=${{SECONDS}}s); skipping remaining"
        break
    fi
    echo "==> $pkg (elapsed=${{SECONDS}}s)"
    pkg_out=$(timeout 90 go test -v -count=1 -timeout 60s "$pkg" 2>&1)
    echo "$pkg_out"
    if [ -n "$_synth_patches" ] && echo "$pkg_out" | grep -q '\[build failed\]'; then
        pkg_path="${{pkg#./}}"
        for tn in $(_extract_test_names_in_pkg "$pkg_path" $_synth_patches); do
            echo "--- FAIL: $tn (synthetic: build failed in $pkg)"
        done
    fi
done
echo "Total elapsed: ${{SECONDS}}s"
exit 0
""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for f in self.files():
            copy_commands += f"COPY {f.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


def _parse_go_test_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    re_pass = re.compile(r"--- PASS: (\S+)")
    re_fail = [re.compile(r"--- FAIL: (\S+)")]
    re_skip = re.compile(r"--- SKIP: (\S+)")

    def base_name(name: str) -> str:
        i = name.rfind("/")
        return name if i == -1 else name[:i]

    for raw in test_log.splitlines():
        line = raw.strip()

        m = re_pass.match(line)
        if m:
            name = m.group(1)
            if name not in failed_tests:
                skipped_tests.discard(name)
                passed_tests.add(base_name(name))

        for rp in re_fail:
            m = rp.match(line)
            if m:
                name = m.group(1)
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(base_name(name))

        m = re_skip.match(line)
        if m:
            name = m.group(1)
            if name not in passed_tests and name not in failed_tests:
                skipped_tests.add(base_name(name))

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


@Instance.register("mudler", "LocalAI_999_to_0")
class LocalAI_999_to_0(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return _ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd if run_cmd else "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd if test_patch_run_cmd else "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd if fix_patch_run_cmd else "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return _parse_go_test_log(test_log)
