import re
from pathlib import Path
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ERA_NAME = Path(__file__).stem
_ERA_BOUNDS = sorted(int(part) for part in _ERA_NAME.split("_") if part.isdigit())
_ERA_MIN, _ERA_MAX = _ERA_BOUNDS[0], _ERA_BOUNDS[-1]

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
        "integration-tests",
        "licenses",
        "publications",
        "web-console",
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


_MVN_RETRY_LOG = "/tmp/mvn-stage-retry.log"

_REMOVED_TESTS = "/tmp/mvn-stage-removed-tests.txt"

_REMOVED_BACKUP = "/tmp/mvn-stage-removed"

_COMPILE_RETRIES = 3


def _mvn_invoke(cmd: str, repo: str) -> str:
    return f"""rc=0
: > {_REMOVED_TESTS}
rm -rf {_REMOVED_BACKUP}
{cmd} 2>&1 | tee {_MVN_LOG} || rc=$?
cp {_MVN_LOG} {_MVN_RETRY_LOG}
attempt=0
while [ "$attempt" -lt {_COMPILE_RETRIES} ] && grep -q 'COMPILATION ERROR' {_MVN_RETRY_LOG}; do
  broken=$(grep -oE '^\\[ERROR\\] /home/{repo}/[^:[:space:]]+/src/test/[^:[:space:]]+\\.java:\\[[0-9]+,[0-9]+\\]' {_MVN_RETRY_LOG} \\
    | sed -E 's/^\\[ERROR\\] //; s/:\\[[0-9]+,[0-9]+\\]$//' | sort -u || true)
  [ -n "$broken" ] || break
  echo "stage: test sources did not compile; rerunning without:"
  printf '%s\\n' "$broken" | tee -a {_REMOVED_TESTS}
  printf '%s\\n' "$broken" | while IFS= read -r f; do
    [ -f "$f" ] || continue
    mkdir -p "{_REMOVED_BACKUP}$(dirname "$f")"
    mv "$f" "{_REMOVED_BACKUP}$f"
  done
  {_PURGE_REPORTS}
  rc=0
  {cmd} 2>&1 | tee {_MVN_RETRY_LOG} || rc=$?
  cat {_MVN_RETRY_LOG} >> {_MVN_LOG}
  attempt=$((attempt + 1))
done"""


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
        classes[fqcn] = (path, methods)
    return classes


_ENCLOSING_CLASS_AWK = r"""{ line = $0 }
line ~ /^[ \t]*((public|protected|private|static|final|abstract)[ \t]+)*(class|interface|enum)[ \t]+[A-Za-z_][A-Za-z0-9_]*/ {
  decl = line
  sub(/^[ \t]*((public|protected|private|static|final|abstract)[ \t]+)*(class|interface|enum)[ \t]+/, "", decl)
  match(decl, /^[A-Za-z_][A-Za-z0-9_]*/)
  pending = substr(decl, RSTART, RLENGTH)
}
line ~ ("void[ \t]+" m "[ \t]*[(]") {
  out = ""
  for (i = 1; i <= n; i++) out = out (i > 1 ? "$" : "") stack[i]
  print out
  exit
}
{
  opens = gsub(/[{]/, "{", line)
  closes = gsub(/[}]/, "}", line)
  for (k = 0; k < opens; k++) { depth++; if (pending != "") { n++; stack[n] = pending; sdepth[n] = depth; pending = "" } }
  for (k = 0; k < closes; k++) { if (n > 0 && sdepth[n] == depth) n--; depth-- }
}"""


_TEST_METHODS_AWK = r"""/@Test/ { intest = 1; next }
intest && /^[ \t]*@/ { next }
intest && match($0, /void[ \t]+[A-Za-z_][A-Za-z0-9_]*[ \t]*\(/) {
  s = substr($0, RSTART, RLENGTH)
  sub(/^.*void[ \t]+/, "", s)
  sub(/[ \t]*\($/, "", s)
  print s
  intest = 0
}
intest && /[;={]/ { intest = 0 }
"""


