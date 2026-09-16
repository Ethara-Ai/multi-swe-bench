import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NON_PROJECT_DIRS = {
    "gradle",
    "buildSrc",
    "ci",
    "src",
    "framework-docs",
    "framework-platform",
    ".github",
    ".idea",
}

_DIFF_PATH = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.MULTILINE)


def _gradle_test_tasks(pr: PullRequest) -> list[str]:
    projects: set[str] = set()
    for patch in (pr.fix_patch or "", pr.test_patch or ""):
        for old, new in _DIFF_PATH.findall(patch):
            for path in (old, new):
                head = path.split("/", 1)[0]
                if "/" not in path or head in _NON_PROJECT_DIRS or "." in head:
                    continue
                projects.add(head)

    if not projects:
        return ["test"]
    return [f":{name}:test" for name in sorted(projects)]


_TEST_LOGGING_INIT = """\
def msbExcluded = []
if (gradle.startParameter.projectProperties.containsKey("msbExcludeTests")) {
    msbExcluded = gradle.startParameter.projectProperties.get("msbExcludeTests").split(",").findAll { it }
}

gradle.allprojects { proj ->
    if (msbExcluded) {
        proj.tasks.matching { it.name.startsWith("compileTest") }.all { ct ->
            ct.exclude { fte -> msbExcluded.contains(fte.file.absolutePath) }
        }
    }

    proj.tasks.withType(org.gradle.api.tasks.testing.Test) { t ->
        t.ignoreFailures = true

        t.outputs.upToDateWhen { false }

        def pathCache = [:]

        t.afterTest { desc, result ->
            def cls = desc.className ?: "unknown"

            def path = pathCache.get(cls)
            if (path == null) {
                path = ""
                def top = cls.indexOf((int) 36) >= 0 ? cls.substring(0, cls.indexOf((int) 36)) : cls
                def rel = top.replace(".", "/")
                def roots = []
                try { roots.addAll(proj.sourceSets.test.allSource.srcDirs) } catch (Throwable ignored) { }
                ["java", "kotlin", "groovy"].each { d ->
                    roots.add(new File(proj.projectDir, "src/test/" + d))
                }
                try {
                    roots.each { dir ->
                        if (path) return
                        ["java", "kotlin", "groovy"].each { ext ->
                            if (path) return
                            def candidate = new File(dir, rel + "." + ext)
                            if (candidate.isFile()) {
                                path = proj.rootDir.toPath()
                                        .relativize(candidate.toPath())
                                        .toString()
                                        .replace(File.separator, "/")
                            }
                        }
                    }
                } catch (Throwable ignored) {
                }
                if (!path) {
                    path = proj.path
                }
                pathCache.put(cls, path)
            }

            println("MSB-TEST|" + path + "|" + cls + "|" + desc.name + "|" + result.resultType)
        }
    }
}
"""


_REPORT_EXCLUDED = r"""#!/bin/bash
set -eo pipefail

ROOT="$1"
EXCLUDES="$2"
BASELINE="$3"

[ -s "$EXCLUDES" ] || exit 0

if [ ! -d "$BASELINE" ] || [ -z "$(ls -A "$BASELINE" 2>/dev/null)" ]; then
    echo "NOTE: no baseline test inventory; uncompilable tests report NONE, not FAILURE" >&2
    exit 0
fi

while read -r src; do
    [ -n "$src" ] || continue
    rel="${src#$ROOT/}"
    fqcn=$(printf '%s' "$rel" \
        | sed -E 's#^[^/]+/src/test/(java|kotlin|groovy)/##; s#\.(java|kt|groovy)$##; s#/#.#g')

    grep -ho '<testcase name="[^"]*" classname="[^"]*"' "$BASELINE"/*.xml 2>/dev/null \
        | sed -E 's#<testcase name="([^"]*)" classname="([^"]*)"#\2\t\1#' \
        | awk -F'\t' -v c="$fqcn" -v p="$rel" '
            BEGIN { n = 0 }
            ($1 == c) || (substr($1, 1, length(c) + 1) == c "$") {
                print "MSB-TEST|" p "|" $1 "|" $2 "|FAILURE"
                n++
            }
            END {
                if (n == 0) {
                    print "NOTE: no baseline tests recorded for " c > "/dev/stderr"
                }
            }'
done < "$EXCLUDES"
"""


