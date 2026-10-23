import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_BASE_TAG = "base-17821_to_17132"

_MVN_TEST_FLAGS = (
    "-Dcheckstyle.skip=true "
    "-Drat.skip=true "
    "-Dspotbugs.skip=true "
    "-Dlicense.skip=true "
    "-Dspotless.check.skip=true "
    "-Denforcer.skip=true "
    "-Ddependency-check.skip=true "
    "-Djacoco.skip=true "
    "-Dmaven.javadoc.skip=true "
    "-Dmaven.test.failure.ignore=true "
    "-DfailIfNoTests=false "
    "-Dsurefire.failIfNoSpecifiedTests=false "
    "-DreuseForks=false "
    "-DskipUT=false "
    "-Dmaven.wagon.http.retryHandler.count=5 "
    "-Dmaven.wagon.httpconnectionManager.ttlSeconds=120 "
    "-Dmaven.wagon.rto=120000"
)

_BASE_DOCKERFILE = r"""# syntax=docker/dockerfile:1.6

FROM __BASE_IMAGE__

ARG TARGETARCH
ARG REPO_URL="https://github.com/__ORG__/__REPO__.git"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    TZ=UTC \
    CI=true \
    JAVA_HOME=/opt/java/openjdk \
    MAVEN_OPTS="-Xmx4g -XX:MaxMetaspaceSize=512m" \
    http_proxy=${http_proxy} \
    https_proxy=${https_proxy} \
    HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    no_proxy=${no_proxy} \
    NO_PROXY=${NO_PROXY} \
    SSL_CERT_FILE=${CA_CERT_PATH} \
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \
    CURL_CA_BUNDLE=${CA_CERT_PATH}

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \
      org.opencontainers.image.description="__ORG__/__REPO__ Docker image" \
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN --mount=type=secret,id=mitm_ca,required=0 \
    if [ -f /run/secrets/mitm_ca ]; then \
        cp /run/secrets/mitm_ca /usr/local/share/ca-certificates/mitm-ca.crt && update-ca-certificates; \
    fi

WORKDIR /home/

RUN git clone "${REPO_URL}" /home/__REPO__

WORKDIR /home/__REPO__

CMD ["/bin/bash"]
"""

_DEFAULT_DOCKERFILE = r"""FROM __BASE_IMAGE__

__COPY_COMMANDS__
RUN bash /home/prepare.sh

RUN set -eux; \
    cd /home/__REPO__; \
    git remote remove origin 2>/dev/null || true; \
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \
        | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all; \
    git reflog expire --expire-unreachable=now --all; \
    git gc --prune=now --aggressive; \
    git repack -a -d -l --quiet; \
    rm -f .git/objects/info/alternates; \
    git config --local gc.auto 0; \
    git config --local fetch.recurseSubmodules false; \
    git config --local remote.pushDefault ""; \
    test "$(git rev-parse HEAD)" = "__BASE_SHA__"; \
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \
    test -z "$(git remote)"; \
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/__REPO__/.gitmodules ]; then \
        git submodule foreach --recursive ' \
            git checkout --detach HEAD; \
            git remote remove origin 2>/dev/null || true; \
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \
                | xargs -r -n1 git update-ref -d; \
            git reflog expire --expire=now --all; \
            git reflog expire --expire-unreachable=now --all; \
            git gc --prune=now --aggressive; \
            rm -f .git/objects/info/alternates; \
        '; \
    fi
"""

_CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
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

_DERIVE_TARGETS_SH = r"""TEST_PATHS="$(grep '^diff --git a/' /home/test.patch | sed -e 's|^diff --git a/||' -e 's| b/.*$||' || true)"
MODULES="$(printf '%s\n' "$TEST_PATHS" | grep '/src/test/' | sed -e 's|/src/test/.*$||' | sort -u || true)"
JAVA_TEST_PATHS="$(printf '%s\n' "$TEST_PATHS" | grep '/src/test/java/.*\.java$' || true)"
CLASSES=""
for path in $JAVA_TEST_PATHS; do
    if [ -f "$path" ]; then
        CLASSES="${CLASSES}$(basename "$path" .java)
"
    fi
done
SEL=""
for module in $MODULES; do
    case "$module" in
        e2e|e2e/*|*-e2e|*-e2e/*|*-api-test|*-api-test/*) continue ;;
    esac
    [ -f "${module}/pom.xml" ] || continue
    parent="."
    in_reactor=1
    for part in $(printf '%s' "$module" | tr '/' ' '); do
        if ! grep -q "<module>${part}</module>" "${parent}/pom.xml"; then
            in_reactor=0
            break
        fi
        parent="${parent}/${part}"
    done
    if [ "$in_reactor" -eq 1 ]; then
        SEL="${SEL:+${SEL},}${module}"
    fi
done
CLASSES="$(printf '%s' "$CLASSES" | sort -u | paste -sd, -)"
TEST_ARG="-Dtest=${CLASSES:+${CLASSES},}!%regex[.*HttpUtilsTest.*#testGetRequest|testGetValidRequest],!%regex[.*WeChatSenderTest.*#testSendWeChat.*]"
"""

