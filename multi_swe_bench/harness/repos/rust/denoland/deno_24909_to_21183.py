import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest



def _patch_paths(patch: str) -> list[str]:
    paths: list[str] = []
    for old, new in re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.MULTILINE).findall(patch or ""):
        for path in (new, old):
            if path not in paths:
                paths.append(path)
    return paths


def _apply_excludes(*patches: str) -> str:
    excludes: list[str] = []
    for patch in patches:
        for old, new in  re.compile(r"^Binary files (\S+) and (\S+) differ$", re.MULTILINE).findall(patch or ""):
            for path in (new, old):
                if path == "/dev/null":
                    continue
                path = re.sub(r"^[ab]/", "", path)
                if path not in excludes:
                    excludes.append(path)
    return "".join(f" --exclude='{path}'" for path in excludes)


def _test_plan(pr: PullRequest) -> dict:
    integration: list[str] = []
    js_unit: list[str] = []
    node_unit: list[str] = []
    specs: list[str] = []
    cli_unit: list[str] = []
    runner_modules: set[str] = set()
    node_compat = False

    def add(bucket: list[str], item: str) -> None:
        if any(item.startswith(existing) for existing in bucket):
            return
        bucket[:] = [existing for existing in bucket if not existing.startswith(item)]
        bucket.append(item)

    paths = _patch_paths(pr.test_patch)
    spec_manifest_dirs = [
        path.split("/specs/", 1)[1].rsplit("/", 1)[0]
        for path in paths
        if "/specs/" in f"/{path}" and path.endswith("/__test__.jsonc")
    ]

    for path in paths:
        if path.startswith("cli/tests/"):
            rel = path[len("cli/tests/") :].split("/")
        elif path.startswith("tests/"):
            rel = path[len("tests/") :].split("/")
        elif path.startswith("cli/") and path.endswith(".rs"):
            parts = path[len("cli/") : -len(".rs")].split("/")
            module = "::".join(parts[:-1]) or parts[-1]
            if module not in ("main", "build", "lib"):
                add(cli_unit, f"{module}::")
            continue
        else:
            continue

        name = rel[-1]
        if rel[0] == "integration" and len(rel) == 2 and name.endswith(".rs"):
            stem = name[: -len(".rs")]
            if stem in {"js_unit_tests", "node_unit_tests", "node_compat_tests"}:
                runner_modules.add(stem)
            elif stem != "mod":
                add(integration, f"{stem.removesuffix('_tests')}::")
        elif rel[0] == "unit" and re.search(r"_test\.[jt]s$", name):
            add(js_unit, f"js_unit_tests::{name.rsplit('.', 1)[0]}")
        elif rel[0] == "unit_node" and re.search(r"_test\.[jt]s$", name):
            add(node_unit, f"node_unit_tests::{name.rsplit('.', 1)[0]}")
        elif rel[0] == "specs" and len(rel) >= 3:
            spec_dir = "/".join(rel[1:-1])
            owners = [
                d for d in spec_manifest_dirs if f"{spec_dir}/".startswith(f"{d}/")
            ]
            test_dir = max(owners, key=len) if owners else "/".join(rel[1:-1][:2])
            add(specs, "specs::" + test_dir.replace("/", "::"))
        elif rel[0] == "node_compat" and len(rel) >= 2 and rel[1] != "runner":
            node_compat = True

    if "js_unit_tests" in runner_modules and not js_unit:
        add(integration, "js_unit_tests::")
    if "node_unit_tests" in runner_modules and not node_unit:
        add(integration, "node_unit_tests::")
    if "node_compat_tests" in runner_modules:
        node_compat = True

    integration = integration + js_unit + node_unit
    if not (integration or specs or cli_unit or node_compat):
        integration = [""]

    return {
        "integration": integration,
        "specs": specs,
        "cli_unit": cli_unit,
        "node_compat": node_compat,
    }


def _cargo_invocations(pr: PullRequest) -> list[tuple[str, str, list[str]]]:
    plan = _test_plan(pr)
    invocations: list[tuple[str, str, list[str]]] = []
    if plan["cli_unit"]:
        invocations.append(("cli_unit", "-p deno --bin deno", plan["cli_unit"]))
    if plan["integration"]:
        invocations.append(
            (
                "integration",
                "$MSWB_ITEST_PKG --test integration_tests",
                [f for f in plan["integration"] if f],
            )
        )
    for spec in plan["specs"]:
        invocations.append(("specs", "$MSWB_ITEST_PKG --test specs", [spec]))
    if plan["node_compat"]:
        invocations.append(
            ("node_compat", "$MSWB_ITEST_PKG --test node_compat_tests", [])
        )
    return invocations


