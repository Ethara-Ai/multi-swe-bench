import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_RUST_IMAGE = "rust:1.97-trixie"

_TAG_PREFIX = "workspace"

_SYNTAX_DIRECTIVE = DockerfileEnhancer.SYNTAX_DIRECTIVE

_PROXY_ARGS = DockerfileEnhancer._PROXY_ARGS

_ENV_BLOCK = DockerfileEnhancer._ENV_BLOCK

_CERT_SYMLINKS = DockerfileEnhancer._CERT_SYMLINKS

_ASCII_FILTER = r"tr -cd '\11\12\15\40-\176'"

_APT_PACKAGES = "git ca-certificates pkg-config clang libclang-dev cmake libssl-dev python3 locales xz-utils perl"

_TEST_RUNNER = ""

_TEST_TIMEOUT = "900"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_PKG_RE = re.compile(r"(?m)^MSWE_PKG (\S+)$")

_TIMEOUT_RE = re.compile(r"(?m)^MSWE_TIMEOUT (\S+)$")

_RERUN_RE = re.compile(r"(?m)^MSWE_RERUN (\S+) (ok|FAILED)$")

_RESULT_RE = re.compile(r"^test (\S+) +\.\.\. (ok|FAILED|ignored)\b.*$", re.MULTILINE)

_SECTION_RE = re.compile(r"(?m)(?=^diff --git )")

_BINARY_RE = re.compile(r"(?m)^(Binary files .* differ|GIT binary patch)$")

_STATUS = {"ok": "passed", "FAILED": "failed", "ignored": "skipped"}

