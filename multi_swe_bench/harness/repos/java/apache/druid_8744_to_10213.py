import re
from pathlib import Path
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ERA_NAME = Path(__file__).stem
_ERA_BOUNDS = sorted(int(part) for part in _ERA_NAME.split("_") if part.isdigit())
_ERA_MIN, _ERA_MAX = _ERA_BOUNDS[0], _ERA_BOUNDS[-1]

_BASE_TAG = f"base-{_ERA_NAME}"
_BASE_IMAGE = "maven:3.9-eclipse-temurin-8"

_DIFF_RE = re.compile(r"^diff --git \"?a/(.+?)\"? \"?b/(.+?)\"?$", re.MULTILINE)

_BINARY_MARKER_RE = re.compile(
    r"^(?:GIT binary patch|Binary files .* differ)\s*$", re.MULTILINE
)

_TEST_METHOD_RE = re.compile(
    r"@Test\b[^\n]*\n(?:[ \t]*@[^\n]*\n)*[ \t]*(?:public\s+)?void\s+(\w+)\s*\(",
    re.MULTILINE,
)

_NON_MODULE_DIRS = frozenset(
    {
        ".editorconfig",
        ".git",
        ".gitattributes",
        ".github",
        ".gitignore",
        ".idea",
        ".licenserc.yaml",
        ".mvn",
        ".travis.yml",
        "codestyle",
        "dev",
        "distribution",
        "docs",
        "examples",
        "hooks",
        "licenses",
        "publications",
        "website",
    }
)

_GROUPING_DIRS = frozenset(
    {
        "cloud",
        "extensions",
        "extensions-contrib",
        "extensions-core",
    }
)

_SKIP_PROPERTIES = (
    "animal.sniffer",
    "checkstyle",
    "cyclonedx",
    "dependency-check",
    "druid.console",
    "enforcer",
    "forbiddenapis",
    "license",
    "maven.javadoc",
    "pmd",
    "rat",
    "remoteresources",
    "spotbugs",
)

_MVN_COMMON = " ".join(
    [
        "-fn",
        "-Dsurefire.useFile=false",
        "-Dmaven.test.skip=false",
        "-DfailIfNoTests=false",
        "-Dsurefire.failIfNoSpecifiedTests=false",
    ]
    + [f"-D{name}.skip=true" for name in _SKIP_PROPERTIES]
)

_SHELL_ENV = """export CI=true
export MAVEN_OPTS="-Xmx4g\""""

_MVN_LOG = "/tmp/mvn-stage.log"

_PURGE_REPORTS = (
    "find . -type d -name surefire-reports -prune -exec rm -rf {} + 2>/dev/null || true"
)

_DUMP_REPORTS = (
    "find . -path '*/target/surefire-reports/TEST-*.xml' -exec cat {} \\; "
    "2>/dev/null || true"
)

_STAGE_GATE = f"""if [ -z "$(find . -path '*/target/surefire-reports/TEST-*.xml' -print -quit 2>/dev/null)" ] \\
   && ! grep -q 'COMPILATION ERROR' {_MVN_LOG} 2>/dev/null; then
  echo "FATAL: maven exited $rc and produced neither surefire reports nor a compilation error" >&2
  exit 1
fi"""


def _mvn_invoke(cmd: str) -> str:
    return f"rc=0\n{cmd} 2>&1 | tee {_MVN_LOG} || rc=$?"


def _result(
    passed_tests: set[str], failed_tests: set[str], skipped_tests: set[str]
) -> TestResult:
    passed_tests = passed_tests - failed_tests
    skipped_tests = skipped_tests - failed_tests - passed_tests
    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


def _changed_paths(patch_text: str) -> list[str]:
    paths = []
    for match in _DIFF_RE.finditer(patch_text or ""):
        target = match.group(2)
        source = match.group(1)
        path = source if target == "/dev/null" else target
        if path and path != "/dev/null":
            paths.append(path)
    return paths