_ENV_PREAMBLE = """\
export RUSTUP_HOME=/usr/local/rustup
export CARGO_HOME=/usr/local/cargo
export PATH=/usr/local/cargo/bin:$PATH
export CI=true
export NO_COLOR=1
export RUST_BACKTRACE=1
export CARGO_INCREMENTAL=0
export CARGO_TERM_COLOR=never
export CARGO_PROFILE_DEV_DEBUG=0
export CARGO_NET_GIT_FETCH_WITH_CLI=true

if grep -q '^name = "cli_tests"' tests/Cargo.toml 2>/dev/null; then
  MSWB_ITEST_PKG="-p cli_tests --features run"
else
  MSWB_ITEST_PKG="-p deno"
fi
"""


def _build_script(pr: PullRequest) -> str:
    lines = ["cargo build --locked --bins"]
    for _, target, _ in _cargo_invocations(pr):
        line = f"cargo test --locked --no-run {target}"
        if line not in lines:
            lines.append(line)
    return "\n".join(lines) + "\n"


def _test_script(pr: PullRequest) -> str:
    lines = ["mswb_status=0", "cargo build --locked --bins"]
    for group, target, filters in _cargo_invocations(pr):
        args = " ".join(f"'{f}'" for f in filters)
        lines.append(f'echo "::mswb::group::{group}"')
        lines.append(
            f"cargo test --locked --no-fail-fast {target} -- {args}".rstrip()
            + " || mswb_status=$?"
        )
    lines.append('echo "::mswb::group::end"')
    lines.append('exit "$mswb_status"')
    return "\n".join(lines) + "\n"


class Deno24909To21183ImageBase(Image):
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
        return "debian:bullseye-slim"

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

        infra = DockerfileEnhancer._infrastructure_block(self, image_name).rstrip("\n")

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {image_name}

{infra}

{self.global_env}

WORKDIR /home/
RUN printf 'deb http://snapshot.debian.org/archive/debian/20260901T000000Z bullseye main\\ndeb http://snapshot.debian.org/archive/debian-security/20260901T000000Z bullseye-security main\\n' > /etc/apt/sources.list \\
    && printf 'Acquire::Check-Valid-Until "false";\\nAcquire::Retries "5";\\n' > /etc/apt/apt.conf.d/99snapshot \\
    && apt-get update && apt-get install -y --no-install-recommends git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}


{self.clear_env}

CMD ["/bin/bash"]
"""


class Deno24909To21183ImageDefault(Image):
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
        return Deno24909To21183ImageBase(self.pr, self.config)

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
                """\
#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
git clean -fd
bash /home/check_git_changes.sh
git checkout --detach {base_sha}
bash /home/check_git_changes.sh

