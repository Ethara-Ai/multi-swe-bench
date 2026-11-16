import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest



_CHECK_GIT_CHANGES = """#!/bin/bash
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

_GOTEST = r"""cat > /tmp/mswe_gotest.sh <<'MSWE_GOTEST_EOF'
#!/bin/bash
cd /home/@@REPO@@
MODE="$1"
grep -E '^diff --git a/' /home/test.patch | sed -E 's#^diff --git a/([^ ]+) b/.*#\1#' | sort -u > /tmp/go_patch_paths
grep -E '_test\.go$' /tmp/go_patch_paths | xargs -r -n1 dirname | sort -u > /tmp/go_test_dirs
: > /tmp/go_test_mods
while read -r d; do
  [ -d "$d" ] || continue
  m="$d"
  while [ "$m" != "." ] && [ ! -f "$m/go.mod" ]; do m=$(dirname "$m"); done
  if [ "$m" = "$d" ]; then p="."; else p="./${d#$m/}"; fi
  echo "$m $p" >> /tmp/go_test_mods
done < /tmp/go_test_dirs
if [ ! -s /tmp/go_test_mods ]; then
  while read -r f; do
    d=$(dirname "$f")
    while [ ! -d "$d" ]; do d=$(dirname "$d"); done
    m="$d"
    while [ "$m" != "." ] && [ ! -f "$m/go.mod" ]; do m=$(dirname "$m"); done
    if [ "$m" = "$d" ]; then p="./..."; else p="./${d#$m/}/..."; fi
    echo "$m $p" >> /tmp/go_test_mods
  done < /tmp/go_patch_paths
fi
rc=0
for m in $(cut -d' ' -f1 /tmp/go_test_mods | sort -u); do
  pkgs=$(awk -v m="$m" '$1==m {print $2}' /tmp/go_test_mods | sort -u | tr '\n' ' ')
  if [ "$MODE" = "warm" ]; then
    (cd "$m" && go test -count=1 -run XXX_NO_TEST $pkgs) || true
  elif [ "$MODE" = "gate" ]; then
    (cd "$m" && go list -deps -test $pkgs > /dev/null) || rc=$?
  else
    pkglist=$(cd "$m" && go list -e $pkgs) || rc=$?
    if [ -z "$pkglist" ]; then
      echo "no Go packages resolved for $m: $pkgs"
      rc=1
    fi
    for pkg in $pkglist; do
      echo ">>> go test $pkg"
      (cd "$m" && go test -v -count=1 "$pkg" 2>&1) || rc=$?
    done
  fi
done
exit $rc
MSWE_GOTEST_EOF
"""

_PREPARE = """#!/bin/bash
set -e

cd /home/@@REPO@@
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e @@SHA@@^{commit} 2>/dev/null || git fetch origin @@SHA@@
git checkout --detach @@SHA@@
bash /home/check_git_changes.sh

@@GOTEST@@
bash /tmp/mswe_gotest.sh warm || true

go version
test -s /tmp/go_test_mods
bash /tmp/mswe_gotest.sh gate
echo DEPS_OK
"""

_RUN = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/@@REPO@@
@@APPLY@@
@@GOTEST@@
rc=0
bash /tmp/mswe_gotest.sh || rc=$?
exit $rc
"""


_RE_PATCH_BLOCK = re.compile(
    r"^[^\n]*MSWE_TEST_PATCH_BEGIN[^\n]*\n(.*?)(?:MSWE_TEST_PATCH_END[^\n]*(?:\n|$)|\Z)",
    re.S | re.M,
)


def _split_patch_block(log: str):
    m = _RE_PATCH_BLOCK.search(log or "")
    if not m:
        return log, ""
    return log[: m.start()] + log[m.end():], m.group(1).replace("\r\n", "\n")


_TEST_PATCH_ECHO = """echo "MSWE_TEST_PATCH_BEGIN"
cat /home/test.patch
echo "MSWE_TEST_PATCH_END"
"""


def _fill(body: str, pr: PullRequest, apply: str = "") -> str:
    return (
        body.replace("@@GOTEST@@", _GOTEST)
        .replace("@@APPLY@@", apply)
        .replace("@@REPO@@", pr.repo)
        .replace("@@SHA@@", pr.base.sha)
    )


def _pr_dockerfile(image: Image) -> str:
    base = image.dependency()
    repo = image.pr.repo
    sha = image.pr.base.sha
    copy_commands = "".join(f"COPY {file.name} /home/\n" for file in image.files())
    return f"""FROM {base.image_full_name()}

{image.global_env}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach "{sha}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test -z "$(git reflog)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi

{image.clear_env}
"""


class BeamImageBase(Image):
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
        return "golang:1.26-bookworm"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return f"""# syntax=docker/dockerfile:1.6

FROM {self.dependency()}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

ENV GOTOOLCHAIN=local \\
    CGO_ENABLED=1 \\
    CI=true

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*' && \\
    go version

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class BeamImageDefault(Image):
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
        return BeamImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        pr = self.pr
        return [
            File(".", "fix.patch", f"{pr.fix_patch}"),
            File(".", "test.patch", f"{pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", _fill(_PREPARE, pr)),
            File(".", "run.sh", _fill(_RUN, pr)),
            File(".", "test-run.sh", _fill(_RUN, pr, _TEST_PATCH_ECHO + "git apply --whitespace=nowarn /home/test.patch")),
            File(".", "fix-run.sh", _fill(_RUN, pr, "git apply --whitespace=nowarn /home/test.patch /home/fix.patch")),
        ]

    def dockerfile(self) -> str:
        return _pr_dockerfile(self)


@Instance.register("apache", "beam_40153_to_40153")
class BEAM_40153_TO_40153(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return BeamImageDefault(self.pr, self._config)

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

    def _added_tests(self, packages, patch_text=""):
        names = set()
        current = None
        for line in (self.pr.test_patch or patch_text or "").splitlines():
            if line.startswith("diff --git "):
                parts = line.split(" b/", 1)
                path = parts[1].strip() if len(parts) > 1 else ""
                current = path if path.endswith("_test.go") else None
                continue
            if current is None:
                continue
            match = re.match(r"^\+func (Test\w+)\(\s*\w+\s+\*testing\.T\s*\)", line)
            if not match:
                continue
            dir_parts = [x for x in current.split("/")[:-1] if x]
            best = None
            best_score = 0
            for pkg in packages:
                pkg_parts = pkg.split("/")
                score = 0
                while (
                    score < len(dir_parts)
                    and score < len(pkg_parts)
                    and dir_parts[-1 - score] == pkg_parts[-1 - score]
                ):
                    score += 1
                if score > best_score:
                    best = pkg
                    best_score = score
            if best:
                names.add(f"{best}::{match.group(1)}")
        return names

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        test_log, patch_text = _split_patch_block(re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log))
        clean_log = test_log
        re_pkg = re.compile(r"^>>> go test (\S+)")
        re_result = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+)")

        package = ""
        packages = []
        for line in clean_log.splitlines():
            match = re_pkg.match(line.strip())
            if match:
                package = match.group(1)
                packages.append(package)
                continue
            match = re_result.match(line)
            if not match:
                continue
            status = match.group(1)
            top = match.group(2).split("/", 1)[0]
            name = f"{package}::{top}" if package else top
            if status == "PASS":
                passed_tests.add(name)
            elif status == "FAIL":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        if re.search(r"\[(build|setup) failed\]", clean_log):
            for name in self._added_tests(packages, patch_text):
                if name not in passed_tests:
                    failed_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
