import base64
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_DIFF_HEADER = re.compile(r"^diff --git a/(\S+) b/", re.MULTILINE)

# Every PR in this range adds a new class plus its test. With only test.patch
# applied the new test cannot compile, javac aborts the whole test-compile, and
# surefire runs nothing -- the test stage reported (0, 0, 0). This script moves
# the uncompilable test files aside, runs the rest of the suite, and reports each
# @Test method of the moved files as FAILED, so the stage records real results.
TEST_RUN_SCRIPT = r"""#!/bin/bash
set -e

cd /home/[[REPO]]
git apply --whitespace=nowarn /home/test.patch

broken_list=/tmp/uncompilable_tests.txt
: > "$broken_list"
for _ in 1 2 3 4 5; do
    if mvn -B -q -Dstyle.color=never test-compile > /tmp/test_compile.log 2>&1; then
        break
    fi
    found=$(grep -oE '/home/[[REPO]]/src/test/java/[^:]+\.java' /tmp/test_compile.log | sort -u || true)
    if [ -z "$found" ]; then
        break
    fi
    for f in $found; do
        if [ -f "$f" ]; then
            cp "$f" "/tmp/$(echo "$f" | tr / _)"
            echo "$f" >> "$broken_list"
            rm -f "$f"
        fi
    done
done
if [ -s "$broken_list" ]; then
    echo "Test files that do not compile without the fix (reported as FAILED):"
    cat "$broken_list"
fi

mvn clean test -B -Dstyle.color=never -Dmaven.test.failure.ignore=true || true
cat target/surefire-reports/TEST-*.xml || true

while read -r f; do
    [ -n "$f" ] || continue
    saved="/tmp/$(echo "$f" | tr / _)"
    cls=$(echo "${f#/home/[[REPO]]/src/test/java/}" | sed 's/\.java$//; s#/#.#g')
    awk '/@(Test|ParameterizedTest|RepeatedTest|TestFactory|TestTemplate)([^A-Za-z]|$)/ {want=1}
         want && match($0, /void[ \t]+[A-Za-z_][A-Za-z0-9_]*[ \t]*\(/) {
             s = substr($0, RSTART, RLENGTH); sub(/^void[ \t]+/, "", s); sub(/[ \t]*\($/, "", s);
             print s; want=0 }' "$saved" | sort -u | while read -r m; do
        echo "<testcase name=\"$m\" classname=\"$cls\"><failure message=\"test does not compile without the fix\"/></testcase>"
    done
done < "$broken_list"
"""

def _resplit_patches(pr: PullRequest) -> None:
    """Route every file diff by path: src/test/ -> test.patch, else -> fix.patch.

    The raw dataset mis-splits some PRs (#6773 ships the new main class
    YensKShortestPaths.java inside test_patch, so the "test" stage already has
    the implementation and nothing can transition to passing). Idempotent and a
    no-op for correctly split PRs.
    """
    tests, fixes = [], []
    for patch in (pr.test_patch, pr.fix_patch):
        starts = [m.start() for m in _DIFF_HEADER.finditer(patch)]
        for i, start in enumerate(starts):
            end = starts[i + 1] if i + 1 < len(starts) else len(patch)
            chunk = patch[start:end]
            if not chunk.endswith("\n"):
                chunk += "\n"
            path = _DIFF_HEADER.match(chunk).group(1)
            (tests if path.startswith("src/test/") else fixes).append(chunk)
    if tests and fixes:
        pr.test_patch = "".join(tests)
        pr.fix_patch = "".join(fixes)