def _patch_sections(patch_text: str):
    for section in re.split(r"(?m)^(?=diff --git )", patch_text or ""):
        match = _DIFF_RE.search(section)
        if not match:
            continue
        target, source = match.group(2), match.group(1)
        path = source if target == "/dev/null" else target
        if path and path != "/dev/null":
            yield path, section


def _binary_paths(patch_text: str) -> list[str]:
    return sorted(
        {
            path
            for path, section in _patch_sections(patch_text)
            if _BINARY_MARKER_RE.search(section)
        }
    )


def _git_apply(patch_file: str, patch_text: str) -> str:
    excludes = "".join(f" --exclude='{path}'" for path in _binary_paths(patch_text))
    return f"git apply --whitespace=nowarn{excludes} {patch_file}"


def _patched_test_classes(patch_text: str) -> dict[str, tuple[str, list[str]]]:
    classes = {}
    for path, section in _patch_sections(patch_text):
        if "/src/test/java/" not in path or not path.endswith(".java"):
            continue
        fqcn = path.split("/src/test/java/", 1)[1][: -len(".java")].replace("/", ".")
        added = "\n".join(
            line[1:]
            for line in section.split("\n")
            if line.startswith("+") and not line.startswith("+++")
        )
        methods = sorted(set(_TEST_METHOD_RE.findall(added)))
        if methods:
            classes[fqcn] = (path, methods)
    return classes


def _compile_fail_block(pr: PullRequest) -> str:
    classes = _patched_test_classes(pr.test_patch)
    if not classes:
        return ""

    blocks = []
    for fqcn, (path, methods) in sorted(classes.items()):
        cases = "\n".join(
            f'<testcase classname="{fqcn}" name="{method}">'
            f'<failure message="test sources did not compile at this stage" '
            f'type="CompilationFailure">The test class did not compile, so this '
            f"test could not run.</failure></testcase>"
            for method in methods
        )
        blocks.append(
            f"""  if [ -f "{path}" ] && [ -z "$(find . -name 'TEST-{fqcn}.xml' -print -quit 2>/dev/null)" ]; then
    cat <<'MSWEBENCH_COMPILE_FAIL_EOF'
<testsuite name="{fqcn}" tests="{len(methods)}" failures="{len(methods)}" errors="0" skipped="0">
{cases}
</testsuite>
MSWEBENCH_COMPILE_FAIL_EOF
  fi"""
        )

    body = "\n".join(blocks)
    return f"""
if grep -q 'COMPILATION ERROR' {_MVN_LOG} 2>/dev/null; then
{body}
fi
"""


def _modules(patch_text: str) -> set[str]:
    modules = set()
    for path in _changed_paths(patch_text):
        segments = path.split("/")
        if len(segments) < 2:
            continue
        top = segments[0]
        if top in _NON_MODULE_DIRS:
            continue
        if top in _GROUPING_DIRS:
            if len(segments) >= 3:
                modules.add(f"{segments[0]}/{segments[1]}")
            continue
        modules.add(top)
    return modules


def _target_modules(pr: PullRequest) -> list[str]:
    modules = _modules(pr.fix_patch) | _modules(pr.test_patch)
    modules.discard("")
    return sorted(modules)


def _build_pl_flag(pr: PullRequest) -> str:
    modules = _target_modules(pr)
    if not modules:
        return ""
    return "-pl " + ",".join(modules) + " -am"


def _warm_surefire(pr: PullRequest) -> str:
    modules = _target_modules(pr)
    if not modules:
        return ""
    listed = " ".join(f'"{module}"' for module in modules)
    base = " ".join(
        part for part in ("mvn test", _MVN_COMMON, _build_pl_flag(pr)) if part
    )
    return """
if [ -z "$(find . -path '*/target/surefire-reports/TEST-*.xml' -print -quit 2>/dev/null)" ]; then
  for module in {listed}; do
    warm=$(find "$module/src/test/java" -name '*Test.java' -print 2>/dev/null | head -1)
    if [ -n "$warm" ]; then
      warm=$(basename "$warm" .java)
      echo "prepare: no test matched the PR scope; warming surefire provider with $warm"
      {base} -Dtest="$warm" || true
      break
    fi
  done
fi
""".format(listed=listed, base=base)


