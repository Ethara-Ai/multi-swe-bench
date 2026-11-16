import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_RE_COMPILE_FAIL = re.compile(r"compileTest\w* FAILED|COMPILATION ERROR|Compilation failure|\.java:\d+:? (?:error:|\[error\])")
_RE_PATCH_BLOCK = re.compile(r"^[^\n]*MSWE_TEST_PATCH_BEGIN[^\n]*\n(.*?)(?:MSWE_TEST_PATCH_END[^\n]*(?:\n|$)|\Z)", re.S | re.M)
_RE_TEST_ANNOT = re.compile(r"@(?:org\.junit(?:\.\w+)*\.)?Test\b(?:\s*\([^)]*\))?")
_RE_LEADING_ANNOT = re.compile(r"^(?:@[\w.]+(?:\s*\((?:[^()]|\([^()]*\))*\))?\s*)+")
_RE_METHOD_DECL = re.compile(r"\b(\w+)\s*\(")
_JAVA_KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "new", "synchronized"}
_TEST_PATCH_ECHO = 'echo "MSWE_TEST_PATCH_BEGIN"\ncat /home/test.patch\necho "MSWE_TEST_PATCH_END"\n'

def _added_test_names(patch):
    found, path, pending = {"+": set(), "-": set()}, None, set()
    for line in (patch or "").splitlines():
        if line.startswith(("diff --git ", "+++ ")):
            p = "/" + line[4:].strip().removeprefix("b/") if line.startswith("+++ ") else ""
            ok = "/src/test/java/" in p and p.endswith(".java")
            path = p.split("/src/test/java/", 1)[1][:-5].replace("/", ".") if ok else None
            pending = set()
            continue
        if path is None or line.startswith(("--- ", "\\", "@@")):
            pending = set() if line.startswith("@@") else pending
            continue
        kind = line[:1]
        kinds = {kind} if kind in found else {"+", "-"}
        body = line[1:].strip()
        m = _RE_TEST_ANNOT.match(body)
        if m:
            pending |= kinds
            body = body[m.end():].strip()
        if not body or body.startswith(("//", "*", "/*")):
            continue
        body = _RE_LEADING_ANNOT.sub("", body).strip()
        if not body:
            continue
        mm = next((x for x in _RE_METHOD_DECL.finditer(body) if x.group(1) not in _JAVA_KEYWORDS), None)
        for k in kinds & pending:
            if kind in found and mm:
                found[k].add(f"{path}#{mm.group(1)}")
        pending -= kinds
    return found["+"] - found["-"]

COMMON_FN = r"""
mswe_targets() {
  MODS="" TESTS="" FQTESTS=""
  while read -r p; do
    case "$p" in *src/test/java/*.java) ;; *) continue ;; esac
    b=$(basename "$p" .java)
    case "$b" in *Test|*Tests|*TestCase) ;; *) continue ;; esac
    case "$p" in src/test/java/*) m=. ;; *) m="${p%%/src/test/java/*}" ;; esac
    [ -f "$m/build.gradle" ] || [ -f "$m/build.gradle.kts" ] || continue
    fq="${p#*src/test/java/}"
    fq="${fq%.java}" && fq="${fq//\//.}"
    case ",$MODS," in *",$m,"*) ;; *) MODS="${MODS:+$MODS,}$m" ;; esac
    case ",$TESTS," in *",$b,"*) ;; *) TESTS="${TESTS:+$TESTS,}$b" ;; esac
    FQTESTS="$FQTESTS $fq"
  done < <(grep -E '^diff --git ' /home/test.patch | sed -E 's|^diff --git a/.* b/(.*)$|\1|' | sort -u)
  echo "mswe targets: modules=[$MODS] tests=[$TESTS]"
}
"""

JDK_FN = r"""
mswe_install_jdk() {
  v=$(grep -ohE 'JavaLanguageVersion\.of\( *[0-9]+' build.gradle build.gradle.kts 2>/dev/null | grep -oE '[0-9]+$' | sort -n | tail -n1)
  for l in 8 11 17 21 25; do [ "$l" -ge "${v:-8}" ] && break; done
  echo "mswe jdk: $l"
  apt-get update
  apt-get install -y --no-install-recommends "temurin-$l-jdk"
  ln -sfn "/usr/lib/jvm/temurin-$l-jdk-$(dpkg --print-architecture)" /usr/lib/jvm/mswe-jdk
  java -version
}
"""

