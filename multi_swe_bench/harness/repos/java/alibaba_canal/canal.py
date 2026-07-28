import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _sanitize_patch(patch: str) -> str:
    """Drop binary-file diff sections that abort `git apply` wholesale.

    canal test fixtures ship binary blobs (parse/.../binlog/**/mysql-bin.* dumps,
    jars, images). A `diff --git` section whose body is "Binary files ... differ"
    or "GIT binary patch" makes `git apply` fail the ENTIRE patch ("cannot apply
    binary patch ... without full index line"), so the textual hunks never land
    and fix-dependent tests silently fail to compile. Strip only those sections;
    keep every textual hunk. Extension-agnostic.
    """
    if not patch:
        return patch
    lines = patch.split("\n")
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        if lines[i].startswith("diff --git"):
            j = i + 1
            while j < n and not lines[j].startswith("diff --git"):
                j += 1
            section = lines[i:j]
            is_binary = any(
                s.startswith("Binary files") or s.startswith("GIT binary patch")
                for s in section
            )
            if not is_binary:
                out.extend(section)
            i = j
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


class CanalImageBase(Image):
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
        return "ubuntu:20.04"

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

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"

{self.global_env}

ENV JAVA_TOOL_OPTIONS="-Dfile.encoding=UTF-8 -Duser.timezone=Asia/Shanghai"
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV TZ=UTC

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/

RUN apt update && apt install -y gnupg ca-certificates git curl maven
RUN curl -s https://repos.azul.com/azul-repo.key | gpg --dearmor -o /usr/share/keyrings/azul.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/azul.gpg] https://repos.azul.com/zulu/deb stable main" | tee /etc/apt/sources.list.d/zulu.list
RUN apt update && apt install -y zulu8-jdk

# Install ZooKeeper 3.5.6 (required by canal tests)
ADD https://archive.apache.org/dist/zookeeper/zookeeper-3.5.6/apache-zookeeper-3.5.6-bin.tar.gz /tmp/zk.tar.gz
RUN tar -xzf /tmp/zk.tar.gz -C /opt/ \
    && mv /opt/apache-zookeeper-3.5.6-bin /opt/zookeeper \
    && rm /tmp/zk.tar.gz
RUN mkdir -p /opt/zookeeper/data /opt/zookeeper/logs
RUN echo "tickTime=2000" > /opt/zookeeper/conf/zoo.cfg \
    && echo "dataDir=/opt/zookeeper/data" >> /opt/zookeeper/conf/zoo.cfg \
    && echo "clientPort=2188" >> /opt/zookeeper/conf/zoo.cfg \
    && echo "maxClientCnxns=60" >> /opt/zookeeper/conf/zoo.cfg
ENV ZK_HOME=/opt/zookeeper

{code}