{env_preamble}
printf 'deb http://snapshot.debian.org/archive/debian/20260901T000000Z bullseye main\\ndeb http://snapshot.debian.org/archive/debian-security/20260901T000000Z bullseye-security main\\n' > /etc/apt/sources.list \\
    && printf 'Acquire::Check-Valid-Until "false";\\nAcquire::Retries "5";\\n' > /etc/apt/apt.conf.d/99snapshot \\
    && apt-get update && apt-get install -y --no-install-recommends \\
        build-essential ca-certificates curl file git gzip libc6-dev libxml2 lsb-release \\
        lld ninja-build nodejs npm pkg-config python2 python2-dev python3 tar unzip xz-utils zip \\
        protobuf-compiler cmake clang libglib2.0-dev \\
    && ln -sf /usr/bin/python2 /usr/bin/python \\
    && rm -rf /var/lib/apt/lists/*

if ! command -v rustup >/dev/null 2>&1; then
    curl -sSf https://sh.rustup.rs -o /tmp/rustup.sh \\
        && sh /tmp/rustup.sh -y --no-modify-path --default-toolchain none \\
        && rm /tmp/rustup.sh
fi
rustup --version && python --version

mswb_toolchain="$(sed -n 's/^channel *= *"\\(.*\\)"/\\1/p' rust-toolchain.toml | head -n1)"
if [ -z "$mswb_toolchain" ]; then
  echo "ERROR: could not resolve channel from rust-toolchain.toml" >&2
  exit 1
fi
rustup toolchain install "$mswb_toolchain" --profile minimal -c rustfmt -c clippy
cargo --version

for sm in {std_submodules}; do
  if git config -f .gitmodules --get-regexp '^submodule\\..*\\.path$' | awk '{{print $2}}' | grep -qx "$sm"; then
    git submodule update --init --recursive --no-recommend-shallow -- "$sm" \\
      || git submodule update --init --recursive -- "$sm"
  fi
done

cargo fetch --locked
{build_script}
test -x target/debug/deno
target/debug/deno --version
""".format(
                    repo=self.pr.repo,
                    base_sha=self.pr.base.sha,
                    env_preamble=_ENV_PREAMBLE,
                    std_submodules="tests/util/std test_util/std",
                    build_script=_build_script(self.pr),
                ),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}

{env_preamble}
{test_script}""".format(
                    repo=self.pr.repo,
                    env_preamble=_ENV_PREAMBLE,
                    test_script=_test_script(self.pr),
                ),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn{excludes} /home/test.patch

{env_preamble}
{test_script}""".format(
                    repo=self.pr.repo,
                    excludes=_apply_excludes(self.pr.test_patch),
                    env_preamble=_ENV_PREAMBLE,
                    test_script=_test_script(self.pr),
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn{excludes} /home/test.patch /home/fix.patch

{env_preamble}
{test_script}""".format(
                    repo=self.pr.repo,
                    excludes=_apply_excludes(self.pr.test_patch, self.pr.fix_patch),
                    env_preamble=_ENV_PREAMBLE,
                    test_script=_test_script(self.pr),
                ),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        name = base.image_name()
        tag = base.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        fetch_base = (
            'RUN git fetch --no-tags origin "${BASE_COMMIT}"\n'
            if self.pr.base.ref != "main"
            else ""
        )

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
{fetch_base}RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}
{self.clear_env}
"""


_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_GROUP = re.compile(r"^::mswb::group::(\S+)$")
_TEST_START = re.compile(r"^test (\S+) \.\.\.(.*)$")
_STATUS = re.compile(
    r"^(ok|FAILED|fail|ignored)(?:,.*)?(?:\s*\(\d+(?:\.\d+)?m?s\))?$"
)
_TIMING_ONLY = re.compile(r"^\(\d+(?:\.\d+)?m?s\)$")
_SUB_TEST = re.compile(r"^(\s+)(\S+) (ok|fail|ignored)$")
_SUB_CATEGORY = re.compile(r"^(\s+)(\S+)$")
_DENO_TEST = re.compile(r"^(\S.*?) \.\.\. (ok|FAILED|ignored) \(\d+(?:\.\d+)?m?s\)$")
_FAILURE_DUMP = re.compile(r"^---- .+ ----$")
_DUMP_END = re.compile(r"^(?:test result:|running \d+ tests?$)")


def parse_deno_cargo_log(log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    def record(name: str, status: str) -> None:
        if status == "ok":
            passed_tests.add(name)
        elif status in ("FAILED", "fail"):
            failed_tests.add(name)
        else:
            skipped_tests.add(name)

    group = ""
    in_dump = False
    pending: str | None = None
    parent: str | None = None
    categories: dict[int, str] = {}

    for raw in _ANSI.sub("", log).split("\n"):
        line = raw.rstrip()

        match = _GROUP.match(line.strip())
        if match:
            group, in_dump, pending, parent, categories = match.group(1), False, None, None, {}
            continue

        if in_dump:
            if _DUMP_END.match(line):
                in_dump = False
            continue
        if _FAILURE_DUMP.match(line):
            in_dump, pending, parent = True, None, None
            continue

        if parent is not None:
            match = _SUB_TEST.match(line)
            if match:
                indent = len(match.group(1))
                path = [categories[k] for k in sorted(categories) if k < indent]
                record("::".join([parent] + path + [match.group(2)]), match.group(3))
                continue
            match = _SUB_CATEGORY.match(line)
            if match and not line.strip().startswith("<"):
                indent = len(match.group(1))
                categories = {k: v for k, v in categories.items() if k < indent}
                categories[indent] = match.group(2)
                continue
            parent, categories = None, {}

        match = _TEST_START.match(line)
        if match:
            name, tail = match.group(1), match.group(2).strip()
            status = _STATUS.match(tail)
            if status:
                record(name, status.group(1))
                pending = None
            elif _TIMING_ONLY.match(tail):
                parent, categories, pending = name, {}, None
            else:
                pending = name
            continue

        if group == "node_compat":
            match = _DENO_TEST.match(line)
            if match:
                record(f"node_compat::{match.group(1)}", match.group(2))
                continue

        if pending is not None:
            status = _STATUS.match(line.strip())
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


@Instance.register("denoland", "deno_24909_to_21183")
class DENO_24909_TO_21183(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]: # type: ignore
        return Deno24909To21183ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, log: str) -> TestResult: # type: ignore
        return parse_deno_cargo_log(log)
