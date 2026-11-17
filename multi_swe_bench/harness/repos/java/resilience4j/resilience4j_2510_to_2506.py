import re
import textwrap

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_BASE_TAG = "base-2510_to_2506"

_GRADLE_JVMARGS = (
    'sed -i "/^org.gradle.jvmargs=/d" gradle.properties 2>/dev/null || true\n'
    'echo "org.gradle.jvmargs=-Xmx4g -XX:MaxMetaspaceSize=512m" >> gradle.properties'
)

_GRADLE_WRAPPER_MIRROR = (
    'WRAPPER_PROPS=gradle/wrapper/gradle-wrapper.properties\n'
    'GRADLE_VERSION=$(sed -n "s/^distributionUrl=.*gradle-\\([0-9.]*\\)-\\(bin\\|all\\)\\.zip.*/\\1/p" "$WRAPPER_PROPS")\n'
    'GRADLE_DIST=$(sed -n "s/^distributionUrl=.*gradle-[0-9.]*-\\(bin\\|all\\)\\.zip.*/\\1/p" "$WRAPPER_PROPS")\n'
    'sed -i "s#^distributionUrl=.*#distributionUrl=https\\\\://mirrors.aliyun.com/gradle/distributions/v${GRADLE_VERSION}/gradle-${GRADLE_VERSION}-${GRADLE_DIST}.zip#" "$WRAPPER_PROPS"\n'
    'if grep -q "^networkTimeout=" "$WRAPPER_PROPS"; then\n'
    '    sed -i "s/^networkTimeout=.*/networkTimeout=120000/" "$WRAPPER_PROPS"\n'
    'else\n'
    '    echo "networkTimeout=120000" >> "$WRAPPER_PROPS"\n'
    'fi'
)

_GRADLE_NET_RETRY = (
    'sed -i "/^systemProp.org.gradle.internal.repository./d" gradle.properties 2>/dev/null || true\n'
    'echo "systemProp.org.gradle.internal.repository.max.retries=10" >> gradle.properties\n'
    'echo "systemProp.org.gradle.internal.repository.initial.backoff=2000" >> gradle.properties'
)

_GRADLE_WRAPPER_FETCH = (
    'for i in 1 2 3; do ./gradlew --version && break; [ "$i" = 3 ] && exit 1; sleep 15; done'
)

_GRADLE_DEPS_FETCH = (
    'for i in 1 2 3; do ./gradlew testClasses --no-build-cache --init-script ~/.gradle/init.gradle --max-workers 2 && break; '
    '[ "$i" = 3 ] && exit 1; sleep 30; done'
)

_GRADLE_CMD = "./gradlew clean test --no-build-cache --init-script ~/.gradle/init.gradle --max-workers 2 --continue"

_GRADLE_GATE = "./gradlew --offline testClasses --no-build-cache --init-script ~/.gradle/init.gradle --max-workers 2"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

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


class Resilience4j2510To2506ImageBase(Image):
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
        return "ubuntu:24.04"

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

        return f"""FROM {image_name}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
WORKDIR /home/
RUN apt-get update && apt-get install -y git openjdk-21-jdk python3

{self.clear_env}

CMD ["/bin/bash"]
"""


