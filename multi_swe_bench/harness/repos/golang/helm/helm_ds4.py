"""helm/helm: 14 scraped PRs (2020-2026), one image per PR on the Go toolchain its base
go.mod declares.

Routing: these dataset records carry neither `number_interval` nor `tag`, so they resolve to the plain
`helm/helm` key. Upstream has no helm config, so the plain key is new here, and the router below accepts only the PR numbers in _PR_GO.

Per PR:
  * base image  golang:<major.minor> from the base commit's go.mod (`go` / `toolchain` line, highest),
    GOTOOLCHAIN=local so no toolchain download happens at test time. No apt step: the Debian images of
    the older Go lines point at archived mirrors.
  * prepare.sh  checks out the base commit, then applies test.patch + fix.patch only to download every
    module either patch needs and to warm the build cache, and resets to the clean base commit. The
    validation runs therefore never need the network.
  * tests       `go test -json -count=1`, package by package, on the packages that hold the test patch's
                *_test.go files; a package that does not compile reports each of its Test functions as
                failed (otherwise a compile failure under the test patch would show no failing test)
                (only those present at that point of the run: a package the fix creates exists only in
                fix-run). parse_log reads the JSON events: <package>::<Test/subtest>.
"""
import json
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_REPO_KEY = "helm"

# PR number -> Go line of its base commit (go.mod `go` / `toolchain`, whichever is higher).
_PR_GO = {
    7514: "1.13", 7972: "1.13", 8326: "1.13",
    8363: "1.14",
    10527: "1.17", 10546: "1.17",   # go.mod says 1.16, but their dependencies (oras-go, sigs.k8s.io/json) need 1.17
    12162: "1.19", 12274: "1.19",
    10677: "1.21",
    12769: "1.22",
    9175: "1.24", 31034: "1.24", 31239: "1.24", 31418: "1.24",
}


# Binary files in a patch arrive as "Binary files ... differ" with no content (compare API), so git apply
# skips them. For these PRs the binaries are new test fixtures; prepare.sh copies them out of the PR's
# merge commit into /home/binfiles/<test|fix>/ and the test-run / fix-run scripts drop them in place.
_MERGE_SHA = {
    7514: "9d7907342da3b51f0dcb9a539da1e1e57368f641",
    7972: "2139e82c68e160a713cf19eb3ac55e91aef21cde",
}


def _binaries(pr: PullRequest) -> list[tuple[str, str]]:
    out = []
    for kind, patch in (("test", pr.test_patch), ("fix", pr.fix_patch)):
        for _a, b in re.findall(r"^Binary files (\S+) and (\S+) differ$", patch, re.M):
            if b.startswith("b/"):
                out.append((kind, b[2:]))
    return out


def _restore(pr: PullRequest, *kinds: str) -> str:
    return "".join(f"cp -a /home/binfiles/{k}/. . 2>/dev/null || true\n" for k in kinds if any(x == k for x, _ in _binaries(pr)))


def _go_line(pr: PullRequest) -> str:
    return _PR_GO[pr.number]


def _minor(go_line: str) -> int:
    return int(go_line.split(".")[1])


def _test_dirs(pr: PullRequest) -> list[str]:
    dirs = set()
    for path in re.findall(r"^diff --git a/\S+ b/(\S+_test\.go)$", pr.test_patch, re.M):
        dirs.add(path.rsplit("/", 1)[0] if "/" in path else ".")
    return sorted(dirs)


def _binary_excludes(pr: PullRequest) -> str:
    paths = sorted(
        {
            p[2:]
            for patch in (pr.test_patch, pr.fix_patch)
            for pair in re.findall(r"^Binary files (\S+) and (\S+) differ$", patch, re.M)
            for p in pair
            if p.startswith(("a/", "b/"))
        }
    )
    return "".join(f" --exclude='{p}'" for p in paths)


class HelmDs4ImageBase(Image):
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
        return f"golang:{_go_line(self.pr)}"

    def image_tag(self) -> str:
        return f"base-ds4-go{_go_line(self.pr)}"

    def workdir(self) -> str:
        return f"base-ds4-go{_go_line(self.pr)}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"
        buildvcs = "\nENV GOFLAGS=-buildvcs=false" if _minor(_go_line(self.pr)) >= 18 else ""

        return f"""FROM {self.dependency()}

ARG REPO_URL=https://github.com/{self.pr.org}/{self.pr.repo}.git

{self.global_env}

ENV CI=true
ENV GOTOOLCHAIN=local{buildvcs}

RUN git config --global user.name "helm-test" \\
    && git config --global user.email "helm-test@example.com" \\
    && git config --global init.defaultBranch main \\
    && git config --global commit.gpgsign false \\
    && git config --global tag.gpgsign false

WORKDIR /home/

{code}

WORKDIR /home/{self.pr.repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class HelmDs4ImageDefault(Image):
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
        return HelmDs4ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _apply(self, *patches: str) -> str:
        return f"git apply --whitespace=nowarn{_binary_excludes(self.pr)} " + " ".join(patches)

    def _extract_binaries(self) -> str:
        bins = _binaries(self.pr)
        if not bins:
            return ""
        sha = _MERGE_SHA[self.pr.number]
        lines = [f"git cat-file -e {sha}^{{commit}} 2>/dev/null || git fetch --quiet https://github.com/{self.pr.org}/{self.pr.repo}.git {sha}"]
        for kind, path in bins:
            lines.append(f"mkdir -p \"/home/binfiles/{kind}/$(dirname '{path}')\" && git show {sha}:'{path}' > '/home/binfiles/{kind}/{path}'")
        return "\n".join(lines) + "\n"

    def _test_block(self) -> str:
        dirs = " ".join(f"'{d}'" for d in _test_dirs(self.pr))
        return f"""pkgs=()
