import re
from dataclasses import dataclass

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# gohugoio/hugo toolchain era: PRs 1479-3420. Self-contained registry for
# JSONL rows with number_interval "hugo_3420_to_1479"; shared base image tag
# base-1479_to_3420. Sibling eras live in their own hugo_<hi>_to_<lo>.py files.


@dataclass(frozen=True)
class _Era:
    lo: int
    hi: int
    go_image: str
    # Shell environment shared by prepare.sh and the three run scripts.
    go_env: str
    # prepare.sh Section 2 (dependency provisioning).
    provision: str
    # Re-vendoring step in fix-run.sh (GOPATH eras only).
    revendor: str
    vet_flag: str

    @property
    def base_tag(self) -> str:
        return f"base-{self.lo}_to_{self.hi}"


_GOPATH_PROVISION = """\
# GOPATH layout: the import path is a symlink to the clone.
mkdir -p "$(dirname "$GO_PKG_DIR")"
ln -sfn /home/hugo "$GO_PKG_DIR"
BASE_DATE="$(git log -1 --format=%cI HEAD)"

# Vendoring tools, built from tagged releases (they vendor their own deps).
if [ ! -x "$GOPATH/bin/govendor" ]; then
  git clone -q --depth 1 -b v1.0.9 https://github.com/kardianos/govendor "$GOPATH/src/github.com/kardianos/govendor"
  go install github.com/kardianos/govendor
fi
DEP_TOOL
# Newest commit of every GOPATH dependency that predates the base commit.
pin_gopath_deps() {
  find "$GOPATH/src" -mindepth 2 -maxdepth 5 -type d -name .git -prune | while read -r g; do
    repo="$(dirname "$g")"
    case "$repo" in
      */kardianos/govendor|*/golang/dep) continue ;;
    esac
    c="$(git -C "$repo" rev-list -1 --before="$BASE_DATE" HEAD 2>/dev/null || true)"
    if [ -n "$c" ]; then
      git -C "$repo" checkout -q --detach "$c" || true
    fi
  done
}

# bitbucket.org/pkg/inflect is gone (404); bep/inflect is its GitHub mirror.
if [ ! -d "$GOPATH/src/bitbucket.org/pkg/inflect" ]; then
  git clone -q https://github.com/bep/inflect "$GOPATH/src/bitbucket.org/pkg/inflect"
fi

cd "$GO_PKG_DIR"
if [ -f Gopkg.lock ]; then
  dep ensure -vendor-only
elif [ -f vendor/vendor.json ]; then
  govendor sync
fi
# Anything the manifest does not cover (no manifest at all, or test-only
# imports excluded by govendor's "ignore": "test") comes from GOPATH, pinned
# by date. `go get -d` first fetches dependency HEADs; pinning then changes
# the import set, so iterate until it settles. `|| true` is safe here because
# Section 3 re-checks the build and test imports.
# Third-party packages newly imported by the patches (read only, never
# applied here) are fetched too, so test-run/fix-run need no network.
PATCH_PKGS="$(grep -hoE '^[+][[:space:]]*([A-Za-z_.]+[[:space:]]+)?"[a-z0-9.-]+[.][a-z]+/[^"]+"' /home/test.patch /home/fix.patch 2>/dev/null \\
  | grep -oE '"[^"]+"' | tr -d '"' | grep -v -e '^github.com/spf13/hugo' -e '^github.com/gohugoio/hugo' | sort -u || true)"
for round in 1 2 3 4 5 6; do
  go get -d -t ./... $PATCH_PKGS || true
  pin_gopath_deps
done
"""

_GOPATH_REVENDOR = """\
# Re-vendor when the fix patch changes the dependency manifest.
if git diff --name-only HEAD | grep -qE '^(Gopkg[.]lock|Gopkg[.]toml|vendor/vendor[.]json)$'; then
  ln -sfn /home/hugo "$GO_PKG_DIR"
  (cd "$GO_PKG_DIR" && if [ -f Gopkg.lock ]; then dep ensure -vendor-only; elif [ -f vendor/vendor.json ]; then govendor sync; fi)
fi
"""

# carry a govendor manifest; older ones are resolved by `go get` + date pin.
_ERA = _Era(
    lo=1479,
    hi=3420,
    go_image="golang:1.9",
    go_env="""export GOPATH=/go
export GO111MODULE=off
export GIT_TERMINAL_PROMPT=0
export PATH="$GOPATH/bin:/usr/local/go/bin:$PATH"
GO_PKG_DIR="/go/src/github.com/spf13/hugo"
""",
    provision=_GOPATH_PROVISION.replace("DEP_TOOL\n", ""),
    revendor=_GOPATH_REVENDOR,
    vet_flag="",
)


