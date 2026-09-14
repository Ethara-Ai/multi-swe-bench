import re
import textwrap

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_NON_MODULE_DIRS = frozenset({"docs", "publications"})


_GROUPING_DIRS = frozenset({"extensions", "extensions-core", "extensions-contrib"})


_SUREFIRE_FLAGS = (
    "-fn "
    "-Dsurefire.useFile=false "
    "-DfailIfNoTests=false "
    "-Dsurefire.failIfNoSpecifiedTests=false "


    "-Dmaven.wagon.http.retryHandler.count=5 "
    "-Dmaven.wagon.httpconnectionManager.ttlSeconds=120"
)


_TOOLCHAIN_ENV = 'export MAVEN_OPTS="-Xmx2g"\nexport LC_ALL=C.UTF-8'


_MAVEN_MIRROR_SETUP = """mkdir -p ~/.m2
_MIRROR='<mirrors><mirror><id>gcs-central</id><name>GCS mirror of Maven Central</name><url>https://maven-central.storage-download.googleapis.com/maven2</url><mirrorOf>central</mirrorOf></mirror></mirrors>'
if [ -f ~/.m2/settings.xml ]; then
    sed -i "s#</settings>#${_MIRROR}</settings>#" ~/.m2/settings.xml
else
    {
        echo '<?xml version="1.0" encoding="UTF-8"?>'
        echo '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">'
        echo "${_MIRROR}"
        echo '</settings>'
    } > ~/.m2/settings.xml
fi"""


def _retry_maven(cmd: str) -> str:

    lines = [
        "for attempt in 1 2 3 4 5; do",
        f"    if {cmd}; then",
        "        break",
        "    fi",
        '    echo "maven warm-up attempt $attempt failed; sleeping 60s before retry"',
        "    sleep 60",
        "done || true",
    ]
    return "\n".join(lines)


def _deps_gate() -> str:

    return (
        "mvn -o -B -q -N validate\n"
        'test -n "$(find . -type d -name test-classes -print -quit)"\n'
        'echo "DEPS_OK"'
    )


def _patch_paths(patch_text: str) -> list[str]:

    paths: list[str] = []
    for line in patch_text.split("\n"):
        if not line.startswith("diff --git "):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        path = parts[2]
        path = path.removeprefix("a/")
        paths.append(path)
    return paths


def _modules_from_patch(patch_text: str) -> set[str]:

    modules: set[str] = set()
    for path in _patch_paths(patch_text):
        segments = path.split("/")

        if len(segments) < 2:
            continue
        top = segments[0]
        if top.startswith(".") or top in _NON_MODULE_DIRS:
            continue
        if top in _GROUPING_DIRS:


            if len(segments) >= 3:
                modules.add(f"{top}/{segments[1]}")
            continue
        modules.add(top)
    return modules


def _build_pl_flag(pr: PullRequest) -> str:

    modules = _modules_from_patch(pr.fix_patch) | _modules_from_patch(pr.test_patch)
    if not modules:
        return ""
    return "-pl " + ",".join(sorted(modules)) + " -am"


def _test_classes_from_patch(patch_text: str) -> set[str]:

    classes: set[str] = set()
    for path in _patch_paths(patch_text):
        if "/src/test/" not in path or not path.endswith(".java"):
            continue
        classes.add(path.rsplit("/", 1)[-1][: -len(".java")])
    return classes


def _companion_test_classes(patch_text: str) -> set[str]:

    classes: set[str] = set()
    for path in _patch_paths(patch_text):
        if "/src/main/" not in path or not path.endswith(".java"):
            continue
        classes.add(path.rsplit("/", 1)[-1][: -len(".java")] + "Test")
    return classes


def _fallback_test_dirs(patch_text: str) -> set[str]:

    dirs: set[str] = set()
    for path in _patch_paths(patch_text):
        if "/src/main/java/" not in path or not path.endswith(".java"):
            continue
        dirs.add(path.split("/src/main/java/", 1)[1].rsplit("/", 1)[0])
    return dirs