def _module_gate(pr: PullRequest) -> str:
    modules = _target_modules(pr)
    if not modules:
        return ""
    listed = " ".join(f'"{module}"' for module in modules)
    return """
for module in {listed}; do
  if [ ! -d "$module/target/classes" ] && [ ! -d "$module/target/test-classes" ]; then
    echo "FATAL: module $module produced no compiled output (maven exit $rc)" >&2
    exit 1
  fi
done
""".format(listed=listed)


def _test_classes(patch_text: str) -> set[str]:
    classes = set()
    for path in _changed_paths(patch_text):
        if "/src/test/" not in path or not path.endswith(".java"):
            continue
        classes.add(path.rsplit("/", 1)[-1][: -len(".java")])
    return classes


def _build_test_flag(pr: PullRequest) -> str:
    classes = _test_classes(pr.test_patch)
    if not classes:
        return ""
    return "-Dtest=" + ",".join(f"{name}*" for name in sorted(classes))


def _mvn_scope(pr: PullRequest) -> str:
    return " ".join(part for part in (_build_pl_flag(pr), _build_test_flag(pr)) if part)


def _mvn_prepare_cmd(pr: PullRequest) -> str:
    return " ".join(
        part for part in ("mvn clean test", _MVN_COMMON, _mvn_scope(pr)) if part
    )


def _mvn_run_cmd(pr: PullRequest) -> str:
    return " ".join(part for part in ("mvn test -o", _MVN_COMMON, _mvn_scope(pr)) if part)


