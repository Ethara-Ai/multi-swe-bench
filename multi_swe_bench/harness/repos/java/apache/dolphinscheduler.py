import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_REPO_DIR = "/home/dolphinscheduler"

_MVN_FLAGS = (
    "-B -ntp "
    "-Dmaven.test.failure.ignore=true "
    "-DfailIfNoTests=false "
    "-Dsurefire.failIfNoSpecifiedTests=false "
    "-Dcheckstyle.skip=true "
    "-Drat.skip=true "
    "-Dspotbugs.skip=true "
    "-Dmaven.javadoc.skip=true "
    "-Dlicense.skip=true"
)

_MVN_TIMEOUT = "timeout -k 60 1800"
_MVN_TIMEOUT_PREPARE = "timeout -k 60 3600"

_SERVICES_START = """\
PGVER="$(ls /etc/postgresql 2>/dev/null | sort -V | tail -1)"
if [ -n "$PGVER" ]; then
    pg_ctlcluster "$PGVER" main start >/dev/null 2>&1 || true
    for _i in $(seq 1 60); do
        pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1 && break
        sleep 1
    done
fi

if [ -x /opt/zookeeper/bin/zkServer.sh ]; then
    mkdir -p /var/log/zookeeper
    ZOO_LOG_DIR=/var/log/zookeeper /opt/zookeeper/bin/zkServer.sh start >/dev/null 2>&1 || true
    for _i in $(seq 1 60); do
        (exec 3<>/dev/tcp/127.0.0.1/2181) >/dev/null 2>&1 && break
        sleep 1
    done
fi
"""

_DERIVE_TARGETS = """\
CLASSES="$(grep -E '^diff --git a/.*/src/test/java/.*Test\\.java b/' /home/test.patch \\
    | sed -E 's#^.* b/##; s#^.*/##; s#\\.java$##' | sort -u | paste -sd, -)"
MODULES="$(grep -E '^diff --git a/.*/src/test/java/' /home/test.patch \\
    | sed -E 's#^.* b/##; s#/src/test/java/.*##' | sort -u)"

# A module whose pom.xml is not on disk is not in the reactor. That is the normal
# state for PRs 4063 and 4165 before the fix lands -- the fix is what creates
# dolphinscheduler-alert-script / dolphinscheduler-alert-http and registers them
# with their parent. Passing such a module to -pl makes Maven abort the whole
# build; dropping it just yields no tests, which is the honest "this test did not
# exist yet" signal the classifier wants.
SEL=""
for _m in $MODULES; do
    if [ -f "$_m/pom.xml" ]; then SEL="$SEL,$_m"; fi
done
SEL="${SEL#,}"
"""

_ENABLE_SUREFIRE = r"""awk '
  /<artifactId>maven-surefire-plugin<\/artifactId>/ { in_sf = 1 }
  in_sf && /<\/plugin>/ { in_sf = 0 }
  in_sf { sub(/<skip>true<\/skip>/, "<skip>false</skip>") }
  { print }
' pom.xml > /tmp/pom.xml.new && mv /tmp/pom.xml.new pom.xml
"""

# Run the selected classes and print the JUnit XML, which is the only surefire
# output that names individual test METHODS. Console scraping would collapse
# every class down to one id.
_RUN_TARGETS = r"""if [ -z "$SEL" ] || [ -z "$CLASSES" ]; then
    echo "NO_TEST_MODULES_AT_THIS_STAGE"
    exit 0
fi

# Wipe reports left behind by prepare.sh, so this log can only ever contain
# results this stage actually produced.
find . -type d -name surefire-reports -prune -exec rm -rf {} + 2>/dev/null || true

for _attempt in 1 2 3 4 5; do
    if _out="$(@@MVN_TIMEOUT@@ mvn @@MVN_FLAGS@@ test-compile -pl "$SEL" -am 2>&1)"; then
        break
    fi
    _bad="$(printf '%s\n' "$_out" \
        | sed -nE 's#^\[ERROR\] (/home/@@REPO@@/[^:]*/src/test/java/[^:]*\.java):\[[0-9]+,[0-9]+\].*#\1#p' \
        | sort -u)"
    if [ -z "$_bad" ]; then break; fi
    echo "DROPPED_UNCOMPILABLE_TEST_SOURCES (attempt $_attempt):"
    printf '%s\n' "$_bad" | sed 's/^/  /'
    printf '%s\n' "$_bad" | xargs -r rm -f
done

@@MVN_TIMEOUT@@ mvn @@MVN_FLAGS@@ test -pl "$SEL" -am -Dtest="$CLASSES" || true

find . -path '*/surefire-reports/TEST-*.xml' -exec cat {} + 2>/dev/null || true
"""