REPORT_FN = r"""
mswe_report() {
  find . \( -path '*/target/surefire-reports/TEST-*.xml' -o -path '*/build/test-results/*/TEST-*.xml' \) -type f 2>/dev/null | sort | while read -r f; do
    awk -v OFS='\t' '
      function attr(s, a) {
        if (match(s, " " a "=\"[^\"]*\"")) return substr(s, RSTART + length(a) + 3, RLENGTH - length(a) - 4)
        return ""
      }
      function emit() { if (open) { print "MSWE_TEST", st, cls, nm; open = 0 } }
      /<testcase[ >]/ {
        emit()
        t = substr($0, index($0, "<testcase"))
        e = index(t, ">")
        head = substr(t, 1, e)
        nm = attr(head, "name")
        cls = attr(head, "classname")
        st = "PASSED"
        open = 1
        if (substr(head, e - 1, 1) == "/") { emit(); next }
        rest = substr(t, e + 1)
        if (rest ~ /<(failure|error)[ >\/]/) st = "FAILED"
        else if (rest ~ /<skipped[ >\/]/) st = "SKIPPED"
        if (rest ~ /<\/testcase>/) emit()
        next
      }
      open && /^[ \t]*<(failure|error)[ >\/]/ { st = "FAILED" }
      open && /^[ \t]*<skipped[ >\/]/ { if (st != "FAILED") st = "SKIPPED" }
      open && /<\/testcase>/ { emit() }
      END { emit() }
    ' "$f"
  done
}
"""

CLEAN_REPORTS = r"""
find . \( -path '*/target/surefire-reports' -o -path '*/build/test-results' \) -type d -prune -exec rm -rf {} + 2>/dev/null
"""

BASE_RUN = "RUN git clone https://github.com/supertokens/supertokens-root.git /home/supertokens-root \\\n    && git clone https://github.com/supertokens/supertokens-plugin-interface.git /home/supertokens-root/supertokens-plugin-interface\nRUN printf '#!/bin/sh\\nexit 1\\n' > /usr/local/bin/docker && chmod +x /usr/local/bin/docker"

PREPARE_EXTRA = r"""
D=$(git log -1 --format=%cI HEAD)
T=$(( $(git log -1 --format=%ct HEAD) + 259200 ))
PI=$(jq -r '.versions[-1]' pluginInterfaceSupported.json)
[ -L /home/supertokens-core ] || { mv /home/supertokens-core /home/supertokens-root/supertokens-core && ln -s /home/supertokens-root/supertokens-core /home/supertokens-core; }
cd /home/supertokens-root
git checkout -q "$(git rev-list -1 --before="$D" origin/HEAD)"
printf 'core,master\nplugin-interface,%s\nsqlite-plugin,master' "$PI" > modules.txt
printf "include 'supertokens-core:cli'\ninclude 'supertokens-core:downloader'\ninclude 'supertokens-core:ee'\ninclude 'supertokens-core'\ninclude 'supertokens-plugin-interface'\n" > settings.gradle
echo 'org.gradle.jvmargs=-Xmx3g' > gradle.properties
R=$(for b in "integration/$PI-next" "$PI"; do git -C supertokens-plugin-interface rev-parse -q --verify "origin/$b"; done)
pick() {
  for c in $(git -C supertokens-plugin-interface rev-list -n 40 --before="$T" $R); do
    git -C supertokens-plugin-interface checkout -q -f "$c"
    [ -z "$1" ] || git -C supertokens-plugin-interface cherry-pick -n "$1" >/dev/null 2>&1
    ./gradlew -q :supertokens-core:classes >/dev/null 2>&1 && return 0
  done
  return 1
}
pick "" || pick 18b127f
./utils/setupTestEnv --local
"""

BUILD_CMD = "(cd /home/supertokens-root && ./gradlew :supertokens-core:compileTestJava)"
TEST_CMD = r"""(cd /home/supertokens-root && ./gradlew :supertokens-core:test $(for t in $FQTESTS; do printf -- '--tests %s ' "$t"; done) --continue)"""
CHECK_GIT_CHANGES = """#!/bin/bash
set -e
git rev-parse --is-inside-work-tree > /dev/null 2>&1 || { echo "check_git_changes: Not inside a git repository"; exit 1; }
[[ -z $(git status --porcelain) ]] || { echo "check_git_changes: Uncommitted changes"; exit 1; }
echo "check_git_changes: No uncommitted changes"
"""

