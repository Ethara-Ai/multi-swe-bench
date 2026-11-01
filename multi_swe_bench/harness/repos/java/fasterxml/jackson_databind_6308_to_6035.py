import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "fasterxml"
_REPO = "jackson-databind"
_INTERVAL = "jackson_databind_6308_to_6035"

_BOUNDS = [int(n) for n in re.search(r"_(\d+)_to_(\d+)$", _INTERVAL).groups()]
_PR_MIN = min(_BOUNDS)
_PR_MAX = max(_BOUNDS)

_BASE_TAG = f"base-{_PR_MAX}_to_{_PR_MIN}"

_SIBLING_REPOS = (
    "jackson-parent",
    "jackson-bom",
    "jackson-annotations",
    "jackson-core",
)

_MVNW = "./mvnw -B -ntp"

_MVN_TEST_FLAGS = (
    "-Dmaven.test.failure.ignore=true "
    "-DfailIfNoTests=false "
    "-Dsurefire.failIfNoSpecifiedTests=false"
)

_ROUTING_FLAG = "_jackson_databind_6308_to_6035_routed"

if not getattr(Instance, _ROUTING_FLAG, False):
    _orig_create = Instance.create.__func__

    def _routed_create(cls, pr, config, *args, **kwargs):
        if (
            not pr.number_interval
            and pr.org.lower() == _ORG
            and pr.repo == _REPO
            and _PR_MIN <= pr.number <= _PR_MAX
        ):
            pr.number_interval = _INTERVAL
        return _orig_create(cls, pr, config, *args, **kwargs)

    Instance.create = classmethod(_routed_create)
    setattr(Instance, _ROUTING_FLAG, True)


_INSTALL_DEPS = r"""#!/bin/bash
set -e

BASE_REF="$1"
DATE="$(git -C /home/jackson-databind show -s --format=%cI HEAD)"
MVN_FLAGS="-B -ntp -nsu -DskipTests -Dmaven.javadoc.skip=true -Dgpg.skip=true"

pom_version() {
    git -C "/home/$1" show "$2:pom.xml" 2>/dev/null \
        | sed -n '/<\/parent>/,$p' | grep -m1 '<version>' \
        | sed 's/ //g;s/<[a-z]*>//;s/<.*//'
}

pom_property() {
    git -C "/home/$1" show "$2:pom.xml" \
        | sed -n "s:.*<$3>\(.*\)</$3>.*:\1:p" | head -n 1
}

pom_parent() {
    git -C "/home/$1" show "$2:pom.xml" \
        | sed -n '/<parent>/,/<\/parent>/p' | grep -m1 "<$3>" \
        | sed 's/ //g;s/<[a-zA-Z]*>//;s/<.*//'
}

find_commit() {
    local repo="$1" version="$2" branch commit
    for branch in "$BASE_REF" $(git -C "/home/$repo" for-each-ref --format='%(refname:lstrip=3)' refs/remotes/origin \
            | grep -E '^[0-9]+\.([0-9]+|x)$' | sort -rV); do
        git -C "/home/$repo" rev-parse -q --verify "origin/$branch" > /dev/null || continue
        commit="$(git -C "/home/$repo" rev-list -1 --first-parent --before="$DATE" "origin/$branch")"
        if [ -n "$commit" ] && [ "$(pom_version "$repo" "$commit")" = "$version" ]; then
            echo "$commit"
            return 0
        fi
    done
    for branch in "$BASE_REF" $(git -C "/home/$repo" for-each-ref --format='%(refname:lstrip=3)' refs/remotes/origin \
            | grep -E '^[0-9]+\.([0-9]+|x)$' | sort -rV); do
        git -C "/home/$repo" rev-parse -q --verify "origin/$branch" > /dev/null || continue
        for commit in $(git -C "/home/$repo" rev-list --first-parent --before="$DATE" "origin/$branch" -- pom.xml); do
            if [ "$(pom_version "$repo" "$commit")" = "$version" ]; then
                echo "$commit"
                return 0
            fi
        done
    done
    return 1
}

install_snapshot() {
    local repo="$1" version="$2" commit
    case "$version" in
        *-SNAPSHOT) ;;
        *) return 0 ;;
    esac
    if ! commit="$(find_commit "$repo" "$version")"; then
        echo "install_deps: no $repo commit found for $version"
        return 0
    fi
    echo "install_deps: installing $repo $version from $commit"
    cd "/home/$repo"
    git checkout -q --detach "$commit"
    git clean -fdxq
    if [ -x ./mvnw ]; then
        ./mvnw $MVN_FLAGS install
    else
        mvn $MVN_FLAGS install
    fi
    git clean -fdxq
}

DATABIND_PARENT="$(pom_parent jackson-databind HEAD version)"
BOM_COMMIT="$(find_commit jackson-bom "$DATABIND_PARENT" || true)"
if [ -z "$BOM_COMMIT" ]; then
    echo "install_deps: no jackson-bom commit found for $DATABIND_PARENT"
    exit 0
fi

if [ "$(pom_parent jackson-bom "$BOM_COMMIT" artifactId)" = "jackson-parent" ]; then
    install_snapshot jackson-parent "$(pom_parent jackson-bom "$BOM_COMMIT" version)"
fi
install_snapshot jackson-bom "$DATABIND_PARENT"

ANNOTATIONS_VERSION="$(pom_property jackson-bom "$BOM_COMMIT" jackson.version.annotations)"
CORE_VERSION="$(pom_property jackson-bom "$BOM_COMMIT" jackson.version.core)"
case "$ANNOTATIONS_VERSION" in *'${'*) ANNOTATIONS_VERSION="$DATABIND_PARENT" ;; esac
case "$CORE_VERSION" in *'${'*|"") CORE_VERSION="$DATABIND_PARENT" ;; esac

install_snapshot jackson-annotations "$ANNOTATIONS_VERSION"
install_snapshot jackson-core "$CORE_VERSION"
"""


