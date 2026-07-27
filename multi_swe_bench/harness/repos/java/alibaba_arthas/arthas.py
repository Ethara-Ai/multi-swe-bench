import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# alibaba/arthas — a Java JVM diagnostic tool, built with Maven (multi-module).
#
# BASE COUNT: exactly ONE. Measured compiler targets across the 35 base.shas:
# 28 at `1.6`, 7 at `1.8` -- JDK 8 builds BOTH (it is the last JDK that still
# accepts `-source 1.6`; JDK 11/17 reject it). So a single JDK-8 base
# (maven:3.9.6-eclipse-temurin-8) serves the whole #48..#3140 range; no era split.
#
#  - Surefire 3.2.2 is supplied by Maven regardless of the pom, so the
#    test-output format is uniform across all eras:
#       Tests run: N, Failures: F, Errors: E, Skipped: S, Time elapsed: T s \
#         [<<< FAILURE!] -- in <FullyQualifiedTestClass>
#  - `arthas-vmtool` compiles C++ via native-maven-plugin -> base needs g++
#    (build-essential). `-fae` keeps the reactor going past any module that
#    fails to build so unrelated modules' tests are still measured.
#
# Known non-resolving outlier -- PR #2642 (1 of 35): its test is a Spring Boot 3
# maven-invoker integration test needing JDK 17 at runtime (Spring Boot 3
# mandates Java 17+), incompatible with the JDK-8 base the oldest PRs require.
# A dedicated JDK-17 era would serve only this one record; deliberately not done.
# All other 34 PRs are covered by the single base.


def _sanitize_patch(patch: str) -> str:
    """Strip binary diff sections git apply cannot take.

    arthas patches carry native libs (*.so/*.dll/*.dylib), images, fonts, jars
    and H2 db files (*.mv.db) as binary hunks emitted WITHOUT a full index line.
    A fixed EXCLUDES extension list is brittle (it missed .mv.db, and --reject
    then leaves the JAVA code only partially applied -> `cannot find symbol`).
    Stripping by the binary MARKER instead is extension-agnostic and lets every
    Java code hunk land cleanly. None of these binaries affect test compilation.
    """
    if not patch:
        return patch
    kept = []
    for sec in re.split(r"(?m)(?=^diff --git )", patch):
        if not sec:
            continue
        if "Binary files " in sec or "GIT binary patch" in sec:
            continue
        kept.append(sec)
    return "".join(kept)


class ArthasImageBase(Image):
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
        # Maven 3.9.6 + JDK 8 (multi-arch amd64+arm64). JDK 8 required -- later
        # JDKs drop `-source 1.6` support the oldest PRs need.
        return "maven:3.9.6-eclipse-temurin-8"

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

        repo = self.pr.repo
        org = self.pr.org

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        # `# syntax` = the sanctioned enhancer opt-out (PIPELINE §2): without it
        # the enhancer injects `git checkout ${BASE_COMMIT}` + the destructive
        # prune into this SHARED base, pinning it to whichever PR built it first
        # and breaking every other record. Strict per-PR hardening is applied in
        # ArthasImageDefault instead.
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    JAVA_TOOL_OPTIONS="-Dfile.encoding=UTF-8 -Duser.timezone=Asia/Shanghai" \\
    MAVEN_OPTS="-Xmx2g" \\
    MAVEN_ARGS="-Dmaven.wagon.http.pool=false -Dhttp.keepAlive=false -Dmaven.wagon.httpconnectionManager.ttlSeconds=120 -Dmaven.wagon.http.retryHandler.count=5"

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

WORKDIR /home/

# git for checkout; build-essential (g++) for the arthas-vmtool native build.
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git build-essential curl ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

