"""TheAlgorithms/Java harness config — PRs #3464 to #6566.

Two JDK eras served by ONE shared base image:

  * #3464 / #4230 / #4384 / #4392  (2022-10 .. 2023-09) -> maven.compiler.release 17, surefire 2.22.2
  * #6566                          (2025-10)            -> maven.compiler.release 21, surefire 3.5.4

Both JDKs are installed in the base and prepare.sh points JAVA_HOME at the one
this PR's pom requires, so a single base covers the whole range.

Artifact split:
  base Dockerfile -> toolchain + git clone, then CMD. Nothing after the clone.
  PR Dockerfile   -> FROM base, 7 COPY lines, RUN prepare.sh, then the git strip.
  prepare.sh      -> JDK selection, dependency warm-up, cd/reset/checkout with
                     check_git_changes asserts. No stripping here.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# maven.compiler.release read from each base commit's pom.xml, not guessed.
_JDK21_FROM = 5142


def _jdk_for(number: int) -> str:
    return "21" if number >= _JDK21_FROM else "17"


class TheAlgorithmsJavaImageBase(Image):
    """Shared base: JDK 17 + JDK 21 + Maven, then the clone. Nothing else.

    The syntax directive is emitted here so DockerfileEnhancer returns this file
    verbatim (image.py:316). Without it the enhancer rewrites the clone into
    clone+checkout+hardening, and the hardening must live in the PR layer.
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

    def dependency(self) -> Union[str, "Image"]:
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        return "base-maven"

    def workdir(self) -> str:
        return "base-maven"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    MAVEN_OPTS="-Xmx2g"

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    ca-certificates \\
    curl \\
    openjdk-17-jdk \\
    openjdk-21-jdk \\
    maven \\
    python3 \\
 && apt-get clean \\
 && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

CMD ["/bin/bash"]
"""


class TheAlgorithmsJavaImageDefault(Image):
    """PR layer: FROM base, exactly 7 COPYs, prepare.sh, then the git strip.

    dependency() returns an Image, so the enhancer emits this verbatim -- no
    ARG/ENV/WORKDIR/CMD is injected, and the hardening below is the only git
    activity in the file.
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

    def dependency(self) -> Optional[Image]:
        return TheAlgorithmsJavaImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        org = self.pr.org
        sha = self.pr.base.sha
        jdk = _jdk_for(self.pr.number)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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
                r"""#!/bin/bash
set -e

cd /home/[[REPO]]
git reset --hard
bash /home/check_git_changes.sh

# The base image clones the default branch; this PR's sha may not be an
# ancestor of it, so fetch the exact object before checkout. One base serves
# every PR in the range this way.
git remote add origin https://github.com/[[ORG]]/[[REPO]].git 2>/dev/null || true
git fetch --depth=1 origin [[SHA]] 2>/dev/null || git fetch origin 2>/dev/null || true
git checkout -f [[SHA]]
bash /home/check_git_changes.sh

# JAVA_HOME is arch-suffixed, so it must be resolved at runtime for the image to
# work on both amd64 and arm64. JDK [[JDK]] matches this commit's
# maven.compiler.release.
cat > /home/java_env.sh <<'ENVEOF'
export JAVA_HOME="/usr/lib/jvm/java-[[JDK]]-openjdk-$(dpkg --print-architecture)"
export PATH="$JAVA_HOME/bin:$PATH"
ENVEOF

# Surefire writes one XML per test class with a <testcase> per METHOD. Parsing
# those gives classname.method ids; parsing Maven's console "[INFO] Running X"
# lines instead yields only CLASS ids, which is what left an earlier Java
# dataset (guava) valid but with zero usable gating tests.
cat > /home/emit_results.py <<'PYEOF'
import glob, os, xml.etree.ElementTree as ET

for path in sorted(glob.glob("target/surefire-reports/TEST-*.xml")):
    try:
        root = ET.parse(path).getroot()
    except Exception:
        continue
    for tc in root.iter("testcase"):
        cls, name = tc.get("classname") or "", tc.get("name") or ""
        if not cls or not name:
            continue
        # Strip surefire's parametrized suffix noise but keep the invocation id
        # so distinct parameter cases stay distinct.
        tid = f"{cls}.{name}"
        kids = {child.tag for child in tc}
        if "failure" in kids or "error" in kids:
            status = "FAILED"
        elif "skipped" in kids:
            status = "SKIPPED"
        else:
            status = "PASSED"
        print(f"{status} {tid}")
PYEOF

source /home/java_env.sh
java -version 2>&1 | head -1
mvn -v 2>&1 | head -1

# Warm the local repository so the graded runs need no network. Static-analysis
# plugins are skipped everywhere: a checkstyle/spotbugs violation would fail the
# build before surefire writes any XML, producing a silent zero-test stage.
mvn -B -q -Dstyle.color=never \
    -Dcheckstyle.skip=true -Dpmd.skip=true -Dspotbugs.skip=true -Djacoco.skip=true \
    dependency:go-offline test-compile || true

# Maven writes target/ (gitignored) but plugins can also touch tracked files;
# restore them so every `git apply` in the run scripts sees exactly BASE_COMMIT.
git checkout -- .
bash /home/check_git_changes.sh
""".replace("[[REPO]]", repo)
                .replace("[[ORG]]", org)
                .replace("[[SHA]]", sha)
                .replace("[[JDK]]", jdk),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/[[REPO]]
