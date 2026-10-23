import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL_NAME = "dubbo_4548_to_1329"

_BASE_TAG = "base-4548_to_1329"

_MVN_FLAGS = (
    "-B -Drat.skip=true -Dcheckstyle.skip=true -Denforcer.skip=true "
    "-Dmaven.javadoc.skip=true -Dgpg.skip=true -Dlicense.skip=true"
)

_MVN_TEST = (
    "mvn " + _MVN_FLAGS + " -fn clean test \"${skip_modules[@]}\" "
    "-Dmaven.test.failure.ignore=true -Dsurefire.useFile=false -DfailIfNoTests=false "
    "-Dsurefire.timeout=1800"
)

_MVN_WARM = "mvn " + _MVN_FLAGS + " -fae install -DskipTests"

_FLAKY_TESTS = re.compile(
    r"(?:^|\.)(?:ImplicitCallBackTest#.+|HessianProtocolTest#testHttpClient|ExecutionListTest#testAddNullRunnable|ExplicitCallbackTest#TestCallbackMultiInstans)$"
)

_APPLY_PATCH = r"""
apply_patch() {
  local patch="$1"
  local excludes=()
  while IFS= read -r path; do
    [ -n "$path" ] && excludes+=("--exclude=$path")
  done < <(awk '/^diff --git /{p=$3; sub(/^a\//,"",p)} /^GIT binary patch$/ || /^Binary files /{print p}' "$patch" | sort -u)
  if ! git apply --whitespace=nowarn "${excludes[@]}" "$patch"; then
    echo "Error: git apply $patch failed" >&2
    exit 1
  fi
}
"""

_EMIT_TESTS = r"""
emit_tests() {
  find . -path '*/target/surefire-reports/TEST-*.xml' 2>/dev/null | sort | while read -r f; do
    tr '\n' ' ' < "$f" | sed 's/<testcase/\n<testcase/g' | sed '1d' | while IFS= read -r el; do
      cls=$(printf '%s' "$el" | sed -n 's/.*classname="\([^"]*\)".*/\1/p')
      meth=$(printf '%s' "$el" | sed -n 's/.*[^s]name="\([^"]*\)".*/\1/p')
      [ -z "$cls" ] && continue
      [ -z "$meth" ] && continue
      meth=$(printf '%s' "$meth" | sed -e 's/{[^}]*}//g' -e 's/\[[^][]*\]$//' -e 's/[[:space:]]*$//' -e 's/[[:space:]]/_/g')
      status=PASS
      case "$el" in
        *"<failure"*|*"<error"*) status=FAIL ;;
        *"<skipped"*)            status=SKIP ;;
      esac
      printf '%s %s#%s\n' "$status" "$cls" "$meth"
    done
  done | sort -u | awk '
    { st=$1; name=$2;
      if (!(name in best)) best[name]=st;
      else {
        if (st=="FAIL" || best[name]=="FAIL") best[name]="FAIL";
        else if (st=="PASS" || best[name]=="PASS") best[name]="PASS";
      }
    }
    END { for (n in best) printf "MSWB_TEST: %s %s\n", best[n], n }
  ' | sort -k3
}
"""

_RUN_TAIL = r"""
skip_modules=()
if grep -q '<module>dubbo-registry-sofa</module>' dubbo-registry/pom.xml 2>/dev/null; then
  skip_modules=(-pl '!dubbo-registry/dubbo-registry-sofa')
fi
mvn_status=0
__MVN_TEST__ || mvn_status=$?
emit_tests
if ! find . -path '*/target/surefire-reports/TEST-*.xml' | grep -q .; then
  echo "Error: no surefire reports produced (mvn exit ${mvn_status})" >&2
  exit 1
fi
""".replace("__MVN_TEST__", _MVN_TEST)