_RANK = {"passed": 0, "skipped": 1, "failed": 2}

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e
git rev-parse --is-inside-work-tree > /dev/null 2>&1 || { echo "check_git_changes: Not inside a git repository"; exit 1; }
test -z "$(git status --porcelain)" || { echo "check_git_changes: Uncommitted changes"; git status --porcelain | head -20; exit 1; }
echo "check_git_changes: No uncommitted changes"
exit 0
"""

_TOOLCHAIN_SH = """#!/bin/bash
set -eo pipefail
cd /home/__REPO__
CURRENT=$(rustc --version | awk '{print $2}')
DECLARED=$(find . -maxdepth 3 -name Cargo.toml -not -path './target/*' -print0 | xargs -0 -r sed -n 's/^rust-version *= *"\\([0-9.]*\\)".*/\\1/p' | sort -V | tail -1)
WANTED=$(printf '%s\\n%s\\n' "${CURRENT}" "${DECLARED}" | sed '/^$/d' | sort -V | tail -1)
echo "TOOLCHAIN: current=${CURRENT} declared=${DECLARED:-none} selected=${WANTED}"
rustup toolchain install --profile minimal "${WANTED}"
rustup default "${WANTED}"
find . -maxdepth 3 \\( -name rust-toolchain -o -name rust-toolchain.toml \\) -not -path './target/*' -print0 | xargs -0 -r -n1 dirname | while read -r dir; do (cd "${dir}" && echo "TOOLCHAIN_FILE: ${dir}" && rustup toolchain install); done
rustc --version
CIDATE=$(TZ=UTC git log -1 --date=format-local:%Y-%m-%dT%H:%M:%SZ --format=%cd HEAD)
HOST=$(rustc -vV | sed -n 's/^host: //p')
release_at() { curl -fsSL --retry 3 "https://api.github.com/repos/$1/releases?per_page=100" | python3 -c 'import json,sys; r=[x for x in json.load(sys.stdin) if not x["draft"] and not x["prerelease"] and x["published_at"]<=sys.argv[1]]; print(max(r,key=lambda x:x["published_at"])["tag_name"])' "$2"; }
WASMTARGETS=$(find . -maxdepth 3 -path './.github/*' -name '*.y*ml' -print0 | xargs -0 -r sed -n 's/.*rustup target add \\(.*\\)$/\\1/p' | tr ' ' '\\n' | awk '/^wasm32-[a-z0-9_-]+$/' | sort -u | paste -sd' ' -)
NIGHTLYTARGETS=$(find . -maxdepth 3 -path './.github/*' -name '*.y*ml' -print0 | xargs -0 -r sed -n 's/.*rustup toolchain install nightly.*--target \\([a-z0-9_,-]*\\).*/\\1/p' | tr ',' '\\n' | awk '/^wasm32-[a-z0-9_-]+$/' | sort -u | paste -sd, -)
NIGHTLY="nightly-$(echo "${CIDATE}" | cut -c1-10)"
echo "CI_DATE: ${CIDATE} CI_WASM_TARGETS: ${WASMTARGETS:-none} CI_NIGHTLY_TARGETS: ${NIGHTLYTARGETS:-none}"
[ -z "${WASMTARGETS}" ] || rustup target add ${WASMTARGETS}
[ -z "${NIGHTLYTARGETS}" ] || rustup toolchain install "${NIGHTLY}" --profile minimal --target "${NIGHTLYTARGETS}"
[ -z "${NIGHTLYTARGETS}" ] || ln -sfn "${RUSTUP_HOME:-$HOME/.rustup}/toolchains/${NIGHTLY}-${HOST}" "${RUSTUP_HOME:-$HOME/.rustup}/toolchains/nightly-${HOST}"
[ -z "${NIGHTLYTARGETS}" ] || rustup run nightly rustc --version
WASIXCC=$(find . -maxdepth 3 -path './.github/*' -name '*.y*ml' -print0 | xargs -0 -r sed -n 's/.*wasix-org\\/wasixcc@\\(v[0-9][0-9.]*\\).*/\\1/p' | sort -V | tail -1)
echo "WASIXCC: ${WASIXCC:-none}"
[ -z "${WASIXCC}" ] || mkdir -p "${HOME}/.wasixcc/bin" "${HOME}/.wasixcc/llvm"
[ -z "${WASIXCC}" ] || curl -fsSL --retry 3 "https://github.com/wasix-org/wasixcc/releases/download/${WASIXCC}/wasixcc-$(uname -m)-unknown-linux-gnu.tar.gz" | tar -xz -C "${HOME}/.wasixcc"
[ -z "${WASIXCC}" ] || "${HOME}/.wasixcc/wasixccenv" install-executables "${HOME}/.wasixcc/bin"
[ -z "${WASIXCC}" ] || SYSROOT_TAG=$(release_at wasix-org/wasix-libc "${CIDATE}")
[ -z "${WASIXCC}" ] || LLVM_TAG=$(release_at wasix-org/llvm-project "${CIDATE}")
[ -z "${WASIXCC}" ] || BINARYEN_TAG=$(release_at WebAssembly/binaryen "${CIDATE}")
[ -z "${WASIXCC}" ] || echo "WASIX_PINS: sysroot=${SYSROOT_TAG} llvm=${LLVM_TAG} binaryen=${BINARYEN_TAG}"
[ -z "${WASIXCC}" ] || "${HOME}/.wasixcc/bin/wasixccenv" download-sysroot "${SYSROOT_TAG}"
[ -z "${WASIXCC}" ] || curl -fsSL --retry 3 "https://github.com/wasix-org/llvm-project/releases/download/${LLVM_TAG}/LLVM-Linux-$(uname -m).tar.gz" | tar -xz -C "${HOME}/.wasixcc/llvm"
[ -z "${WASIXCC}" ] || "${HOME}/.wasixcc/bin/wasixccenv" download-binaryen "${BINARYEN_TAG}"
[ -z "${WASIXCC}" ] || "${HOME}/.wasixcc/bin/wasixcc" --version
"""

_TEST_PLAN_PY = r"""import functools
import json
import os
import re
import subprocess
import sys

ROOT = "/home/__REPO__"
TEST_ATTR = re.compile(r"^\+\s*#\[([A-Za-z_]+::)?test")
PATH_RE = re.compile(r"^(?:\+\+\+ b|--- a)/(\S+)", re.M)


def read(path):
    return open(path, errors="replace").read()


def fix_test_files(text):
    out, cur = {}, ""
    for line in text.splitlines():
        cur = (re.match(r"^\+\+\+ b/(\S+)", line) or [None, cur])[1]
        out.update(dict.fromkeys([cur][: int(bool(TEST_ATTR.match(line)))]))
    return list(out)


def is_pkg(rel_dir):
    manifest = os.path.join(ROOT, rel_dir, "Cargo.toml")
    return os.path.isfile(manifest) and bool(re.search(r"(?m)^\[package\]", read(manifest)))