_APPLY_TEST_PATCH = """\
git apply --whitespace=nowarn /home/test.patch \\
    || git apply --whitespace=nowarn --reject /home/test.patch \\
    || true
find . -name '*.rej' -delete 2>/dev/null || true
"""

_APPLY_FIX_PATCH = """\
git apply --whitespace=nowarn /home/fix.patch \\
    || git apply --whitespace=nowarn --reject /home/fix.patch \\
    || true
find . -name '*.rej' -delete 2>/dev/null || true
"""

# The integrity guard prepare.sh calls around every checkout (rule 8).
_CHECK_GIT_CHANGES = """\
#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""


def _script(body: str, pr: PullRequest) -> str:
    """Fill the @@...@@ placeholders.

    Placeholders rather than str.format() or an f-string on purpose: these bodies
    are full of ${VAR}, $(...) and find's {} , every one of which would otherwise
    have to be brace-doubled.
    """
    return (
        body.replace("@@REPO@@", pr.repo)
        .replace("@@REPO_URL@@", "https://github.com/%s/%s.git" % (pr.org, pr.repo))
        .replace("@@SHA@@", pr.base.sha)
        .replace("@@MVN_FLAGS@@", _MVN_FLAGS)
        .replace("@@MVN_TIMEOUT_PREPARE@@", _MVN_TIMEOUT_PREPARE)
        .replace("@@MVN_TIMEOUT@@", _MVN_TIMEOUT)
    )


def parse_surefire_xml(test_log: str) -> TestResult:
    """Parse the surefire JUnit XML echoed by the stage scripts.

    Ids are "<fqcn>#<method>". The '#' separator is deliberate: report.py's
    _file_hosts_test() splits on it to recover the class name and compare it
    against the test file's basename, which is what keeps the cheating guard
    accurate. A dotted "<fqcn>.<method>" id would defeat that check.
    """
    clean = re.sub(r"\x1B\[[0-?9;]*[mK]", "", test_log)

    passed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()

    testcase_re = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.DOTALL)
    name_re = re.compile(r'\bname="([^"]*)"')
    classname_re = re.compile(r'\bclassname="([^"]*)"')

    for m in testcase_re.finditer(clean):
        attrs = m.group(1)
        nm = name_re.search(attrs)
        cn = classname_re.search(attrs)
        if not nm or not cn:
            continue
        test_id = "%s#%s" % (cn.group(1), nm.group(1))
        closing = m.group(2)
        inner = m.group(3) or ""
        if closing == "/>":
            passed.add(test_id)
        elif "<failure" in inner or "<error" in inner:
            failed.add(test_id)
        elif "<skipped" in inner:
            skipped.add(test_id)
        else:
            passed.add(test_id)

    # A rerun can emit the same id twice. Failure wins over pass, and pass wins
    # over skip, so one id never lands in two buckets.
    failed -= passed
    skipped -= passed
    skipped -= failed

    return TestResult(
        passed_count=len(passed),
        failed_count=len(failed),
        skipped_count=len(skipped),
        passed_tests=passed,
        failed_tests=failed,
        skipped_tests=skipped,
    )


class DolphinSchedulerImageBase(Image):
    """Shared era base. Owns the toolchain, the clone, the pin to BASE_COMMIT and
    the FULL history scrub (rule 8)."""

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
        return "ubuntu:22.04"

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
            code = 'RUN git clone "${REPO_URL}" /home/%s' % self.pr.repo
        else:
            code = "COPY %s /home/%s" % (self.pr.repo, self.pr.repo)

        label = (
            'LABEL org.opencontainers.image.title="%s/%s" \\\n'
            '      org.opencontainers.image.description="%s/%s Docker image" \\\n'
            '      org.opencontainers.image.source="https://github.com/%s/%s" \\\n'
            '      org.opencontainers.image.authors="https://www.ethara.ai/"'
        ) % (
            self.pr.org, self.pr.repo,
            self.pr.org, self.pr.repo,
            self.pr.org, self.pr.repo,
        )

        # Maven from the Apache archive, NOT from apt.
        #
        # `apt-get install maven` pulls default-jre-headless ->
        # openjdk-11-jre-headless, whose postinst writes into /usr/share/binfmts/.
        # That directory ships with binfmt-support, a Recommends which
        # --no-install-recommends drops, so the install dies:
        #     update-alternatives: error: error creating symbolic link
        #     '/usr/share/binfmts/jar.dpkg-tmp': No such file or directory
        # The tarball keeps JDK 8 the only JVM in the image and pins Maven to
        # 3.6.3, which is what DolphinScheduler CI ran in this era.
        maven_install = (
            "RUN set -eux; \\\n"
            "    curl -fsSL -o /tmp/maven.tar.gz \\\n"
            "        https://archive.apache.org/dist/maven/maven-3/3.6.3/binaries/apache-maven-3.6.3-bin.tar.gz; \\\n"
            '    echo "c35a1803a6e70a126e80b2b3ae33eed961f83ed74d18fcd16909b2d44d7dada3203f1ffe726c17ef8dcca2dcaa9fca676987befeadc9b9f759967a8cb77181c0  /tmp/maven.tar.gz" \\\n'
            "        | sha512sum -c -; \\\n"
            "    tar -xzf /tmp/maven.tar.gz -C /opt; \\\n"
            "    ln -s /opt/apache-maven-3.6.3/bin/mvn /usr/local/bin/mvn; \\\n"
            "    rm -f /tmp/maven.tar.gz; \\\n"
            "    mvn -v"
        )

        # ZooKeeper 3.4.14 -- the exact version the pom pins
        # (<zookeeper.version>3.4.14</zookeeper.version>, curator 4.3.0).
        #
        # Needed because PR 4267's test patch turns TaskInstanceControllerTest into
        # `extends AbstractControllerTest`, which is @SpringBootTest on
        # ApiApplicationServer and therefore starts the registry client. Absent
        # ZooKeeper does not fail that test, it HANGS it: curator retries
        # localhost:2181 forever.
        zookeeper_install = (
            "RUN set -eux; \\\n"
            "    curl -fsSL -o /tmp/zk.tar.gz \\\n"
            "        https://archive.apache.org/dist/zookeeper/zookeeper-3.4.14/zookeeper-3.4.14.tar.gz; \\\n"
            '    echo "b2e03d95f8cf18b97a46e2f53871cef5a5da9d5d80b97009375aed7fb35368c440ca944c7e8b64efabbc065f6fb98bb86239f7c1491f0490efc71876d5a7f424  /tmp/zk.tar.gz" \\\n'
            "        | sha512sum -c -; \\\n"
            "    tar -xzf /tmp/zk.tar.gz -C /opt; \\\n"
            "    mv /opt/zookeeper-3.4.14 /opt/zookeeper; \\\n"
            "    mkdir -p /var/lib/zookeeper /var/log/zookeeper; \\\n"
            "    printf '%s\\n' 'tickTime=2000' 'initLimit=10' 'syncLimit=5' \\\n"
            "        'dataDir=/var/lib/zookeeper' 'clientPort=2181' 'maxClientCnxns=0' \\\n"
            "        > /opt/zookeeper/conf/zoo.cfg; \\\n"
            "    rm -f /tmp/zk.tar.gz"
        )

        # The pom pins the PostgreSQL JDBC driver at 42.1.4 (2017). SCRAM-SHA-256
        # support only arrived in pgjdbc 42.2.0, so against PG14's default the
        # dolphinscheduler-dao tests cannot authenticate at all. Switching the
        # cluster to md5 is what makes that driver work; PG14 still supports it.
        # fsync/full_page_writes go off because this database is a throwaway.
        postgres_setup = (
            "RUN set -eux; \\\n"
            '    PGVER="$(ls /etc/postgresql | sort -V | tail -1)"; \\\n'
            "    sed -i 's/scram-sha-256/md5/g' /etc/postgresql/$PGVER/main/pg_hba.conf; \\\n"
            "    printf '%s\\n' \"password_encryption = md5\" \"fsync = off\" \"full_page_writes = off\" \\\n"
            "        >> /etc/postgresql/$PGVER/main/postgresql.conf"
        )

        sections = [
            "# syntax=docker/dockerfile:1.6",
            "FROM %s" % image_name,
            # BASE_COMMIT pins this shared base. Which commit that is comes from
            # the FIRST row of the routed dataset, which run_pipeline.sh sorts
            # descending (rule 7) so it is the newest. Any PR whose commit this
            # prune removes fetches it back in prepare.sh.
            (
                "ARG TARGETARCH\n"
                'ARG REPO_URL="https://github.com/%s/%s.git"\n'
                "ARG BASE_COMMIT"
            ) % (self.pr.org, self.pr.repo),
            # Proxy and CA plumbing. DockerfileEnhancer normally injects this, but
            # enhance() returns a file untouched when the "# syntax=" directive is
            # present -- and that directive is here on purpose. Defaults are empty
            # so no proxy host is ever baked into the image.
            (
                'ARG http_proxy=""\n'
                'ARG https_proxy=""\n'
                'ARG HTTP_PROXY=""\n'
                'ARG HTTPS_PROXY=""\n'
                'ARG no_proxy="localhost,127.0.0.1,::1"\n'
                'ARG NO_PROXY="localhost,127.0.0.1,::1"\n'
                'ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"'
            ),
            # TZ is Asia/Shanghai, not UTC, and that is a TEST REQUIREMENT.
            # TimePlaceholderUtilsTest hardcodes CST expectations:
            #   expected:<Sun Jan 01 01:01:01 CST 2017=yyyy>
            #   but was: <Sun Jan 01 01:01:01 UTC 2017=yyyy>
            # It never caught this upstream because the surefire include the fix
            # patch adds, **/common/utils/TimePlaceholderUtilsTest.java, does not
            # match the file's real path under common/utils/placeholder/ -- so the
            # class has never run in DolphinScheduler CI. Verified in the built
            # image: under Asia/Shanghai all 8 tests pass with zero <failure>.
            "ENV DEBIAN_FRONTEND=noninteractive \\\n"
            "    LANG=C.UTF-8 \\\n"
            "    LC_ALL=C.UTF-8 \\\n"
            "    TZ=Asia/Shanghai \\\n"
            "    http_proxy=${http_proxy} \\\n"
            "    https_proxy=${https_proxy} \\\n"
            "    HTTP_PROXY=${HTTP_PROXY} \\\n"
            "    HTTPS_PROXY=${HTTPS_PROXY} \\\n"
            "    no_proxy=${no_proxy} \\\n"
            "    NO_PROXY=${NO_PROXY} \\\n"
            "    SSL_CERT_FILE=${CA_CERT_PATH} \\\n"
            "    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\\n"
            "    CURL_CA_BUNDLE=${CA_CERT_PATH}",
            label,
            # CA-cert symlink farm. It MUST sit before the first network RUN,
            # because the four network steps below -- apt, the two
            # archive.apache.org curls, and the git clone -- are exactly what needs
            # to trust a proxy-injected CA. Different tools look for the bundle at
            # different canonical paths, so the one real file is linked into all.
            (
                "RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\\n"
                "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\\n"
                "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\\n"
                "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\\n"
                "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\\n"
                "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\\n"
                "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt"
            ),
            "WORKDIR /home/",
            # mkdir /usr/share/binfmts is cheap insurance: any package whose
            # postinst registers a binfmt needs it to exist, and the directory only
            # arrives with the binfmt-support Recommends.
            "RUN mkdir -p /usr/share/binfmts \\\n"
            "    && apt-get update && apt-get install -y --no-install-recommends \\\n"
            "        git ca-certificates curl tzdata openjdk-8-jdk \\\n"
            "        postgresql postgresql-contrib \\\n"
            "    && ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \\\n"
            "    && rm -rf /var/lib/apt/lists/*",
            "RUN ln -s /usr/lib/jvm/java-8-openjdk-$(dpkg --print-architecture) /usr/lib/jvm/java-8-openjdk",
            "ENV JAVA_HOME=/usr/lib/jvm/java-8-openjdk\n"
            "ENV MAVEN_HOME=/opt/apache-maven-3.6.3\n"
            'ENV MAVEN_OPTS="-Xmx3g -XX:+TieredCompilation -XX:TieredStopAtLevel=1"',
            maven_install,
            zookeeper_install,
            postgres_setup,
            code,
            "WORKDIR /home/%s" % self.pr.repo,
            "RUN git reset --hard",
            "RUN git checkout ${BASE_COMMIT}",
            Image._HARDENING_BLOCK.rstrip("\n"),
            'CMD ["/bin/bash"]',
        ]

        blocks = [s for s in sections if s]
        if self.global_env:
            blocks.insert(3, self.global_env)
        if self.clear_env:
            blocks.insert(len(blocks) - 1, self.clear_env)

        return "\n\n".join(blocks) + "\n"


class DolphinSchedulerImageDefault(Image):
    """Per-PR layer. COPY lines and one `RUN bash /home/prepare.sh` (rule 8)."""

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
        return DolphinSchedulerImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return "pr-%d" % self.pr.number

    def workdir(self) -> str:
        return "pr-%d" % self.pr.number

    def files(self) -> list[File]:
        prepare = """#!/bin/bash