class TheAlgorithmsJava6756To6818ImageBase(Image):
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
        return "maven:3.9-eclipse-temurin-21"

    def image_tag(self) -> str:
        return "base-TheAlgorithms_Java_6756_to_6818"

    def workdir(self) -> str:
        return "base-TheAlgorithms_Java_6756_to_6818"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # Shared base: toolchain + full-history clone only. The syntax directive
        # makes DockerfileEnhancer return this verbatim (so it injects neither the
        # proxy/CA block nor a BASE_COMMIT checkout) -- hence both are written here
        # and the checkout + history prune live in the PR layer. BASE_COMMIT is
        # declared because the harness passes it, but deliberately unused.
        return f"""# syntax=docker/dockerfile:1.6

FROM maven:3.9-eclipse-temurin-21

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
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

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
    git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""


_base_image: Optional[TheAlgorithmsJava6756To6818ImageBase] = None


class TheAlgorithmsJava6756To6818ImageDefault(Image):
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
        global _base_image
        if _base_image is None:
            _base_image = TheAlgorithmsJava6756To6818ImageBase(self.pr, self._config)
        return _base_image

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
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
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {pr.base.sha}
bash /home/check_git_changes.sh

mkdir -p /root/.m2
printf '%s' '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"><mirrors><mirror><id>aliyunmaven</id><mirrorOf>central</mirrorOf><url>https://maven.aliyun.com/repository/public</url></mirror></mirrors></settings>' > /root/.m2/settings.xml
mvn -B dependency:go-offline || true

# go-offline does not fetch surefire's JUnit provider; it is resolved only when
# surefire actually runs a test. Run one (present at every base commit in this
# range) so the graded runs need no network.
mvn -B -q -Dstyle.color=never test -Dtest=AbsoluteMaxTest -Dsurefire.failIfNoSpecifiedTests=false || true

# Hard gate, offline and last: the tree compiles and surefire can run.
mvn -B -o -q -Dstyle.color=never clean test -Dtest=AbsoluteMaxTest -Dsurefire.failIfNoSpecifiedTests=false
echo DEPS_OK

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
mvn clean test -B -Dstyle.color=never -Dmaven.test.failure.ignore=true || true
cat target/surefire-reports/TEST-*.xml || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                TEST_RUN_SCRIPT.replace("[[REPO]]", self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
mvn clean test -B -Dstyle.color=never -Dmaven.test.failure.ignore=true || true
cat target/surefire-reports/TEST-*.xml || true

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        repo = self.pr.repo
        sha = self.pr.base.sha

        # prepare.sh checks out this PR's base commit on the shared full-history
        # clone; the prune then removes everything outside its ancestry. No reset/
        # clean here -- it would discard prepare.sh's work.
        return f"""FROM {base.image_full_name()}

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
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test -z "$(git reflog)"; \\
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


@Instance.register("TheAlgorithms", "TheAlgorithms_Java_6756_to_6818")
class TheAlgorithmsJava6756To6818(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        _resplit_patches(pr)
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TheAlgorithmsJava6756To6818ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd

        return "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd

        # Re-materialise test-run.sh from this config before running it, so images
        # built with an older test-run.sh grade with the current one (no rebuild).
        script = TEST_RUN_SCRIPT.replace("[[REPO]]", self.pr.repo)
        encoded = base64.b64encode(script.encode()).decode()
        return (
            f"bash -c 'echo {encoded} | base64 -d > /home/test-run.sh"
            f" && bash /home/test-run.sh'"
        )

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd

        return "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        re_event = re.compile(
            r"<testcase\s[^>]*>|</testcase>|<(?:failure|error|skipped)\b[^>]*"
        )
        re_name = re.compile(r'\bname="([^"]+)"')
        re_classname = re.compile(r'\bclassname="([^"]+)"')

        statuses: dict[str, str] = {}
        current: Optional[str] = None

        for event in re_event.finditer(test_log):
            token = event.group(0)
            if token.startswith("<testcase"):
                name = re_name.search(token)
                classname = re_classname.search(token)
                if name and classname:
                    current = f"{classname.group(1)}.{name.group(1)}"
                    statuses[current] = "passed"
                else:
                    current = None
            elif token.startswith("</testcase"):
                current = None
            elif current is not None:
                if token.startswith("<skipped"):
                    statuses[current] = "skipped"
                else:
                    statuses[current] = "failed"

        passed_tests = {t for t, s in statuses.items() if s == "passed"}
        failed_tests = {t for t, s in statuses.items() if s == "failed"}
        skipped_tests = {t for t, s in statuses.items() if s == "skipped"}

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