def _compile_fail_block(pr: PullRequest) -> str:
    classes = _patched_test_classes(pr.test_patch)
    if not classes:
        return ""

    blocks = []
    for fqcn, (path, methods) in sorted(classes.items()):
        package = fqcn.rsplit(".", 1)[0] if "." in fqcn else ""
        prefix = package + "." if package else ""
        fallback = " ".join(methods)
        backup = f"{_REMOVED_BACKUP}/home/{pr.repo}/{path}"
        blocks.append(
            f"""  if {{ [ -f "{path}" ] || grep -qx "/home/{pr.repo}/{path}" {_REMOVED_TESTS} 2>/dev/null; }} \\
     && [ -z "$(find . -name 'TEST-{fqcn}.xml' -print -quit 2>/dev/null)" ]; then
    if [ -f "{path}" ]; then src="{path}"; else src="{backup}"; fi
    ms=$(awk -f /tmp/mswb-testmethods.awk "$src" 2>/dev/null | tr '\\n' ' ')
    [ -n "$ms" ] || ms="{fallback}"
    if [ -n "$ms" ]; then
      n=$(echo $ms | wc -w | tr -d ' ')
      printf '<testsuite name="{fqcn}" tests="%s" failures="%s" errors="0" skipped="0">\\n' "$n" "$n"
      for m in $ms; do
        cls=$(awk -v m="$m" -f /tmp/mswb-enclosing.awk "$src" 2>/dev/null)
        if [ -n "$cls" ]; then cn="{prefix}$cls"; else cn='{fqcn}'; fi
        printf '<testcase classname="%s" name="%s"><failure message="test sources did not compile at this stage" type="CompilationFailure">The test class did not compile, so this test could not run.</failure></testcase>\\n' "$cn" "$m"
      done
      echo '</testsuite>'
    fi
  fi"""
        )

    body = "\n".join(blocks)
    return (
        "\nif grep -q 'COMPILATION ERROR' " + _MVN_LOG + " 2>/dev/null; then\n"
        "cat > /tmp/mswb-enclosing.awk <<'MSWEBENCH_AWK_EOF'\n"
        + _ENCLOSING_CLASS_AWK
        + "\nMSWEBENCH_AWK_EOF\n"
        "cat > /tmp/mswb-testmethods.awk <<'MSWB_TMAWK_EOF'\n"
        + _TEST_METHODS_AWK
        + "\nMSWB_TMAWK_EOF\n"
        + body
        + "\nfi\n"
    )


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
    warm=$(find "$module/src/test/java" -name '*Test.java' -print -quit 2>/dev/null)
    if [ -n "$warm" ]; then
      warm=$(basename "$warm" .java)
      echo "prepare: no test matched the PR scope; warming surefire provider in $module with $warm"
      {base} -Dtest="$warm" || true
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


def _touches_pom(pr: PullRequest) -> bool:
    paths = _changed_paths(pr.fix_patch) + _changed_paths(pr.test_patch)
    return any(path.endswith("pom.xml") for path in paths)


def _mvn_run_cmd(pr: PullRequest) -> str:
    offline = "" if _touches_pom(pr) else "-o"
    return " ".join(
        part for part in ("mvn test", offline, _MVN_COMMON, _mvn_scope(pr)) if part
    )


def _probe_fallback(pr: PullRequest) -> str:
    modules = _target_modules(pr)
    if not modules:
        return ""
    listed = " ".join(f'"{module}"' for module in modules)
    offline = "" if _touches_pom(pr) else "-o"
    base = " ".join(
        part for part in ("mvn test", offline, _MVN_COMMON, _build_pl_flag(pr)) if part
    )
    return f"""
if [ -z "$(find . -path '*/target/surefire-reports/TEST-*.xml' -print -quit 2>/dev/null)" ] \\
   && ! grep -q 'COMPILATION ERROR' {_MVN_LOG} 2>/dev/null; then
  for module in {listed}; do
    probe=$(find "$module/src/test/java" -name '*Test.java' -print -quit 2>/dev/null)
    if [ -n "$probe" ]; then
      probe=$(basename "$probe" .java)
      echo "stage: no tests matched the scope; probing with $probe"
      rc=0
      {base} -Dtest="$probe" 2>&1 | tee -a {_MVN_LOG} || rc=$?
      break
    fi
  done
fi
"""


class Druid11257To14136ImageBase(Image):
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
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{repo}

CMD ["/bin/bash"]
"""


class Druid11257To14136ImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]: # type: ignore
        return Druid11257To14136ImageBase(self.pr, self._config)

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

{probe}

{dump_reports}

{stage_gate}
""".format(
                    shell_env=_SHELL_ENV,
                    repo=repo,
                    purge_reports=_PURGE_REPORTS,
                    mvn_invoke=_mvn_invoke(run_cmd, repo),
                    probe=_probe_fallback(self.pr),
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

{probe}

{dump_reports}
{compile_fail}
{stage_gate}
""".format(
                    shell_env=_SHELL_ENV,
                    repo=repo,
                    apply_test=apply_test,
                    purge_reports=_PURGE_REPORTS,
                    mvn_invoke=_mvn_invoke(run_cmd, repo),
                    probe=_probe_fallback(self.pr),
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

{probe}

{dump_reports}
{compile_fail}
{stage_gate}
""".format(
                    shell_env=_SHELL_ENV,
                    repo=repo,
                    apply_test=apply_test,
                    apply_fix=apply_fix,
                    purge_reports=_PURGE_REPORTS,
                    mvn_invoke=_mvn_invoke(run_cmd, repo),
                    probe=_probe_fallback(self.pr),
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

        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        return f"""FROM {image.image_name()}:{image.image_tag()}

ARG BASE_COMMIT="{sha}"

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

{hardening}
"""


@Instance.register("apache", _ERA_NAME)
class DRUID_11257_TO_14136(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]: # type: ignore
        return Druid11257To14136ImageDefault(self.pr, self._config)

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
            return DRUID_11257_TO_14136(pr, config, *args, **kwargs)
        if _INCUMBENT is not None:
            return _INCUMBENT(pr, config, *args, **kwargs)
        raise ValueError(
            f"apache/druid#{pr.number} is outside {_ERA_MIN}-{_ERA_MAX} and no "
            f"other apache/druid adapter owns the bare name"
        )
