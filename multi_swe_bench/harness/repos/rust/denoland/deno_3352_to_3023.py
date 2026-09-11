
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

GN_2020_REVISION = "152c5144ceed9592c20f0c8fd55769646077569b"
GN_2020_PATH = "/usr/local/bin/gn-2020"
GN_2020_INSTALL = f"""if [ ! -x {GN_2020_PATH} ]; then
  curl -fsSL -o /tmp/gn-2020.zip \\
    "https://chrome-infra-packages.appspot.com/dl/gn/gn/linux-amd64/+/git_revision:{GN_2020_REVISION}"
  unzip -oq -j /tmp/gn-2020.zip gn -d /tmp/gn-2020
  mv /tmp/gn-2020/gn {GN_2020_PATH}
  chmod +x {GN_2020_PATH}
  rm -rf /tmp/gn-2020 /tmp/gn-2020.zip
fi
{GN_2020_PATH} --version | grep -q "{GN_2020_REVISION[:8]}\""""

LIBXML2_INSTALL = """apt-get update -qq >/dev/null 2>&1 || true
apt-get install -y -qq libxml2 >/dev/null 2>&1 || true
ldconfig -p | grep -q libxml2.so.2"""

LIBDENO_CLANG_BASE_PATH = (
    "/home/deno/core/libdeno/third_party/llvm-build/Release+Asserts"
)


class DenoImageBase(Image):
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
        return "ubuntu:18.04"

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

        infra = DockerfileEnhancer._infrastructure_block(self, image_name).rstrip("\n")

        light_hardening = (
            "RUN git remote remove origin 2>/dev/null || true; \\\n"
            "    git config --local fetch.recurseSubmodules false; \\\n"
            "    git config --local gc.auto 0"
        )

        sections = [
            DockerfileEnhancer.SYNTAX_DIRECTIVE,
            f"FROM --platform=linux/amd64 {image_name}",
            infra,
        ]

        if self.global_env:
            sections.append(self.global_env)

        sections.append("WORKDIR /home/")

        sections.append(
            "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
            "    build-essential ca-certificates curl git pkg-config libssl-dev \\\n"
            "    python2.7 python-minimal unzip zip xz-utils file \\\n"
            "    && ln -sf /usr/bin/python2.7 /usr/bin/python \\\n"
            "    && rm -rf /var/lib/apt/lists/*"
        )
        sections.append(
            "RUN apt-get update \\\n"
            "    && apt-get install -y --no-install-recommends \\\n"
            "       clang-8 lld-8 ninja-build libglib2.0-dev libatomic1 python2.7-dev \\\n"
            "    || apt-get install -y --no-install-recommends \\\n"
            "       clang lld ninja-build libglib2.0-dev libatomic1 python2.7-dev \\\n"
            "    ; rm -rf /var/lib/apt/lists/*"
        )

        sections.append(
            "RUN curl -fsSL -o /tmp/gn.zip \\\n"
            '      "https://chrome-infra-packages.appspot.com/dl/gn/gn/linux-amd64/+/latest" \\\n'
            "    && unzip -o -d /usr/local/bin /tmp/gn.zip gn \\\n"
            "    && chmod +x /usr/local/bin/gn \\\n"
            "    && rm -f /tmp/gn.zip \\\n"
            "    || true"
        )

        sections.append(
            "ENV RUSTUP_HOME=/usr/local/rustup \\\n"
            "    CARGO_HOME=/usr/local/cargo \\\n"
            "    PATH=/usr/local/cargo/bin:$PATH \\\n"
            "    CARGO_INCREMENTAL=0 \\\n"
            "    CARGO_TERM_COLOR=never \\\n"
            "    CARGO_NET_GIT_FETCH_WITH_CLI=true \\\n"
            "    RUST_BACKTRACE=full"
        )
        sections.append(
            "RUN curl -sSf https://sh.rustup.rs \\\n"
            "    | sh -s -- -y --no-modify-path --profile minimal --default-toolchain none"
        )
        sections.append(
            "RUN set -eux; \\\n"
            "    for v in 1.37.0 1.39.0 1.41.0 1.42.0 1.43.0 1.44.0 1.46.0 1.47.0 1.48.0; do \\\n"
            '        rustup toolchain install "$v" --profile minimal; \\\n'
            '        rustup target add --toolchain "$v" wasm32-unknown-unknown || true; \\\n'
            '        rustup target add --toolchain "$v" wasm32-wasi || true; \\\n'
            "    done; \\\n"
            "    rustup default 1.48.0"
        )

        sections.append(code)
        sections.append(f"WORKDIR /home/{self.pr.repo}")
        sections.append(light_hardening)

        if self.clear_env:
            sections.append(self.clear_env)

        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