source /home/java_env.sh

mvn -B test -Dstyle.color=never \\
    -Dcheckstyle.skip=true -Dpmd.skip=true -Dspotbugs.skip=true -Djacoco.skip=true \\
    2>&1 || true

python3 /home/emit_results.py
""".replace("[[REPO]]", repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/[[REPO]]
source /home/java_env.sh

if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

mvn -B test -Dstyle.color=never \\
    -Dcheckstyle.skip=true -Dpmd.skip=true -Dspotbugs.skip=true -Djacoco.skip=true \\
    2>&1 || true

python3 /home/emit_results.py
""".replace("[[REPO]]", repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/[[REPO]]
source /home/java_env.sh

if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

mvn -B test -Dstyle.color=never \\
    -Dcheckstyle.skip=true -Dpmd.skip=true -Dspotbugs.skip=true -Djacoco.skip=true \\
    2>&1 || true

python3 /home/emit_results.py
""".replace("[[REPO]]", repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # Strip runs AFTER prepare.sh: prepare.sh fetches this PR's sha, and
        # stripping first would prune the objects it needs. The sha is inlined so
        # this layer declares no ARG/ENV of its own.
        return f"""FROM {name}:{tag}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach "{sha}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "{sha}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""


@Instance.register("TheAlgorithms", "TheAlgorithms_Java_6566_to_3464")
class TheAlgorithmsJava6566To3464(Instance):
    """Harness instance for TheAlgorithms/Java — PRs #3464 to #6566."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TheAlgorithmsJavaImageDefault(self.pr, self._config)

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
        """Parse the `STATUS classname.method` lines emitted by emit_results.py.

        Ids are METHOD-level. Class-level ids (Maven's "[INFO] Running X") would
        collapse every method of a test class into one entry, which is how a
        previous Java dataset ended up with zero gating tests.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        line_re = re.compile(r"^(PASSED|FAILED|SKIPPED)\s+(\S+)\s*$")
        for raw in test_log.splitlines():
            m = line_re.match(raw.strip())
            if not m:
                continue
            status, tid = m.group(1), m.group(2)
            if status == "PASSED":
                passed_tests.add(tid)
            elif status == "FAILED":
                failed_tests.add(tid)
            else:
                skipped_tests.add(tid)

        # TestResult.__post_init__ rejects overlapping sets: a retried test can
        # appear twice. Failure wins over a later pass, then skip.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