class Druid8744To10213ImageBase(Image):
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
        return _BASE_IMAGE

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

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

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

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class Druid8744To10213ImageDefault(Image):
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
        return Druid8744To10213ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        number = self.pr.number
        prepare_cmd = _mvn_prepare_cmd(self.pr)
        run_cmd = _mvn_run_cmd(self.pr)
        apply_test = _git_apply("/home/test.patch", self.pr.test_patch)
        apply_fix = _git_apply("/home/fix.patch", self.pr.fix_patch)
        compile_fail = _compile_fail_block(self.pr)

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
                """#!/bin/bash
set -eo pipefail

{shell_env}

cd /home/{repo}

git cat-file -e {sha}^{{commit}} 2>/dev/null \\
    || git fetch --no-tags --depth=2147483647 origin {sha} \\
    || git fetch --no-tags origin "+refs/pull/{number}/head:refs/remotes/origin/pr-{number}"

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach {sha}
bash /home/check_git_changes.sh

rc=0
{mvn_cmd} || rc=$?
{warm_surefire}{module_gate}
if [ -z "$(find . -type d -path '*/target/test-classes' -print -quit 2>/dev/null)" ]; then
  echo "FATAL: prepare compiled no test classes (maven exit $rc)" >&2
  exit 1
fi

if [ -z "$(find "$HOME/.m2/repository" -type d -name 'surefire-junit*' -print -quit 2>/dev/null)" ]; then
  echo "FATAL: the surefire test provider is absent, offline runs cannot execute tests (maven exit $rc)" >&2
  exit 1
fi

{purge_reports}
""".format(
                    shell_env=_SHELL_ENV,
                    repo=repo,
                    sha=sha,
                    number=number,
                    mvn_cmd=prepare_cmd,
                    warm_surefire=_warm_surefire(self.pr),
                    module_gate=_module_gate(self.pr),
                    purge_reports=_PURGE_REPORTS,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

{shell_env}

cd /home/{repo}

{purge_reports}

{mvn_invoke}

{dump_reports}

{stage_gate}
""".format(
                    shell_env=_SHELL_ENV,
                    repo=repo,
                    purge_reports=_PURGE_REPORTS,
                    mvn_invoke=_mvn_invoke(run_cmd),
                    dump_reports=_DUMP_REPORTS,
                    stage_gate=_STAGE_GATE,
                ),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

{shell_env}

cd /home/{repo}
if ! {apply_test}; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi

{purge_reports}

{mvn_invoke}

{dump_reports}
{compile_fail}
{stage_gate}
""".format(
                    shell_env=_SHELL_ENV,
                    repo=repo,
                    apply_test=apply_test,
                    purge_reports=_PURGE_REPORTS,
                    mvn_invoke=_mvn_invoke(run_cmd),
                    dump_reports=_DUMP_REPORTS,
                    compile_fail=compile_fail,
                    stage_gate=_STAGE_GATE,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

{shell_env}

cd /home/{repo}
if ! {apply_test}; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi

if ! {apply_fix}; then
    echo "Error: git apply fix.patch failed" >&2
    exit 1
fi

{purge_reports}

{mvn_invoke}

{dump_reports}
{compile_fail}
{stage_gate}
""".format(
                    shell_env=_SHELL_ENV,
                    repo=repo,
                    apply_test=apply_test,
                    apply_fix=apply_fix,
                    purge_reports=_PURGE_REPORTS,
                    mvn_invoke=_mvn_invoke(run_cmd),
                    dump_reports=_DUMP_REPORTS,
                    compile_fail=compile_fail,
                    stage_gate=_STAGE_GATE,
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if not isinstance(image, Image):
            raise ValueError(f"{type(self).__name__} needs an Image dependency")

        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {image.image_name()}:{image.image_tag()}

{copy_commands}
WORKDIR /home/{repo}


RUN set -eux; \\
    git checkout --detach {sha}; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse {sha})"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive ' \\
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

RUN bash /home/prepare.sh
"""


@Instance.register("apache", _ERA_NAME)
class DRUID_8744_TO_10213(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Druid8744To10213ImageDefault(self.pr, self._config)

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

        test_log = re.sub(r"\x1B\[[0-9;?]*[a-zA-Z]", "", test_log)

        re_case = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.S)
        re_attr = re.compile(r'(\w+)="([^"]*)"')
        saw_xml = False
        for match in re_case.finditer(test_log):
            attrs = dict(re_attr.findall(match.group(1)))
            class_name = attrs.get("classname", "")
            method = attrs.get("name", "")
            if not method:
                continue
            saw_xml = True
            name = f"{class_name}.{method}" if class_name else method
            body = match.group(3) or ""
            if "<failure" in body or "<error" in body:
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
            elif "<skipped" in body:
                if name not in passed_tests and name not in failed_tests:
                    skipped_tests.add(name)
            else:
                if name not in failed_tests:
                    skipped_tests.discard(name)
                    passed_tests.add(name)

        if saw_xml:
            return _result(passed_tests, failed_tests, skipped_tests)

        re_summary = re.compile(
            r"Running\s+(.+?)\s*\n(?:(?!.*Tests run:)(?!.*Running\s)(?!.*Results:).*\n)*"
            r".*?Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)"
            r"(?P<failed>.*<<<\s*FAILURE!)?"
        )

        for match in re_summary.finditer(test_log):
            name = match.group(1)
            tests_run = int(match.group(2))
            failures = int(match.group(3))
            errors = int(match.group(4))
            skipped = int(match.group(5))
            if match.group("failed") or failures > 0 or errors > 0:
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
            elif name in failed_tests:
                continue
            elif tests_run > 0 and skipped == tests_run:
                skipped_tests.add(name)
            elif tests_run > 0:
                passed_tests.add(name)

        return _result(passed_tests, failed_tests, skipped_tests)


_INCUMBENT = Instance._registry.get("apache/druid")


@Instance.register("apache", "druid")
class DruidEraDispatch(Instance):
    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        if _ERA_MIN <= pr.number <= _ERA_MAX:
            return DRUID_8744_TO_10213(pr, config, *args, **kwargs)
        if _INCUMBENT is not None:
            return _INCUMBENT(pr, config, *args, **kwargs)
        raise ValueError(
            f"apache/druid#{pr.number} is outside {_ERA_MIN}-{_ERA_MAX} and no "
            f"other apache/druid adapter owns the bare name"
        )
