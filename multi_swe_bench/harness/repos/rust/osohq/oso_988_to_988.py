from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_JDK_IMAGE = "maven:3.8.8-eclipse-temurin-11-focal"
_RUST_TOOLCHAIN = "1.75.0"
_BASE_TAG = "base-988_to_988"
_MODULE = "languages/java/oso"
_NATIVE_DIR = f"{_MODULE}/src/main/resources/linux"
_MVN = "mvn -B --no-transfer-progress"
_SUREFIRE_FLAGS = "-Dmaven.test.failure.ignore=true -DfailIfNoTests=false -Dsurefire.useFile=false"
_RUN_FLAGS = f"{_SUREFIRE_FLAGS} -Dmaven.compiler.failOnError=false"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_FILE_MARKER_RE = re.compile(r"^##### FILE: (\S+)[ \t]*$", re.M)
_MODULE_RE = re.compile(r"^(.*?)/?target/surefire-reports/")
_TESTCASE_RE = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.S)
_NAME_RE = re.compile(r'\bname="([^"]*)"')
_CLASSNAME_RE = re.compile(r'\bclassname="([^"]*)"')
_PARAMS_RE = re.compile(r"\(.*\)$")

_CHECK_GIT_CHANGES_SH = """\
#!/bin/bash
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
"""


def _prepare_sh(pr: PullRequest) -> str:
    return f"""\
#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {pr.base.sha}
bash /home/check_git_changes.sh

java -version
mvn -version
cargo --version

cargo fetch --locked || {{ echo "prepare.sh: cargo fetch --locked failed at the base commit"; exit 1; }}
cargo build --locked -p polar-c-api || {{ echo "prepare.sh: cargo build of polar-c-api failed at the base commit"; exit 1; }}
mkdir -p {_NATIVE_DIR}
cp target/debug/libpolar.so {_NATIVE_DIR}/libpolar.so

cd /home/{pr.repo}/{_MODULE}
{_MVN} test {_SUREFIRE_FLAGS} || {{ echo "prepare.sh: maven could not build and run the java suite at the base commit"; exit 1; }}
rm -rf target/surefire-reports

test -s src/main/resources/linux/libpolar.so
find "${{HOME}}/.m2/repository/org/apache/maven/surefire" -name 'surefire-junit-platform-*.jar' | grep -q .
{_MVN} -o -q test-compile
echo DEPS_OK
"""


def _run_sh(pr: PullRequest, apply_line: str) -> str:
    return f"""\
#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export CI=true
{apply_line}cd /home/{pr.repo}/{_MODULE}
rm -rf target/surefire-reports
mkdir -p target/surefire-reports
rc=0
{_MVN} -o test {_RUN_FLAGS} || rc=$?
echo "===== BEGIN TEST RESULTS ====="
find target/surefire-reports -name 'TEST-*.xml' | sort | while IFS= read -r f; do
    echo "##### FILE: {_MODULE}/$f"
    cat "$f"
    echo
done
echo "===== END TEST RESULTS ====="
exit "$rc"
"""


class Oso988To988ImageBase(Image):
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
        return _JDK_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl build-essential \\
    && rm -rf /var/lib/apt/lists/*

ENV RUSTUP_HOME=/usr/local/rustup \\
    CARGO_HOME=/usr/local/cargo \\
    PATH=/usr/local/cargo/bin:$PATH \\
    MAVEN_OPTS="-Xmx1g"

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --no-modify-path --profile minimal --default-toolchain {_RUST_TOOLCHAIN} && \\
    rustc --version && cargo --version

RUN git --version && \\
    java -version && \\
    mvn -version && \\
    gcc --version | head -1 && \\
    test -f /etc/ssl/certs/ca-certificates.crt

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class Oso988To988ImageDefault(Image):
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
        return Oso988To988ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", _prepare_sh(self.pr)),
            File(".", "run.sh", _run_sh(self.pr, "")),
            File(
                ".",
                "test-run.sh",
                _run_sh(self.pr, "git apply --whitespace=nowarn /home/test.patch\n"),
            ),
            File(
                ".",
                "fix-run.sh",
                _run_sh(
                    self.pr,
                    "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n",
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT={self.pr.base.sha}
ENV BASE_COMMIT=${{BASE_COMMIT}}

{copy_commands}

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


@Instance.register("osohq", "oso_988_to_988")
class OSO_988_TO_988(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Oso988To988ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()

        chunks = _FILE_MARKER_RE.split(_ANSI_RE.sub("", test_log))[1:]
        for report_path, body in zip(chunks[0::2], chunks[1::2]):
            module_match = _MODULE_RE.match(report_path)
            module = module_match.group(1) if module_match else ""
            for case in _TESTCASE_RE.finditer(body):
                attrs = case.group(1) or ""
                name = _NAME_RE.search(attrs)
                classname = _CLASSNAME_RE.search(attrs)
                if not name or not classname:
                    continue
                rel = classname.group(1).split("$", 1)[0].replace(".", "/") + ".java"
                test_id = f"{module}/src/test/java/{rel}::{_PARAMS_RE.sub('', name.group(1))}"
                inner = case.group(3) or ""
                if case.group(2) == "/>":
                    passed.add(test_id)
                elif "<failure" in inner or "<error" in inner:
                    failed.add(test_id)
                elif "<skipped" in inner:
                    skipped.add(test_id)
                else:
                    passed.add(test_id)

        passed -= failed
        skipped -= failed
        passed -= skipped

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