_ENABLE_SUREFIRE_SH = r"""awk '
  /<artifactId>maven-surefire-plugin<\/artifactId>/ { in_sf = 1 }
  in_sf && /<\/plugin>/ { in_sf = 0 }
  in_sf { sub(/<skip>true<\/skip>/, "<skip>false</skip>"); sub(/<skipTests>true<\/skipTests>/, "<skipTests>false</skipTests>") }
  { print }
' pom.xml > /tmp/pom.xml.new && cat /tmp/pom.xml.new > pom.xml && rm -f /tmp/pom.xml.new
if ! grep -rqs --include='*.java' 'org\.junit\.jupiter' --exclude-dir=target . \
   && ! grep -qE 'surefire-junit4|junit-vintage' pom.xml; then
    perl -0pi -e 's#(<artifactId>maven-surefire-plugin</artifactId>\s*<version>([^<]*)</version>)#$1<dependencies><dependency><groupId>org.apache.maven.surefire</groupId><artifactId>surefire-junit4</artifactId><version>$2</version></dependency></dependencies>#' pom.xml
fi
"""



_PREPARE_SH = r"""#!/bin/bash
set -eo pipefail

cd /home/__REPO__

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git cat-file -e "__BASE_SHA__^{commit}" 2>/dev/null || git fetch origin "__BASE_SHA__"
git checkout --detach "__BASE_SHA__"
bash /home/check_git_changes.sh

export CI=true
export MAVEN_OPTS="-Xmx4g -XX:MaxMetaspaceSize=512m"

mkdir -p /root/.m2
printf '%s\n' \
    '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"' \
    '          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"' \
    '          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.0.0 https://maven.apache.org/xsd/settings-1.0.0.xsd">' \
    '</settings>' > /root/.m2/settings.xml

proxy_url="${http_proxy:-${https_proxy:-}}"
if [ -n "$proxy_url" ]; then
    proxy_stripped="${proxy_url#*://}"
    proxy_stripped="${proxy_stripped%%/*}"
    proxy_stripped="${proxy_stripped##*@}"
    proxy_host="${proxy_stripped%%:*}"
    proxy_port="${proxy_stripped##*:}"
    if [ "$proxy_port" = "$proxy_stripped" ]; then
        proxy_port="80"
    fi
    proxy_nonhosts="$(printf '%s' "${no_proxy:-localhost,127.0.0.1}" | tr ',' '|')"
    sed -i '$d' /root/.m2/settings.xml
    printf '%s\n' \
        '    <proxies>' \
        '        <proxy>' \
        '            <id>build-proxy</id>' \
        '            <active>true</active>' \
        '            <protocol>http</protocol>' \
        "            <host>${proxy_host}</host>" \
        "            <port>${proxy_port}</port>" \
        "            <nonProxyHosts>${proxy_nonhosts}</nonProxyHosts>" \
        '        </proxy>' \
        '    </proxies>' \
        '</settings>' >> /root/.m2/settings.xml
fi

__ENABLE_SUREFIRE__
__DERIVE_TARGETS__

WARM_SEL="$SEL"
if [ -z "$WARM_SEL" ]; then
    for module in $MODULES; do
        m="$module"
        while [ "$m" != "." ] && [ ! -f "$m/pom.xml" ]; do
            m="$(dirname "$m")"
        done
        if [ "$m" != "." ]; then
            WARM_SEL="${WARM_SEL:+${WARM_SEL},}${m}"
        fi
    done
fi
test -n "$WARM_SEL"

cat > /tmp/patch_deps.pl <<'PERL_EOF'
use strict;
use warnings;
my ($root, @patches) = @ARGV;
my (%props, %ver);
sub slurp { my ($p) = @_; open(my $f, '<', $p) or return ''; local $/; my $t = <$f>; close $f; return $t; }
sub blocks {
    my ($t) = @_;
    my @b;
    while ($t =~ m{<dependency>(.*?)</dependency>}gs) {
        my $x = $1;
        my %d;
        for my $k (qw(groupId artifactId version type classifier)) {
            $d{$k} = $1 if $x =~ m{<$k>\s*([^<]+?)\s*</$k>};
        }
        push @b, \%d if $d{groupId} && $d{artifactId};
    }
    return @b;
}
sub props {
    my ($t) = @_;
    while ($t =~ m{<([A-Za-z0-9_.\-]*version[A-Za-z0-9_.\-]*)>\s*([^<\$\s]+)\s*</\1>}gi) {
        $props{$1} = $2 if lc($1) ne 'version';
    }
}
my $rt = slurp($root);
props($rt);
for my $d (blocks($rt)) { $ver{"$d->{groupId}:$d->{artifactId}"} = $d->{version} if $d->{version}; }
my $add = '';
for my $p (@patches) {
    open(my $f, '<', $p) or next;
    my $cur = '';
    while (my $l = <$f>) {
        if ($l =~ m{^diff --git a/\S+ b/(\S+)}) { $cur = $1; next; }
        next unless $cur =~ m{(^|/)pom\.xml$};
        next if $l =~ /^\+\+\+/;
        $add .= substr($l, 1) if $l =~ /^\+/;
    }
    close $f;
}
props($add);
my @deps = blocks($add);
for my $d (@deps) { $ver{"$d->{groupId}:$d->{artifactId}"} = $d->{version} if $d->{version}; }
my %seen;
for my $d (@deps) {
    my ($g, $a) = ($d->{groupId}, $d->{artifactId});
    next if $g =~ /dolphinscheduler|escheduler|\$/ || $a =~ /^(dolphinscheduler|escheduler)/;
    my $v = $d->{version} // $ver{"$g:$a"} // '';
    for (1 .. 5) { $v =~ s/\$\{([^}]+)\}/exists $props{$1} ? $props{$1} : "\${$1}"/ge; }
    next if $v eq '' || $v =~ /\$/;
    my $c = "$g:$a:$v";
    my $t = $d->{type} // 'jar';
    if ($t eq 'test-jar') { $c .= ':jar:tests'; }
    elsif ($d->{classifier}) { $c .= ":$t:$d->{classifier}"; }
    elsif ($t ne 'jar') { $c .= ":$t"; }
    print "$c\n" unless $seen{$c}++;
}
PERL_EOF
perl /tmp/patch_deps.pl pom.xml /home/fix.patch /home/test.patch > /tmp/patch_deps.txt || true
while read -r coord; do
    [ -n "$coord" ] || continue
    timeout 900 mvn -B dependency:get -Dartifact="$coord" -Dtransitive=true >> /tmp/dep-get.log 2>&1 || true
done < /tmp/patch_deps.txt

attempt=1
until timeout 3600 mvn -B test -pl "$WARM_SEL" -am "$TEST_ARG" __MVN_FLAGS__ > /tmp/warm-mvn.log 2>&1; do
    if [ "$attempt" -ge 3 ]; then
        break
    fi
    sleep "$((attempt * 15))"
    attempt=$((attempt + 1))
done

sed -i '/<proxies>/,/<\/proxies>/d' /root/.m2/settings.xml

find . -type d -name surefire-reports -prune -exec rm -rf {} +
set +e
timeout 3600 mvn -o -B test -pl "$WARM_SEL" -am "$TEST_ARG" __MVN_FLAGS__ > /tmp/gate-mvn.log 2>&1
GATE_STATUS=$?
set -e
GATE_REPORTS="$(find . -path '*/target/surefire-reports/TEST-*.xml' | wc -l | tr -d ' ')"
echo "GATE_MVN_EXIT=$GATE_STATUS"
echo "GATE_SUREFIRE_REPORTS=$GATE_REPORTS"

git reset --hard
git clean -fdx

! grep -qE 'Could not resolve dependencies|Could not transfer artifact|Cannot access .* in offline mode' /tmp/gate-mvn.log
if [ -n "$SEL" ]; then
    test "$GATE_REPORTS" -gt 0
    grep -q "Tests run:" /tmp/gate-mvn.log
else
    test "$GATE_STATUS" -eq 0
fi
echo "DEPS_OK"
"""

