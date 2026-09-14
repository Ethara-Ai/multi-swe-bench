from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_RUST_IMAGE = "rust:1.69-bookworm"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_CRATE_RE = re.compile(r"^=== MSB_CRATE: (\S+) ===$")
_TARGET_RE = re.compile(r"^Running (?:unittests )?(\S+) \(")
_DOCTESTS_RE = re.compile(r"^Doc-tests\b")
_RESULT_RE = re.compile(r"^test (.+?) \.\.\. (ok|FAILED|ignored)")
_DOC_NAME_RE = re.compile(r"^(\S+\.rs) - (.+)$")


def _cargo_loop(mode: str) -> str:
    return f"""rc=0
for crate in crates/*/; do
    crate="${{crate%/}}"
    [ -f "$crate/Cargo.toml" ] || continue
    echo "=== MSB_CRATE: $crate ==="
    if [ -f "$crate/src/lib.rs" ]; then
        (cd "$crate" && cargo test --lib {mode} 2>&1) || rc=$?
    fi
    if [ -f "$crate/src/main.rs" ] || [ -d "$crate/src/bin" ]; then
        (cd "$crate" && cargo test --bins {mode} 2>&1) || rc=$?
    fi
    for target in "$crate"/tests/*.rs "$crate"/tests/*/main.rs; do
        [ -f "$target" ] || continue
        name="$(basename "$target" .rs)"
        if [ "$name" = main ]; then
            name="$(basename "$(dirname "$target")")"
        fi
        (cd "$crate" && cargo test --test "$name" {mode} 2>&1) || rc=$?
    done
done"""


class HyperswitchImageBase(Image):
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
        return "base"

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

RUN printf 'Acquire::Check-Valid-Until "false";\\nAcquire::Retries "5";\\n' > /etc/apt/apt.conf.d/99no-check-valid-until

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential pkg-config libssl-dev libpq-dev zlib1g-dev \\
    && rm -rf /var/lib/apt/lists/*

ENV CARGO_TERM_COLOR=never \\
    CARGO_REGISTRIES_CRATES_IO_PROTOCOL=sparse \\
    CARGO_NET_RETRY=10

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class HyperswitchImageDefault(Image):
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
        return HyperswitchImageBase(self.pr, self.config)

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
bash /home/check_git_changes.sh
git checkout --detach {self.pr.base.sha}
bash /home/check_git_changes.sh

rustc --version
cargo --version

cargo fetch || true
grep -oE 'git[+]https://[^?#"]+[?]rev=[0-9a-f]{{40}}#[0-9a-f]{{40}}' Cargo.lock | sort -u | while IFS= read -r src; do
    url="${{src#git+}}"
    url="${{url%%[?]*}}"
    sha="${{src##*#}}"
    for db in "$CARGO_HOME"/git/db/"$(basename "$url")"-*; do
        [ -d "$db" ] || continue
        git -C "$db" fetch -q "$url" "$sha:refs/commit/$sha" || true
    done
done
cargo fetch || true
{_cargo_loop("--no-run")}
[ "$rc" -eq 0 ] || {{ echo "prepare.sh: a test target failed to compile at the base commit"; exit 1; }}
git checkout -- .
git clean -fdq
bash /home/check_git_changes.sh
""",
            ),
            File(
                ".",
                "run.sh",
                f"""\
#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}
export CI=true
{_cargo_loop("--no-fail-fast")}
exit $rc
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""\
#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}
export CI=true
git apply --3way --whitespace=nowarn /home/test.patch
{_cargo_loop("--no-fail-fast")}
exit $rc
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""\
#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}
export CI=true
git apply --3way --whitespace=nowarn /home/test.patch /home/fix.patch
{_cargo_loop("--no-fail-fast")}
exit $rc
""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError(
                "HyperswitchImageDefault dependency must be an Image"
            )
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT={self.pr.base.sha}
ENV BASE_COMMIT=${{BASE_COMMIT}}

{copy_commands}

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


@Instance.register("juspay", "hyperswitch")
class HYPERSWITCH(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return HyperswitchImageDefault(self.pr, self._config)

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
        buckets = {"ok": passed, "FAILED": failed, "ignored": skipped}

        crate = ""
        target = ""
        for raw in _ANSI_RE.sub("", test_log).splitlines():
            line = raw.strip()

            marker = _CRATE_RE.match(line)
            if marker:
                crate = marker.group(1)
                target = ""
                continue

            running = _TARGET_RE.match(line)
            if running:
                target = running.group(1)
                continue

            if _DOCTESTS_RE.match(line):
                target = ""
                continue

            result = _RESULT_RE.match(line)
            if not result:
                continue

            name, status = result.group(1), result.group(2)
            doc = _DOC_NAME_RE.match(name)
            if doc:
                path, name = doc.group(1), doc.group(2)
            else:
                path = target
            if crate and path:
                path = f"{crate}/{path}"
            buckets[status].add(f"{path}::{name}" if path else name)

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