class DenoImageDefault(Image):
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
        return DenoImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        toolchain_env = (
            "export DENO_BUILD_MODE=debug\n"
            "export DENO_NO_BINARY_DOWNLOAD=1\n"
            f"export DENO_GN_PATH={GN_2020_PATH}\n"
            f"export DENO_BUILD_ARGS='clang_base_path=\"{LIBDENO_CLANG_BASE_PATH}\" "
            "clang_use_chrome_plugins=false use_sysroot=false'\n"
            f"export GN={GN_2020_PATH}\n"
            "export NINJA=/usr/bin/ninja\n"
            f"export CLANG_BASE_PATH={LIBDENO_CLANG_BASE_PATH}"
        )
        extra_setup = (
            f"{GN_2020_INSTALL}\n"
            f"{LIBXML2_INSTALL}\n"
            "git submodule update --init --recursive --depth 1 "
            "|| git submodule update --init --recursive || true\n"
            "python ./tools/setup.py || true\n"
            f"test -x {LIBDENO_CLANG_BASE_PATH}/bin/clang"
        )

        test_command = (
            "cargo test --locked --no-fail-fast --all-targets -- --test-threads=1"
            " --skip _061_permissions_request"
            " --skip _062_permissions_request_global"
            " --skip _066_prompt"
        )

        env_preamble = """export CI=true
export RUST_BACKTRACE=full
export CARGO_INCREMENTAL=0
export CARGO_TERM_COLOR=never
export CARGO_NET_GIT_FETCH_WITH_CLI=true
export RUSTUP_TOOLCHAIN="$(cat /home/.mswb_rust_toolchain 2>/dev/null || true)"
{toolchain_env}""".format(toolchain_env=toolchain_env)

        if "Subproject commit" in (self.pr.fix_patch or ""):
            fix_submodule_sync = (
                "\ngit submodule sync --recursive\n"
                "git submodule update --init --recursive\n"
            )
        else:
            fix_submodule_sync = ""

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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

{env_preamble}

cd /home/{pr.repo}
git config --global --add safe.directory /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh

# The base image clones the default branch only, so a base commit that lives
# on refs/pull/* is not present yet.  Fetch it by sha, then fall back to the
# pull refs, then drop the temporary refs again so hardening still holds.
if ! git cat-file -e {pr.base.sha} 2>/dev/null; then
    git fetch --quiet https://github.com/{pr.org}/{pr.repo}.git {pr.base.sha} \\
        || git fetch --quiet origin "+refs/pull/*/head:refs/mswb/pull/*" \\
        || true
fi
git checkout --detach {pr.base.sha}
git for-each-ref --format='%(refname)' refs/mswb | xargs -r -n1 git update-ref -d

# Submodule working trees pick up build artefacts (tools/setup.py writes into
# third_party).  Those are not repository changes, so keep them out of
# `git status --porcelain` for every later stage.
git config --local diff.ignoreSubmodules all
git config --local status.showUntrackedFiles normal
bash /home/check_git_changes.sh

git remote remove origin 2>/dev/null || true
git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
    | xargs -r -n1 git update-ref -d