_RUN_BODY_SH = r"""
__ENABLE_SUREFIRE__
__DERIVE_TARGETS__

if [ -z "$SEL" ]; then
    echo "NO_TEST_MODULES_AT_THIS_STAGE"
    echo "===== SUREFIRE REPORTS BEGIN ====="
    echo "===== SUREFIRE REPORTS END ====="
    exit 0
fi

find . -type d -name surefire-reports -prune -exec rm -rf {} +

set +e
timeout 3600 mvn -o -B test -pl "$SEL" -am "$TEST_ARG" __MVN_FLAGS__ 2>&1 | tee /tmp/mvn-stage.log
MVN_STATUS=${PIPESTATUS[0]}
set -e

echo "modules ok: $(grep -cE '^\[INFO\] .+ SUCCESS \[' /tmp/mvn-stage.log || true)"
echo "modules failed: $(grep -cE '^\[INFO\] .+ FAILURE \[' /tmp/mvn-stage.log || true)"
echo "surefire reports: $(find . -path '*/target/surefire-reports/TEST-*.xml' | wc -l)"
echo "===== SUREFIRE REPORTS BEGIN ====="
find . -path '*/target/surefire-reports/TEST-*.xml' | sort | while read -r f; do
    cat "$f"
    echo
done
echo "===== SUREFIRE REPORTS END ====="

exit $MVN_STATUS
"""