class Resilience4j2510To2506ImageDefault(Image):
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
        return Resilience4j2510To2506ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_script(self) -> str:
        preamble = textwrap.dedent("""\
            #!/bin/bash
            set -eo pipefail

            export CI=true
            cd /home/{pr.repo}
            git reset --hard
            git clean -fdx
            bash /home/check_git_changes.sh
            git checkout --detach {pr.base.sha}
            bash /home/check_git_changes.sh
            {gradle_jvmargs}
            mkdir -p ~/.gradle && cat <<'INITGRADLE' > ~/.gradle/init.gradle
            allprojects {{
                buildscript {{
                    repositories {{
                        maven {{ url 'https://maven.aliyun.com/repository/public/' }}
                        maven {{ url 'https://maven.aliyun.com/repository/jcenter/' }}
                        maven {{ url 'https://maven.aliyun.com/repository/google/' }}
                        maven {{ url 'https://plugins.gradle.org/m2/' }}
                        mavenCentral()
                    }}
                }}

                repositories {{
                    maven {{ url 'https://maven.aliyun.com/repository/public/' }}
                    maven {{ url 'https://maven.aliyun.com/repository/jcenter/' }}
                    maven {{ url 'https://maven.aliyun.com/repository/google/' }}
                    mavenCentral()
                }}

                tasks.withType(Test) {{
                    testLogging {{
                        events 'passed', 'failed', 'skipped'
                        showStandardStreams = false
                        exceptionFormat 'full'
                    }}
                    outputs.upToDateWhen {{ false }}
                }}
            }}
            INITGRADLE""").format(pr=self.pr, gradle_jvmargs=_GRADLE_JVMARGS)

        return f"{preamble}\n{_GRADLE_NET_RETRY}\n{_GRADLE_WRAPPER_MIRROR}\n{_GRADLE_WRAPPER_FETCH}\n{_GRADLE_DEPS_FETCH}\n{_GRADLE_CMD} || true\n{_GRADLE_GATE}\necho DEPS_OK\n"

    def _run_script(self) -> str:
        return f"""#!/bin/bash
set -eo pipefail

export CI=true
cd /home/{self.pr.repo}
git checkout -- .
{_GRADLE_JVMARGS}
{_GRADLE_NET_RETRY}
{_GRADLE_WRAPPER_MIRROR}
{_GRADLE_CMD}
"""

    def _test_run_script(self) -> str:
        return f"""#!/bin/bash
set -eo pipefail

export CI=true
cd /home/{self.pr.repo}
git checkout -- .
python3 -c "
import re
with open('/home/test.patch') as f: content = f.read()
content = re.sub(r'diff --git [^\\n]*\\n(?:(?:old|new|deleted|similarity|rename|index|copy)[^\\n]*\\n)*Binary files [^\\n]*differ\\n?', '', content)
with open('/tmp/test.patch.clean', 'w') as f: f.write(content)
" 2>/dev/null || cp /home/test.patch /tmp/test.patch.clean
git apply --whitespace=nowarn /tmp/test.patch.clean
{_GRADLE_JVMARGS}
{_GRADLE_NET_RETRY}
{_GRADLE_WRAPPER_MIRROR}
{_GRADLE_CMD}
"""

    def _fix_run_script(self) -> str:
        return f"""#!/bin/bash
set -eo pipefail

export CI=true
cd /home/{self.pr.repo}
git checkout -- .
for PFILE in /home/test.patch /home/fix.patch; do
    CLEAN="/tmp/$(basename $PFILE).clean"
    python3 -c "
import re
with open('$PFILE') as f: content = f.read()
content = re.sub(r'diff --git [^\\\\n]*\\\\n(?:(?:old|new|deleted|similarity|rename|index|copy)[^\\\\n]*\\\\n)*Binary files [^\\\\n]*differ\\\\n?', '', content)
with open('$CLEAN', 'w') as f: f.write(content)
" 2>/dev/null || cp "$PFILE" "$CLEAN"
done
git apply --whitespace=nowarn /tmp/test.patch.clean /tmp/fix.patch.clean
{_GRADLE_JVMARGS}
{_GRADLE_NET_RETRY}
{_GRADLE_WRAPPER_MIRROR}
{_GRADLE_CMD}
"""

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._prepare_script()),
            File(".", "run.sh", self._run_script()),
            File(".", "test-run.sh", self._test_run_script()),
            File(".", "fix-run.sh", self._fix_run_script()),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

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
                    r"^ENV\s*(http[s]?_proxy)=http[s]?://([^:]+):(\d+)", line
                )
                if match:
                    proxy_host = match.group(2)
                    proxy_port = match.group(3)
                    break
            if proxy_host and proxy_port:
                proxy_setup = textwrap.dedent(
                    f"""
                    RUN mkdir -p ~/.gradle && \\
                        if [ ! -f "$HOME/.gradle/gradle.properties" ]; then \\
                            touch "$HOME/.gradle/gradle.properties"; \\
                        fi && \\
                        if ! grep -q "systemProp.http.proxyHost" "$HOME/.gradle/gradle.properties"; then \\
                            echo 'systemProp.http.proxyHost={proxy_host}' >> "$HOME/.gradle/gradle.properties" && \\
                            echo 'systemProp.http.proxyPort={proxy_port}' >> "$HOME/.gradle/gradle.properties" && \\
                            echo 'systemProp.https.proxyHost={proxy_host}' >> "$HOME/.gradle/gradle.properties" && \\
                            echo 'systemProp.https.proxyPort={proxy_port}' >> "$HOME/.gradle/gradle.properties"; \\
                        fi && \\
                        echo 'export GRADLE_USER_HOME=/root/.gradle' >> ~/.bashrc && \\
                        /bin/bash -c "source ~/.bashrc"
                """
                )
                proxy_cleanup = textwrap.dedent(
                    """
                    RUN rm -f ~/.gradle/gradle.properties
                """
                )

        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

{proxy_setup}

WORKDIR /home/
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

{copy_commands}

RUN bash /home/prepare.sh

{proxy_cleanup}
"""
        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail


@Instance.register("resilience4j", "resilience4j_2510_to_2506")
class RESILIENCE4J_2510_TO_2506(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Resilience4j2510To2506ImageDefault(self.pr, self._config)

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

        passed_res = [
            re.compile(r"^(.+ > .+) PASSED$"),
        ]

        failed_res = [
            re.compile(r"^(.+ > .+) FAILED$"),
        ]

        skipped_res = [
            re.compile(r"^(.+ > .+) SKIPPED$"),
        ]

        clean_log = _ANSI_RE.sub("", test_log).replace("\r", "")

        for line in clean_log.splitlines():
            for passed_re in passed_res:
                m = passed_re.match(line)
                if m and m.group(1) not in failed_tests:
                    passed_tests.add(m.group(1))

            for failed_re in failed_res:
                m = failed_re.match(line)
                if m:
                    failed_tests.add(m.group(1))
                    if m.group(1) in passed_tests:
                        passed_tests.remove(m.group(1))

            for skipped_re in skipped_res:
                m = skipped_re.match(line)
                if m:
                    skipped_tests.add(m.group(1))

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
