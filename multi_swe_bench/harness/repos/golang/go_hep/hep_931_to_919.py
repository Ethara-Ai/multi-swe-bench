import json
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_GO_IMAGE = "golang:1.18"
_BASE_TAG = "base-931_to_919"

_PKG_MARKER = re.compile(r"^=== GO PACKAGE (\S+)$")
_LOOPBACK = re.compile(r"127\.0\.0\.1:\d+")

_RUN_TESTS_BODY = r"""PKGS=$(grep -oE '^diff --git a/[^ ]+_test\.go' /home/test.patch | sed 's#^diff --git a/##' | xargs -n1 dirname | sort -u)
for pkg in $PKGS; do
  out="/tmp/gotest-$(echo "$pkg" | tr / _).json"
  echo "=== GO PACKAGE $pkg"
  go test -json -count=1 "./$pkg" > "$out" 2>&1
  cat "$out"
  grep -q '"Test":' "$out" && continue
  ls "$pkg"/*_test.go 2>/dev/null | xargs -r grep -hoE '^func (Test|Example)[A-Za-z0-9_]*\(' | sed -E 's/^func ([A-Za-z0-9_]+)\(/{"Action":"fail","Test":"\1"}/' | sort -u
done
"""


def _run_tests_sh(repo: str) -> str:
    return "#!/bin/bash\n" f"cd /home/{repo}\n" + _RUN_TESTS_BODY


class HepImageBase_HEP_931_TO_919(Image):
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
        return _GO_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_img = self.dependency()
        infra = DockerfileEnhancer._infrastructure_block(self, base_img).rstrip("\n")
        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {base_img}

{infra}

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

CMD ["/bin/bash"]
"""


class HepImageDefault_HEP_931_TO_919(Image):
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
        return HepImageBase_HEP_931_TO_919(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo

        check_git_changes_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            "git rev-parse --is-inside-work-tree > /dev/null\n"
            "git status --porcelain\n"
            'test -z "$(git status --porcelain)"\n'
            'echo "check_git_changes: No uncommitted changes"\n'
        )

        prepare_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "bash /home/check_git_changes.sh\n"
            'git checkout --detach "${BASE_COMMIT}"\n'
            "bash /home/check_git_changes.sh\n"
            "go mod download\n"
        )

        run_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            f"cd /home/{repo}\n"
            "bash /home/run_tests.sh\n"
        )

        test_run_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            f"cd /home/{repo}\n"
            "git apply --whitespace=nowarn /home/test.patch\n"
            "bash /home/run_tests.sh\n"
        )

        fix_run_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            f"cd /home/{repo}\n"
            "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            "bash /home/run_tests.sh\n"
        )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", check_git_changes_sh),
            File(".", "prepare.sh", prepare_sh),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run_sh),
            File(".", "fix-run.sh", fix_run_sh),
            File(".", "run_tests.sh", _run_tests_sh(repo)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copies = "".join(f"COPY {f.name} /home/\n" for f in self.files())
        return f"""FROM {image.image_full_name()}

{copies}
ARG BASE_COMMIT="{self.pr.base.sha}"

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{Image._HARDENING_BLOCK}
"""


@Instance.register("go-hep", "hep_931_to_919")
class HEP_931_TO_919(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return HepImageDefault_HEP_931_TO_919(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        buckets = {"pass": set(), "fail": set(), "skip": set()}
        pkg = "."
        for raw in test_log.splitlines():
            line = raw.strip()
            marker = _PKG_MARKER.match(line)
            if marker:
                pkg = marker.group(1)
                continue
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            test = event.get("Test")
            action = event.get("Action")
            if not test or action not in buckets:
                continue
            buckets[action].add(f"go::{pkg}::{_LOOPBACK.sub('127.0.0.1:PORT', test)}")
        failed_tests = buckets["fail"]
        passed_tests = buckets["pass"] - failed_tests
        skipped_tests = buckets["skip"] - failed_tests - passed_tests
        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
