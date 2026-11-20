import json as _json
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL_NAME = "trivy_4047_to_4047"
_BASE_IMAGE = "golang:1.24"
_BASE_TAG = "base-go124"
_REPO_PREFIXES = ("github.com/aquasecurity/trivy/",)

_ENV = r"""export CI=true
export GOTOOLCHAIN=local
export CGO_ENABLED=0
export GOFLAGS="-buildvcs=false -mod=mod"
export GOPRIVATE=github.com/csaf-poc/csaf_distribution
git config --global url."https://github.com/gocsaf/csaf".insteadOf "https://github.com/csaf-poc/csaf_distribution"
"""

_EXCLUDES = r"""EXCLUDES=(--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \
    --exclude='*.svg' --exclude='*.ico' --exclude='*.pdf' --exclude='*.tar' \
    --exclude='*.gz' --exclude='*.zip' --exclude='*.woff*' --exclude='*.wasm' \
    --exclude='*.bin' --exclude='*.test' --exclude='*.rpm' --exclude='*.deb' \
    --exclude='*.tgz' --exclude='*.jar' --exclude='*.war' --exclude='*.par' \
    --exclude='*.db' --exclude='*.zst' --exclude='*.tfplan' --exclude='*.exe' \
    --exclude='*.elf' --exclude='*.macho' --exclude='*.egg' --exclude='*.vmdk' \
    --exclude='executable_*')
"""

_RUN_TESTS = r"""TEST_DIRS=$({ grep -E '^diff --git a/\S+_test\.go' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\1#' \
    | grep -vE '(^|/)(integration|e2e)/' \
    | grep -vE 'pkg/fanal/test/integration/' \
    | sed -E 's#/[^/]+$##' | sort -u; } || true)
if echo "$TEST_DIRS" | grep -q '^pkg/module'; then
    if [ -d pkg/module/testdata ]; then
        ( GOOS=wasip1 GOARCH=wasm go generate ./pkg/module/testdata/... ) 2>&1 || true
    fi
fi
PKGS=""
for d in $TEST_DIRS; do
    if ls "$d"/*.go > /dev/null 2>&1; then PKGS="$PKGS ./$d"; fi
done
if [ -z "$PKGS" ]; then echo "NO_TEST_DIRS"; exit 0; fi
go test -json -count=1 $PKGS 2>&1
"""


def parse_go_test_log(log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

    for raw in log.splitlines():
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            ev = _json.loads(raw)
        except Exception:
            continue
        test = ev.get("Test")
        action = ev.get("Action")
        pkg = ev.get("Package", "") or ""
        if not test or action not in ("pass", "fail", "skip"):
            continue
        for prefix in _REPO_PREFIXES:
            if pkg.startswith(prefix):
                pkg = pkg[len(prefix):]
                break
        name = f"{pkg}::{test}"
        if action == "pass":
            passed_tests.add(name)
        elif action == "fail":
            failed_tests.add(name)
        else:
            skipped_tests.add(name)

    passed_tests -= failed_tests
    passed_tests -= skipped_tests
    skipped_tests -= failed_tests

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


class TrivyPr4047ImageBase(Image):
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
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        label = (
            f'LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        sections = [
            DockerfileEnhancer.SYNTAX_DIRECTIVE,
            f"FROM {_BASE_IMAGE}",
            (
                f"{DockerfileEnhancer._TARGETARCH_ARG}\n"
                f'ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"\n'
                "ARG BASE_COMMIT"
            ),
            DockerfileEnhancer._PROXY_ARGS,
            DockerfileEnhancer._ENV_BLOCK,
            label,
            DockerfileEnhancer._CERT_SYMLINKS,
            "ENV CGO_ENABLED=0 \\\n    GOTOOLCHAIN=local \\\n    CI=true",
            'ENV GOFLAGS="-buildvcs=false -mod=mod"',
            "WORKDIR /home/",
            "RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*",
            code,
            f"WORKDIR /home/{self.pr.repo}",
            'CMD ["/bin/bash"]',
        ]
        return "\n\n".join(sections) + "\n"


class TrivyPr4047ImageDefault(Image):
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
        return TrivyPr4047ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        cd = f"cd /home/{self.pr.repo}\n"
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                "#!/bin/bash\nset -eo pipefail\n"
                + _ENV
                + cd
                + "git reset --hard\n"
                + "git clean -fdx\n"
                + 'test -z "$(git status --porcelain)"\n'
                + f"git checkout --detach {self.pr.base.sha}\n"
                + 'test -z "$(git status --porcelain)"\n'
                + "timeout 600 go mod download || true\n"
                + "go list -deps -test ./... > /dev/null\n"
                + "git checkout -- go.mod go.sum\n",
            ),
            File(
                ".",
                "run.sh",
                "#!/bin/bash\nset -eo pipefail\n" + _ENV + cd + _RUN_TESTS,
            ),
            File(
                ".",
                "test-run.sh",
                "#!/bin/bash\nset -eo pipefail\n"
                + _ENV
                + cd
                + _EXCLUDES
                + 'git apply --whitespace=nowarn "${EXCLUDES[@]}" /home/test.patch\n'
                + "if grep -qE '^diff --git a/go\\.(mod|sum)' /home/test.patch; then\n"
                + "    timeout 600 go mod download || true\n"
                + "fi\n"
                + _RUN_TESTS,
            ),
            File(
                ".",
                "fix-run.sh",
                "#!/bin/bash\nset -eo pipefail\n"
                + _ENV
                + cd
                + _EXCLUDES
                + 'git apply --whitespace=nowarn "${EXCLUDES[@]}" /home/test.patch\n'
                + 'git apply --whitespace=nowarn "${EXCLUDES[@]}" /home/fix.patch\n'
                + "if grep -qhE '^diff --git a/go\\.(mod|sum)' /home/test.patch /home/fix.patch; then\n"
                + "    timeout 600 go mod download || true\n"
                + "fi\n"
                + _RUN_TESTS,
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        file_names = " ".join(file.name for file in self.files())
        copy_command = f"COPY {file_names} /home/"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard && git checkout --detach ${{BASE_COMMIT}}

{copy_command}

RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}

CMD ["/bin/bash"]
"""


@Instance.register("aquasecurity", _INTERVAL_NAME)
class TRIVY_4047_TO_4047(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TrivyPr4047ImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        return parse_go_test_log(log)