@functools.lru_cache(maxsize=None)
def test_targets(pkg):
    result = subprocess.run(
        ["cargo", "metadata", "--no-deps", "--format-version", "1"],
        cwd=os.path.join(ROOT, pkg), capture_output=True, text=True,
    )
    data = json.loads(result.stdout or '{"packages": []}')
    manifest = os.path.realpath(os.path.join(ROOT, pkg, "Cargo.toml"))
    packages = filter(lambda package: os.path.realpath(package["manifest_path"]) == manifest, data["packages"])
    return frozenset(t["name"] for package in packages for t in package["targets"] if "test" in t["kind"])


def owner(rel):
    parts = rel.split("/")[:-1]
    ancestors = ["/".join(parts[:i]) or "." for i in range(len(parts), -1, -1)]
    return ([a for a in ancestors if is_pkg(a)] + [None])[0]


def classify(pkg, rel, created):
    parts = rel.split("/")
    module = re.sub(r"(/mod)?\.rs$", "", rel[4:]).replace("/", "::")
    name = re.sub(r"\.rs$", "", (parts + [""])[1])
    known = name in test_targets(pkg) or bool({f"tests/{name}.rs", f"tests/{name}/main.rs"} & created)
    is_module = rel.startswith("src/") and rel.endswith(".rs") and module not in ("lib", "main") and not module.startswith("bin::")
    is_target = parts[0] == "tests" and len(parts) > 1 and bool(name) and known
    return {
        (True, False): ("filter", module + "::"),
        (False, True): ("test", name),
    }.get((is_module, is_target), ("whole", ""))


test_patch = read("/home/test.patch")
new_files = set(re.findall(r"^--- /dev/null\n\+\+\+ b/(\S+)", test_patch, re.M))
paths = list(dict.fromkeys(PATH_RE.findall(test_patch) + fix_test_files(read("/home/fix.patch"))))
owned = [(owner(p), p) for p in paths]
plan = {}
for pkg, path in filter(lambda item: item[0] is not None, owned):
    rel = os.path.relpath(path, pkg)
    plan.setdefault(pkg, {"whole": {}, "filter": {}, "test": {}})
    kind, value = classify(pkg, rel, {os.path.relpath(p, pkg) for p in new_files})
    plan[pkg][kind][value] = None

for pkg in plan:
    entry = plan[pkg]
    whole = bool(entry["whole"])
    lines = {
        True: [("--tests", "", 1)],
        False: [("--tests", " ".join(entry["filter"]), 1)][: int(bool(entry["filter"]))]
        + [(f"--test {t}", "", int(t in test_targets(pkg))) for t in entry["test"]],
    }[whole]
    for opts, filters, present in lines:
        print(f"{pkg}\t{opts}\t{filters or '-'}\t{present}")
"""

_TEST_CMD_SH = """#!/bin/bash
set -eo pipefail
export CI=true
export RUST_TEST_THREADS=1
export RUST_MIN_STACK=16777216
export CARGO_BUILD_JOBS=4
export PATH="${HOME}/.wasixcc/bin:${HOME}/.wasixcc/binaryen/bin:${PATH}"
cd /home/__REPO__
find . -maxdepth 3 -name tauri.conf.json -not -path './target/*' | while read -r conf; do
  sed -n 's/.*"frontendDist" *: *"\\([^":]*\\)".*/\\1/p' "$conf" | head -1 | while read -r dist; do mkdir -p "$(dirname "$conf")/${dist}"; done