def _selector_resolution_block(pr: PullRequest) -> str:

    always = sorted(_test_classes_from_patch(pr.test_patch))
    candidates = sorted(_companion_test_classes(pr.fix_patch) - set(always))
    dirs = sorted(_fallback_test_dirs(pr.fix_patch))
    if not always and not candidates and not dirs:
        return """echo "MVN_TEST_SELECTOR=" > /home/mvn_test_selector
. /home/mvn_test_selector"""

    return f"""_ALWAYS="{' '.join(always)}"
_CANDIDATES="{' '.join(candidates)}"
_FALLBACK_DIRS="{' '.join(dirs)}"
_sel=""
_present=0
# test.patch's own classes are ALWAYS selected. They are usually absent at the
# base commit -- that is the whole point, test.patch creates them -- so an
# existence check must never drop them or the f2p/n2p signal disappears.
for _c in $_ALWAYS; do
    _sel="$_sel,$_c*"
    if [ -n "$(find . -path "*/src/test/java/*/$_c.java" -print -quit 2>/dev/null)" ]; then
        _present=$((_present+1))
    fi
done
# companion classes are only useful if they actually exist at the base commit.
for _c in $_CANDIDATES; do
    if [ -n "$(find . -path "*/src/test/java/*/$_c.java" -print -quit 2>/dev/null)" ]; then
        _sel="$_sel,$_c*"
        _present=$((_present+1))
    fi
done
# Nothing selected exists yet => run.sh would score 0 and the instance would have
# no p2p baseline. Widen to the package(s) the fix touches.
if [ "$_present" -eq 0 ]; then
    echo "selector: no selected test class exists at this commit; falling back to package scope"
    for _d in $_FALLBACK_DIRS; do
        for _f in $(find . -path "*/src/test/java/$_d/*Test.java" 2>/dev/null); do
            _sel="$_sel,$(basename "$_f" .java)*"
        done
    done
fi
_sel="${{_sel#,}}"
if [ -n "$_sel" ]; then
    echo "MVN_TEST_SELECTOR=-Dtest=$_sel" > /home/mvn_test_selector
else
    echo "MVN_TEST_SELECTOR=" > /home/mvn_test_selector
fi
cat /home/mvn_test_selector
. /home/mvn_test_selector"""


def _build_test_flag(pr: PullRequest) -> str:

    classes = _test_classes_from_patch(pr.test_patch) | _companion_test_classes(
        pr.fix_patch
    )
    if not classes:
        return ""
    return "-Dtest=" + ",".join(f"{c}*" for c in sorted(classes))


def _mvn_scope(pr: PullRequest) -> str:

    return _build_pl_flag(pr)


def _mvn_prepare_cmd(pr: PullRequest) -> str:

    return " ".join(
        x for x in (f"mvn clean test {_SUREFIRE_FLAGS}", _mvn_scope(pr)) if x
    )


def _mvn_graded_cmd(pr: PullRequest) -> str:

    return " ".join(x for x in (f"mvn test {_SUREFIRE_FLAGS}", _mvn_scope(pr)) if x)


_PURGE_REPORTS = (
    "find . -type d -name surefire-reports -prune -exec rm -rf {} + 2>/dev/null || true"
)
_DUMP_REPORTS = (
    "find . -path '*/target/surefire-reports/TEST-*.xml' -exec cat {} \\; "
    "2>/dev/null || true"
)


class DruidOssParent2ImageBase(Image):
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


        return "maven:3.8.6-openjdk-8"

    def image_tag(self) -> str:


        return "base-6107-to-3312"

    def workdir(self) -> str:
        return "base-6107-to-3312"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:

        base_img = self.dependency()
        if isinstance(base_img, Image):
            base_img = base_img.image_full_name()

        repo = self.pr.repo
        infra = DockerfileEnhancer._infrastructure_block(self, base_img)

        body = f"""{self.global_env}

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""

        return "\n".join(
            [
                DockerfileEnhancer.SYNTAX_DIRECTIVE,
                "",
                f"FROM {base_img}",
                "",
                infra,
                body,
            ]
        )


class DruidOssParent2ImageDefault(Image):
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
        return DruidOssParent2ImageBase(self.pr, self._config)

    def image_tag(self) -> str:


        return f"pr-{self.pr.number}"

    def workdir(self) -> str:


        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        base_sha = self.pr.base.sha
        prepare_cmd = _mvn_prepare_cmd(self.pr)
        graded_cmd = _mvn_graded_cmd(self.pr)

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
                f"""#!/bin/bash
set -e
export CI=true
{_TOOLCHAIN_ENV}

{_MAVEN_MIRROR_SETUP}

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout --detach {base_sha}
bash /home/check_git_changes.sh

{_selector_resolution_block(self.pr)}

{_retry_maven(prepare_cmd)}

{_deps_gate()}
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true
{_TOOLCHAIN_ENV}

