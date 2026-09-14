import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NON_MODULE_DIRS = frozenset(
    {".github", ".git", ".gitignore", ".gitattributes", ".mvn", "docs", "style"}
)
_NO_TEST_MODULES = frozenset({"distribution"})


def _diff_paths(patch: str):
    for line in (patch or "").split("\n"):
        if not line.startswith("diff --git"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        p = parts[2]
        yield p[2:] if p.startswith(("a/", "b/")) else p


def _extract_modules_from_patch(patch: str) -> set[str]:
    modules: set[str] = set()
    for path in _diff_paths(patch):
        segments = path.split("/")
        if len(segments) < 2 or segments[0] in _NON_MODULE_DIRS:
            continue
        modules.add(segments[0])
    return modules


def _build_pl_flag(pr: PullRequest) -> str:
    mods = _extract_modules_from_patch(pr.fix_patch) | _extract_modules_from_patch(
        pr.test_patch
    )
    mods.discard("")
    mods = (mods - _NO_TEST_MODULES) or mods
    return ",".join(sorted(mods)) if mods else ""


def _extract_test_classes(patch: str) -> list[str]:
    classes: set[str] = set()
    for path in _diff_paths(patch):
        if "/src/test/java/" not in path or not path.endswith(".java"):
            continue
        name = path.rsplit("/", 1)[-1][: -len(".java")]
        if not (
            name.startswith("Test")
            or name.endswith(("Test", "Tests", "TestCase", "IT"))
        ):
            continue
        if name.endswith("TestBase") or "Base" in name:
            continue
        classes.add(name)
    return sorted(classes)


def _extract_test_dirs(patch: str) -> list[str]:
    dirs: set[str] = set()
    for path in _diff_paths(patch):
        if "/src/test/java/" not in path or not path.endswith(".java"):
            continue
        dirs.add(path.rsplit("/", 1)[0])
    return sorted(dirs)


_MAX_SIBLINGS = 10

_TEST_RESOLVER_SH = """\
export TEST_FLAG=""
_GOLD="{gold}"
_DIRS="{dirs}"
_list="$_GOLD"
_nsib=0
_seen() {{ case ",$_list," in *",$1,"*) return 0;; esac; return 1; }}
_scan() {{
    [ -d "$1" ] || return 0
    for _f in "$1"/*Test.java "$1"/*Tests.java "$1"/*TestCase.java; do
        [ -f "$_f" ] || continue
        _b=$(basename "$_f" .java)
        case "$_b" in *Base*|Abstract*|PlainAccessValidatorTest) continue;; esac
        _seen "$_b" && continue
        [ "$_nsib" -ge {maxsib} ] && return 0
        _list="$_list,$_b"
        _nsib=$((_nsib + 1))
    done
}}
if [ -n "$_GOLD" ]; then
    for _d in $_DIRS; do _scan "$_d"; done
    if [ "$_nsib" -lt 3 ]; then
        for _d in $_DIRS; do _scan "$(dirname "$_d")"; done
    fi
    echo "test selector: $_nsib sibling(s) added for p2p -> $_list"
    TEST_FLAG="-Dtest=$_list -Dsurefire.failIfNoSpecifiedTests=false"
fi
"""


def _build_test_resolver(pr: PullRequest) -> str:
    return _TEST_RESOLVER_SH.format(
        gold=",".join(_extract_test_classes(pr.test_patch)),
        dirs=" ".join(_extract_test_dirs(pr.test_patch)),
        maxsib=_MAX_SIBLINGS,
    )


_JUNIT5_MARKER = "org.junit.jupiter"
_JUPITER_ENGINE_VERSION = "5.9.1"
_SUREFIRE_JUNIT5_VERSION = "3.2.5"


def _uses_junit5(pr: PullRequest) -> bool:
    for line in (pr.test_patch or "").split("\n"):
        if line.startswith("+") and _JUNIT5_MARKER in line:
            return True
    return False


_SKIP_FLAGS = (
    "-Drat.skip=true -Dcheckstyle.skip=true -Dcheckstyle_unix.skip=true "
    "-Djacoco.skip=true -Denforcer.skip=true -Dmaven.javadoc.skip=true "
    "-Dclirr.skip=true -Dversions.skip=true -Dspotbugs.skip=true "
    "-Dlicense.skip=true -Dgpg.skip=true"
)
_SUREFIRE_FLAGS = "-Dsurefire.skipAfterFailureCount=0 -Dsurefire.useFile=false"
_MVN_BASE = (
    f"mvn -B -ntp test -fn "
    f"{_SUREFIRE_FLAGS} -Dmaven.test.skip=false -DfailIfNoTests=false "
    f"{_SKIP_FLAGS}"
)


def _mvn_test_command(pr: PullRequest) -> str:
    parts = [_MVN_BASE]
    if _uses_junit5(pr):
        parts.append(f"-Dmaven-surefire-plugin.version={_SUREFIRE_JUNIT5_VERSION}")
    parts.extend(["$PL_FLAG", "$TEST_FLAG"])
    return " ".join(parts)


def _mvn_warmup_command(pr: PullRequest) -> str:
    version = (
        f" -Dmaven-surefire-plugin.version={_SUREFIRE_JUNIT5_VERSION}"
        if _uses_junit5(pr)
        else ""
    )
    return (
        f"mvn -B -ntp clean test-compile -fn -DskipTests"
        f"{version} {_SKIP_FLAGS} $PL_FLAG"
    )


_SKIP_AFTER_FAILURE_SH = r"""
_n=$(grep -c '<skipAfterFailureCount>' pom.xml || true)
if [ "$_n" != "0" ]; then
    sed -i 's|<skipAfterFailureCount>[0-9]\+</skipAfterFailureCount>|<skipAfterFailureCount>0</skipAfterFailureCount>|g' pom.xml
    echo "build_config: skipAfterFailureCount -> 0 ($_n occurrence(s) in pom.xml)"
else
    echo "build_config: pom.xml pins no skipAfterFailureCount - nothing to relax"
fi
"""

_JUNIT5_ENGINE_SH = r"""
python3 - <<'MSWB_ENGINE'
import re

DEP = "\n".join([
    "",
    "        <dependency>",
    "            <groupId>org.junit.jupiter</groupId>",
    "            <artifactId>junit-jupiter-engine</artifactId>",
    "            <version>{engine}</version>",
    "            <scope>test</scope>",
    "        </dependency>",
    "        <dependency>",
    "            <groupId>org.junit.vintage</groupId>",
    "            <artifactId>junit-vintage-engine</artifactId>",
    "            <version>{engine}</version>",
    "            <scope>test</scope>",
    "        </dependency>",
])

for pom in "{poms}".split():
    try:
        text = open(pom).read()
    except OSError:
        print("build_config: %s not present at this commit - skipped" % pom)
        continue
    if "junit-jupiter-engine" in text:
        print("build_config: %s already declares the engine" % pom)
        continue
    m = re.search(r"^[ \t]*<dependencies>[ \t]*$", text, re.MULTILINE)
    if not m:
        print("build_config: %s has no project-level <dependencies> - skipped" % pom)
        continue
    open(pom, "w").write(text[: m.end()] + DEP + text[m.end():])
    print("build_config: junit-jupiter-engine {engine} added to %s" % pom)
MSWB_ENGINE
for _art in org.junit.jupiter:junit-jupiter-engine org.junit.vintage:junit-vintage-engine; do
    mvn -B -ntp dependency:get -Dartifact=$_art:{engine} > /dev/null 2>&1 \
        && echo "build_config: $_art:{engine} warmed into the local repo" \
        || echo "build_config: could not pre-fetch $_art:{engine}"
done
mvn -B -ntp org.apache.maven.plugins:maven-surefire-plugin:{surefire}:help > /dev/null 2>&1 \
    && echo "build_config: surefire {surefire} warmed into the local repo" \
    || echo "build_config: could not pre-fetch surefire {surefire}"
"""


def _build_config_steps(pr: PullRequest) -> str:
    steps = [_SKIP_AFTER_FAILURE_SH]
    if _uses_junit5(pr):
        modules = [m for m in _build_pl_flag(pr).split(",") if m]
        poms = " ".join(f"{m}/pom.xml" for m in modules) or "pom.xml"
        steps.append(
            _JUNIT5_ENGINE_SH.format(
                engine=_JUPITER_ENGINE_VERSION,
                surefire=_SUREFIRE_JUNIT5_VERSION,
                poms=poms,
            )
        )
    return "".join(steps)


_M2_HEAD = [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"',
    '          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"',
    '          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.0.0'
    ' https://maven.apache.org/xsd/settings-1.0.0.xsd">',
    "</settings>",
]
_M2_PROXY = [
    "<proxies>", "    <proxy>", "        <id>example-proxy</id>",
    "        <active>true</active>", "        <protocol>http</protocol>",
    "        <host>{host}</host>", "        <port>{port}</port>",
    "        <username></username>", "        <password></password>",
    "        <nonProxyHosts></nonProxyHosts>", "    </proxy>", "</proxies>",
    "</settings>",
]
_PROXY_ENV_RE = re.compile(r"^ENV\s*(http[s]?_proxy)=http[s]?://([^:]+):(\d+)")
_M2 = "~/.m2/settings.xml"


def _m2_proxy_setup(host: str, port: str) -> str:
    head = [
        f"        echo '{x}' {'>' if i == 0 else '>>'} {_M2}"
        for i, x in enumerate(_M2_HEAD)
    ]
    body = (
        ["RUN mkdir -p ~/.m2", f"    if [ ! -f {_M2} ]; then"]
        + head
        + ["    fi", f"    sed -i '$d' {_M2}"]
        + [f"    echo '{x.format(host=host, port=port)}' >> {_M2}" for x in _M2_PROXY]
    )
    last, head_end = len(body) - 1, len(head) + 1

    def sep(i: int) -> str:
        if i == last:
            return ""
        if i == 1:
            return " \\"
        return "; \\" if i == head_end else " && \\"

    return "\n" + "\n".join(l + sep(i) for i, l in enumerate(body)) + "\n"


_M2_PROXY_CLEANUP = "\nRUN sed -i '/<proxies>/,/<\\/proxies>/d' ~/.m2/settings.xml\n"


_PL_RESOLVER_SH = """\
export PL_FLAG=""
if [ -n "{modules}" ]; then
    _present=""
    IFS=',' read -ra _mods <<< "{modules}"
    for _m in "${{_mods[@]}}"; do
        if [ -d "$_m" ]; then
            _present="${{_present:+$_present,}}$_m"
        else
            echo "module '$_m' does not exist at this commit - dropping it from -pl"
        fi
    done
    if [ -n "$_present" ]; then
        PL_FLAG="-pl $_present -am"
    else
        echo "none of the selected modules exist here - running the full reactor"
    fi
fi
"""

_ACT_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export JAVA_HOME=$(dirname $(dirname $(readlink -f $(which javac))))
export PATH="$JAVA_HOME/bin:$PATH"
export MAVEN_OPTS="-Xmx4g -XX:+UseParallelGC"

cd /home/{repo}

. /home/prepare.sh

{pl_resolver}
{patch_step}{test_resolver}
{mvn_cmd} 2>&1 | tee /tmp/mvn.log

if grep -qE '^\[ERROR\] Could not find the selected project in the reactor' /tmp/mvn.log; then
    echo "reactor_guard: -pl named a module absent from the reactor; -fn masked it" >&2
    exit 1
fi

mswb_emit_results {repo}
"""

_PATCH_STEP = """mswb_apply_patch {patches}
mswb_compile_guard "{skip_flags}"
. /tmp/pl_flag.env
"""

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

_PREPARE_SH = """#!/bin/bash
set -e

mswb_apply_patch() {{
  EXCLUDES=(--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.bmp' --exclude='*.odg' --exclude='*.swp' --exclude='*.class')
  for patch in "$@"; do
      if git apply --whitespace=nowarn "${{EXCLUDES[@]}}" "$patch" 2>/dev/null; then
          echo "apply_patch: $patch applied cleanly"
      else
          echo "apply_patch: $patch needs --3way"
          git apply --3way --whitespace=nowarn "${{EXCLUDES[@]}}" "$patch"
          echo "apply_patch: $patch applied via --3way"
      fi
  done
  if grep -rqE '^<<<<<<< ' --include='*.java' .; then
      echo "apply_patch: CONFLICT MARKERS present after apply" >&2
      exit 1
  fi
}}

mswb_compile_guard() {{
  (
    set -uo pipefail
    MVN_SKIPS="$1"

    echo "export PL_FLAG='$PL_FLAG'" > /tmp/pl_flag.env

    _attempt=1
    _max=8
    while [ "$_attempt" -le "$_max" ]; do
        if mvn -B -ntp test-compile -DskipTests $MVN_SKIPS $PL_FLAG > /tmp/cg.log 2>&1; then
            [ "$_attempt" = "1" ] \
                && echo "compile_guard: test sources compiled cleanly" \
                || echo "compile_guard: compiles after $((_attempt - 1)) pass(es)"
            break
        fi

        grep -oE '^\\[ERROR\\] /home/[^ :]+\\.java' /tmp/cg.log \
            | sed 's|^\\[ERROR\\] ||' | sort -u > /tmp/cg_all.txt
        grep '/src/test/'  /tmp/cg_all.txt > /tmp/cg_test.txt || true
        grep -v '/src/test/' /tmp/cg_all.txt > /tmp/cg_main.txt || true

        _n=0
        while read -r f; do
            if [ -f "$f" ]; then
                mv "$f" "$f.uncompilable"
                echo "compile_guard:   EXCLUDED $f"
                _n=$((_n + 1))
            fi
        done < /tmp/cg_test.txt

        if [ "$_n" = "0" ]; then
            _bad_mods=$(sed -E 's|^/home/[^/]+/([^/]+)/.*|\\1|' /tmp/cg_main.txt | sort -u)
            if [ -n "$_bad_mods" ] && [ -n "$PL_FLAG" ]; then
                _keep=""
                for _m in $(echo "$PL_FLAG" | sed 's/-pl //; s/ -am//' | tr ',' ' '); do
                    echo "$_bad_mods" | grep -qx "$_m" \
                        && echo "compile_guard:   DROPPED module '$_m' (its main sources need the fix)" \
                        || _keep="${{_keep:+$_keep,}}$_m"
                done
                if [ -n "$_keep" ]; then
                    export PL_FLAG="-pl $_keep -am"
                    echo "export PL_FLAG='$PL_FLAG'" > /tmp/pl_flag.env
                    echo "compile_guard: retrying with $PL_FLAG"
                    _attempt=$((_attempt + 1))
                    continue
                fi
            fi
            echo "compile_guard: nothing further can be set aside - the acts will report what they can"
            break
        fi
        echo "compile_guard: pass $_attempt set aside $_n test file(s); recompiling"
        _attempt=$((_attempt + 1))
    done
  )
}}

mswb_emit_results() {{
python3 - "/home/$1" <<'MSWB_EMIT'
import os, sys, xml.etree.ElementTree as ET

root = sys.argv[1] if len(sys.argv) > 1 else "."
for dirpath, _dirs, files in os.walk(root):
    if os.path.basename(dirpath) != "surefire-reports":
        continue
    module = os.path.basename(os.path.dirname(os.path.dirname(dirpath))) or "."
    for fn in files:
        if not (fn.startswith("TEST-") and fn.endswith(".xml")):
            continue
        try:
            tree = ET.parse(os.path.join(dirpath, fn))
        except Exception:
            continue
        for tc in tree.getroot().iter("testcase"):
            cls = tc.get("classname") or ""
            name = tc.get("name") or ""
            if not cls or not name:
                continue
            if tc.find("failure") is not None or tc.find("error") is not None:
                status = "FAILED"
            elif tc.find("skipped") is not None:
                status = "SKIPPED"
            else:
                status = "PASSED"
            print("surefire:%s/%s#%s %s" % (module, cls, name, status))
MSWB_EMIT
}}

[ "${{BASH_SOURCE[0]}}" = "$0" ] || return 0

apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

apt-get update && apt-get install -y --no-install-recommends \\
    openjdk-8-jdk \\
    && apt-get install -y --no-install-recommends \\
    curl maven python3 \\
    && rm -rf /var/lib/apt/lists/*

export JAVA_HOME=$(dirname $(dirname $(readlink -f $(which javac))))
export PATH="$JAVA_HOME/bin:$PATH"
export MAVEN_OPTS="-Xmx4g -XX:+UseParallelGC"

cd /home/{repo}
bash /home/check_git_changes.sh

test "$(git rev-parse HEAD)" = "{sha}"

git reset --hard
git checkout {sha}
{build_config}
{pl_resolver}
{mvn_warmup} || true
"""

class _Img(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config


class Rocketmq9153To9694ImageBase(_Img):
    def dependency(self) -> str | Image:
        return "buildpack-deps:jammy-scm"

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

{self.global_env}

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class Rocketmq9153To9694ImageDefault(_Img):
    def dependency(self) -> Image:
        return Rocketmq9153To9694ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _act(self, name: str, *patches: str) -> File:
        repo = self.pr.repo
        step = (
            _PATCH_STEP.format(
                repo=repo, patches=" ".join(patches), skip_flags=_SKIP_FLAGS
            )
            if patches
            else ""
        )
        return File(
            ".",
            name,
            _ACT_SH.format(
                repo=repo,
                pl_resolver=_PL_RESOLVER_SH.format(modules=_build_pl_flag(self.pr)),
                patch_step=step,
                test_resolver=_build_test_resolver(self.pr),
                mvn_cmd=_mvn_test_command(self.pr),
            ),
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(
                ".",
                "prepare.sh",
                _PREPARE_SH.format(
                    repo=self.pr.repo,
                    sha=self.pr.base.sha,
                    build_config=_build_config_steps(self.pr),
                    pl_resolver=_PL_RESOLVER_SH.format(modules=_build_pl_flag(self.pr)),
                    mvn_warmup=_mvn_warmup_command(self.pr),
                ),
            ),
            self._act("run.sh"),
            self._act("test-run.sh", "/home/test.patch"),
            self._act("fix-run.sh", "/home/test.patch", "/home/fix.patch"),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"
        copy_commands = copy_commands.rstrip("\n")

        base_commit_arg = f'ARG BASE_COMMIT="{self.pr.base.sha}"'
        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        proxy_setup = ""
        proxy_cleanup = ""
        if self.global_env:
            for line in self.global_env.splitlines():
                match = _PROXY_ENV_RE.match(line)
                if match:
                    proxy_setup = _m2_proxy_setup(match.group(2), match.group(3))
                    proxy_cleanup = _M2_PROXY_CLEANUP
                    break

        return f"""FROM {name}:{tag}

{base_commit_arg}

{self.global_env}

{proxy_setup}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}

RUN bash /home/prepare.sh

{hardening}

{proxy_cleanup}

{self.clear_env}
"""


_ANSI_RE = re.compile(r"\x1B\[[0-9;?]*[a-zA-Z]")
_LINE_RE = re.compile(
    r"^(?P<name>\S+(?::|(?=.* > )).*?) (?P<status>PASSED|FAILED|SKIPPED)$"
)


@Instance.register("apache", "rocketmq_9153_to_9694")
@Instance.register("apache", "9153_to_9694")
class ROCKETMQ_9153_TO_9694(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Rocketmq9153To9694ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        found: dict[str, set[str]] = {"PASSED": set(), "FAILED": set(), "SKIPPED": set()}
        for line in _ANSI_RE.sub("", test_log).replace("\r", "").split("\n"):
            m = _LINE_RE.match(line.rstrip())
            if m:
                found[m.group("status")].add(m.group("name").strip())

        passed, failed, skipped = found["PASSED"], found["FAILED"], found["SKIPPED"]
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