def _test_dirs(test_patch: str) -> list[str]:
    """Package directories touched by the test patch (Go files only)."""
    dirs: set[str] = set()
    for a, b in re.findall(r"^diff --git a/(\S+) b/(\S+)", test_patch, re.M):
        for path in (a, b):
            if not path.endswith(".go"):
                continue
            if "/testdata/" in f"/{path}" or path.startswith("vendor/"):
                continue
            dirs.add(path.rsplit("/", 1)[0] if "/" in path else ".")
    return sorted(dirs)


def _binary_excludes(*patches: str) -> str:
    """`--exclude=` flags for binary files in the patches.

    The dataset's binary hunks lack full index lines, so `git apply` cannot
    apply them; they are test fixtures/images that the Go tests here do not
    need to compile.
    """
    paths: set[str] = set()
    for patch in patches:
        for section in re.split(r"^(?=diff --git )", patch, flags=re.M):
            m = re.match(r"diff --git a/(\S+) b/(\S+)", section)
            if not m:
                continue
            if re.search(r"^(Binary files .* differ|GIT binary patch)$", section, re.M):
                paths.update(m.groups())
    return "".join(f" --exclude='{p}'" for p in sorted(paths))


def _go_test_cmd(test_patch: str, vet_flag: str) -> str:
    """Identical in run.sh, test-run.sh and fix-run.sh.

    Only directories that exist and hold Go files at run time are passed, so
    a package the test patch adds is simply absent from run.sh instead of
    aborting the whole `go test` invocation. Falls back to ./... when empty.
    """
    dirs = " ".join(f"'{d}'" for d in _test_dirs(test_patch))
    return f"""PKGS=""
for d in {dirs}; do
  if [ -d "$d" ] && ls "$d"/*.go >/dev/null 2>&1; then
    PKGS="$PKGS ./$d"
  fi
done
if [ -z "$PKGS" ]; then
  PKGS="./..."
fi
go test {vet_flag}-v -count=1 $PKGS"""