_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export MAVEN_OPTS="-Xmx4g -XX:MaxMetaspaceSize=512m"
cd /home/__REPO__
__RUN_BODY__"""

_TEST_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export MAVEN_OPTS="-Xmx4g -XX:MaxMetaspaceSize=512m"
cd /home/__REPO__

git status --porcelain > /dev/null
git apply --3way --whitespace=nowarn __PATCH_EXCLUDES__ /home/test.patch
__RUN_BODY__"""

_FIX_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export MAVEN_OPTS="-Xmx4g -XX:MaxMetaspaceSize=512m"
cd /home/__REPO__

git status --porcelain > /dev/null
git apply --3way --whitespace=nowarn __PATCH_EXCLUDES__ /home/test.patch /home/fix.patch
__RUN_BODY__"""

_PATCH_EXCLUDES = (
    "--exclude='*.png' "
    "--exclude='*.jpg' "
    "--exclude='*.jpeg' "
    "--exclude='*.gif' "
    "--exclude='*.ico' "
    "--exclude='*.eot' "
    "--exclude='*.ttf' "
    "--exclude='*.otf' "
    "--exclude='*.woff' "
    "--exclude='*.woff2'"
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def dolphinscheduler_parse_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    clean_log = _ANSI_RE.sub("", test_log).replace("\r", "")

    re_case = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.S)
    re_attr = re.compile(r'(\w+)="([^"]*)"')

    for match in re_case.finditer(clean_log):
        attrs = dict(re_attr.findall(match.group(1)))
        method = attrs.get("name", "")
        classname = attrs.get("classname", "")
        if not method or not classname:
            continue
        name = f"{classname}#{method}"
        body = match.group(3) or ""
        if "<failure" in body or "<error" in body:
            failed_tests.add(name)
        elif "<skipped" in body:
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


class DolphinschedulerImageBase(Image):

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
        return "maven:3.8.4-eclipse-temurin-8"

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

        return (
            _BASE_DOCKERFILE.replace("__BASE_IMAGE__", image_name)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
        )


class DolphinschedulerImageDefault(Image):

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
        return DolphinschedulerImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        return (
            template.replace("__RUN_BODY__", _RUN_BODY_SH)
            .replace("__PATCH_EXCLUDES__", _PATCH_EXCLUDES)
            .replace("__ENABLE_SUREFIRE__", _ENABLE_SUREFIRE_SH)
            .replace("__REPO__", self.pr.repo)
            .replace("__ORG__", self.pr.org)
            .replace("__BASE_SHA__", self.pr.base.sha)
            .replace("__DERIVE_TARGETS__", _DERIVE_TARGETS_SH)
            .replace("__MVN_FLAGS__", _MVN_TEST_FLAGS)
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._render(_PREPARE_SH)),
            File(".", "run.sh", self._render(_RUN_SH)),
            File(".", "test-run.sh", self._render(_TEST_RUN_SH)),
            File(".", "fix-run.sh", self._render(_FIX_RUN_SH)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return (
            self._render(_DEFAULT_DOCKERFILE)
            .replace("__BASE_IMAGE__", image.image_full_name())
            .replace("__COPY_COMMANDS__", copy_commands)
        )


@Instance.register("apache", "dolphinscheduler_17821_to_17132")
class DOLPHINSCHEDULER_17821_TO_17132(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return DolphinschedulerImageDefault(self.pr, self._config)

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
        return dolphinscheduler_parse_log(test_log)
