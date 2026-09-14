import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_WATCHDOG = re.compile(r"test (?:(?!\.\.\.).)+? has been running for over \d+ seconds\n")

_RUNNING_LINE = re.compile(
    r"^\s*Running\s+(?:unittests\s+)?(?P<target>\S+)\s+\(target/[^)]*\)\s*$"
)
_DOCTEST_LINE = re.compile(r"^\s*Doc-tests\s+(?P<crate>\S+)\s*$")
_RUNNING_COUNT = re.compile(r"^running \d+ tests?$")
_RESULT_LINE = re.compile(r"^test result:")

_TRYBUILD_LINE = re.compile(
    r"^test (?P<path>\S+) \[should (?:pass|fail to compile)\] \.\.\. (?P<status>\S+)"
)
_CASE_LINE = re.compile(r"^test (?P<name>.+?) \.\.\. (?P<status>\S+)")

_SHOULD_PANIC = re.compile(r" - should panic$")

_TRYBUILD_DRIVER = "tests/progress.rs::tests"


def parse_cargo_test_log(log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    clean = ANSI_ESCAPE.sub("", log)
    clean = _WATCHDOG.sub("", clean)

    current_target = ""
    pending_target = ""

    for raw in clean.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue

        m = _RUNNING_LINE.match(line)
        if m:
            pending_target = m.group("target")
            continue

        m = _DOCTEST_LINE.match(line)
        if m:
            pending_target = f"Doc-tests {m.group('crate')}"
            continue

        if _RUNNING_COUNT.match(line.strip()):
            if pending_target:
                current_target, pending_target = pending_target, ""
            continue

        if _RESULT_LINE.match(line):
            continue

        m = _TRYBUILD_LINE.match(line)
        if m:
            name = f"trybuild::{m.group('path')}"
            if m.group("status").lower() == "ok":
                passed_tests.add(name)
            else:
                failed_tests.add(name)
            continue

        m = _CASE_LINE.match(line)
        if m:
            leaf = _SHOULD_PANIC.sub("", m.group("name").strip())
            if not leaf:
                continue
            qualified = f"{current_target}::{leaf}" if current_target else leaf
            if qualified == _TRYBUILD_DRIVER:
                continue
            name = f"cargo-test::{qualified}"
            status = m.group("status").lower()
            if status == "ok":
                passed_tests.add(name)
            elif status == "ignored":
                skipped_tests.add(name)
            else:
                failed_tests.add(name)

    passed_tests -= failed_tests
    skipped_tests -= failed_tests
    passed_tests -= skipped_tests

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


_TEST_BODY = """\
export CARGO_TERM_COLOR=never
OUT=/tmp/suite.out
rm -f "$OUT"

set +e
cargo test --locked --no-fail-fast --lib -- --test-threads=1 >> "$OUT" 2>&1
cargo test --locked --no-fail-fast --test tests -- --test-threads=1 panic_tests:: >> "$OUT" 2>&1
cargo test --locked --no-fail-fast --doc -- --test-threads=1 >> "$OUT" 2>&1
cargo test --locked --no-fail-fast --test tests -- --test-threads=1 --exact tests --nocapture >> "$OUT" 2>&1
set -e

# parse_log reads stdout, so the captured output has to land there.
cat "$OUT"

grep -q "^test result:" "$OUT"
"""


class ModularBitfieldImageBase(Image):
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
        return "rust:1.90-bookworm"

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

        org = self.pr.org
        repo = self.pr.repo
        enh = DockerfileEnhancer

        code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'

        label_block = (
            f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
            f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        return f"""{enh.SYNTAX_DIRECTIVE}

FROM {image_name}

{enh._TARGETARCH_ARG}
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{enh._PROXY_ARGS}

{enh._ENV_BLOCK}

ENV CARGO_TERM_COLOR=never \\
    CARGO_TERM_PROGRESS_WHEN=never \\
    NO_COLOR=1

{label_block}

{enh._CERT_SYMLINKS}

{self.global_env}

WORKDIR /home/

{code}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class ModularBitfieldImageDefault(Image):
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
        return ModularBitfieldImageBase(self.pr, self._config)

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
                """#!/bin/bash
set -e
cd /home/{pr.repo}
if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "check_git_changes: /home/{pr.repo} is not a git repository" >&2
    exit 1
fi
if [ -n "$(git status --porcelain)" ]; then
    echo "check_git_changes: working tree is dirty:" >&2
    git status --porcelain >&2
    exit 1
fi
# `git status` can pass on stat-cache luck alone -- a file whose size and mtime
# are unchanged is never re-read. This forces a real content comparison against
# HEAD, so a build that quietly shipped a modified worktree fails here instead.
if ! git diff --quiet HEAD --; then
    echo "check_git_changes: tracked content differs from HEAD:" >&2
    git diff --stat HEAD -- >&2
    exit 1
fi
echo "check_git_changes: clean at $(git rev-parse HEAD)"
""".format(pr=self.pr),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

export CARGO_TERM_COLOR=never

# Resolve ONCE, here, while the network is still available.
#
# No Cargo.lock is committed -- it is gitignored (.gitignore:11) -- so without
# this every graded stage would resolve against crates.io independently and a
# release landing mid-run would be indistinguishable from the fix working.
# Writing the lock cannot dirty the tree for the asserts above or for the
# `git apply` in the run scripts, precisely because it is gitignored.
#
# No `|| true` anywhere below: this is the step that makes the graded stages
# deterministic and network-free, so it has to fail the BUILD if it fails, not
# leak a cold cache into the run.
cargo generate-lockfile
cargo fetch --locked

# Warm the build so the graded stages only recompile what a patch changed.
cargo test --locked --no-run

# Run the suite once as well. This is not redundant with the line above:
# trybuild compiles its cases at RUN time inside a nested cargo project under
# target/tests/trybuild/, which does not exist until the driver has executed
# once. Materialising it here is what lets the graded stages run with no network
# at all. At this point the test patch has NOT been applied, so the driver sees
# the base commit's 28 cases; case 29 is compiled by the graded stages, and the
# nested project plus the whole registry cache it needs already exist by then.
cargo test --locked --no-fail-fast --test tests -- --test-threads=1 --exact tests --nocapture > /tmp/warm.out 2>&1 || true
grep -q "^test result:" /tmp/warm.out

# The warm-up compiles into target/ (gitignored) and must not have touched a
# tracked file. `set -e` turns a failure here into a failed build rather than an
# image that silently ships a modified baseline.
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "{pr.base.sha}"
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
""".format(pr=self.pr)
                + _TEST_BODY,
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi
""".format(pr=self.pr)
                + _TEST_BODY,
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi
if ! git apply --whitespace=nowarn /home/fix.patch; then
    echo "Error: git apply fix.patch failed" >&2
    exit 1
fi
""".format(pr=self.pr)
                + _TEST_BODY,
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{Image._HARDENING_BLOCK}

{self.clear_env}

"""


@Instance.register("modular-bitfield", "modular_bitfield_29_to_25")
class ModularBitfield(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return ModularBitfieldImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        return parse_cargo_test_log(log)


_MB_ORG = "modular-bitfield"
_MB_REPO = "modular-bitfield"
_MB_INTERVAL = "modular_bitfield_29_to_25"
_MB_LO = 25
_MB_HI = 29
_MB_MODULE_PARTS = __name__.split(".")
_MB_LANG = (
    _MB_MODULE_PARTS[_MB_MODULE_PARTS.index("repos") + 1]
    if "repos" in _MB_MODULE_PARTS[:-1]
    else ""
)


def _mb_in_range(pr) -> bool:
    if getattr(pr, "org", "") != _MB_ORG or getattr(pr, "repo", "") != _MB_REPO:
        return False
    try:
        return _MB_LO <= int(getattr(pr, "number", None)) <= _MB_HI
    except (TypeError, ValueError):
        return False


def _mb_fill_routing_fields(pr):
    try:
        if _mb_in_range(pr):
            if not getattr(pr, "number_interval", ""):
                pr.number_interval = _MB_INTERVAL
            if not getattr(pr, "lang", "") and _MB_LANG:
                pr.lang = _MB_LANG
    except Exception:
        pass
    return pr


if not getattr(PullRequest, "_modular_bitfield_ni_shim", False):
    _mb_orig_from_json = PullRequest.from_json.__func__
    _mb_orig_from_dict = PullRequest.from_dict.__func__

    def _mb_from_json(cls, *args, **kwargs):
        return _mb_fill_routing_fields(_mb_orig_from_json(cls, *args, **kwargs))

    def _mb_from_dict(cls, *args, **kwargs):
        return _mb_fill_routing_fields(_mb_orig_from_dict(cls, *args, **kwargs))

    PullRequest.from_json = classmethod(_mb_from_json)
    PullRequest.from_dict = classmethod(_mb_from_dict)
    PullRequest._modular_bitfield_ni_shim = True


if not getattr(Instance, "_modular_bitfield_route_shim", False):
    _mb_orig_create = Instance.create.__func__

    def _mb_create(cls, pr, config, *args, **kwargs):
        try:
            return _mb_orig_create(cls, pr, config, *args, **kwargs)
        except ValueError:
            key = f"{_MB_ORG}/{_MB_INTERVAL}"
            if _mb_in_range(pr) and key in cls._registry:
                return cls._registry[key](pr, config, *args, **kwargs)
            raise

    Instance.create = classmethod(_mb_create)
    Instance._modular_bitfield_route_shim = True
