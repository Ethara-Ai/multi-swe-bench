
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_CLI_TESTS = "cli/tests/"
_INTEGRATION_RS = "cli/tests/integration_tests.rs"
_ADDED_TEST_RE = re.compile(
    r"^\+\s*#\[(?:tokio::)?test\]\s*\n(?:\+\s*#\[[^\]]*\]\s*\n)*"
    r"\+\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)",
    re.M,
)
_ADDED_ITEST_RE = re.compile(r"^\+\s*itest(?:_flaky)?!\((\w+)", re.M)
_UNIT_TEST_FN_RE = re.compile(r"^\+.*?\bfunction\s+(\w+)\s*\(", re.M)

# Per-PR p2p additions: existing integration tests that exercise the code the
# fix changes, chosen by hand when the test patch alone yields no p2p.
_P2P_TESTS: dict[int, list[str]] = {}

# Exact JS unit tests that replace the path-based plan when the test patch is
# mostly a formatter rewrite (every hunk would otherwise select its test).
_PINNED_UNIT: dict[int, list[str]] = {
    4498: ["urlNormalize"],
}


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)


def _edited_unit_ranges(block: str) -> list[str]:
    # Post-image line ranges ("start:end") of every hunk; resolved to the
    # enclosing unitTest at run time, against the actually patched file.
    ranges: list[str] = []
    for match in _HUNK_RE.finditer(block):
        start = int(match.group(1))
        count = int(match.group(2)) if match.group(2) is not None else 1
        ranges.append(f"{start}:{start + max(count, 1) - 1}")
    return ranges


def _patch_files(patch: str) -> list[tuple[str, bool, str]]:
    files: list[tuple[str, bool, str]] = []
    for block in re.split(r"(?=^diff --git )", patch or "", flags=re.M):
        match = re.match(r"diff --git a/(\S+) b/(\S+)", block)
        if match:
            files.append((match.group(2), "new file mode" in block[:400], block))
    return files


def _test_plan(pr: PullRequest) -> dict[str, list[str]]:
    patch = pr.test_patch or ""
    plan: dict[str, list[str]] = {
        "names": [],
        "fixtures": [],
        "modules": [],
        "targets": [],
        "unit": [],
        "unit_edits": [],
    }
    if pr.number in _PINNED_UNIT:
        plan["unit"] = list(_PINNED_UNIT[pr.number])
        return plan

    def add(key: str, item: str) -> None:
        if item and item not in plan[key]:
            plan[key].append(item)

    for name in _P2P_TESTS.get(pr.number, []):
        add("names", name)

    for path, is_new, block in _patch_files(patch):
        if path == _INTEGRATION_RS:
            for name in _ADDED_ITEST_RE.findall(block) + _ADDED_TEST_RE.findall(block):
                add("names", name)
            continue
        if path.startswith((_CLI_TESTS + "unit/", "cli/js/tests/")):
            if path.endswith("_test.ts"):
                for name in _UNIT_TEST_FN_RE.findall(block):
                    add("unit", name)
                ranges = ",".join(_edited_unit_ranges(block))
                if ranges:
                    add("unit_edits", f"{path}@{ranges}")
        elif path.startswith(_CLI_TESTS) and path.endswith(".rs") and path.count("/") == 2:
            add("targets", path[len(_CLI_TESTS) : -len(".rs")])
        elif path.startswith(_CLI_TESTS) and not path.endswith(".rs"):
            if not is_new:
                add("fixtures", path[len(_CLI_TESTS) :])
        elif path.startswith("cli/") and path.endswith(".rs"):
            module = path[len("cli/") : -len(".rs")].replace("/", "::")
            if module.endswith("::mod"):
                module = module[: -len("::mod")]
            add("modules", module)
    return plan


# Resolves plan names/fixtures to fully qualified integration test paths at
# run time (the test-patched integration_tests.rs is only on disk then).
# python2-compatible: ubuntu:18.04's `python` is 2.7.
_RESOLVE_SCRIPT = r"""python - "$@" <<'MSWBPLAN' > /tmp/mswb_sel.txt
import re, sys
src = open("cli/tests/integration_tests.rs").read()
mods = []
for m in re.finditer(r"^mod (\w+) \{", src, re.M):
    mods.append((m.start(), m.group(1)))
def qualify(pos, name):
    prefix = ""
    for start, mod in mods:
        if start < pos and mod != "util":
            prefix = mod + "::"
    return prefix + name
bodies = {}
for m in re.finditer(r"^\s*itest(?:_flaky)?!\((\w+)\s*\{(.*?)^\s*\}\);", src, re.M | re.S):
    bodies[m.group(1)] = (m.start(), m.group(2))
for m in re.finditer(
        r"#\[(?:tokio::)?test\]\s*(?:#\[[^\]]*\]\s*)*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)"
        r"\s*\([^)]*\)[^{]*\{(.*?)^\s{0,2}\}", src, re.M | re.S):
    bodies.setdefault(m.group(1), (m.start(), m.group(2)))
mode = sys.argv[1]
items = sys.argv[2:]
out = set()
for item in items:
    if mode == "names" and item in bodies:
        out.add(qualify(bodies[item][0], item))
    elif mode == "fixtures":
        needle = re.compile(r"[\"/ ]" + re.escape(item) + r"[\"\s,]")
        for name, (pos, body) in bodies.items():
            if needle.search(body):
                out.add(qualify(pos, name))
print("\n".join(sorted(out)))
MSWBPLAN"""