class HugoImageBase_HUGO_3420_TO_1479(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return _ERA.go_image

    def image_tag(self) -> str:
        return _ERA.base_tag

    def workdir(self) -> str:
        return _ERA.base_tag

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        """Environment and clone only.

        Emits the BuildKit syntax directive itself, which makes
        DockerfileEnhancer.enhance() return this file untouched (image.py:317).
        That keeps the injected checkout and history scrub out of the shared
        base, and is why the ARGs, ENV block, OCI labels and CA-certificate
        symlink farm are written here rather than inherited.

        The symlink farm precedes every network RUN. No apt-get step: the
        official golang images already ship git, curl and ca-certificates, and
        the older Debian suites behind the early golang tags are no longer on
        the live mirrors. BASE_COMMIT is declared because the harness passes
        it, but deliberately unused: pinning happens in the PR layer.
        """
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = (
                f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\\n'
                f"    cd /home/{self.pr.repo} && git rev-parse HEAD > /dev/null"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    GIT_SSL_CAINFO=${{CA_CERT_PATH}}

ENV CGO_ENABLED=1 \\
    GOPROXY=https://proxy.golang.org,direct \\
    GORACE=halt_on_error=1 \\
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

WORKDIR /home/

RUN git config --global --add safe.directory '*'

{code}

CMD ["/bin/bash"]
"""


class HugoImageDefault_HUGO_3420_TO_1479(Image):
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
        return HugoImageBase_HUGO_3420_TO_1479(self.pr, self.config)

    def image_tag(self) -> str:
        # Registry-scoped so it never collides with hugo.py's pr-{n} images,
        # which share the image name mswebench/gohugoio_m_hugo.
        return f"pr-{self.pr.number}-hugo_3420_to_1479"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        era = _ERA
        test_cmd = _go_test_cmd(self.pr.test_patch, era.vet_flag)
        test_excludes = _binary_excludes(self.pr.test_patch)
        fix_excludes = _binary_excludes(self.pr.test_patch, self.pr.fix_patch)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """\
#!/bin/bash
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
                """\
#!/bin/bash
set -euo pipefail

# --- Section 1: pin the base commit on a clean tree ---
cd /home/REPO
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach BASE_SHA
bash /home/check_git_changes.sh

# --- Section 2: provision the toolchain and dependencies ---
GO_ENV
PROVISION
# --- Section 3: hard gate -- base builds and its test imports resolve ---
cd "$GO_PKG_DIR"
go version
go build ./...
# Every external package imported by the test files must resolve. (Some base
# commits ship test files that do not compile, e.g. PR 2619's
# helpers/path_test.go, so this checks resolution rather than compilation.)
go list -f '{{join .TestImports "\\n"}}{{"\\n"}}{{join .XTestImports "\\n"}}' ./... \\
  | sort -u | grep -v -e '^$' -e '^github.com/spf13/hugo' | xargs -r go list > /dev/null
echo DEPS_OK
""".replace("PROVISION\n", era.provision)
                .replace("VET_FLAG", era.vet_flag)
                .replace("REPO", self.pr.repo)
                .replace("BASE_SHA", self.pr.base.sha)
                .replace("GO_ENV", era.go_env),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true
GO_ENV

cd /home/REPO
cd "$GO_PKG_DIR"
TEST_CMD
""".replace("REPO", self.pr.repo)
                .replace("GO_ENV", era.go_env)
                .replace("TEST_CMD", test_cmd),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true
GO_ENV

cd /home/REPO
git apply --whitespace=nowarnTEST_EXCLUDES /home/test.patch
cd "$GO_PKG_DIR"
TEST_CMD
""".replace("TEST_EXCLUDES", test_excludes)
                .replace("REPO", self.pr.repo)
                .replace("GO_ENV", era.go_env)
                .replace("TEST_CMD", test_cmd),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true
GO_ENV

cd /home/REPO
git apply --whitespace=nowarnFIX_EXCLUDES /home/test.patch /home/fix.patch
REVENDOR
cd "$GO_PKG_DIR"
TEST_CMD
""".replace("FIX_EXCLUDES", fix_excludes)
                .replace("REVENDOR\n", era.revendor)
                .replace("REPO", self.pr.repo)
                .replace("GO_ENV", era.go_env)
                .replace("TEST_CMD", test_cmd),
            ),
        ]

    def dockerfile(self) -> str:
        """Pin to the base commit, COPY scripts, install deps, then scrub.

        Never touched by DockerfileEnhancer (dependency() is an Image, see
        image.py:315), so BASE_COMMIT is declared here with a default taken
        from self.pr.base.sha. The base SHA is fetched explicitly when it is
        not reachable from any cloned ref (e.g. PRs 2520, 2619, 3254). The four
        `test` lines assert HEAD is the base commit, no refs survive, no remote
        survives, and no unreachable history survives.
        """
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("dependency must be an Image")
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

WORKDIR /home/{self.pr.repo}

RUN git cat-file -e "${{BASE_COMMIT}}^{{commit}}" 2>/dev/null \\
    || git fetch --no-tags origin "${{BASE_COMMIT}}"

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    test "$(git rev-parse HEAD)" = "${{BASE_COMMIT}}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "${{BASE_COMMIT}}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{self.pr.repo}/.gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi

{self.clear_env}
"""


@Instance.register("gohugoio", "hugo_3420_to_1479")
class HUGO_3420_TO_1479(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return HugoImageDefault_HUGO_3420_TO_1479(self.pr, self._config)

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
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        re_result = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+)")
        # Package summary line, e.g. "ok  \tpkg\t0.1s" or "FAIL\tpkg [build failed]".
        re_pkg = re.compile(r"^(ok|FAIL)\s+(\S+)(.*)$")
        re_pkg_broken = re.compile(r"\[(?:build|setup) failed\]")

        buckets = {"PASS": passed_tests, "FAIL": failed_tests, "SKIP": skipped_tests}
        # go test prints a package's results before its summary line, so
        # buffer results and prefix them with the package once it is known.
        pending: list[tuple[str, str]] = []

        for line in log.splitlines():
            m = re_result.match(line)
            if m:
                pending.append((m.group(1), m.group(2)))
                continue
            m = re_pkg.match(line)
            if m:
                pkg = m.group(2)
                for status, name in pending:
                    buckets[status].add(f"{pkg}/{name}")
                pending.clear()
                if m.group(1) == "FAIL" and re_pkg_broken.search(m.group(3)):
                    failed_tests.add(pkg)

        # Results without a trailing summary (e.g. killed run) keep bare names.
        for status, name in pending:
            buckets[status].add(name)

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
