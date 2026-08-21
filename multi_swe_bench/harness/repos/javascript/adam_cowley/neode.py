import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class NeodeImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return "node:12-bullseye"

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

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""\
FROM {image_name}

{self.global_env}

ENV LC_ALL=C.UTF-8 \\
    NEO4J_HOME=/opt/neo4j \\
    PATH="/opt/neo4j/bin:${{PATH}}" \\
    NEO4J_PROTOCOL=bolt \\
    NEO4J_HOST=localhost \\
    NEO4J_PORT=7687 \\
    NEO4J_USERNAME=neo4j \\
    NEO4J_PASSWORD=neo4j-test-pw

WORKDIR /home/
RUN apt-get update && apt-get install -y git wget openjdk-11-jre-headless procps && rm -rf /var/lib/apt/lists/*

RUN wget -q https://dist.neo4j.org/neo4j-community-4.0.0-unix.tar.gz -O /tmp/neo4j.tar.gz \\
    && tar -xzf /tmp/neo4j.tar.gz -C /opt \\
    && mv /opt/neo4j-community-4.0.0 /opt/neo4j \\
    && rm /tmp/neo4j.tar.gz \\
    && /opt/neo4j/bin/neo4j-admin set-initial-password neo4j-test-pw

{code}

{self.clear_env}

"""


class NeodeImageDefault(Image):
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
        return NeodeImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        start_neo4j = """\
start_neo4j() {
    neo4j start
    for i in $(seq 1 60); do
        if (echo > /dev/tcp/127.0.0.1/7687) >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    echo "Neo4j failed to start" >&2
    return 1
}
"""

        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
            File(
                ".",
                "prepare.sh",
                """\
#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}

rm -rf node_modules
npm install --force || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true
export NODE_ENV=test

{start_neo4j}
cd /home/{pr.repo}
start_neo4j
npm test 2>&1
""".format(pr=self.pr, start_neo4j=start_neo4j),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true
export NODE_ENV=test

{start_neo4j}
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
npm install --force || true
start_neo4j
npm test 2>&1
""".format(pr=self.pr, start_neo4j=start_neo4j),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true
export NODE_ENV=test

{start_neo4j}
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
npm install --force || true
start_neo4j
npm test 2>&1
""".format(pr=self.pr, start_neo4j=start_neo4j),
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        return f"""\
FROM {dep.image_name()}:{dep.image_tag()}

{self.global_env}

COPY fix.patch /home/fix.patch
COPY test.patch /home/test.patch
COPY prepare.sh /home/prepare.sh
COPY run.sh /home/run.sh
COPY test-run.sh /home/test-run.sh
COPY fix-run.sh /home/fix-run.sh
WORKDIR /home/{self.pr.repo}
RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("adam-cowley", "neode")
class Neode(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return NeodeImageDefault(self.pr, self._config)

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
        """Parse mocha spec reporter output (nested describe blocks, ✓ / N) / - lines)."""
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        suite_stack: list[tuple[int, str]] = []
        in_summary = False

        summary_re = re.compile(r"^\s+\d+\s+(passing|pending|failing)\b")
        timing_re = re.compile(r"\s*\(\d+m?s\)\s*$")
        pass_re = re.compile(r"^✓\s+(.+)$")
        fail_re = re.compile(r"^(\d+)\)\s+(.+)$")
        skip_re = re.compile(r"^-\s+(.+)$")

        def clean_name(name: str) -> str:
            return timing_re.sub("", name).strip()

        for line in clean_log.splitlines():
            if not line.strip():
                continue

            if summary_re.match(line.rstrip()):
                in_summary = True
                continue
            if in_summary:
                continue

            indent = len(line) - len(line.lstrip(" "))
            stripped = line.strip()

            pass_match = pass_re.match(stripped)
            fail_match = fail_re.match(stripped)
            skip_match = skip_re.match(stripped)

            if not (pass_match or fail_match or skip_match):
                while suite_stack and suite_stack[-1][0] >= indent:
                    suite_stack.pop()
                suite_stack.append((indent, stripped))
                continue

            prefix = " > ".join(name for _, name in suite_stack)
            prefix = f"{prefix} > " if prefix else ""

            if pass_match:
                passed_tests.add(f"{prefix}{clean_name(pass_match.group(1))}")
            elif fail_match:
                failed_tests.add(f"{prefix}{clean_name(fail_match.group(2))}")
            elif skip_match:
                skipped_tests.add(f"{prefix}{clean_name(skip_match.group(1))}")

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