done
python3 /home/test-plan.py > /tmp/mswe-test-plan.tsv
echo "TEST_PLAN:"
sed 's/^/  /' /tmp/mswe-test-plan.tsv
test -s /tmp/mswe-test-plan.tsv || { echo "NO_TEST_PACKAGES"; exit 0; }
compile_only=0
[ "${1:-}" != "--no-run" ] || compile_only=1
rc=0
while IFS=$'\t' read -r d opts filters present; do
  echo "MSWE_PKG ${d}"
  cd "/home/__REPO__/${d}"
  filters=${filters#-}
  [ "${compile_only}${present}" != "10" ] || continue
  feats=""
  built=0
  cargo test --no-fail-fast ${opts} --no-run < /dev/null > /tmp/mswe-build.out 2>&1 && built=1
  [ "${built}" = "1" ] || { CARGO_BUILD_JOBS=1 cargo test --no-fail-fast ${opts} --no-run < /dev/null > /tmp/mswe-build.out 2>&1 && built=1; }
  cat /tmp/mswe-build.out
  [ "${built}" = "1" ] || feats=$(sed -n -e 's#.*/registry/src/[^/]*/\\([^/]*\\)/src/.*#C \\1#p' -e 's#.*the item is gated behind the `\\([A-Za-z0-9_-]*\\)` feature.*#F \\1#p' /tmp/mswe-build.out | awk '$1=="C"{c=$2; sub(/-[0-9][0-9.]*.*$/,"",c)} $1=="F"&&c!=""{print c"/"$2}' | sort -u | paste -sd, -)
  [ "${built}" = "1" ] || [ -z "${feats}" ] || echo "MSWE_FEATURES ${feats}"
  [ "${built}" = "1" ] || [ -z "${feats}" ] || { CARGO_BUILD_JOBS=1 cargo test --no-fail-fast ${opts} --features "${feats}" --no-run < /dev/null && built=1; }
  [ "${built}" = "1" ] || { rc=1; continue; }
  fopt=""
  [ -z "${feats}" ] || fopt="--features ${feats}"
  [ "${compile_only}" = "0" ] || continue
  skips=""
  tries=0
  status=124
  : > /tmp/mswe-pkg.out
  while [ "${status}" = "124" ] || [ "${status}" = "137" ]; do
    tries=$((tries + 1))
    status=0
    timeout -k 30 __TEST_TIMEOUT__ __RUNNER__ cargo test --no-fail-fast ${opts} ${fopt} -- ${filters} ${skips} < /dev/null > /tmp/mswe-run.out 2>&1 || status=$?
    cat /tmp/mswe-run.out
    cat /tmp/mswe-run.out >> /tmp/mswe-pkg.out
    echo
    [ "${status}" != "124" ] && [ "${status}" != "137" ] && break
    hung=$(tail -n 1 /tmp/mswe-run.out | sed -n 's/^test \\([^ ]*\\)  *\\.\\.\\. *$/\\1/p')
    echo "MSWE_TIMEOUT ${hung:-unknown}"
    skips="${skips} --skip ${hung:-unknown}"
    [ -n "${hung}" ] && [ "${tries}" -lt 5 ] || break
  done
  sed -n 's/^test \\([^ ]*\\)  *\\.\\.\\. FAILED$/\\1/p' /tmp/mswe-pkg.out | awk '!seen[$0]++' | head -n 30 > /tmp/mswe-failed.txt
  while read -r t; do
    rr=0
    timeout -k 30 300 __RUNNER__ cargo test --no-fail-fast ${opts} ${fopt} -- --exact "${t}" < /dev/null > /tmp/mswe-rerun.out 2>&1 || rr=$?
    sed 's/^/  | /' /tmp/mswe-rerun.out
    verdict=FAILED
    [ "${rr}" = "0" ] && awk -v t="${t}" '$1 == "test" && $2 == t && $NF == "ok" {f = 1} END {exit !f}' /tmp/mswe-rerun.out && verdict=ok
    echo "MSWE_RERUN ${t} ${verdict}"
  done < /tmp/mswe-failed.txt
  [ "${status}" = "0" ] || rc=1
done < /tmp/mswe-test-plan.tsv
exit ${rc}
"""

_PREPARE_SH = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e __BASE_SHA__^{commit} 2>/dev/null || git fetch --quiet https://github.com/__ORG__/__REPO__.git __BASE_SHA__
git checkout --detach __BASE_SHA__
git -c url.https://github.com/.insteadOf=git@github.com: -c url.https://github.com/.insteadOf=ssh://git@github.com/ submodule update --init --recursive --depth 1 --jobs 4 --quiet
git submodule foreach --recursive --quiet 'git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags | xargs -r -n1 git update-ref -d; git reflog expire --expire=now --all; git gc --quiet --prune=now'
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "__BASE_SHA__"
find . -path ./.git -prune -o -path ./target -prune -o -print0 | xargs -0 -r touch -h -d "@$(git log -1 --format=%ct HEAD)"
git update-index -q --refresh

bash /home/toolchain.sh

find . -maxdepth 3 -name Cargo.lock -not -path './target/*' -print0 | xargs -0 -r -n1 dirname | while read -r dir; do (cd "${dir}" && cargo fetch); done
bash /home/test-cmd.sh --no-run
git update-index -q --refresh
git reset --hard
echo DEPS_OK
"""

_STAGE_SH = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/__REPO__
git update-index -q --refresh
git reset --hard
git clean -qfd
__APPLY__
bash /home/test-cmd.sh
"""


def _render(template: str, pr: PullRequest) -> str:
    return (
        template.replace("__REPO__", pr.repo)
        .replace("__ORG__", pr.org)
        .replace("__BASE_SHA__", pr.base.sha)
        .replace("__RUNNER__", _TEST_RUNNER)
        .replace("__TEST_TIMEOUT__", _TEST_TIMEOUT)
    )


def _sanitize_patch(patch: str) -> str:
    return "".join(filter(lambda section: not _BINARY_RE.search(section), _SECTION_RE.split(patch or "")))


class PingoraImageBase(Image):
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
        return _RUST_IMAGE

    def image_tag(self) -> str:
        return f"{_TAG_PREFIX}-base"

    def workdir(self) -> str:
        return f"{_TAG_PREFIX}-base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        fetch = {
            True: f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}',
            False: f"COPY {self.pr.repo} /home/{self.pr.repo}",
        }[bool(self.config.need_clone)]

        return f"""{_SYNTAX_DIRECTIVE}
FROM {_RUST_IMAGE}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"

{_PROXY_ARGS}

{_ENV_BLOCK}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{_CERT_SYMLINKS}

{self.global_env}

ENV CARGO_TERM_COLOR=never \\
    CARGO_TERM_PROGRESS_WHEN=never \\
    CARGO_INCREMENTAL=0 \\
    CARGO_BUILD_JOBS=4 \\
    CARGO_PROFILE_DEV_DEBUG=0 \\
    CARGO_PROFILE_TEST_DEBUG=0 \\
    RUST_BACKTRACE=1 \\
    NO_COLOR=1 \\
    LC_ALL=C.UTF-8 \\
    CI=true

RUN /bin/bash -o pipefail -c "( apt-get update && apt-get install -y --no-install-recommends {_APT_PACKAGES} && rm -rf /var/lib/apt/lists/* ) 2>&1 | {_ASCII_FILTER}"

RUN git config --system --add safe.directory '*'

WORKDIR /home/

{fetch}

WORKDIR /home/{self.pr.repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class PingoraImageDefault(Image):
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
        return PingoraImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        stage = _render(_STAGE_SH, self.pr)
        return [
            File(".", "fix.patch", _sanitize_patch(self.pr.fix_patch)),
            File(".", "test.patch", _sanitize_patch(self.pr.test_patch)),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "toolchain.sh", _render(_TOOLCHAIN_SH, self.pr)),
            File(".", "test-plan.py", _render(_TEST_PLAN_PY, self.pr)),
            File(".", "test-cmd.sh", _render(_TEST_CMD_SH, self.pr)),
            File(".", "prepare.sh", _render(_PREPARE_SH, self.pr)),
            File(".", "run.sh", stage.replace("__APPLY__", "")),
            File(
                ".",
                "test-run.sh",
                stage.replace("__APPLY__", "git apply --whitespace=nowarn /home/test.patch"),
            ),
            File(
                ".",
                "fix-run.sh",
                stage.replace(
                    "__APPLY__",
                    "git apply --whitespace=nowarn /home/test.patch /home/fix.patch",
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {image.image_name()}:{image.image_tag()}

{self.global_env}

{copy_commands}

RUN /bin/bash -o pipefail -c "bash /home/prepare.sh 2>&1 | {_ASCII_FILTER}"

WORKDIR /home/{self.pr.repo}

{self._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)}

{self.clear_env}

CMD ["/bin/bash"]

"""


@Instance.register("cloudflare", "cargo_workspace")
class PINGORA_CARGO_WORKSPACE(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PingoraImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        log = _ANSI_RE.sub("", test_log).replace("\r", "")
        parts = _PKG_RE.split(log)
        results: dict[str, str] = {}
        for pkg, body in zip(parts[1::2], parts[2::2]):
            for name in _TIMEOUT_RE.findall(body):
                results[f"{pkg}::{name}"] = "failed"
            for name, raw in _RESULT_RE.findall(body):
                key = f"{pkg}::{name}"
                status = _STATUS[raw]
                results[key] = max(results.get(key, status), status, key=_RANK.get)
            for name, raw in _RERUN_RE.findall(body):
                results[f"{pkg}::{name}"] = _STATUS[raw]

        buckets: dict[str, set[str]] = {"passed": set(), "failed": set(), "skipped": set()}
        for name, status in results.items():
            buckets[status].add(name)

        passed_tests = buckets["passed"] - buckets["failed"] - buckets["skipped"]
        skipped_tests = buckets["skipped"] - buckets["failed"]
        failed_tests = buckets["failed"]

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