set -e

# ---------------------------------------------------------------- services
@@SERVICES_START@@

# ------------------------------------------------------- pin to BASE_COMMIT
#
# The shared base image was pruned (git gc) down to a single BASE_COMMIT's
# ancestry by its own hardening block, so THIS PR's commit may be absent.
# Re-attach the remote and fetch the exact sha before the checkout, so one base
# can serve every PR. That is the mechanism that lets the base carry the full
# scrub instead of splitting it.
#
# For this dataset the base pins to PR 4267 (the highest number, built first --
# rule 7). 4142 and 4111 are ancestors of it and survive the prune, so their
# fetch is a no-op. 4063 and 4165 are on the other chain and genuinely need it.
cd @@REPO_DIR@@
git reset --hard
bash /home/check_git_changes.sh
git remote add origin @@REPO_URL@@ 2>/dev/null || true
git fetch --depth=1 origin @@SHA@@ 2>/dev/null || git fetch origin 2>/dev/null || true
git checkout @@SHA@@
bash /home/check_git_changes.sh

# ------------------------------------------------------------- dependencies
#
# dolphinscheduler-dao's TaskInstanceMapperTest is @RunWith(SpringRunner) +
# @SpringBootTest, so it needs the real database named in
# dolphinscheduler-dao/src/main/resources/datasource.properties:
#   jdbc:postgresql://localhost:5432/dolphinscheduler , user/password test/test
# The schema comes from the repo, so this has to run AFTER the checkout above.
# The DAO tests are @Transactional @Rollback(true), so they never dirty it.
echo "CREATE ROLE test WITH LOGIN SUPERUSER PASSWORD 'test';" > /tmp/ds_role.sql
su postgres -c "psql -v ON_ERROR_STOP=1 -f /tmp/ds_role.sql" || true
su postgres -c "createdb -O test dolphinscheduler" || true
rm -f /tmp/ds_role.sql