PRUNE = r"""RUN set -eux; \
    cd /home/{repo}; \
    git checkout --detach "{sha}"; \
    git remote remove origin 2>/dev/null || true; \
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \
        | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all; \
    git reflog expire --expire-unreachable=now --all; \
    git gc --prune=now --aggressive; \
    rm -f .git/objects/info/alternates; \
    git config --local gc.auto 0; \
    git config --local fetch.recurseSubmodules false; \
    git config --local remote.pushDefault ""; \
    test "$(git rev-parse HEAD)" = "{sha}"; \
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \
    test -z "$(git remote)"; \
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \
        cd /home/{repo} && git submodule foreach --recursive ' \
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

class SupertokensCore1492To1467ImageBase(Image):
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
        return "base-autojdk"

    def workdir(self) -> str:
        return "base-autojdk"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org, repo = self.pr.org, self.pr.repo
        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo} && \\\n    cd /home/{repo} && git rev-parse HEAD >/dev/null'
        else:
            code = f"COPY {repo} /home/{repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {self.dependency()}

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
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CI=true \\
    ST_PLUGIN_NAME=sqlite \\
    JAVA_HOME=/usr/lib/jvm/mswe-jdk

ENV PATH=/usr/lib/jvm/mswe-jdk/bin:$PATH

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

RUN apt-get update && apt-get install -y --no-install-recommends git curl wget ca-certificates unzip gnupg jq \\
    && curl -fsSL https://packages.adoptium.net/artifactory/api/gpg/key/public | gpg --dearmor -o /usr/share/keyrings/adoptium.gpg \\
    && echo "deb [signed-by=/usr/share/keyrings/adoptium.gpg] https://packages.adoptium.net/artifactory/deb $(. /etc/os-release && echo $VERSION_CODENAME) main" > /etc/apt/sources.list.d/adoptium.list \\
    && rm -rf /var/lib/apt/lists/*
{BASE_RUN}

RUN git config --global --add safe.directory '*'

{code}
{self.clear_env}
CMD ["/bin/bash"]
"""

class SupertokensCore1492To1467ImageDefault(SupertokensCore1492To1467ImageBase):
    def dependency(self) -> Image | None:
        return SupertokensCore1492To1467ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def run_script(self, apply: str) -> str:
        script = "#!/bin/bash\nset -eo pipefail\nexport CI=true\n"
        script += f"cd /home/{self.pr.repo}\n"
        script += COMMON_FN + REPORT_FN
        script += "mswe_targets\n"
        if apply:
            script += _TEST_PATCH_ECHO if apply == "/home/test.patch" else ""
            script += f"git apply --whitespace=nowarn {apply}\n"
        script += CLEAN_REPORTS
        script += "rc=0\n"
        script += TEST_CMD + " || rc=$?\n"
        script += "mswe_report\nexit $rc\n"
        return script

    def files(self) -> list[File]:
        repo, sha = self.pr.repo, self.pr.base.sha
        prepare = f"""#!/bin/bash
set -e
export CI=true

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e {sha}^{{commit}} 2>/dev/null || git fetch origin {sha}
git checkout --detach {sha}
bash /home/check_git_changes.sh
"""
        prepare += COMMON_FN + JDK_FN
        prepare += "mswe_targets\nmswe_install_jdk || true\n"
        prepare += "{\n" + PREPARE_EXTRA.strip() + "\n} || true\n"
        prepare += f"cd /home/{repo}\n"
        prepare += TEST_CMD + " || true\n"
        prepare += CLEAN_REPORTS.strip() + " || true\n"
        prepare += f'test "$(git rev-parse HEAD)" = "{sha}"\n'
        prepare += "java -version\n"
        prepare += BUILD_CMD + "\n"
        prepare += "echo DEPS_OK\n"
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", self.run_script("")),
            File(".", "test-run.sh", self.run_script("/home/test.patch")),
            File(".", "fix-run.sh", self.run_script("/home/test.patch /home/fix.patch")),
        ]

    def dockerfile(self) -> str:
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())
        prune = PRUNE.replace("{repo}", self.pr.repo).replace("{sha}", self.pr.base.sha)

        return f"""FROM {self.dependency().image_full_name()}

{self.global_env}
{copy_commands}
RUN bash /home/prepare.sh

{prune}
{self.clear_env}
"""

@Instance.register("supertokens", "supertokens_core_1492_to_1467")
class SUPERTOKENS_CORE_1492_TO_1467(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return SupertokensCore1492To1467ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
        block = _RE_PATCH_BLOCK.search(test_log)
        patch_text = block.group(1) if block else ""
        test_log = test_log[: block.start()] + test_log[block.end():] if block else test_log
        re_line = re.compile(r"^MSWE_TEST\t(PASSED|FAILED|SKIPPED)\t([^\t]*)\t(.*)$")
        tests = {"PASSED": set(), "FAILED": set(), "SKIPPED": set()}
        for raw in test_log.splitlines():
            m = re_line.match(raw.rstrip("\r"))
            if m:
                cls, method = m.group(2).strip(), m.group(3).strip()
                name = f"{cls}#{method}" if method else cls
                if name:
                    tests[m.group(1)].add(name)
        failed_tests = tests["FAILED"]
        if _RE_COMPILE_FAIL.search(test_log):
            failed_tests |= _added_test_names(self.pr.test_patch or patch_text) - tests["PASSED"]
        passed_tests = tests["PASSED"] - failed_tests
        skipped_tests = tests["SKIPPED"] - failed_tests - passed_tests
        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