cd /home/{repo}
. /home/mvn_test_selector
{_PURGE_REPORTS}
{graded_cmd}
{_DUMP_REPORTS}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true
{_TOOLCHAIN_ENV}

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
. /home/mvn_test_selector
{_PURGE_REPORTS}
{graded_cmd}
{_DUMP_REPORTS}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true
{_TOOLCHAIN_ENV}

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
. /home/mvn_test_selector
{_PURGE_REPORTS}
{graded_cmd}
{_DUMP_REPORTS}
""",
            ),
        ]

    def dockerfile(self) -> str:

        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        proxy_setup = ""
        proxy_cleanup = ""

        if self.global_env:
            proxy_host = None
            proxy_port = None

            for line in self.global_env.splitlines():


                match = re.match(
                    r'^ENV\s*(http[s]?_proxy)="?http[s]?://([^:"]+):(\d+)', line
                )
                if match:
                    proxy_host = match.group(2)
                    proxy_port = match.group(3)
                    break
            if proxy_host and proxy_port:
                proxy_setup = textwrap.dedent(
                    f"""
                RUN mkdir -p ~/.m2 && \\
                    if [ ! -f ~/.m2/settings.xml ]; then \\
                        echo '<?xml version="1.0" encoding="UTF-8"?>' > ~/.m2/settings.xml && \\
                        echo '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"' >> ~/.m2/settings.xml && \\
                        echo '          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"' >> ~/.m2/settings.xml && \\
                        echo '          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.0.0 https://maven.apache.org/xsd/settings-1.0.0.xsd">' >> ~/.m2/settings.xml && \\
                        echo '</settings>' >> ~/.m2/settings.xml; \\
                    fi && \\
                    sed -i '$d' ~/.m2/settings.xml && \\
                    echo '<proxies>' >> ~/.m2/settings.xml && \\
                    echo '    <proxy>' >> ~/.m2/settings.xml && \\
                    echo '        <id>example-proxy</id>' >> ~/.m2/settings.xml && \\
                    echo '        <active>true</active>' >> ~/.m2/settings.xml && \\
                    echo '        <protocol>http</protocol>' >> ~/.m2/settings.xml && \\
                    echo '        <host>{proxy_host}</host>' >> ~/.m2/settings.xml && \\
                    echo '        <port>{proxy_port}</port>' >> ~/.m2/settings.xml && \\
                    echo '        <username></username>' >> ~/.m2/settings.xml && \\
                    echo '        <password></password>' >> ~/.m2/settings.xml && \\
                    echo '        <nonProxyHosts></nonProxyHosts>' >> ~/.m2/settings.xml && \\
                    echo '    </proxy>' >> ~/.m2/settings.xml && \\
                    echo '</proxies>' >> ~/.m2/settings.xml && \\
                    echo '</settings>' >> ~/.m2/settings.xml
                """
                )

                proxy_cleanup = textwrap.dedent(
                    """
                    RUN sed -i '/<proxies>/,/<\\/proxies>/d' ~/.m2/settings.xml
                """
                )


        blocks = [
            f"FROM {name}:{tag}",
            f'ARG BASE_COMMIT="{self.pr.base.sha}"',
            self.global_env,
            proxy_setup.strip(),
            copy_commands.strip(),
            "RUN bash /home/prepare.sh",
            "RUN git reset --hard\nRUN git checkout ${BASE_COMMIT}",
            Image._HARDENING_BLOCK.rstrip("\n"),
            proxy_cleanup.strip(),
            self.clear_env,
        ]
        return "\n\n".join(b for b in blocks if b) + "\n"


@Instance.register("apache", "druid")
@Instance.register("apache", "druid_3284_to_2285")
class DRUID_3284_TO_2285(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return DruidOssParent2ImageDefault(self.pr, self._config)

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


        clean_log = re.sub(r"\x1B\[[0-9;?]*[a-zA-Z]", "", test_log)


        re_case = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.DOTALL)
        re_attr = re.compile(r'(\w+)="([^"]*)"')


        re_captured_output = re.compile(
            r"<system-(out|err)\b.*?</system-\1>|<system-(out|err)\b[^>]*/>", re.DOTALL
        )
        saw_xml = False
        for m in re_case.finditer(clean_log):
            attrs = dict(re_attr.findall(m.group(1)))
            cls = attrs.get("classname", "")
            meth = attrs.get("name", "")
            if not meth:
                continue
            saw_xml = True


            name = f"{cls}#{meth}" if cls else meth
            body = re_captured_output.sub("", m.group(3) or "")
            if "<failure" in body or "<error" in body:
                failed_tests.add(name)
            elif "<skipped" in body:
                skipped_tests.add(name)
            else:
                passed_tests.add(name)

        if not saw_xml:


            re_pass_tests = [
                re.compile(
                    r"Running\s+(.+?)\s*\n(?:(?!.*Tests run:)(?!.*Running\s)(?!.*Results:).*\n)*.*?Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)"
                )
            ]
            re_fail_tests = [
                re.compile(
                    r"Running\s+(.+?)\s*\n(?:(?!.*Tests run:)(?!.*Running\s)(?!.*Results:).*\n)*.*?Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+).*<<<\s*FAILURE!"
                )
            ]

            for re_fail_test in re_fail_tests:
                for m in re_fail_test.finditer(clean_log):
                    failed_tests.add(m.group(1))

            for re_pass_test in re_pass_tests:
                for m in re_pass_test.finditer(clean_log):
                    test_name = m.group(1)
                    if test_name in failed_tests:
                        continue
                    tests_run = int(m.group(2))
                    failures = int(m.group(3))
                    errors = int(m.group(4))
                    skipped = int(m.group(5))
                    if failures > 0 or errors > 0:
                        failed_tests.add(test_name)
                    elif tests_run > 0 and skipped == tests_run:
                        skipped_tests.add(test_name)
                    elif tests_run > 0:
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