_TEST_BODY = """\
: > /tmp/msb-excludes.txt

for msb_attempt in 1 2 3 4 5; do
    rm -f /tmp/gradle-stage.log

    MSB_EXARGS=()
    if [ -s /tmp/msb-excludes.txt ]; then
        MSB_EXARGS=(-PmsbExcludeTests="$(paste -sd, /tmp/msb-excludes.txt)")
    fi

    set +e
    ./gradlew {tasks} \
        --no-daemon \
        --console=plain \
        --continue \
        --no-build-cache \
        --max-workers 4 \
        -I /home/msb-test-logging.gradle \
        "${{MSB_EXARGS[@]}}" \
        -Dorg.gradle.jvmargs="-Xmx3g -XX:MaxMetaspaceSize=768m" \
        -Dorg.gradle.java.installations.auto-download=false \
        > /tmp/gradle-stage.log 2>&1
    GRADLE_RC=$?
    set -e

    grep -oE '/home/{repo}/[^:]*/src/test/[^:]*\\.(java|kt|groovy):[0-9]+: error' \
        /tmp/gradle-stage.log 2>/dev/null \
        | sed 's/:[0-9]*: error$//' | sort -u > /tmp/msb-broken.txt || true

    MSB_NEW=$(comm -13 /tmp/msb-excludes.txt /tmp/msb-broken.txt)
    if [ -z "$MSB_NEW" ]; then
        break
    fi

    printf '%s\\n' "$MSB_NEW" | while read -r msb_f; do
        [ -n "$msb_f" ] && echo "NOTE: test source will not compile against this stage's tree, excluding: $msb_f"
    done
    printf '%s\\n' "$MSB_NEW" >> /tmp/msb-excludes.txt
    sort -u -o /tmp/msb-excludes.txt /tmp/msb-excludes.txt
done

if [ -s /tmp/msb-excludes.txt ]; then
    echo "NOTE: $(wc -l < /tmp/msb-excludes.txt) test source file(s) did not compile this stage."
fi

bash /home/msb-report-excluded.sh \
    /home/{repo} /tmp/msb-excludes.txt /home/msb-baseline-results \
    >> /tmp/gradle-stage.log

cat /tmp/gradle-stage.log

if ! grep -qE '^(BUILD SUCCESSFUL|BUILD FAILED|FAILURE: Build)' /tmp/gradle-stage.log; then
    echo "Error: gradle produced no build result (exit ${{GRADLE_RC}})" >&2
    exit 1
fi
"""


class SpringFrameworkImageBase(Image):
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
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

ENV JAVA_TOOL_OPTIONS="-Dfile.encoding=UTF-8"
ENV LC_ALL=C.UTF-8
ENV CI=true

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    gnupg ca-certificates git curl \\
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://repos.azul.com/azul-repo.key | gpg --dearmor -o /usr/share/keyrings/azul.gpg \\
    && echo "deb [signed-by=/usr/share/keyrings/azul.gpg] https://repos.azul.com/zulu/deb stable main" > /etc/apt/sources.list.d/zulu.list
RUN apt-get update && apt-get install -y --no-install-recommends \\
    zulu17-jdk zulu21-jdk zulu24-jdk zulu25-jdk \\
    && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/usr/lib/jvm/zulu17
ENV PATH=/usr/lib/jvm/zulu17/bin:$PATH

{code}

{self.clear_env}

"""


class SpringFrameworkImageBaseJDK11(Image):
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
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

ENV JAVA_TOOL_OPTIONS="-Dfile.encoding=UTF-8"
ENV LC_ALL=C.UTF-8
ENV CI=true

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    gnupg ca-certificates git curl \\
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://repos.azul.com/azul-repo.key | gpg --dearmor -o /usr/share/keyrings/azul.gpg \\
    && echo "deb [signed-by=/usr/share/keyrings/azul.gpg] https://repos.azul.com/zulu/deb stable main" > /etc/apt/sources.list.d/zulu.list
RUN apt-get update && apt-get install -y --no-install-recommends \\
    zulu11-jdk \\
    && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/usr/lib/jvm/zulu11
ENV PATH=/usr/lib/jvm/zulu11/bin:$PATH

{code}

{self.clear_env}

"""