for d in {dirs}; do
  if [ -d "$d" ] && ls "$d"/*.go >/dev/null 2>&1; then pkgs+=("./$d"); fi
done
if [ ${{#pkgs[@]}} -eq 0 ]; then echo "no test package present"; exit 1; fi
rc=0
for p in "${{pkgs[@]}}"; do
  out=$(go test -json -count=1 -timeout 40m "$p" 2>&1) || rc=1
  printf '%s\\n' "$out"
  # A package whose tests do not compile (e.g. the test patch calls code only the fix adds) reports
  # no test events at all. Report each of its Test functions as failed so the harness sees them.
  if ! grep -q '"Test":' <<<"$out"; then
    ip=$(go list -e -f '{{{{.ImportPath}}}}' "$p" 2>/dev/null)
    for t in $(grep -hoE '^func Test[A-Za-z0-9_]*\\(' "$p"/*_test.go 2>/dev/null | sed -E 's/^func (Test[A-Za-z0-9_]*)\\(/\\1/' | sort -u); do
      [ "$t" = TestMain ] && continue
      printf '{{"Action":"fail","Package":"%s","Test":"%s"}}\\n' "$ip" "$t"
    done
  fi
done
exit $rc
"""

    def files(self) -> list[File]:
        pr = self.pr
        dirs = " ".join(f"'./{d}'" for d in _test_dirs(pr))
        return [
            File(".", "fix.patch", f"{pr.fix_patch}"),
            File(".", "test.patch", f"{pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -euo pipefail
# Under QEMU (the amd64 half of a multi-arch build on an arm64 host) the Go toolchain can map memory
# above the emulated 47-bit limit and crash; an unlimited stack makes the kernel use the legacy
# (bottom-up) mmap layout for every child process. Harmless on native builds.
ulimit -s unlimited 2>/dev/null || true

cd /home/{pr.repo}
git reset --hard
git clean -fdx
# The base commit can sit on a branch a plain clone does not have (release branch, rewritten branch):
# fetch it by SHA from GitHub when it is missing.
git cat-file -e {pr.base.sha}^{{commit}} 2>/dev/null || git fetch --quiet https://github.com/{pr.org}/{pr.repo}.git {pr.base.sha}
git checkout --detach {pr.base.sha}

{self._extract_binaries()}
# Download every module the base, the test patch and the fix need, and warm the build cache,
# so the validation runs work offline. The tree is reset to the clean base commit afterwards.
go mod download
{self._apply('/home/test.patch', '/home/fix.patch')}
go mod download || true
go test -count=1 -run '^$' {dirs} > /dev/null 2>&1 || true
git reset --hard
git clean -fdx
go test -count=1 -run '^$' {dirs} > /dev/null 2>&1 || true
test "$(git rev-parse HEAD)" = "{pr.base.sha}"
test -z "$(git status --porcelain)"
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -o pipefail
cd /home/{pr.repo}
{self._test_block()}""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -o pipefail
cd /home/{pr.repo}
{self._apply('/home/test.patch')} || exit 1
{_restore(pr, 'test')}{self._test_block()}""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -o pipefail
cd /home/{pr.repo}
{self._apply('/home/test.patch', '/home/fix.patch')} || exit 1
{_restore(pr, 'test', 'fix')}{self._test_block()}""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copy_commands = "".join(f"COPY {file.name} /home/{file.name}\n" for file in self.files())

        return f"""FROM {image.image_name()}:{image.image_tag()}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class HELM_DS4(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return HelmDs4ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return parse_go_test_json(test_log)


def parse_go_test_json(test_log: str) -> TestResult:
    passed, failed, skipped = set(), set(), set()
    clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
    for line in clean.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        package, test, action = event.get("Package"), event.get("Test"), event.get("Action")
        if not package or not test:
            continue
        name = f"{package}::{test}"
        if action == "pass":
            passed.add(name)
        elif action == "fail":
            failed.add(name)
        elif action == "skip":
            skipped.add(name)
    passed -= failed
    skipped -= failed
    passed -= skipped
    return TestResult(
        passed_count=len(passed),
        failed_count=len(failed),
        skipped_count=len(skipped),
        passed_tests=passed,
        failed_tests=failed,
        skipped_tests=skipped,
    )


_PREVIOUS = Instance._registry.get(f"helm/{_REPO_KEY}")


@Instance.register("helm", _REPO_KEY)
class HelmDs4Router(Instance):
    """Plain helm/helm key: our 14 PRs only; anything else goes to a previous owner of the key."""

    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        if pr.number in _PR_GO:
            return HELM_DS4(pr, config, *args, **kwargs)
        if _PREVIOUS is not None:
            return _PREVIOUS(pr, config, *args, **kwargs)
        raise ValueError(f"helm/helm PR {pr.number} has no config (not one of the scraped PRs)")
