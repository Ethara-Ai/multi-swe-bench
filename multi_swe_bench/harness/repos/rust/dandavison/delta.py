import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Strips whole-file binary diffs ("Binary files a/x and b/x differ") from a
# patch so `git apply` does not choke on them ("cannot apply binary patch ...
# without full index line"). Buffers each `diff --git` block and drops it if a
# "Binary files ... differ" line appears before any hunk header; text blocks are
# flushed unchanged on the first ---/+++/@@ line.
# NOTE: this string is embedded verbatim inside single quotes in run.sh, so it
# must already be valid awk. Use a raw string so `\n` and `\+` reach awk as
# literal backslash escapes (do NOT add shell-style `\"` escaping — that breaks
# the awk parser).
_AWK_BINARY_FILTER = (
    r"""awk '/^diff --git /{ block=$0"\n"; next } """
    r"""{ if (block!="") { block=block$0"\n"; """
    r"""if (/^Binary files .* differ$/) { block=""; next }; """
    r"""if (/^--- /||/^\+\+\+ /||/^@@ /) { printf "%s",block; block="" } } else print } """
    r"""END { if (block!="") printf "%s",block }'"""
)


class DeltaImageBase(Image):
    """Shared toolchain base (tag ':base') for dandavison/delta.

    Contains ONLY the Rust toolchain + apt deps — NO repo clone, so it holds no
    git history and cannot leak future commits. Built once, reused by every PR.
    DockerfileEnhancer processes this image (string dependency, no syntax
    directive) and injects the standard infra (TARGETARCH/REPO_URL ARGs, ENV,
    LABEL). No hardening pass is injected because there is no `git clone` here.
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
        return "rust:latest"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return """FROM rust:latest

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends git \\
    && rm -rf /var/lib/apt/lists/*

CMD ["/bin/bash"]
"""


class DeltaImage(Image):
    """Single per-PR image for dandavison/delta (Rust).

    Two-stage build: layers on the shared ':base' toolchain image (see
    DeltaImageBase). Because dependency() returns an Image (not a str),
    DockerfileEnhancer skips this Dockerfile, so REPO_URL / BASE_COMMIT ARGs are
    declared explicitly below. Hardening block is explicit and runs per-PR after
    the BASE_COMMIT checkout to prevent reward hacking via git history.
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

    def dependency(self) -> Image:
        # Two-stage build: per-PR image layers on the shared ':base' toolchain.
        # Returning an Image (not a str) makes DockerfileEnhancer skip this
        # Dockerfile, so it must be self-contained (ARGs declared below).
        return DeltaImageBase(self.pr, self._config)

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
        base = self.dependency().image_full_name()
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())
        # Enhancer is skipped (dependency is an Image), so REPO_URL / BASE_COMMIT
        # ARGs are declared here (BASE_COMMIT value passed as --build-arg by
        # build_dataset.py). git is inherited from the ':base' image.
        # Explicit _HARDENING_BLOCK runs per-PR after the BASE_COMMIT checkout.
        return f"""FROM {base}

ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

WORKDIR /home/

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


# Route bundled PRs that carry a dash-joined number_interval (the list of
# prs_in_bundle, e.g. "284-289-290-296-297-298-301-302-315") to this config.
# Instance.create() looks up f"{org}/{number_interval}", so each bundle's
# interval string must be registered against this class.
_NUMBER_INTERVALS = [
    "284-289-290-296-297-298-301-302-315",
    "318-323-325-327-331-333-341-344-346-347",
    "414-419-455-457-459-467-468-469-470-471-472-473-474-475-476-477-478-479-480",
    "518-562-612-620-623-627-628-632-636-641-643",
    "831-836-838",
    "835-1034-1055-1057-1058-1078-1082-1085-1106-1108-1110-1111-1112-1115-1116-1117-1119-1127-1138-1151-1158-1165",
    "861-862-865-869-870-871-874-875-876-882-883-884-885-887-889-894-898-901-914-915-917-918-921-934-935-939-950",
    "945-946-981-986-1003-1008-1012-1015-1025-1035-1052-1062-1073",
    "1260-1769-1829-1830-1856-1864-1877-1879-1884-1889-1890-1892-1893-1896-1901-1903-1904-1909-1916-1918-1920-1924-1930-1936-1937-1938-1940-1941-1995-2007-2011-2015-2016-2023-2038-2048-2053-2057-2058-2061-2063-2065-2074-2115",
]
for _interval in _NUMBER_INTERVALS:
    Instance.register("dandavison", _interval)(Delta)