if [ -f sql/dolphinscheduler-postgre.sql ]; then
    PGPASSWORD=test psql -h 127.0.0.1 -U test -d dolphinscheduler \\
        -f sql/dolphinscheduler-postgre.sql >/dev/null 2>&1 || true
fi

# --------------------------------------------------------- Maven cache warmup
#
# Warmed against the POST-FIX tree, because the fix patch is what introduces the
# new modules (PRs 4063 and 4165) and their dependencies. Doing it at build time
# means the three stage runs do not each re-download the world.
@@APPLY_TEST_PATCH@@
@@APPLY_FIX_PATCH@@

@@ENABLE_SUREFIRE@@

@@DERIVE_TARGETS@@

if [ -n "$SEL" ] && [ -n "$CLASSES" ]; then
    @@MVN_TIMEOUT_PREPARE@@ mvn @@MVN_FLAGS@@ test -pl "$SEL" -am -Dtest="$CLASSES" || true
fi

# ------------------------------------------------------- back to a clean base
#
# Everything the warmup built has to go. maven-compiler-plugin 3.3's staleness
# check is not reliable enough to trust: leaving post-fix classes in target/
# risks the baseline stage passing on them, which would silently empty f2p.
git reset --hard @@SHA@@
git clean -fd
find . -type d -name target -prune -exec rm -rf {} + 2>/dev/null || true

