import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_AWK_BINARY_FILTER = (
    r"awk '/^diff --git /{ block=$0\"\\n\"; next }"
    r" { if (block!=\"\") { block=block$0\"\\n\";"
    r" if (/^Binary files .* differ$/) { block=\"\"; next };"
    r" if (/^--- /||/^\\+\\+\\+ /||/^@@ /) { printf \"%s\",block; block=\"\" } } else print }"
    r" END { if (block!=\"\") printf \"%s\",block }'"
)


class DeltaImage(Image):
    """Single per-PR image for dandavison/delta (Rust).

    Uses ${REPO_URL} and ${BASE_COMMIT} ARGs injected by DockerfileEnhancer.
    Hardening block is explicit to prevent reward hacking via git history.
    """

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
        # String dependency triggers DockerfileEnhancer (proxy/cert/ARG injection)
        return "rust:latest"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
cargo test 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
for pfile in /home/test.patch; do
    if [ -s "$pfile" ]; then
        {awk} "$pfile" > "${{pfile}}.tmp" && mv "${{pfile}}.tmp" "$pfile"
    fi
done
git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch
cargo test 2>&1
""".format(pr=self.pr, awk=_AWK_BINARY_FILTER),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
for pfile in /home/test.patch /home/fix.patch; do
    if [ -s "$pfile" ]; then
        {awk} "$pfile" > "${{pfile}}.tmp" && mv "${{pfile}}.tmp" "$pfile"
    fi
done
git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch
cargo test 2>&1
""".format(pr=self.pr, awk=_AWK_BINARY_FILTER),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())
        # Use "${REPO_URL}" (not hardcoded URL) so DockerfileEnhancer._standardize_repo_fetch
        # skips replacement (negative lookahead on that pattern).
        # Use "${BASE_COMMIT}" — value passed as --build-arg by build_dataset.py.
        # Explicit _HARDENING_BLOCK prevents _inject_final_sanitize from adding a duplicate.
        return f"""FROM rust:latest

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

RUN git checkout ${{BASE_COMMIT}}

RUN cargo build || true

{copy_commands}
{self._HARDENING_BLOCK}
CMD ["/bin/bash"]
"""


@Instance.register("dandavison", "delta")
class Delta(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DeltaImage(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        test_log = ansi_escape.sub("", test_log)

        re_pass = re.compile(r"^test (.+) \.\.\. ok$")
        re_fail = re.compile(r"^test (.+) \.\.\. FAILED$")
        re_skip = re.compile(r"^test (.+) \.\.\. ignored(?:\s.*)?$")

        for line in test_log.splitlines():
            line = line.strip()
            m = re_pass.match(line)
            if m:
                passed_tests.add(m.group(1))
                continue
            m = re_fail.match(line)
            if m:
                failed_tests.add(m.group(1))
                continue
            m = re_skip.match(line)
            if m:
                skipped_tests.add(m.group(1))

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