_UNIT_RESOLVE_SCRIPT = r"""python - "$@" <<'MSWBUNIT' >> /tmp/mswb_unit.txt
import re, sys
decl = re.compile(r"\bfunction\s+(\w+)\s*\(")
start_re = re.compile(r"^unitTest\(")
for arg in sys.argv[1:]:
    path, spans = arg.split("@", 1)
    try:
        lines = open(path).read().splitlines()
    except IOError:
        continue
    blocks = []
    current = None
    for i, line in enumerate(lines, 1):
        if start_re.match(line):
            if current:
                blocks.append(current)
            current = [i, i, None]
        if current is not None:
            current[1] = i
            if current[2] is None:
                m = decl.search(line)
                if m:
                    current[2] = m.group(1)
    if current:
        blocks.append(current)
    for span in spans.split(","):
        lo, hi = [int(x) for x in span.split(":")]
        for start, end, name in blocks:
            if name and start <= hi and lo <= end:
                print(name)
MSWBUNIT"""


def _test_command(pr: PullRequest) -> str:
    plan = _test_plan(pr)
    # A test patch that edits a Cargo.toml without the matching Cargo.lock
    # change cannot build under --locked; --offline still forbids new crates.
    edits_manifest = any(
        path.endswith("Cargo.toml") for path, _, _ in _patch_files(pr.test_patch or "")
    )
    lock_flag = "--offline" if edits_manifest else "--locked"
    cargo = f"cargo test {lock_flag} --no-fail-fast -p deno"
    lines = ["MSWB_RC=0", ": > /tmp/mswb_all.txt"]

    for mode in ("names", "fixtures"):
        if plan[mode]:
            args = " ".join(plan[mode])
            lines.append(f"set -- {mode} {args}\n{_RESOLVE_SCRIPT}")
            lines.append("cat /tmp/mswb_sel.txt >> /tmp/mswb_all.txt")

    if plan["names"] or plan["fixtures"]:
        lines.append(
            "sort -u /tmp/mswb_all.txt | sed '/^$/d' > /tmp/mswb_run.txt\n"
            'echo "MSWB_SELECTED $(wc -l < /tmp/mswb_run.txt) integration tests"\n'
            "while read -r t; do\n"
            f"    {cargo} --test integration_tests -- --exact --test-threads=1 \"$t\" 2>&1 || MSWB_RC=$?\n"
            "done < /tmp/mswb_run.txt"
        )

    for target in plan["targets"]:
        lines.append(f"{cargo} --test {target} -- --test-threads=1 2>&1 || MSWB_RC=$?")

    for module in plan["modules"]:
        lines.append(f"{cargo} --bin deno -- --test-threads=1 '{module}::' 2>&1 || MSWB_RC=$?")

    if plan["unit"] or plan["unit_edits"]:
        lines.append(f"cargo build {lock_flag} --bins 2>&1 || true")
        lines.append("printf '%s\\n' " + " ".join(plan["unit"] or ["''"]) + " > /tmp/mswb_unit.txt")
        if plan["unit_edits"]:
            edits = " ".join(plan["unit_edits"])
            lines.append(f"set -- {edits}\n{_UNIT_RESOLVE_SCRIPT}")
        lines.append(
            "sort -u /tmp/mswb_unit.txt | sed '/^$/d' > /tmp/mswb_unit_run.txt\n"
            'echo "MSWB_SELECTED $(wc -l < /tmp/mswb_unit_run.txt) js unit tests"\n'
            "MSWB_UNIT_RUNNER=cli/tests/unit/unit_test_runner.ts\n"
            "MSWB_UNSTABLE=--unstable\n"
            "if [ ! -f \"$MSWB_UNIT_RUNNER\" ]; then\n"
            "    MSWB_UNIT_RUNNER=cli/js/tests/unit_test_runner.ts\n"
            "    ./target/debug/deno run --help 2>&1 | grep -q -- '--unstable' || MSWB_UNSTABLE=\n"
            "fi\n"
            "while read -r name; do\n"
            "    ./target/debug/deno run $MSWB_UNSTABLE --reload -A "
            "\"$MSWB_UNIT_RUNNER\" --master --verbose -- \"$name\" 2>&1 || MSWB_RC=$?\n"
            "done < /tmp/mswb_unit_run.txt"
        )

    if len(lines) == 2:
        lines.append('echo "MSWB_NO_TESTS_SELECTED from test.patch" >&2\nexit 1')

    lines.append('exit "$MSWB_RC"')
    return "\n".join(lines)


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
        return "base-ubuntu1804"

    def workdir(self) -> str:
        return "base-ubuntu1804"

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
            f"FROM {image_name}",
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
            '      "https://chrome-infra-packages.appspot.com/dl/gn/gn/linux-$(dpkg --print-architecture)/+/latest" \\\n'
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
        toolchain_env = "unset V8_FROM_SOURCE\nunset GN\nunset NINJA"
        extra_setup = (
            "git submodule update --init --recursive --depth 1 "
            "|| git submodule update --init --recursive || true"
        )

        test_command = _test_command(self.pr)

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

rustup toolchain list | grep -q "^$mswb_rust_version-" \\
    || rustup toolchain install "$mswb_rust_version" --profile minimal
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


@Instance.register("denoland", "deno_8539_to_4757")
class DENO_8539_TO_4757(Instance):
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