git reflog expire --expire=now --all
git reflog expire --expire-unreachable=now --all
git gc --prune=now --aggressive
git repack -a -d -l --quiet
rm -f .git/objects/info/alternates
test "$(git rev-parse HEAD)" = "$(git rev-parse {pr.base.sha})"
test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"
test -z "$(git remote)"
test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

# Resolve the pinned Rust toolchain dynamically from the checked-out base
# commit's CI config (.github/workflows -> rust-version:).  Fail loudly if it
# cannot be resolved so the build never silently uses the wrong toolchain.
# Persist it outside the work tree so run/test/fix stages (and
# check_git_changes.sh) see it without touching the repo.
mswb_rust_version="$(grep -rhoE 'rust-version:[[:space:]]*"?[0-9]+[.][0-9]+[.][0-9]+"?' .github/workflows 2>/dev/null | grep -oE '[0-9]+[.][0-9]+[.][0-9]+' | head -n1)"
case "$mswb_rust_version" in
    [0-9]*[.][0-9]*[.][0-9]*) : ;;
    *) echo "ERROR: could not resolve rust-version from .github/workflows" >&2; exit 1 ;;
esac
echo "$mswb_rust_version" > /home/.mswb_rust_toolchain
export RUSTUP_TOOLCHAIN="$mswb_rust_version"

rustup default "$mswb_rust_version"

{extra_setup}

# Warm the dependency and V8 caches at build time, where the network is
# available and the output is never parsed.
cargo fetch --locked || true
cargo build --locked --all-targets || true

git reset --hard
git clean -fd
bash /home/check_git_changes.sh
""".format(
                    pr=self.pr,
                    env_preamble=env_preamble,
                    extra_setup=extra_setup,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

{env_preamble}

cd /home/{pr.repo}

{test_command}
""".format(pr=self.pr, env_preamble=env_preamble, test_command=test_command),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

{env_preamble}

cd /home/{pr.repo}

git update-index -q --refresh || true
if ! git apply --3way --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi

{test_command}
""".format(pr=self.pr, env_preamble=env_preamble, test_command=test_command),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

{env_preamble}

cd /home/{pr.repo}

git update-index -q --refresh || true
if ! git apply --3way --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply test.patch + fix.patch failed" >&2
    exit 1
fi
{fix_submodule_sync}
{test_command}
""".format(
                    pr=self.pr,
                    env_preamble=env_preamble,
                    test_command=test_command,
                    fix_submodule_sync=fix_submodule_sync,
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{copy_commands}
RUN bash /home/prepare.sh
"""


@Instance.register("denoland", "deno_3352_to_3023")
class DENO_3352_TO_3023(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DenoImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi_re = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        start_re = re.compile(r"^test (.+?) \.\.\.(.*)$")
        status_re = re.compile(r"^(ok|FAILED|ignored)(?:\s*\(\d+(?:\.\d+)?m?s\))?$")
        legacy_ok_re = re.compile(r"^OK\s+(.+?)\s+\(\d+(?:\.\d+)?m?s\)$")
        legacy_fail_re = re.compile(r"^FAILED\s+([A-Za-z_].*?)\s*$")

        clean_log = ansi_re.sub("", log)
        pending: str | None = None

        def record(name: str, status: str) -> None:
            if status == "ok":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        for raw in clean_log.split("\n"):
            line = raw.strip()

            legacy_ok = legacy_ok_re.match(line)
            if legacy_ok:
                record(legacy_ok.group(1), "ok")
                continue

            legacy_fail = legacy_fail_re.match(line)
            if legacy_fail:
                record(legacy_fail.group(1), "FAILED")
                continue

            start = start_re.match(line)
            if start:
                pending = start.group(1)
                tail = start.group(2).strip()
                tail_status = status_re.match(tail)
                if tail_status:
                    record(pending, tail_status.group(1))
                    pending = None
                continue

            if pending is None:
                continue

            status = status_re.match(line)
            if status:
                record(pending, status.group(1))
                pending = None
            elif line.startswith("test result:"):
                pending = None

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
