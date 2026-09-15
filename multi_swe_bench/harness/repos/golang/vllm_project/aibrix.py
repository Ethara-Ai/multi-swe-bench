import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_GO_BASE = "golang:1.22-bookworm"

# Derive the package dirs to test from the test.patch (*_test.go paths -> their
# dir), and run scoped `go test` on just those packages. Avoids the repo's full
# Makefile `test` target which needs k8s envtest (KUBEBUILDER_ASSETS); the target
# files here are plain package unit tests.
_TARGETS = (
    "PKGS=$(grep -E '^\\+\\+\\+ b/' /home/test.patch 2>/dev/null "
    "| sed 's#^+++ b/##' | grep -E '_test\\.go$' | xargs -n1 dirname 2>/dev/null "
    "| sort -u | sed 's#^#./#' | tr '\\n' ' ')\n"
    '[ -z "$PKGS" ] && PKGS="./..."\n'
    'echo "SCOPED GO PKGS: $PKGS"\n'
)

_RESET = "git reset --hard\ngit clean -fdq\n"

_SETUP = "go mod download 2>/dev/null || true\n"

_TEST = "go test -v -count=1 $PKGS 2>&1\n"


def _apply_one(patch: str) -> str:
    return (
        f"git apply --whitespace=nowarn {patch} 2>/dev/null "
        f"|| git apply --3way --whitespace=nowarn {patch} 2>/dev/null "
        f"|| git apply --whitespace=nowarn --include='*.go' {patch} 2>/dev/null || true\n"
    )


class ImageBase(Image):
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
        return _GO_BASE

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()
        org = self.pr.org
        repo = self.pr.repo
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 GOFLAGS=-mod=mod

RUN apt-get -o Acquire::Check-Valid-Until=false -o Acquire::Retries=5 update && apt-get install -y --no-install-recommends git make && rm -rf /var/lib/apt/lists/*

WORKDIR /home/
RUN git clone "${{REPO_URL}}" /home/{repo}
WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class ImageDefault(Image):
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
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                "#!/bin/bash\n"
                "set -e\n"
                f"cd /home/{repo}\n"
                f"git cat-file -e {self.pr.base.sha}^{{commit}} 2>/dev/null \\\n"
                f"    || git fetch --no-tags --depth=2147483647 origin {self.pr.base.sha} \\\n"
                f'    || git fetch --no-tags origin "+refs/pull/{self.pr.number}/head:refs/remotes/origin/pr-{self.pr.number}"\n'
                "git reset --hard\n"
                "git clean -fdq\n"
                f"git checkout --detach {self.pr.base.sha}\n"
                "git clean -fdq\n"
                + _SETUP,
            ),
            File(
                ".",
                "run.sh",
                "#!/bin/bash\nset -e\n" f"cd /home/{repo}\n" + _RESET + _SETUP + _TARGETS + _TEST,
            ),
            File(
                ".",
                "test-run.sh",
                "#!/bin/bash\nset -e\n" f"cd /home/{repo}\n" + _RESET
                + _apply_one("/home/test.patch") + _SETUP + _TARGETS + _TEST,
            ),
            File(
                ".",
                "fix-run.sh",
                "#!/bin/bash\nset -e\n" f"cd /home/{repo}\n" + _RESET
                + _apply_one("/home/test.patch") + _apply_one("/home/fix.patch") + _SETUP + _TARGETS + _TEST,
            ),
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency().image_full_name()
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"
        return f"""FROM {image_name}

{copy_commands}
RUN bash /home/prepare.sh
"""


@Instance.register("vllm-project", "aibrix")
class Aibrix(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        # go test -v: "--- PASS: TestName (0.00s)" / "--- FAIL:" / "--- SKIP:"
        res = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+)")
        for line in test_log.splitlines():
            m = res.match(line)
            if not m:
                continue
            status, name = m.group(1), m.group(2)
            if status == "PASS":
                if name not in failed_tests:
                    passed_tests.add(name)
            elif status == "FAIL":
                failed_tests.add(name)
                passed_tests.discard(name)
            else:
                skipped_tests.add(name)
        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