class JacksonDatabind6308To6035ImageBase(Image):
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
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        repo = self.pr.repo
        if self.config.need_clone:
            code = (
                f'RUN git clone "${{REPO_URL}}" /home/{repo} && \\\n'
                f"    cd /home/{repo} && git rev-parse HEAD >/dev/null"
            )
        else:
            code = f"COPY {repo} /home/{repo}"

        siblings = "\n".join(
            f'RUN git clone "https://github.com/FasterXML/{sibling}.git" /home/{sibling}'
            for sibling in _SIBLING_REPOS
        )

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{repo}.git"
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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CI=true \\
    MAVEN_OPTS="-Xmx4g"

{self.global_env}

LABEL org.opencontainers.image.title="{self.pr.org}/{repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl unzip openjdk-17-jdk maven \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

{code}

{siblings}

{self.clear_env}

CMD ["/bin/bash"]
"""


class JacksonDatabind6308To6035ImageDefault(Image):
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
        return JacksonDatabind6308To6035ImageBase(self.pr, self._config)

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
                "install_deps.sh",
                _INSTALL_DEPS,
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

bash /home/install_deps.sh {pr.base.ref} || true

cd /home/{pr.repo}
{mvn} -nsu clean test-compile || true
{mvn} -nsu test {flags}

{mvn} -o -q test-compile
echo DEPS_OK
""".format(pr=self.pr, mvn=_MVNW, flags=_MVN_TEST_FLAGS),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
{mvn} -o clean test {flags}
find . -path '*/surefire-reports/TEST-*.xml' -exec cat {{}} +
""".format(pr=self.pr, mvn=_MVNW, flags=_MVN_TEST_FLAGS),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
{mvn} -o clean test {flags}
find . -path '*/surefire-reports/TEST-*.xml' -exec cat {{}} +
""".format(pr=self.pr, mvn=_MVNW, flags=_MVN_TEST_FLAGS),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{mvn} -o clean test {flags}
find . -path '*/surefire-reports/TEST-*.xml' -exec cat {{}} +
""".format(pr=self.pr, mvn=_MVNW, flags=_MVN_TEST_FLAGS),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        repo = self.pr.repo
        sha = self.pr.base.sha

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach "{sha}"; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
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

{self.clear_env}
"""


@Instance.register(_ORG, _INTERVAL)
@Instance.register("FasterXML", _INTERVAL)
class JACKSON_DATABIND_6308_TO_6035(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return JacksonDatabind6308To6035ImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        token_pattern = re.compile(
            r"<testsuite\b([^>]*)>|<testcase\b([^>]*?)(/>|>(.*?)</testcase>)",
            re.DOTALL,
        )
        name_pattern = re.compile(r'\bname="([^"]*)"')
        classname_pattern = re.compile(r'\bclassname="([^"]*)"')

        # Generated suites (e.g. Guava testlib) repeat the same classname#name
        # within one surefire suite; number repeats by order within that suite.
        cases = []
        suite = ""
        for match in token_pattern.finditer(test_log):
            if match.group(1) is not None:
                suite_name = name_pattern.search(match.group(1))
                suite = suite_name.group(1) if suite_name else ""
                continue

            attrs = match.group(2)
            name = name_pattern.search(attrs)
            classname = classname_pattern.search(attrs)
            if not name or not classname:
                continue

            test_name = f"{classname.group(1)}#{name.group(1)}"
            body = match.group(4) or ""

            if "<failure" in body or "<error" in body:
                status = "failed"
            elif "<skipped" in body:
                status = "skipped"
            else:
                status = "passed"
            cases.append((suite, test_name, status))

        totals = {}
        for suite, test_name, _ in cases:
            totals[(suite, test_name)] = totals.get((suite, test_name), 0) + 1

        seen = {}
        for suite, test_name, status in cases:
            key = (suite, test_name)
            if totals[key] > 1:
                seen[key] = seen.get(key, 0) + 1
                test_name = f"{test_name}[{seen[key]}]"

            if status == "failed":
                failed_tests.add(test_name)
            elif status == "skipped":
                skipped_tests.add(test_name)
            else:
                passed_tests.add(test_name)

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