# Content compare, not just a status read -- a stat-cache hit can make a dirty
# worktree look clean.
git diff --quiet HEAD
bash /home/check_git_changes.sh

ZOO_LOG_DIR=/var/log/zookeeper /opt/zookeeper/bin/zkServer.sh stop || true
pg_ctlcluster "$PGVER" main stop || true
"""

        run = """#!/bin/bash
set -e

@@SERVICES_START@@
cd @@REPO_DIR@@

@@ENABLE_SUREFIRE@@

@@DERIVE_TARGETS@@

@@RUN_TARGETS@@
"""

        test_run = """#!/bin/bash
set -e

@@SERVICES_START@@
cd @@REPO_DIR@@

@@APPLY_TEST_PATCH@@

@@ENABLE_SUREFIRE@@

@@DERIVE_TARGETS@@

@@RUN_TARGETS@@
"""

        fix_run = """#!/bin/bash
set -e

@@SERVICES_START@@
cd @@REPO_DIR@@

@@APPLY_TEST_PATCH@@
@@APPLY_FIX_PATCH@@

@@ENABLE_SUREFIRE@@

@@DERIVE_TARGETS@@

@@RUN_TARGETS@@
"""

        def build(body):
            body = (
                body.replace("@@REPO_DIR@@", _REPO_DIR)
                .replace("@@SERVICES_START@@", _SERVICES_START)
                .replace("@@ENABLE_SUREFIRE@@", _ENABLE_SUREFIRE)
                .replace("@@DERIVE_TARGETS@@", _DERIVE_TARGETS)
                .replace("@@RUN_TARGETS@@", _RUN_TARGETS)
                .replace("@@APPLY_TEST_PATCH@@", _APPLY_TEST_PATCH)
                .replace("@@APPLY_FIX_PATCH@@", _APPLY_FIX_PATCH)
            )
            return _script(body, self.pr)

        return [
            File(".", "fix.patch", "%s" % self.pr.fix_patch),
            File(".", "test.patch", "%s" % self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", build(prepare)),
            File(".", "run.sh", build(run)),
            File(".", "test-run.sh", build(test_run)),
            File(".", "fix-run.sh", build(fix_run)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        # Rule 8: COPY lines only, one per line, then the single prepare.sh RUN.
        # No ARG, no ENV, no WORKDIR (inherited from the base, which ends in
        # /home/<repo>), no git command, no scrub, no CMD (inherited).
        copy_commands = "\n".join("COPY %s /home/" % f.name for f in self.files())

        return "FROM %s:%s\n\n%s\n\nRUN bash /home/prepare.sh\n" % (
            name,
            tag,
            copy_commands,
        )


@Instance.register("apache", "dolphinscheduler")
class DolphinScheduler(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DolphinSchedulerImageDefault(self.pr, self._config)

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
        return parse_surefire_xml(test_log)


# Routing aliases for a dataset row whose number_interval carries the bare PR
# number instead of "".
#
# Not cosmetic, and not our bug. python/langchain_ai_langgraph/langgraph.py
# monkeypatches Dataset.build GLOBALLY with no org/repo guard:
#
#     ni = (ds.number_interval or "").strip() or str(pr.number)
#
# so every row this harness writes - including this Java dataset - comes out
# carrying its own PR number in number_interval. Instance.create() then looks up
# "apache/<number>", which is NOT the key registered above, and evaluation dies:
#
#     ValueError: Instance 'apache/4267' is not registered
#
# Registering the numbers as aliases is the in-tree remedy; see
# golang/go_playground/validator.py, which keeps "go-playground/1110" alive for
# exactly this reason.
#
# Checked before adding: there is no apache/<number> key anywhere in the registry
# (24 apache keys, none numeric), so nothing is shadowed. Instance.register
# OVERWRITES silently rather than raising, so if another apache repo ever claims
# one of these numbers the last import would quietly win - keep this list to the
# five PRs this dataset actually contains.
for _pr_number in ("4063", "4111", "4142", "4165", "4267"):
    Instance.register("apache", _pr_number)(DolphinScheduler)