class Dubbo4548To1329ImageBase(Image):

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
        return "maven:3.8.8-eclipse-temurin-8"

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

        org = self.pr.org
        repo = self.pr.repo

        global_env = self.global_env
        clear_env = self.clear_env
        global_block = f"\n{global_env}\n" if global_env else ""
        clear_block = f"\n{clear_env}\n" if clear_env else ""

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
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
    CI=true \\
    JAVA_HOME=/opt/java/openjdk \\
    MAVEN_OPTS="-Xmx2g -Dmaven.wagon.http.retryHandler.count=5"

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
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
    && apt-get clean \\
    && rm -rf /var/lib/apt/lists/*
{global_block}
WORKDIR /home/

RUN git config --global http.version HTTP/1.1 \\
    && git config --global http.postBuffer 524288000 \\
    && for attempt in 1 2 3 4 5; do \\
        rm -rf /home/{repo}; \\
        if git clone "${{REPO_URL}}" /home/{repo}; then break; fi; \\
        echo "clone attempt ${{attempt}} failed; retrying in 15s" >&2; \\
        sleep 15; \\
    done \\
    && test -d /home/{repo}/.git
{clear_block}
CMD ["/bin/bash"]
"""


class Dubbo4548To1329ImageDefault(Image):

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
        return Dubbo4548To1329ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        org = self.pr.org
        sha = self.pr.base.sha

        check_git_changes = """#!/bin/bash
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

        prepare = """#!/bin/bash
set -eo pipefail

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git remote add origin https://github.com/{org}/{repo}.git 2>/dev/null || true
git rev-parse --verify --quiet "{sha}^{{commit}}" >/dev/null 2>&1 \\
    || git fetch --depth=1 origin {sha} 2>/dev/null \\
    || git fetch origin 2>/dev/null || true
git checkout --detach --force {sha}
bash /home/check_git_changes.sh

warm_status=1
for attempt in 1 2 3; do
  if {warm}; then
    warm_status=0
    break
  fi
  echo "dependency warm-up attempt ${{attempt}} failed; retrying" >&2
  sleep 10
done

git checkout -- .
git clean -fdx

test "${{warm_status}}" -eq 0
test -d "${{HOME}}/.m2/repository"
echo "DEPS_OK"
""".format(repo=repo, org=org, sha=sha, warm=_MVN_WARM)

        run = (
            "#!/bin/bash\nset -eo pipefail\nexport CI=true\n\ncd /home/{repo}\n".format(repo=repo)
            + _EMIT_TESTS
            + _RUN_TAIL
        )

        test_run = (
            "#!/bin/bash\nset -eo pipefail\nexport CI=true\n\ncd /home/{repo}\n".format(repo=repo)
            + _APPLY_PATCH
            + _EMIT_TESTS
            + "\napply_patch /home/test.patch\n"
            + _RUN_TAIL
        )

        fix_run = (
            "#!/bin/bash\nset -eo pipefail\nexport CI=true\n\ncd /home/{repo}\n".format(repo=repo)
            + _APPLY_PATCH
            + _EMIT_TESTS
            + "\napply_patch /home/test.patch\napply_patch /home/fix.patch\n"
            + _RUN_TAIL
        )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", check_git_changes),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", run),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("Dubbo4548To1329ImageDefault requires an Image dependency")
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{copy_commands}
ARG BASE_COMMIT="{self.pr.base.sha}"

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{Image._HARDENING_BLOCK}
"""


@Instance.register("apache", _INTERVAL_NAME)
class DUBBO_4548_TO_1329(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Dubbo4548To1329ImageDefault(self.pr, self._config)

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
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        emitted = re.compile(r"^MSWB_TEST:\s+(PASS|FAIL|SKIP)\s+(\S+)\s*$")
        for line in clean_log.splitlines():
            match = emitted.match(line.strip())
            if not match:
                continue
            status, name = match.group(1), match.group(2)
            if _FLAKY_TESTS.search(name):
                continue
            if status == "FAIL":
                failed_tests.add(name)
            elif status == "SKIP":
                skipped_tests.add(name)
            else:
                passed_tests.add(name)

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