{code}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class ArthasImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return ArthasImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", _sanitize_patch(self.pr.fix_patch)),
            File(".", "test.patch", _sanitize_patch(self.pr.test_patch)),
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
git config --global --add safe.directory /home/{pr.repo}
git config core.autocrlf input
git config core.filemode false
echo ".gitattributes" >> .git/info/exclude
echo "*.zip binary" >> .gitattributes
echo "*.png binary" >> .gitattributes
echo "*.jpg binary" >> .gitattributes
git add .
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Warm the ~/.m2 cache: compile main+test sources at base.sha (downloads every
# dependency the test phase needs). `clean test` later only recompiles.
mvn -V -B --no-transfer-progress -fae clean test-compile || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
mvn -V -B --no-transfer-progress -fae clean test \\
    -Dsurefire.useFile=false -DfailIfNoTests=false -Dmaven.test.failure.ignore=true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
EXCLUDES="--exclude=*.jar --exclude=*.png --exclude=*.PNG --exclude=*.gif \\
--exclude=*.ico --exclude=*.ttf --exclude=*.woff --exclude=*.woff2 \\
--exclude=*.jpg --exclude=*.jpeg --exclude=*.zip --exclude=*.so --exclude=*.dylib"
git apply --whitespace=nowarn $EXCLUDES /home/test.patch \\
  || git apply --whitespace=nowarn --3way $EXCLUDES /home/test.patch \\
  || git apply --whitespace=nowarn --reject $EXCLUDES /home/test.patch \\
  || echo "git apply test.patch failed (continuing)"
mvn -V -B --no-transfer-progress -fae clean test \\
    -Dsurefire.useFile=false -DfailIfNoTests=false -Dmaven.test.failure.ignore=true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
EXCLUDES="--exclude=*.jar --exclude=*.png --exclude=*.PNG --exclude=*.gif \\
--exclude=*.ico --exclude=*.ttf --exclude=*.woff --exclude=*.woff2 \\
--exclude=*.jpg --exclude=*.jpeg --exclude=*.zip --exclude=*.so --exclude=*.dylib"
git apply --whitespace=nowarn $EXCLUDES /home/test.patch /home/fix.patch \\
  || git apply --whitespace=nowarn --3way $EXCLUDES /home/test.patch /home/fix.patch \\
  || git apply --whitespace=nowarn --reject $EXCLUDES /home/test.patch /home/fix.patch \\
  || echo "git apply test+fix patch failed (continuing)"
mvn -V -B --no-transfer-progress -fae clean test \\
    -Dsurefire.useFile=false -DfailIfNoTests=false -Dmaven.test.failure.ignore=true

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        # Strict per-PR hardening (PIPELINE §2/§4): detach onto the LITERAL
        # base.sha and prune every other ref so the fix cannot be recovered from
        # git history. Applied here because the Image-typed base opts the
        # enhancer out of auto-injecting it.
        hardening = self._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("alibaba", "arthas")
class Arthas(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ArthasImageDefault(self.pr, self._config)

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
        ansi = re.compile(r"\x1B\[[0-?9;]*[mK]")
        clean = ansi.sub("", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Surefire per-class summary (granularity = test class):
        #   Tests run: 1, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.8 s -- in com.Foo
        #   Tests run: 19, Failures: 2, ... Time elapsed: 1.2 s <<< FAILURE! -- in com.Bar
        pattern = re.compile(
            r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), "
            r"Skipped: (\d+), Time elapsed: [\d.]+ .+? in (.+)"
        )

        for line in clean.splitlines():
            m = pattern.search(line)
            if not m:
                continue
            tests_run = int(m.group(1)); failures = int(m.group(2))
            errors = int(m.group(3)); skipped = int(m.group(4))
            name = m.group(5).strip()
            if failures > 0 or errors > 0:
                failed_tests.add(name)
            elif tests_run > 0 and skipped == tests_run:
                skipped_tests.add(name)
            elif tests_run > 0:
                passed_tests.add(name)

        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        failed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === bundle number_interval routing (prs_in_bundle dash-joined, PIPELINE §11b) ===
# One key per bundle (35 keys == 35 instances). Data-derived from
# alibaba__arthas_lht_final.jsonl -- regenerate if bundles change.
_BUNDLE_NIS_ARTHAS = [
    "48-57-64-65-68-88-95-99-103-104-105-106-107-108-109-110-114-116-117-120-121-122-123-124-125-126-129-136-138-139-142-143-145-146-148-149-150-151-152-154-163-172-175-190-191-192-194-209-213-271",
    "331-352-354-356-379-380-382-386-389-407-424-460-470-481-484-485-498-511-525-541-548-581-582-583-584-586-591-606-610-612-623-625-627-629-631-632-635-638-649-666-668-676",
    "712-726-751-757-758-766-775-779-785-793-802-803-810-815-816-818-825-840",
    "853-855",
    "882-885-897-902-905-911-917-927-942",
    "967-976-1009-1014-1036-1071",
    "1097-1203-1253-1260-1263-1274-1279-1280",
    "1099-1117-1119-1122-1123-1127-1129-1149-1150-1152-1153-1157-1160-1166-1176-1177-1182-1188-1192-1204-1206",
    "1128-1285-1306-1308-1312-1313-1314-1315-1316-1317-1318-1319-1320-1321-1322-1323-1326-1328-1329-1330-1331-1347-1352-1355",
    "1216-1235",
    "1259-1533-1540-1543-1551-1557",
    "1292-1335-1336-1337-1338-1339-1340-1343-1359-1365-1367-1370-1371-1374-1375-1376-1417-1418-1419-1420-1421-1423-1426-1428-1431-1433-1434-1435-1436-1439-1440-1443-1445-1447-1451-1453-1454-1455-1456-1458-1460",
    "1327-1332-1334-1341-1342-1344-1354-1357-1360-1364-1368-1369-1372-1373-1377-1378-1379-1380-1381-1382-1383-1384-1385-1387-1389-1401-1403-1406-1408",
    "1413-1414-1415",
    "1472-1481-1489",
    "1501-1511-1517",
    "1604-1608-1623-1628-1633-1642-1645-1652-1660",
    "1662-1666-1667-1679-1705",
    "1713-1724",
    "1744-1746-1758-1764-1779-1784-1785-1786",
    "1822-1824-1826-1834-1835-1840-1852-1869",
    "1909-1913-1921-1970-1974-1975-1976-1977-1979-1980-1981-1982-1984-1985-1986-1987-1988-1989-1990-1991-1992-1993-1994-1996-1997-1998-1999-2000-2017-2028-2034",
    "2168-2182",
    "2187-2209-2218-2220-2224-2227",
    "2233-2236-2237-2238-2242",
    "2286-2290-2292-2293-2294-2298-2302-2304-2308-2316-2318-2325-2328-2329-2331-2334",
    "2344-2347-2353-2371-2385-2388-2392-2397-2399-2401-2404-2405-2406-2411-2412-2422-2432-2441-2443-2455-2468-2478",
    "2642-2775",
    "2956-2995",
    "3000-3002-3003-3005-3009-3017-3025-3026-3030-3041-3050-3057-3063-3070-3072-3073",
    "3079-3080-3084-3087",
    "3089-3092-3094-3096-3097",
    "3100-3102-3106-3112-3114-3115-3116-3117",
    "3121-3122-3123-3125-3127-3128",
    "3140-3150-3151-3155-3156-3157-3158-3159-3164-3171-3173-3177-3178-3179-3181-3182-3185-3188",
]
for _ni in _BUNDLE_NIS_ARTHAS:
    Instance.register("alibaba", _ni)(Arthas)