class SpringFrameworkImageBaseJDK8(Image):
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
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

ENV JAVA_TOOL_OPTIONS="-Dfile.encoding=UTF-8"
ENV LC_ALL=C.UTF-8
ENV CI=true

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    gnupg ca-certificates git curl \\
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://repos.azul.com/azul-repo.key | gpg --dearmor -o /usr/share/keyrings/azul.gpg \\
    && echo "deb [signed-by=/usr/share/keyrings/azul.gpg] https://repos.azul.com/zulu/deb stable main" > /etc/apt/sources.list.d/zulu.list
RUN apt-get update && apt-get install -y --no-install-recommends \\
    zulu8-jdk \\
    && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/usr/lib/jvm/zulu8
ENV PATH=/usr/lib/jvm/zulu8/bin:$PATH

{code}

{self.clear_env}

"""


class SpringFrameworkImageDefault(Image):
    JDK_8_BRANCHES = {"3.2.x", "4.2.x"}
    JDK_11_BRANCHES = {"5.2.x"}

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def _select_jdk(self) -> str:
        ref = self.pr.base.ref
        if ref in self.JDK_8_BRANCHES:
            return "8"
        if ref in self.JDK_11_BRANCHES:
            return "11"
        if ref == "master" and self.pr.number < 2000:
            return "8"
        if ref == "master" and self.pr.number < 25000:
            return "11"
        return "17"

    def dependency(self) -> Image | None:
        jdk = self._select_jdk()
        if jdk == "8":
            return SpringFrameworkImageBaseJDK8(self.pr, self._config)
        if jdk == "11":
            return SpringFrameworkImageBaseJDK11(self.pr, self._config)
        return SpringFrameworkImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        tasks = " ".join(_gradle_test_tasks(self.pr))

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "msb-test-logging.gradle", _TEST_LOGGING_INIT),
            File(".", "msb-report-excluded.sh", _REPORT_EXCLUDED),
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
export CI=true

cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

sed -i "/io.spring.gradle-enterprise-conventions/d" settings.gradle 2>/dev/null || true
sed -i "/io.spring.gradle-enterprise-conventions/d" build.gradle 2>/dev/null || true
sed -i "/com.gradle.build-scan/d" build.gradle 2>/dev/null || true
sed -i '/io.spring.ge.conventions/d' settings.gradle 2>/dev/null || true
sed -i '/com.gradle.enterprise/d' settings.gradle 2>/dev/null || true

sed -i '/settings.gradle.projectsLoaded/,$d' settings.gradle 2>/dev/null || true
sed -i '/^gradleEnterprise[[:space:]]*{{/,$d' settings.gradle 2>/dev/null || true
sed -i '/^develocity[[:space:]]*{{/,$d' settings.gradle 2>/dev/null || true

if grep -q "gradle-4" gradle/wrapper/gradle-wrapper.properties 2>/dev/null; then
    if [ "$JAVA_HOME" = "/usr/lib/jvm/zulu8" ]; then
        sed -i 's|distributionUrl=.*|distributionUrl=https\\://services.gradle.org/distributions/gradle-4.10.3-bin.zip|' gradle/wrapper/gradle-wrapper.properties
    else
        sed -i 's|distributionUrl=.*|distributionUrl=https\\://services.gradle.org/distributions/gradle-7.6.4-bin.zip|' gradle/wrapper/gradle-wrapper.properties
    fi
elif grep -q -e "gradle-5" -e "gradle-6" gradle/wrapper/gradle-wrapper.properties 2>/dev/null; then
    sed -i 's|distributionUrl=.*|distributionUrl=https\\://services.gradle.org/distributions/gradle-7.6.4-bin.zip|' gradle/wrapper/gradle-wrapper.properties
fi

chmod +x gradlew

if [ -n "$MSB_TARGETARCH" ] && [ -n "$MSB_BUILDARCH" ] && [ "$MSB_TARGETARCH" != "$MSB_BUILDARCH" ]; then
    echo "NOTE: cross-building $MSB_TARGETARCH on $MSB_BUILDARCH -- skipping the Gradle warm-up."
    echo "NOTE: this image ships a cold Gradle cache and no baseline test inventory."
else
    ./gradlew {tasks} \\
        --no-daemon \\
        --console=plain \\
        --continue \\
        --no-build-cache \\
        --max-workers 4 \\
        -I /home/msb-test-logging.gradle \\
        -Dorg.gradle.jvmargs="-Xmx3g -XX:MaxMetaspaceSize=768m" \\
        -Dorg.gradle.java.installations.auto-download=false \\
        > /tmp/gradle-warmup.log 2>&1 || true

    tail -n 30 /tmp/gradle-warmup.log
    grep -qE '^(BUILD SUCCESSFUL|BUILD FAILED|FAILURE: Build)' /tmp/gradle-warmup.log

    rm -rf /home/msb-baseline-results
    mkdir -p /home/msb-baseline-results
    find . -path '*/build/test-results/test/TEST-*.xml' -print0 \
        | xargs -0 -r cp -t /home/msb-baseline-results
    echo "baseline test inventory: $(ls -1 /home/msb-baseline-results | wc -l) class result file(s)"

fi
""".format(pr=self.pr, tasks=tasks),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
""".format(pr=self.pr)
                + _TEST_BODY.format(tasks=tasks, repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn \\
        --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' \\
        --exclude='*.gif' --exclude='*.ico' --exclude='*.bmp' --exclude='*.class' \\
        /home/test.patch; then
    echo "Error: git apply failed for test.patch" >&2
    exit 1
fi
""".format(pr=self.pr)
                + _TEST_BODY.format(tasks=tasks, repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn \\
        --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' \\
        --exclude='*.gif' --exclude='*.ico' --exclude='*.bmp' --exclude='*.class' \\
        /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed for test.patch/fix.patch" >&2
    exit 1
fi
""".format(pr=self.pr)
                + _TEST_BODY.format(tasks=tasks, repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        arch_args = (
            "ARG TARGETARCH\n"
            "ARG BUILDARCH\n"
            "ENV MSB_TARGETARCH=${TARGETARCH}\n"
            "ENV MSB_BUILDARCH=${BUILDARCH}"
        )

        prepare_commands = "RUN bash /home/prepare.sh"

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
                proxy_setup = f"""RUN mkdir -p /root/.gradle && \\
    printf '%s\\n' \\
        'systemProp.http.proxyHost={proxy_host}' \\
        'systemProp.http.proxyPort={proxy_port}' \\
        'systemProp.https.proxyHost={proxy_host}' \\
        'systemProp.https.proxyPort={proxy_port}' \\
        >> /root/.gradle/gradle.properties"""

                proxy_cleanup = "RUN rm -f /root/.gradle/gradle.properties"

        return f"""FROM {name}:{tag}

{self.global_env}

{proxy_setup}

{arch_args}

{copy_commands}

{prepare_commands}

{proxy_cleanup}

{self.clear_env}

"""


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_RESULT_LINE = re.compile(
    r"^MSB-TEST\|([^|]*)\|([^|]*)\|(.*)\|(SUCCESS|FAILURE|SKIPPED)\s*$"
)


def parse_gradle_marker_log(log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    clean = ANSI_ESCAPE.sub("", log)

    for line in clean.splitlines():
        m = _RESULT_LINE.match(line.rstrip())
        if not m:
            continue

        path, cls, method, result = m.groups()
        name = f"{path} > {cls} > {method}"

        if result == "SUCCESS":
            passed_tests.add(name)
        elif result == "FAILURE":
            failed_tests.add(name)
        else:
            skipped_tests.add(name)

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


@Instance.register("spring-projects", "spring-framework")
class SpringFramework(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return SpringFrameworkImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        return parse_gradle_marker_log(log)
