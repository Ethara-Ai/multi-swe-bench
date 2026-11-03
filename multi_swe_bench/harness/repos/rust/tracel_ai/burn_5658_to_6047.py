from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_RUST_IMAGE = "rust:1.99.1-bookworm"
_BASE_TAG = "base-5658_to_6047"
_RUST_RELEASES = (
    ("2026-10-15", "1.99.1"),
    ("2026-10-01", "1.99.0"),
    ("2026-09-03", "1.98.1"),
)
_EXTRA_TOOLCHAINS = tuple(v for _, v in _RUST_RELEASES if v != "1.99.1")
_RELEASE_CASES = "\n".join(
    f'  if [ "$base_date" \\> "{d}" ] || [ "$base_date" = "{d}" ]; then echo {v}; return; fi'
    for d, v in _RUST_RELEASES
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_CRATE_RE = re.compile(r"^=== MSB_CRATE: (\S+) ===$")
_RUNNING_RE = re.compile(r"^Running (?:unittests )?(\S+) \(")
_LIBTEST_RE = re.compile(r"^test (\S+)(?: - should panic)? \.\.\. (ok|FAILED|ignored)\b")

_MEMBERS_JQ = r'.workspace_root as $r | .workspace_members as $m | .packages[] | select(.id as $i | $m | index($i)) | "\(.name) \(.manifest_path | ltrimstr($r + "/") | rtrimstr("/Cargo.toml"))"'
_FILES_AWK = r'/^diff --git a\// { sub(/^a\//, "", $3); print $3 }'
_CRATES_AWK = r'NR == FNR { f[$0]; next } { for (p in f) if (index(p, $2 "/") == 1) { print; break } }'

_RUN_HEADER = """#!/bin/bash
set -eo pipefail

export CI=true"""


def _plan_cmds(repo: str) -> str:
    return f"""cd /home/{repo}

awk '{_FILES_AWK}' /home/test.patch | sort -u > /tmp/msb-test-files.txt
cargo metadata --offline --no-deps --format-version 1 | jq -r '{_MEMBERS_JQ}' | sort > /tmp/msb-members.txt
awk '{_CRATES_AWK}' /tmp/msb-test-files.txt /tmp/msb-members.txt > /tmp/msb-crates.txt
[ -s /tmp/msb-crates.txt ] || {{ echo "=== MSB: no test crates found in test.patch ==="; exit 1; }}

: > /tmp/msb-plan.txt
while read -r pkg dir; do
  case "$pkg" in
    burn-backend-tests)
      echo "$pkg $dir flex flex,std --tests" >> /tmp/msb-plan.txt
      if grep -qx "$dir/tests/graph_capture.rs" /tmp/msb-test-files.txt; then
        features=flex,cpu,std
        grep -q '^capture *=' "$dir/Cargo.toml" && features="$features,capture"
        echo "$pkg $dir cpu $features --test=graph_capture" >> /tmp/msb-plan.txt
      fi
      ;;
    burn-signal) echo "$pkg $dir flex flex,std,autodiff --tests" >> /tmp/msb-plan.txt ;;
    burn-linalg) echo "$pkg $dir flex flex,std --tests" >> /tmp/msb-plan.txt ;;
    *) echo "=== MSB: no test plan for $pkg ==="; exit 1 ;;
  esac
done < /tmp/msb-crates.txt
"""


def _test_cmds(repo: str) -> str:
    return f"""{_plan_cmds(repo)}
fatal=0
while read -r pkg dir device features targets; do
  echo "=== MSB_CRATE: $dir@$device ==="
  rc=0
  BURN_DEVICE="$device" cargo test --offline --release --no-fail-fast -p "$pkg" --no-default-features --features "$features" "$targets" < /dev/null 2>&1 || rc=$?
  [ "$rc" -eq 0 ] || [ "$rc" -eq 101 ] || {{ echo "=== MSB: cargo test -p $pkg ($device) exited with status $rc ==="; fatal=$rc; }}
done < /tmp/msb-plan.txt

exit "$fatal"
"""


class Burn5658To6047ImageBase(Image):
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
        return _RUST_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

RUN printf 'Acquire::Retries "5";\\n' > /etc/apt/apt.conf.d/80-retries

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl build-essential pkg-config cmake lld jq xz-utils procps \\
    && rm -rf /var/lib/apt/lists/*

ENV RUSTUP_AUTO_INSTALL=0

RUN set -eu; \\
    rustup set auto-self-update disable; \\
    rustup toolchain install {" ".join(_EXTRA_TOOLCHAINS)} --profile minimal; \\
    for toolchain in {" ".join(v for _, v in _RUST_RELEASES)}; do \\
      rustup toolchain list | grep -q "^$toolchain-" || {{ echo "toolchain $toolchain missing"; exit 1; }}; \\
    done

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class Burn5658To6047ImageDefault(Image):
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
        return Burn5658To6047ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
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
  git status --porcelain | head -20
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
""",
            ),
            File(
                ".",
                "prepare.sh",
                f"""\
#!/bin/bash
set -e

cd /home/{self.pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e "{self.pr.base.sha}^{{commit}}" 2>/dev/null || git fetch --no-tags origin "{self.pr.base.sha}"
git checkout --detach {self.pr.base.sha}
bash /home/check_git_changes.sh

export CARGO_NET_RETRY=10 CARGO_HTTP_TIMEOUT=120

base_date="$(git log -1 --format=%cs HEAD)"
pick_toolchain() {{
{_RELEASE_CASES}
}}
toolchain="$(pick_toolchain)"
[ -n "$toolchain" ] || {{ echo "prepare.sh: no pinned stable release on or before $base_date"; exit 1; }}
rustup toolchain list | grep -q "^$toolchain-" || {{ echo "prepare.sh: toolchain $toolchain is not in the base image"; exit 1; }}
rustup default "$toolchain"
rustup show active-toolchain | grep -q "^$toolchain-" || {{ echo "prepare.sh: toolchain $toolchain is not active"; exit 1; }}

cargo fetch --locked
{_plan_cmds(self.pr.repo)}
while read -r pkg dir device features targets; do
  BURN_DEVICE="$device" cargo test --offline --release --locked --no-run -p "$pkg" --no-default-features --features "$features" "$targets" < /dev/null || {{ echo "prepare.sh: cargo test --no-run -p $pkg ($device) failed"; exit 1; }}
done < /tmp/msb-plan.txt
""",
            ),
            File(
                ".",
                "run.sh",
                f"""\
{_RUN_HEADER}

{_test_cmds(self.pr.repo)}""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""\
{_RUN_HEADER}

cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch || {{ echo "test-run.sh: git apply failed" >&2; exit 1; }}
{_test_cmds(self.pr.repo)}""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""\
{_RUN_HEADER}

cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ echo "fix-run.sh: git apply failed" >&2; exit 1; }}
{_test_cmds(self.pr.repo)}""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("Burn5658To6047ImageDefault dependency must be an Image")
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT={self.pr.base.sha}
ENV BASE_COMMIT=${{BASE_COMMIT}} \\
    CARGO_INCREMENTAL=0 \\
    CARGO_TERM_COLOR=never \\
    CARGO_BUILD_JOBS=6 \\
    CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_RUSTFLAGS="-C target-feature=+fullfp16 -C link-arg=-fuse-ld=lld" \\
    RUST_BACKTRACE=1

{copy_commands}

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


@Instance.register("tracel-ai", "burn_5658_to_6047")
class BURN_5658_TO_6047(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Burn5658To6047ImageDefault(self.pr, self._config)

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
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()

        crate = ""
        target = ""
        for raw in _ANSI_RE.sub("", test_log).splitlines():
            line = raw.strip()

            marker = _CRATE_RE.match(line)
            if marker:
                crate = marker.group(1)
                target = ""
                continue

            running = _RUNNING_RE.match(line)
            if running:
                target = running.group(1)
                continue

            result = _LIBTEST_RE.match(line)
            if not result or not crate or not target:
                continue

            name = f"{crate}/{target}::{result.group(1)}"
            status = result.group(2)
            if status == "ok":
                passed.add(name)
            elif status == "FAILED":
                failed.add(name)
            else:
                skipped.add(name)

        passed -= failed
        passed -= skipped
        skipped -= failed

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