WORKDIR /home/{self.pr.repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class CanalImageDefault(Image):
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
        return CanalImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                _sanitize_patch(self.pr.fix_patch),
            ),
            File(
                ".",
                "test.patch",
                _sanitize_patch(self.pr.test_patch),
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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
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

# Configure Maven mirror for faster downloads
if [ ! -f ~/.m2/settings.xml ]; then
    mkdir -p ~/.m2 && cat <<EOFXML > ~/.m2/settings.xml
<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"
          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.0.0 https://maven.apache.org/xsd/settings-1.0.0.xsd">

    <mirrors>
        <mirror>
            <id>aliyunmaven</id>
            <mirrorOf>central</mirrorOf>
            <name>Aliyun Maven Mirror</name>
            <url>https://maven.aliyun.com/repository/public</url>
        </mirror>
    </mirrors>

</settings>
EOFXML
else
  grep -q "<mirror>" ~/.m2/settings.xml || sed -i '/<\\/settings>/i \\
  <mirrors> \\
      <mirror> \\
          <id>aliyunmaven</id> \\
          <mirrorOf>central</mirrorOf> \\
          <name>Aliyun Maven Mirror</name> \\
          <url>https://maven.aliyun.com/repository/public</url> \\
      </mirror> \\
  </mirrors>' ~/.m2/settings.xml
fi

mvn -V --no-transfer-progress clean package -DskipTests || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

# Start ZooKeeper
/opt/zookeeper/bin/zkServer.sh start

cd /home/{pr.repo}
mvn -V --no-transfer-progress --fail-never clean test -Dsurefire.timeout=300 -Dsurefire.useFile=false -Dmaven.test.skip=false -DfailIfNoTests=false -Dmaven.test.failure.ignore=true

# Stop ZooKeeper
/opt/zookeeper/bin/zkServer.sh stop

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

# Start ZooKeeper
/opt/zookeeper/bin/zkServer.sh start

cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude='*.jar' --exclude='*.png' --exclude='*.PNG' --exclude='*.gif' --exclude='*.ico' --exclude='*.ttf' --exclude='*.woff' --exclude='mysql-bin.*' --exclude='*/node' /home/test.patch || git apply --whitespace=nowarn --3way /home/test.patch || git apply --whitespace=nowarn --reject /home/test.patch || echo "git apply (test) failed (continuing)"
mvn -V --no-transfer-progress --fail-never clean test -Dsurefire.timeout=300 -Dsurefire.useFile=false -Dmaven.test.skip=false -DfailIfNoTests=false -Dmaven.test.failure.ignore=true

# Stop ZooKeeper
/opt/zookeeper/bin/zkServer.sh stop

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

# Start ZooKeeper
/opt/zookeeper/bin/zkServer.sh start

cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude='*.jar' --exclude='*.png' --exclude='*.PNG' --exclude='*.gif' --exclude='*.ico' --exclude='*.ttf' --exclude='*.woff' --exclude='mysql-bin.*' --exclude='*/node' /home/test.patch /home/fix.patch || git apply --whitespace=nowarn --3way /home/test.patch /home/fix.patch || git apply --whitespace=nowarn --reject /home/test.patch /home/fix.patch || echo "git apply (fix) failed (continuing)"
mvn -V --no-transfer-progress --fail-never clean test -Dsurefire.timeout=300 -Dsurefire.useFile=false -Dmaven.test.skip=false -DfailIfNoTests=false -Dmaven.test.failure.ignore=true

# Stop ZooKeeper
/opt/zookeeper/bin/zkServer.sh stop

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
        # Bake the canonical git-leak hardening with the LITERAL base.sha (PIPELINE §4).
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


@Instance.register("alibaba", "canal")
class Canal(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CanalImageDefault(self.pr, self._config)

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
        # Strip ANSI escape sequences
        ansi_escape_pattern = re.compile(r"\x1B\[[0-?9;]*[mK]")
        test_log = ansi_escape_pattern.sub("", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # canal's old surefire prints "Running <Class>" then a class-summary
        # "Tests run: ... Time elapsed: N sec" with NO "in <Class>" suffix; newer
        # eras use "Tests run: ... -- in <Class>". Handle both: pair each
        # "Running <Class>" with its next class-summary line, preferring an
        # explicit "-- in <Class>" when present. Reactor/module summaries (no
        # class name) are skipped so they don't inflate counts.
        # Match "Running <fqcn>" anywhere on the line: newer canal eras prefix
        # maven output with "[INFO] "/"[WARNING] ", so an anchored ^Running misses
        # them. Require a dotted FQCN so we don't match "Running <goal>" noise.
        running = re.compile(r"Running ([\w$]+(?:\.[\w$]+)+)")
        result = re.compile(
            r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)"
            r"(?:.*?--\s*in\s+([\w.$]+))?"
        )
        current = None
        for line in test_log.splitlines():
            line = line.strip()
            m = running.search(line)
            if m:
                current = m.group(1)
                continue
            m = result.search(line)
            if not m:
                continue
            tests_run = int(m.group(1))
            failures = int(m.group(2))
            errors = int(m.group(3))
            skipped = int(m.group(4))
            test_name = m.group(5) or current
            if test_name is None:
                continue  # reactor/module summary line with no class
            current = None

            if failures > 0 or errors > 0:
                failed_tests.add(test_name)
            elif tests_run > 0 and skipped == tests_run:
                skipped_tests.add(test_name)
            elif tests_run > 0:
                passed_tests.add(test_name)

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
# Single-era repo: every bundle key routes to the one Canal class. Data-derived from
# alibaba__canal_lht_final.jsonl — regenerate if the bundles change.
_BUNDLE_NIS_CANAL = [
    "248-250-256-259-282-283-291-293-319-331-333-334-343-374-393-424",
    "447-455-473-487-497-521-522-528-529-532-536-555-618-621-622-623-682-695-698-700-709-734-737-747-754-759-762-768-777-780-782-788-790-806-807-810-813-814-816-825-828-833-839-840-841-844-845-853-854-855",
    "861-865-873-876-881-892-894-896-900-902-919-927-931-938-941-942-947-949-953-958-969-970-973-981-990-1002-1006-1014-1028-1031-1037-1039-1042-1045-1050-1054-1055-1056-1057-1059",
    "1065-1069-1079-1084-1086-1090-1098-1099-1103-1104-1106-1108-1112-1116-1118-1120-1126-1127-1128-1131-1132-1133-1140-1141-1142-1143-1147-1149-1151-1153-1154-1155-1169-1171-1173-1193-1194",
    "1202-1205-1208-1218-1228-1232-1263-1266-1272-1278-1289-1299-1300-1305-1308-1310-1326-1340-1345-1349-1355-1357-1362-1383-1391-1407-1412-1413-1418-1419-1440-1442-1449-1456-1464-1475-1482-1484-1496-1507-1523-1526-1533-1540-1542-1544-1550-1560-1567-1581-1590-1597-1605-1607-1613-1619-1620-1622-1639-1641-1658-1661",
    "1669-1670-1671-1678-1685-1696-1712-1736-1738-1743-1758-1776-1788-1794-1808-1810-1814-1824-1827-1848-1849-1857-1866-1889-1895-1914-1932-1936-1937-1959-1967-1973-1981-1998-2019-2034-2039-2046-2053-2074-2090-2093-2104-2106-2107-2108-2121-2122-2123",
    "2137-2156-2157-2167-2181-2188-2190-2202-2235-2242-2246-2249-2264-2274-2294-2318-2329-2358-2396-2479-2508-2518-2521-2526-2554-2562-2595-2597-2663-2694-2748-2749-2763-2801-2836-2918-2919-2939-3025-3074-3077-3080-3104-3121-3127-3132-3146-3182-3184-3220-3223-3228-3236-3276-3300-3398-3432-3438-3450-3452-3459-3461",
    "3229-3290-3326-3480-3532-3550-3587-3593-3670-3684-3693-3708-3712-3754-3772-3791-3840-3851-3855-3864-3866-3871-3880-3887-3923-3928-3964-3968-3969-3976-3984-3985-4017-4055-4060-4103-4122-4132-4160-4161-4183",
    "4927-4934-4942-4943-4957-4965-5018-5044-5045-5050-5053-5060-5097-5106-5118-5130-5147-5149-5173-5175-5191-5202-5208-5231-5240-5255-5270-5281-5285-5290-5299-5352",
]
for _ni in _BUNDLE_NIS_CANAL:
    Instance.register("alibaba", _ni)(Canal)
