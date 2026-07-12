import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# hashicorp/terraform-provider-aws — the AWS Terraform provider (large Go repo).
#
# Discovery (GitHub API + dataset analysis):
#  - 189-PR range #12145..#46760. go.mod spans `go 1.20` -> `go 1.26`; a
#    recent Go toolchain with GOTOOLCHAIN=auto builds the whole range (Go is
#    backward compatible and auto-fetches a newer toolchain when a go.mod asks).
#  - Tests are Go's `go test`. `TestAcc*` are acceptance tests that create real
#    AWS resources and only run when TF_ACC=1 — the registry never sets it, so
#    they SKIP and the unit `Test*` functions run. A PR resolves on a unit
#    test going !PASS->PASS.
#  - Per-PR: the test_patch's `*_test.go` files identify the Go packages to
#    exercise; `go test` is run on each. Runs are fenced with a `### TFPKG ###`
#    marker so test ids stay unique across packages.


def _test_pkgs(patch: str) -> list[str]:
    """Go package directories owning the `*_test.go` files in a patch."""
    pkgs: set[str] = set()
    for line in (patch or "").splitlines():
        if not line.startswith("diff --git a/"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        path = parts[2][2:] if parts[2].startswith("a/") else parts[2]
        if path.endswith("_test.go"):
            pkgs.add(path.rsplit("/", 1)[0] if "/" in path else ".")
    return sorted(pkgs)


class TerraformProviderAwsImageBase(Image):
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
        return "golang:1-bookworm"

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
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
# Let Go auto-fetch whatever toolchain a given PR's go.mod requests.
ENV GOTOOLCHAIN=auto
ENV GOFLAGS=-mod=mod
WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git curl ca-certificates build-essential \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class TerraformProviderAwsImageDefault(Image):
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
        return TerraformProviderAwsImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        pkgs = _test_pkgs(self.pr.test_patch)
        pkg_list = " ".join(pkgs) if pkgs else "."

        check_git = """#!/bin/bash
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

        prepare = """#!/bin/bash
set -e
cd /home/__REPO__
git config --global --add safe.directory /home/__REPO__
git reset --hard
bash /home/check_git_changes.sh
git checkout __SHA__
bash /home/check_git_changes.sh

# Warm the Go module cache at the base commit (cached into the image layer).
go mod download 2>/dev/null || true
""".replace("__REPO__", repo).replace("__SHA__", sha)

        # Per-package `go test`. No TF_ACC -> TestAcc* skip, unit Test* run.
        # -vet=off matches the upstream Makefile and avoids vet-only failures.
        run_tests = """#!/bin/bash
set -uo pipefail
cd /home/__REPO__
go mod download 2>/dev/null || true

for pkg in __PKGS__; do
  [ -d "$pkg" ] || continue
  echo "### TFPKG: $pkg ###"
  go test -v -count=1 -vet=off -timeout=20m "./$pkg/" 2>&1 || true
done
""".replace("__REPO__", repo).replace("__PKGS__", pkg_list)

        run_sh = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
bash /home/run_tests.sh
""".replace("__REPO__", repo)

        excludes = (
            "--exclude=*.png --exclude=*.jpg --exclude=*.jpeg --exclude=*.gif "
            "--exclude=*.ico --exclude=*.svg --exclude=*.pdf --exclude=*.zip "
            "--exclude=*.gz --exclude=*.tar --exclude=*.woff --exclude=*.woff2"
        )

        test_run = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git apply --3way --whitespace=nowarn __EXCLUDES__ /home/test.patch \\
  || git apply --whitespace=nowarn --reject __EXCLUDES__ /home/test.patch \\
  || echo "git apply test.patch failed (continuing)"
bash /home/run_tests.sh
""".replace("__REPO__", repo).replace("__EXCLUDES__", excludes)

        fix_run = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git apply --3way --whitespace=nowarn __EXCLUDES__ /home/test.patch /home/fix.patch \\
  || git apply --whitespace=nowarn --reject __EXCLUDES__ /home/test.patch /home/fix.patch \\
  || echo "git apply test+fix patch failed (continuing)"
bash /home/run_tests.sh
""".replace("__REPO__", repo).replace("__EXCLUDES__", excludes)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", check_git),
            File(".", "prepare.sh", prepare),
            File(".", "run_tests.sh", run_tests),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("hashicorp", "terraform-provider-aws")
class TerraformProviderAws(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TerraformProviderAwsImageDefault(self.pr, self._config)

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
        # Strip ANSI escape sequences.
        ansi = re.compile(r"\x1B\[[0-9;]*[a-zA-Z]")
        clean = ansi.sub("", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # `go test -v` per-test result lines (possibly indented for subtests):
        #   --- PASS: TestFlattenFoo (0.00s)
        #   --- FAIL: TestExpandBar (0.01s)
        #   --- SKIP: TestAccBaz (0.00s)
        # Fenced by `### TFPKG: <pkg> ###` so ids stay unique across packages.
        res_re = re.compile(r"^\s*--- (PASS|FAIL|SKIP):\s+(\S+)")
        pkg_re = re.compile(r"^### TFPKG:\s+(\S+)\s+###")

        pkg = ""
        for line in clean.splitlines():
            line = line.rstrip()
            pm = pkg_re.match(line.strip())
            if pm:
                pkg = pm.group(1)
                continue
            m = res_re.match(line)
            if not m:
                continue
            status, name = m.group(1), m.group(2)
            tid = f"{pkg}::{name}" if pkg and pkg != "." else name
            if status == "PASS":
                passed_tests.add(tid)
            elif status == "FAIL":
                failed_tests.add(tid)
            elif status == "SKIP":
                skipped_tests.add(tid)

        # Disjoint sets: failed > skipped > passed.
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


# ============================================================================
# hashicorp/terraform-provider-azurerm — same terraform-provider family as the
# AWS provider above; reuses _test_pkgs() + imports. Reward-hack-clean shared
# base (tag "base-azurerm") + literal-sha hardening in the PR layer.
# ============================================================================

class TerraformProviderAzurermImageBase(Image):
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
        return "golang:1-bookworm"

    def image_tag(self) -> str:
        return "base-azurerm"

    def workdir(self) -> str:
        return "base-azurerm"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # Shared base for every azurerm PR (built once, tag "base-azurerm"). The
        # `# syntax` directive opts out of DockerfileEnhancer so this hand-written
        # layout is used verbatim: clone FULL history + light harden only. The
        # strict anti-reward-hack strip runs in the PR layer at each PR's base.sha.
        image_name = self.dependency()
        org = self.pr.org
        repo = self.pr.repo

        if self.config.need_clone:
            fetch = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            fetch = f"COPY {repo} /home/{repo}"

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    GOTOOLCHAIN=auto \\
    GOFLAGS=-mod=mod

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        ca-certificates curl build-essential git make \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'
{fetch}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

CMD ["/bin/bash"]
"""


class TerraformProviderAzurermImageDefault(Image):
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
        return TerraformProviderAzurermImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        pkgs = _test_pkgs(self.pr.test_patch)
        pkg_list = " ".join(pkgs) if pkgs else "."

        check_git = """#!/bin/bash
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

        prepare = """#!/bin/bash
set -e
cd /home/__REPO__
git config --global --add safe.directory /home/__REPO__
git reset --hard
bash /home/check_git_changes.sh
git checkout __SHA__
bash /home/check_git_changes.sh

# Warm the Go module cache at the base commit (cached into the image layer).
go mod download 2>/dev/null || true
""".replace("__REPO__", repo).replace("__SHA__", sha)

        # Per-package `go test`. No TF_ACC -> TestAcc* skip, unit Test* run.
        # -vet=off matches the upstream Makefile and avoids vet-only failures.
        run_tests = """#!/bin/bash
set -uo pipefail
cd /home/__REPO__
go mod download 2>/dev/null || true

for pkg in __PKGS__; do
  [ -d "$pkg" ] || continue
  echo "### TFPKG: $pkg ###"
  go test -v -count=1 -vet=off -timeout=30m "./$pkg/" 2>&1 || true
done
""".replace("__REPO__", repo).replace("__PKGS__", pkg_list)

        run_sh = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
bash /home/run_tests.sh
""".replace("__REPO__", repo)

        excludes = (
            "--exclude=*.png --exclude=*.jpg --exclude=*.jpeg --exclude=*.gif "
            "--exclude=*.ico --exclude=*.svg --exclude=*.pdf --exclude=*.zip "
            "--exclude=*.gz --exclude=*.tar --exclude=*.woff --exclude=*.woff2"
        )

        test_run = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git apply --3way --whitespace=nowarn __EXCLUDES__ /home/test.patch \\
  || git apply --whitespace=nowarn --reject __EXCLUDES__ /home/test.patch \\
  || echo "git apply test.patch failed (continuing)"
bash /home/run_tests.sh
""".replace("__REPO__", repo).replace("__EXCLUDES__", excludes)

        fix_run = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git apply --3way --whitespace=nowarn __EXCLUDES__ /home/test.patch /home/fix.patch \\
  || git apply --whitespace=nowarn --reject __EXCLUDES__ /home/test.patch /home/fix.patch \\
  || echo "git apply test+fix patch failed (continuing)"
bash /home/run_tests.sh
""".replace("__REPO__", repo).replace("__EXCLUDES__", excludes)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", check_git),
            File(".", "prepare.sh", prepare),
            File(".", "run_tests.sh", run_tests),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # Strict anti-reward-hack hardening in the PR layer: prepare.sh checked out
        # this PR's base.sha; the canonical block then detaches at that literal sha
        # and strips every other ref/reflog so the fix commit is unreachable.
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

"""


@Instance.register("hashicorp", "terraform-provider-azurerm")
class TerraformProviderAzurerm(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TerraformProviderAzurermImageDefault(self.pr, self._config)

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
        # Strip ANSI escape sequences.
        ansi = re.compile(r"\x1B\[[0-9;]*[a-zA-Z]")
        clean = ansi.sub("", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # `go test -v` per-test result lines (possibly indented for subtests):
        #   --- PASS: TestFlattenFoo (0.00s)
        #   --- FAIL: TestExpandBar (0.01s)
        #   --- SKIP: TestAccBaz (0.00s)
        # Fenced by `### TFPKG: <pkg> ###` so ids stay unique across packages.
        res_re = re.compile(r"^\s*--- (PASS|FAIL|SKIP):\s+(\S+)")
        pkg_re = re.compile(r"^### TFPKG:\s+(\S+)\s+###")

        pkg = ""
        for line in clean.splitlines():
            line = line.rstrip()
            pm = pkg_re.match(line.strip())
            if pm:
                pkg = pm.group(1)
                continue
            m = res_re.match(line)
            if not m:
                continue
            status, name = m.group(1), m.group(2)
            tid = f"{pkg}::{name}" if pkg and pkg != "." else name
            if status == "PASS":
                passed_tests.add(tid)
            elif status == "FAIL":
                failed_tests.add(tid)
            elif status == "SKIP":
                skipped_tests.add(tid)

        # Disjoint sets: failed > skipped > passed.
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


# --- bundle routing: register every number_interval so create() resolves
# f"hashicorp/{number_interval}" -> the azurerm Instance class (single-era).
_BUNDLE_NIS_AZURERM = [
    "18239-18240",
    "19998-20011",
    "23377-23889-24363-24435-24461-24475-24480-24481-24483-24485-24488-24492-24498-24521-24526-24535-24539-24543-24550",
    "26119-26136-26167-26173",
    "26521-26915-26929-27320-27372-27384-27493-27530-27532-27588-27596-27599-27606-27629-27700-27720-27721-27729-27731-27746-27762-27763-27764-27767-27769-27774-27785-27787-27790-27793-27794-27795-27797-27799-27800-27802-27811-27812-27813-27823-27829-27833-27834",
    "27363-27528-27680-27791-27859-27932-28307-28406-28419-28445-28446-28456-28465-28472-28474-28476-28484-28488-28491-28504-28505-28510-28516-28519-28525-28527",
    "27502-30243-30913-31265-31721-31898-31901-31957-32085-32159-32218-32225-32233-32243-32245-32246-32253-32254-32264-32266-32275",
    "27524-29886-30021-30035-30368-30394-30401-30438-30449-30464-30465-30475-30477",
    "28457-29426-30616-30765-30889-30907-30945-30964-31007-31012-31014-31015-31081-31085-31095-31117-31125-31126-31127-31130-31132",
    "29023-29513-29625-29631-29632-29634-29641-29646-29664-29665-29671-29676-29677-29683",
    "30020-30434-30542-30599-30600-30610-30614-30620-30621-30626-30627-30632-30635-30644-30647",
    "30060-30796-30817-30872-30882-30883-30891-30898-30901-30905-30914-30921-30924-30928-30931-30932",
    "30348-30439-30762-30823-30887-30904-30936-30937-30952-30962-30972-30978",
    "30983-31401-31570-31598-31805-31838-31862-31868-31878-31886-31896-31897-31899-31902-31905-31909-31911-31912-31914-31919-31930-31939",
    "31096-31402-31643-31653-31654-31679-31682-31736-31737-31760-31767-31768-31769-31770-31775-31780-31782-31783-31785-31792-31793-31796-31797-31798-31799-31802-31807-31811-31817-31826-31830-31831",
    "31203-31385-31566-31581-31724-31732-31733-31734-31735-31738-31740-31741-31742-31744-31745-31748-31749-31757-31758-31759-31762-31764",
    "31678-31821-31849-31853-31854-31855-31856-31870-31873-31877-31879-31890-31892",
    "31842-31846-31858-31866",
    "13899-14754-15264-15782-16208-16315-16595-16609-16758-16987-17008-17114-17116-17137-17226-17250-17361-17365-17416-17447-17448-17454-17477-17552-17616-17630-17632-17635-17651-17694-17696-17700-17712-17715-17756-17757-17766-17768-17770-17782-17790-17824-17842-17853-17856-17871-17873-17878-17895-17904-17917-17934-17939-17941-17954-17959-17960-17963-17964-17966-17973-17977-17978-17984-17985-17986-17993-17994-17995-17996-17998-18014-18015-18016-18027-18028-18035-18036",
    "14524-16527-16912-17100-17102-17122-17146-17151-17154-17196-17200-17201-17215-17216-17217-17218-17219-17223-17225-17234-17235-17237-17248-17258-17269-17270-17282-17289-17297-17299-17301-17312-17323-17326-17327-17328-17329-17330-17336-17337-17339-17340-17348",
    "14991-17536-19129-19268-19341-19345-19346-19385-19389-19402-19433-19470-19497-19530-19550-19568-19569-19572-19594-19599-19604-19606-19609-19612-19613-19623-19625-19626-19627-19634-19635-19636-19637-19638-19639-19640-19641-19644-19648-19656-19659-19660-19662-19663-19665-19668-19670-19671-19672-19673-19674-19677-19681-19688-19693-19696-19697-19702-19703",
    "15120-15967-15999-16367-16907-17313-17351-17441-17456-17538-17549-17571-17744-17767-17795-17798-17805-17823-17840-17864-17879-17881-17893-17926-17929-17962-17992-18005-18010-18042-18043-18091-18096-18098-18117-18120-18124-18125-18133-18136-18137-18138-18142-18145-18149-18151-18153-18154-18159-18160-18161-18163-18167-18168-18170-18174-18176-18180-18181-18183-18191-18193-18196-18201-18208-18211-18212-18214-18216-18217-18219",
    "15317-16657-16671-16741-18396-18436-18589-18590-18592-18601-18602-18604-18608-18619-18625-18627-18628-18629-18630-18631-18632-18633-18646-18647-18654",
    "15401-20567-20929-20977-21000-21032-21111-21112-21132-21145-21159-21176-21190-21204-21208-21211-21213-21215-21217-21219-21221-21222-21227-21228-21229-21234-21235-21237-21238-21239-21243-21245-21247-21248-21250-21252-21254-21256-21262-21263-21265-21268-21275-21276-21282-21285-21286-21312-21316-21317-21319",
    "15550-15758-16578-17089-17130-17256-17342-17368-17581-17595-17625-17628-17645-17647-17650-17653-17654-17658-17673-17679-17680-17697",
    "15621-15849-16368-16484-16731-16732-16775-16820-16966-17001-17045-17075-17076-17077-17084-17109-17209-17241-17243-17283-17284-17295-17296-17316-17322-17325-17334-17346-17354-17364-17366-17367-17369-17374-17381-17382-17383-17384-17385-17386-17387-17388-17390-17391-17392-17396-17398-17399-17400-17402-17407-17411-17414-17417-17418-17419-17424-17429-17430-17431-17432-17434-17435-17437-17438",
    "15644-16000-16581-16800-16810-16984-17111-17124-17230-17236-17238-17259-17332-17380-17395-17415-17436-17445-17457-17465-17466-17468-17469-17470-17472-17497-17499-17500",
    "15783-16299-17101-17338-17464-17523-17684-17695-17874-17880-17882-17888-17889-17894-17896-17898-17901-17905-17908-17920-17923-17932-17946-17947-17952-17958-17965",
    "15939-16437-17335-17489-17812-17846-17860-17876-17927-17948-18002-18004-18059-18115-18156-18158-18175-18190-18192-18207-18209-18229-18230-18233-18246-18247-18262-18264-18276-18278-18280-18282-18283-18285-18290-18291-18292-18295-18296-18299-18301-18303-18308",
    "16603-17274-17810-18251-18668-18680-18684-18709-18738-18749-18758-18759-18763-18765-18766-18770-18772-18774-18775-18777-18782-18783-18785-18790-18791-18792-18794-18797-18798-18799-18803-18804-18806-18808-18816-18817-18818-18822-18823-18824-18828-18831-18833-18836-18851-18856-18857-18858-18859-18860-18861-18862-18863-18867-18871-18872-18874-18876-18882-18884-18885-18890",
    "16652-16798-16983-17013-17018-17141-17231-17298-17300-17345-17394-17422-17427-17433-17463-17467-17471-17475-17485-17487-17490-17496-17498-17509-17519-17520-17521-17522-17524-17525-17526-17530-17535-17554-17556-17568-17570-17574-17580-17587-17588-17589-17590-17591-17594-17598-17599-17602-17606-17608-17609-17614-17615-17617-17629",
    "17006-17671-17902-18025-18404-18405-18407-18409-18414-18420-18423-18425-18427-18429-18430-18432-18435-18437-18438-18440-18441-18445-18446-18449-18467-18477-18478",
    "17104-17731-18037-18045-18109-18150-18166-18226-18306-18314-18318-18319-18320-18327-18329-18330-18331-18332-18336-18339-18340-18341-18342-18343-18348-18353-18354-18356-18357-18358-18359-18364-18369-18378-18379-18380-18385-18390-18394-18398-18400",
    "17166-17449-17785-17825-18213-18321-18473-18523-18525-18529-18689-18705-18729-18760-18761-18762-18789-18805-18832-18848-18850-18853-18898-18900-18903-18905-18906-18909-18910-18912-18916-18921-18923-18924-18926-18927-18928-18929-18931-18932-18934-18935-18936-18937-18943-18944-18945-18946-18949-18950-18951-18952-18953-18955-18957-18959-18960-18961-18962-18963-18964-18965-18966-18967-18968-18972-18976-18978-18983-18984-18986-18987-18989-18990-18991-18992-18993-18994-18996-18997-19007-19009-19011-19012-19014-19020-19021",
    "17194-17203-17547-17577-17668-17729-17734-17737-17776-17780-17793-17794-17796-17807-17819-17820-17822-17826-17830-17835-17836-17849-17854-17857-17861-17870-17887",
    "17245-19060-19092-19093-19123-19124-19126-19140-19141-19142-19150-19159-19161-19167-19169-19175-19178-19180-19181-19184-19186-19187-19189-19193-19197-19198-19199-19201-19206-19208-19210-19211-19223-19224-19228-19229-19230-19232-19233-19236-19239-19243-19245",
    "17714-17961-18130-18317-18431-18472-18491-18510-18586-18588-18600-18660-18665-18669-18670-18671-18672-18674-18692-18693-18694-18696-18699-18700-18702-18704-18706-18708-18710-18713-18716-18720-18722-18727-18728-18730-18735-18737-18740-18742-18744-18745-18746-18748-18750",
    "17765-17772-18041-18046-18055-18061-18070-18074-18081-18094-18100-18101-18106-18111-18118-18131",
    "17999-18056-18116-18231-18279-18335-18384-18392-18406-18419-18421-18428-18434-18442-18451-18457-18461-18469-18486-18490-18492-18504-18508-18511-18512-18516-18517-18520-18521-18522-18524-18526-18527-18530-18536-18538-18539-18542-18555-18556-18557-18559-18562-18564-18565-18566-18569-18570-18571-18572-18573",
    "18008-19934-20045-20102-20128-20198-20212-20229-20230-20231-20232-20236-20237-20246-20249-20253-20254-20268-20271-20272-20273-20274-20275-20281-20285-20287-20288-20290-20293-20295-20297-20298-20300-20302-20307-20309-20310-20311-20312-20313-20314-20315-20316-20317-20318-20335-20342-20345-20346-20348-20349-20352-20353-20364-20366-20367-20369-20375-20378-20379-20381-20391",
    "18162-19082-19162-19313-19334-19365-19368-19396-19472-19482-19493-19495-19499-19506-19507-19511-19512-19513-19514-19516-19519-19524-19525-19527-19528-19531-19537-19546-19547-19548-19552-19553-19555-19557-19559-19560-19564-19565-19566-19570-19573-19574-19576-19578-19580-19586-19601-19602-19616-19618-19620-19624",
    "18194-19261-19710-19939-20034-20054-20133-20137-20194-20195-20202-20204-20205-20206-20207-20208-20209-20214-20215-20218-20219-20221-20225-20234-20239-20244-20282",
    "18258-20667-20732-20736-20760-20873-20880-20881-20882-20884-20885-20890-20893-20894-20903-20904-20905-20906-20910-20911-20912-20918-20921-20926-20928-20932-20937-20948-20950-20955-20963-20965-20966-20970-20986",
    "18494-19165-19209-19309-19438-19504-19571-19592-19617-19621-19645-19679-19685-19754-19822-19825-19852-19860-19865-19866-19868-19873-19881-19882-19883-19884-19885-19886-19887-19890-19891-19895-19899-19900-19905-19906-19907-19910-19913-19914-19927-19929-19937-19941-19942-19947-19948-19950-19951-19954-19956-19957-19958-19959-19960-19961-19964-19969-19973-19974-19975-19978-19979-19981-19984-19986-19987-19988-19990-19993",
    "18568-19225-19285-19382-19391-19395-19399-19413-19422-19425-19428-19434-19441-19445-19446-19447-19452-19453-19458-19461-19462-19464-19465-19466-19468-19476-19477-19479-19483-19484-19485-19486-19487-19489-19492-19494-19518",
    "18813-20544-21034-21052-21059-21077-21106-21113-21114-21121-21124-21125-21128-21129-21134-21135-21138-21143-21146-21151-21152-21154-21155-21158-21160-21162-21163-21166-21170-21172-21174-21175-21178-21189-21191-21193-21194-21199-21200-21203-21205",
    "18918-19248-19269-19298-19303-19325-19335-19336-19338-19340-19344-19352-19354-19356-19357-19360-19362-19363-19371-19378-19380-19384-19386-19387-19390-19392-19400-19403-19412-19415-19418-19419-19429",
    "19036-19498-19708-19712-19731-19759-19940-19992-20073-20086-20092-20097-20103-20111-20114-20116-20130-20134-20139-20144-20145-20154-20155-20158-20160-20165-20168-20172-20180-20184-20185-20191",
    "19062-19551-19567-19610-19692-19845-19893-19909-19977-19997-20000-20001-20002-20003-20010-20012-20015-20022-20023-20025-20027-20028-20029-20030-20031-20040-20042-20044-20046-20047-20048-20051-20058-20060-20061-20062-20063-20068-20075-20077-20081-20083-20088-20090-20096-20099-20101-20107-20109-20110-20113-20129",
    "19083-19163-19164-19222-19227-19246-19247-19249-19251-19254-19259-19263-19264-19265-19266-19267-19270-19271-19273-19274-19284-19286-19288-19290-19295-19297-19304-19305-19306-19308-19314-19319-19320-19337-19342-19348-19350",
    "19113-20131-20132-20338-20368-20416-20472-20524-20574-20599-20611-20613-20618-20638-20641-20642-20649-20655-20658-20662-20665-20669-20670-20679-20680-20681-20685-20688-20689-20698-20699-20703-20706-20708-20710-20711-20715-20722-20724-20726-20729-20733-20734-20738-20739-20746-20752-20755-20758",
    "19312-19436-19526-19669-19690-19699-19704-19707-19713-19714-19715-19716-19722-19723-19732-19733-19736-19743-19753-19755-19756-19760-19762",
    "19423-19593-19628-19675-19676-19689-19698-19719-19763-19768-19771-19772-19773-19774-19775-19780-19786-19787-19791-19792-19794-19800-19801-19804-19811-19812-19816-19820-19821-19823-19824-19826-19827-19829-19830-19831-19832-19833-19834-19835-19836-19837-19838-19839-19840-19849-19851-19864-19871-19872-19875-19878-19880",
    "19608-20203-20324-20449-20454-20473-20474-20512-20513-20516-20541-20543-20548-20555-20558-20560-20562-20563-20565-20566-20568-20570-20576-20577-20578-20579-20580-20583-20584-20585-20593-20595-20605-20609-20610-20619-20620-20622-20624-20626-20630-20632-20636",
    "19661-20855-21437-21529-21682-21764-21935-21954-21956-21962-21969-21979-21981-22015-22017-22019-22031-22035-22036-22037-22038-22040-22045-22047-22049-22052-22053-22054-22056-22057-22062-22063-22065-22066-22067-22072-22081-22083-22085-22091-22093",
    "19933-20033-20211-20233-20247-20320-20333-20334-20336-20337-20339-20341-20361-20362-20365-20370-20371-20383-20384-20387-20390-20399-20403-20405-20406-20407-20411-20417-20418-20419-20420-20423-20424-20426-20428-20429-20430-20431-20432-20434-20443-20444-20445-20446-20448-20450-20452-20456-20457-20458-20462-20465-20466-20468-20469-20476-20479-20480-20481-20484-20486-20491-20494-20495-20498-20499-20501-20504-20505-20510",
    "19936-22709-22804-22871-22906-22932-22942-22951-23003-23019-23021-23022-23029-23042-23048-23049-23069-23072-23079-23080-23081-23082-23087-23088-23089-23093-23096-23102-23104-23105-23106-23110-23124-23126-23128-23135-23140-23146-23151-23153-23154-23155-23156-23160-23171-23178-23179-23185-23189-23191-23199-23200-23203-23205-23206-23209",
    "20049-22945-23871-24157-24346-24914-25393-25467-25480-25530-25686-25716-25743-25746-25778-25779-25785-25787-25790-25797-25805-25807-25814-25816-25823-25840-25847-25850-25853-25854-25861-25862-25869-25870-25871-25872-25873-25874-25876-25877-25878-25879-25884-25886-25890-25903-25909-25916-25923",
    "20082-20607-20661-20666-20686-20728-20730-20771-20796-20840-20886-20895-20927-20934-20947-20949-20951-20952-20954-20956-20971-20974-20975-20979-20987-20999-21001-21004-21007-21009-21010-21013-21016-21017-21026-21028-21029-21031-21033-21037-21038-21041-21042-21046-21050-21057-21058-21060-21062-21063-21065-21068-21070-21076-21079-21083-21084-21089-21091-21095-21100-21104",
    "20235-22953-23387-23546-23601-23625-23627-23629-23634-23635-23645-23646-23647-23648-23652-23654-23659-23660-23665-23677-23684-23691-23696-23697-23698-23700",
    "20286-21054-21310-21323-21432-21471-21477-21481-21502-21503-21513-21514-21515-21516-21519-21524-21526-21528-21531-21532-21533-21536-21542-21544-21546-21547-21555-21580-21582-21583",
    "20356-21212-21328-22452-22833-22979-23011-23062-23108-23127-23147-23149-23161-23204-23217-23239-23260-23261-23262-23263-23264-23271-23272-23274-23276-23277-23279-23288-23293-23294-23296-23298-23300-23301-23302-23303-23306-23307-23308-23310-23313-23320-23327-23330-23331-23332-23333-23335-23337-23338-23344",
    "20451-21397-21530-21595-21596-21597-21598-21621-21645-21647-21654-21656-21658-21659-21660-21661-21665-21667-21670-21672-21680-21684-21685-21695-21697-21698-21704-21707-21713-21715-21716-21718-21720-21721-21725-21726-21728-21729-21730-21731-21732-21734-21735-21738-21740-21746-21753-21755-21759",
    "20471-20924-20972-21273-21279-21303-21307-21321-21325-21327-21342-21387-21405-21413-21420-21421-21422-21423-21430-21433-21436-21438-21446-21449-21451-21456-21458-21459-21462-21465-21469-21474-21475-21482-21484-21488-21491-21494-21495-21501",
    "20517-20518-20523-20526-20533-20536-20539",
    "20519-22332-23220-23412-23463-23478-23517-23619-23679-23818-23969-23970-23971-23984-23985-24003-24054-24061-24076-24083-24085-24088-24089-24095-24098-24108-24115-24116-24122-24124-24125-24127-24128-24129-24130-24139-24140-24142-24149",
    "20603-20628-20751-20757-20761-20762-20765-20766-20768-20781-20782-20788-20789-20790-20797-20798-20799-20801-20807-20808-20809-20810-20813-20815-20816-20819-20821-20822-20824-20825-20826-20830-20837-20839-20841-20845-20854-20856-20864-20870-20871-20872-20874-20875-20877",
    "20608-21053-21579-21961-21980-21999-22016-22021-22048-22051-22094-22095-22097-22098-22100-22102-22103-22110-22112-22114-22118-22121-22123-22129-22134-22137",
    "20627-21541-21569-22519-22542-22597-22643-22662-22663-22665-22680-22689-22700-22707-22708-22733-22782-22800-22807-22809-22811-22812-22813-22814-22815-22816-22824-22826-22828-22832-22835-22836-22837-22839-22840-22841-22844-22845-22848-22850-22851-22860-22865-22867-22868-22872-22873-22875-22888-22892-22895-22897-22898-22899",
    "20643-20663-20764-21255-21270-21311-21314-21315-21329-21330-21331-21332-21335-21336-21337-21344-21347-21349-21359-21362-21364-21370-21371-21372-21374-21379-21381-21391-21392-21393-21394-21403-21412",
    "20668-21055-21071-21153-21367-21389-21400-21404-21434-21549-21666-21668-21688-21712-21745-21751-21754-21779-21780-21782-21786-21788-21789-21810-21813-21835-21836-21837-21838-21849-21863-21865-21867-21869-21871-21873-21885-21887-21893-21898-21899-21905-21907-21908-21909-21913-21914-21915-21916-21917-21919-21923-21924-21926",
    "20731-22216-22223-22229-22241-22295-22314-22317-22331-22335-22336-22342-22344-22347-22350-22351-22352-22373-22375-22383-22386-22387-22388",
    "21226-21834-23324-23403-23955-23965-24015-24028-24072-24075-24077-24078-24101-24143-24153-24156-24159-24164-24169-24176-24177-24178-24179-24185-24192-24194-24202-24204-24205-24207-24208-24210-24211-24214-24216-24217-24219-24221-24222-24224-24226-24228-24230-24231-24233-24236-24237-24238-24239-24240-24241-24245-24246",
    "21322-22199-22221-22400-22455-22522-22615-22628-22644-22673-22676-22687-22690-22710-22711-22718-22720-22721-22722-22723-22725-22726-22727-22729-22731-22741-22742-22743-22745-22748-22749-22750-22752-22754-22756-22758-22759-22769-22776-22778-22779-22781-22788-22790-22795-22798-22803",
    "21377-21479-21490-21676-21694-21719-21750-21796-21910-21911-21927-21934-21941-21942-21943-21944-21945-21946-21948-21949-21953-21955-21959-21964-21966-21974-21976-21982-21987-21989-21992-21994-21996-22006-22007",
    "21410-22783-22808-22834-22857-22874-22891-22893-22907-22912-22913-22914-22915-22916-22925-22926-22928-22929-22930-22935-22940-22943-22946-22947-22955-22957-22959-22962-22966-22967-22972-22981-22982-22983-22986-22994",
    "21511-21566-21575-21588-21594-21599-21600-21606-21612-21620-21623-21624-21625-21631-21636-21637-21649-21657",
    "21571-21664-22128-22312-22356-22390-22393-22396-22399-22403-22404-22405-22409-22410-22411-22412-22413-22414-22415-22416-22418-22419-22420-22421-22422-22423-22426-22428-22433-22437-22438-22442-22443-22451-22453-22454-22456-22457-22463-22469-22470-22480-22482-22483-22486-22490-22491-22496-22499-22504-22508-22511-22512",
    "21614-21938-21958-22119-22136-22139-22144-22145-22147-22148-22149-22153-22154-22157-22165-22166-22167-22170-22176-22177-22179-22182-22184-22187-22190-22192-22196-22197-22200-22202-22203-22207-22209-22227-22231-22232-22246",
    "21677-21681-21683-21691-21693-21709-21768-21770-21771-21784-21787-21792-21793-21795-21799-21800-21804-21806-21811-21814-21828-21850",
    "21822-22517-22863-22997-23098-23114-23251-23299-23329-23342-23345-23347-23348-23354-23357-23360-23362-23368-23384-23389-23410-23419",
    "21928-21940-22168-22309-22334-22348-22425-22440-22497-22502-22503-22521-22524-22525-22528-22531-22532-22535-22536-22538-22541-22553-22554-22555-22557-22566-22568-22571-22574-22575-22577-22578-22580-22587-22591-22596-22609-22610-22612-22613-22614-22616-22620-22625",
    "21965-22478-22579-22952-22989-22991-23005-23006-23007-23008-23018-23024-23033-23034-23035-23037-23040-23044-23045-23046-23052-23054-23057-23058-23059-23060-23061-23066-23073-23075-23076-23077",
    "22164-25429-25449-25451-25763-26004-26179-26184-26185-26225-26228-26229-26232-26235-26249-26310-26320-26329-26330-26331-26332-26333-26336-26337-26339-26349-26352-26353-26354-26355-26356-26358-26359-26366-26367-26368-26372-26383-26397-26399",
    "22215-22274-22349-22398-22434-22449-22479-22552-22627-22631-22632-22638-22642-22646-22647-22654-22655-22661-22664-22671-22672-22677-22681-22682-22683-22684-22685-22686-22688-22692-22698-22706-22712",
    "22250-22477-23129-24011-24181-24291-24328-24334-24459-24473-24496-24507-24571-24602-24615-24627-24636-24638-24640-24642-24643-24644-24645-24646-24647-24648-24649-24650-24652-24653-24654-24657-24658-24659-24663-24664-24667-24668-24669-24672-24674-24675-24687-24688-24689-24690-24691-24694-24695-24696-24697-24698-24699-24700-24702-24703-24704-24706-24709-24714-24720-24723-24725-24726-24728-24732-24734-24735-24738-24740-24745",
    "22520-23612-23641-23718-23756-23757-23758-23769-23770-23771-23780-23783-23785-23787-23788-23790-23794-23795-23803-23810-23812-23819-23820-23822-23829-23832-23836-23837-23839-23843",
    "22583-24802-24848-24950-25166-25181-25187-25193-25213-25217-25240-25242-25247-25251-25262-25268-25271-25272-25279-25280-25281-25282-25283-25285-25290-25293-25295-25296-25299-25301-25302-25304-25305-25307-25310-25312-25313-25316-25319-25320-25322-25327-25332-25338-25340-25342-25345-25346-25347-25350-25355-25358-25361-25362",
    "22774-23065-23119-23122-23138-23158-23163-23181-23214-23216-23219-23221-23230-23233-23235-23236-23241-23249-23254-23256-23259",
    "22802-23380-23383-23539-23574-23585-23586-23682-23689-23692-23695-23704-23708-23709-23716-23728-23736-23737-23741-23744-23746-23749-23750-23751-23752-23753-23754",
    "22806-23420-23799-23893-23976-23987-23996-24002-24008-24012-24013-24014-24016-24017-24019-24023-24024-24025-24026-24027-24029-24030-24031-24032-24033-24035-24039-24043-24053-24056-24063-24070-24074",
    "22975-23823-23935-24278-24370-24497-24595-24676-24755-24768-24775-24833-24868-24892-24905-24906-24909-24912-24913-24917-24918-24921-24922-24923-24924-24925-24938-24939-24944-24951-24952-24954-24955-24962-24964-24967-24971-24973-24974-24977-24981",
    "22976-23370-23411-23491-23513-23538-23541-23543-23544-23545-23547-23548-23554-23555-23558-23559-23563-23564-23565-23566-23568-23570-23575-23576-23581-23583-23588-23594-23596-23598-23599-23605-23615-23618",
    "23031-23500-23733-24141-24166-24191-24264-24375-24458-24470-24500-24509-24518-24540-24541-24542-24553-24554-24555-24556-24557-24558-24561-24562-24563-24564-24570-24572-24574-24575-24577-24578-24579-24580-24581-24582-24588-24592-24593-24598-24599-24600-24603-24605-24606-24613-24614-24619-24626-24628-24633",
    "23107-23242-23318-23382-23415-23421-23428-23429-23444-23476-23483-23484-23494-23498-23499-23502-23514-23518-23520-23521-23524-23529-23530-23533-23534-23535-23549-23551-23552-23553",
    "23132-23760-24266-24329-24513-24569-24573-24716-24718-24737-24748-24749-24750-24751-24756-24760-24761-24766-24769-24774-24779-24783-24789-24791-24792-24794-24800-24806-24815-24819-24827-24828",
    "23372-23628-23638-23650-23681-23721-23781-23796-23797-23816-23821-23860-23862-23864-23887-23888-23904-23908-23918-23921-23928-23929-23932-23933-23934-23936-23938-23939-23941-23943-23945-23959-23964-23966-23973-23974-23993-24005-24007-24009",
    "23373-25200-25343-25389-25405-25450-25488-25489-25526-25533-25535-25541-25543-25555-25581-25592-25594-25596-25598-25599-25609-25610-25620-25624-25627-25628-25629-25631-25636-25639-25642-25643-25644-25647-25650-25652-25653-25659-25664-25672",
    "23394-24091-24514-24524-24813-24867-24904-24919-24937-24956-24966-24976-24978-24979-24987-24993-24996-24997-25000-25002-25003-25006-25008-25009-25011-25013-25014-25017-25019-25021-25027-25032-25034-25036-25038-25040-25055-25071-25079-25081-25082-25089-25092",
    "23644-23838-23849-23854-23859-23863-23866-23872-23873-23874-23875-23884-23886-23890-23892-23897-23900-23922",
    "23761-24102-24331-24384-24421-24527-24940-24943-24975-24994-25010-25016-25033-25035-25046-25065-25068-25074-25093-25094-25095-25102-25108-25111-25117-25121-25127-25128-25129-25130-25131-25132-25134-25137-25139-25140-25144-25145-25154-25155-25157-25170-25172-25173-25174-25175-25180-25189",
    "23793-25022-25354-25356-25611-25655-25677-25680-25681-25682-25687-25690-25694-25696-25699-25702-25708-25709-25711-25712-25714-25717-25725-25735-25749",
    "23806-23980-24107-24167-24180-24229-24243-24248-24250-24251-24254-24255-24256-24257-24262-24263-24265-24272-24273-24274-24280-24281-24288-24289-24296-24301-24306-24307-24312-24314-24315-24320-24321-24322-24324-24326-24327-24332-24333-24335-24340-24341-24343-24347-24350-24354-24360-24365-24366-24381-24382",
    "23811-24258-24261-24292-24303-24361-24369-24515-24717-24771-24829-24830-24832-24835-24837-24840-24841-24846-24849-24851-24857-24858-24861-24862-24871-24872-24875-24877-24879-24880-24886-24888-24889-24895-24900-24903",
    "23870-24042-24066-24100-24145-24270-24271-24290-24299-24330-24367-24368-24374-24376-24383-24389-24393-24395-24397-24398-24399-24406-24407-24409-24412-24413-24414-24416-24417-24418-24419-24423-24424-24426-24427-24428-24431-24439-24440-24443-24446-24448-24453-24456-24457-24460-24463-24474-24477-24478-24479-24486",
    "23911-25088-25147-25568-25601-25710-25723-25732-25742-25745-25758-25759-25783-25793-25804-25825-25831",
    "24073-24733-24798-25031-25103-25178-25186-25191-25192-25197-25198-25203-25208-25210-25211-25212-25214-25226-25227-25235-25243-25250-25255",
    "24267-26008-26223-26254-26432-26528-26531-26708-26800-26847-26850-26863-26878-26892-26894-26922-26928-26936-26940-26941-26949-26953-26968-26973-26976-26978-26981-26982-26983-26985-26986-26991-26992-26993-26996-26999-27001-27007-27011-27017-27019-27021-27022-27027-27029-27030-27034-27035-27045-27049-27055-27059-27060-27063-27066",
    "24276-25246-25363-25366-25367",
    "24342-24773-25091-25110-25323-25325-25331-25360-25365-25368-25369-25371-25376-25379-25384-25385-25388-25391-25398-25400-25403-25404-25406-25407-25411-25416-25418-25425-25428-25430-25436-25437-25439-25446-25465-25471-25472-25473-25475-25477-25482-25484-25486-25498-25500-25503-25508-25509-25515",
    "24520-25408-25421-25427-25510-25516-25518-25520-25523-25525-25534-25536-25539-25540-25544-25546-25549-25551-25552-25553-25554-25556-25557-25559-25567-25570-25571-25573-25578",
    "24670-25529-26082-26160-26263-26292-26360-26361-26364-26365-26376-26380-26382-26384-26400-26401-26402-26406-26411-26416-26419-26420-26421-26424-26427-26436-26437-26440-26441-26442-26444-26445-26447-26448-26452-26456-26460-26462-26466-26467-26471-26475-26480-26484-26485-26487-26490-26496-26497",
    "24801-25695-27122-27375-27401-27830-27874-27876-27894-27931-27985-28005-28069-28131-28161-28215-28216-28233-28243-28269-28275-28276-28278-28279-28280-28281-28282-28283-28285-28286-28290-28299-28300-28311-28312-28319-28322-28324-28332-28335-28339-28340-28341-28345-28346-28348-28349-28351-28352-28353-28360-28367-28370-28373-28379-28380-28384-28386-28387-28388-28390-28395-28398-28404-28415-28425-28427-28430-28442-28443-28444-28463-28467",
    "24968-25634-25663-25812-25838-25885-25996-26012-26017-26029-26030-26036-26049-26053-26055-26058-26059-26060-26069-26073-26074-26077-26083-26084",
    "25168-25259-25773-25969-25970-26024-26117-26150-26163-26168-26174-26175-26181-26182-26188-26189-26191-26196-26197-26198-26199-26204-26205-26206-26207-26212-26216-26217-26221-26231-26242-26246",
    "25339-25537-25660-25731-25809-25910-25955-25979-25986-25992-25993-26003-26031-26034-26037-26046-26052-26054-26063-26065-26066-26068-26070-26075-26085-26087-26090-26097-26099-26102-26103-26105-26106-26107-26108-26109-26111-26112-26113-26114-26115-26116-26126-26130-26132-26134-26135-26137-26140-26141-26148-26149-26161-26162-26166",
    "25412-26015-27189-27232-27529-27630-27682-27734-27758-27816-27828-27935-27954-27976-27982-27983-27986-27987-27993-28011-28021-28026-28029-28030-28031-28033-28039-28041-28042-28045-28046-28056-28057-28071-28075-28083",
    "25531-25900-26006-26165-26176-26237-26248-26251-26255-26264-26266-26270-26274-26275-26277-26281-26282-26283-26284-26286-26293-26294-26302-26305-26308-26309-26317",
    "25574-27135-27389-27440-27509-27511-27515-27551-27632-27659-27692-27694-27696-27698-27699-27703-27706-27707-27709-27711-27712-27713-27714-27718-27735-27737-27745",
    "25646-25972-26177-26345-26370-26430-26509-26955-26994-27054-27196-27205-27211-27288-27306-27315-27345-27346-27352-27357-27365-27366-27369-27370-27376-27377-27379-27380-27381-27394-27395-27396-27397-27402-27404-27408-27409-27411-27412-27416-27417-27419-27420-27421-27422-27425-27426-27427-27434",
    "25688-25719-25777-25844-25882-25888-25905-25919-25931-25932-25935-25939-25940-25941-25947-25949-25953-25956-25959-25968-25971-25976-25980-25984-25985-25989-25997-26000",
    "25715-26227-26525-26599-26640-26699-26701-26747-26758-26775-26867-26898-26899-26903-26904-26913-26918-26924-26932-26945-26947-26950-26954-26957-26960-26962-26965-26972",
    "25753-26057-26393-26417-26473-26477-26546-26608-26610-26614-26624-26627-26650-26664-26666-26673-26678-26679-26683-26686-26689-26690-26692-26693-26694-26696-26697-26698-26700-26709-26711-26714-26715-26718-26719-26720-26722-26723-26724-26725-26726-26728-26732-26734-26735-26736-26737-26738-26740-26742-26743-26744-26745-26749-26750-26751-26752-26753-26755-26756-26757-26759-26761-26762-26764-26765-26767-26771-26772-26774-26780-26782-26783-26784-26789-26790-26791-26792-26793-26795-26798-26799-26801-26802-26806-26807-26808-26809-26810-26811-26812-26813-26814-26815-26816-26817-26818-26819-26820-26821-26822-26823-26824-26825-26826-26827-26828-26829-26830-26831-26832-26833-26834-26835-26836-26837-26841-26842-26843-26845-26846-26848-26849-26852-26857-26859-26861-26864-26865-26866-26875-26877-26883-26889-26890-26901",
    "26047-27760-27824-27853-27950-28013-28146-28221-28291-28372-28450-28453-28469-28480-28490-28492-28512-28528-28529-28546-28549-28550-28551-28561-28567-28571-28583-28590-28592-28594-28599-28600-28605-28607-28609-28620-28622-28625-28630-28639-28644-28648",
    "26093-26194-26289-26291-26298-26299-26307-26404-26422-26429-26439-26479-26486-26488-26506-26510-26526-26533-26535-26540-26543-26547-26552-26553-26560-26561-26569-26572-26574-26576-26578-26579-26580-26581-26582-26583-26585-26588-26589-26591-26601",
    "26139-26201-26262-26316-26351-26431-26446-26451-26474-26478-26499-26501-26502-26508-26518-26523-26524-26530-26536-26541",
    "26218-26586-26595-26611-26616-26619-26620-26621-26625-26629-26630-26634-26638-26639-26643-26652-26653-26654-26655-26656-26659-26660-26669-26671-26674-26675-26676",
    "26304-27776-27915-27981-28016-28043-28080-28139-28160-28194-28196-28197-28207-28211-28214-28222-28227-28228-28229-28230-28231-28241-28244-28246-28250-28257",
    "26606-27026-27478-27803-27913-28037-28052-28055-28074-28089-28092-28098-28099-28107-28109-28110-28114-28120-28137",
    "26647-26657-27197-27240-27277-27281-27291-27294-27302-27303-27305-27313-27319",
    "26680-27424-28195-28391-28441-28523-28524-28602-28633-28659-28691-28703-28712-28714-28717-28722-28723-28724-28725-28726-28728-28729-28733-28735-28741-28742-28750-28751-28757-28767-28770",
    "26702-28334-29084-29182-29251-29270-29274-29293-29297-29428-29675-29725-29736-29823-29829-29840-29866-29876-29882-29959-30061-30066-30092-30128-30165-30169-30171-30178-30179-30180-30181-30182-30186-30188-30189-30198-30200-30202-30211-30212-30214-30218-30221-30222-30231-30232-30245-30250-30252-30254-30261-30262-30263",
    "26888-27413-27432-27464-27636-27653-27691-27733-27748-27775-27781-27808-27818-27845-27850-27857-27858-27867-27868-27871-27873-27875-27881-27882-27883-27884-27888-27889-27890-27892-27896-27897-27911-27912-27926-27930-27938",
    "26914-27188-27535-27544-27557-27590-27827-27851-27880-27886-27909-27919-27936-27941-27951-27955-27956-27958-27959-27966-27967-27968-27971-27973-27974-27975-27977-27979-27990-27992-27995-27996-28000-28003-28006-28010-28012",
    "27037-27237-27316-27322-27324-27326-27327-27329-27331-27333-27334-27335-27353-27360-27414-27443-27445-27447-27448-27459-27460-27462-27469-27471-27480-27483-27491-27494-27499-27504-27508-27512-27514-27517-27537-27538-27540-27552-27560",
    "27137-27163-27169-27174-27184-27191-27195-27202-27206-27208-27210-27212-27218-27220-27224-27226-27230-27231-27244-27246-27259-27261-27263-27264-27268-27269-27270-27276-27280-27284-27286-27287-27295-27296",
    "27156-27164-27165-27171-27173-27178-27183",
    "27176-27177-27456-27465-27479-27534-27543-27547-27568-27577-27581-27583-27585-27586-27591-27594-27595-27597-27598-27604-27610-27617-27618-27620-27621-27624-27625-27634-27635-27637-27638-27641-27644-27648-27649-27650-27654-27656-27661-27669-27673-27674-27675-27676-27677-27678-27685-27686-27688-27690",
    "27278-27323-27328-27351-27355-27442-27461-27474-27476-27495-27505-27516-27519-27521-27569-27615-27616",
    "27454-28705-28747-28919-28954-29150-29197-29218-29268-29363-29396-29397-29410-29417-29421-29424-29431-29432-29435-29439-29443-29456-29458-29460-29469-29472-29479-29480",
    "27533-27872-28284-28308-28363-28416-28447-28532-28536-28537-28611-28640-28646-28651-28656-28667-28669-28673-28674-28675-28700-28708",
    "27805-27962-28122-28199-28248-28316-28494-28539-28685-28696-28754-28771-28779-28799-28808-28818-28821-28825-28839-28840-28842-28845-28851-28853-28856-28857-28858-28859-28862-28864-28867-28871-28879-28892-28895-28896-28897-28898",
    "27947-28223-28381-28530-28531-28679-28763-28768-28774-28780-28783-28784-28788-28789-28790-28793-28798-28809-28814-28815",
    "28025-28100-28105-28123-28142-28143-28147-28150-28151-28154-28157-28158-28159-28168-28171-28177-28178-28184-28186-28192",
    "28034-30163-30329-30361-30494-30497-30532-30537-30557-30560-30570-30577-30578-30582-30583-30587-30590-30591-30595-30597",
    "28112-29501-29783-30115-30350-30373-30391-30398-30399-30404-30413-30419-30421-30425-30427-30447",
    "28133-28262-29024-29216-29221-29246-29247-29273-29310-29319-29337-29373-29377-29406-29433-29466-29482-29499-29538-29624-29649-29666-29669-29684-29685-29686-29687-29688-29689-29690-29691-29692-29698-29704-29706-29707-29709-29710-29711-29715-29716-29717-29719-29720-29723-29727-29751",
    "28149-28260-28371-28560-28647-28655-28730-28781-28922-28939-29071-29073-29090-29092-29093-29102-29111-29120-29124-29126-29131-29135-29137-29147-29151-29153-29154-29156-29157-29158-29162-29164-29169-29174-29176-29179-29184-29187-29199-29204-29206",
    "28173-28368-28485-28569-28670-28739-28822-28884-28888-28920-28964-28970-28972-28977-28983-28992-28993-28994-29012-29018-29019-29040-29041-29070-29077-29079-29080-29081-29082-29088-29094-29096-29097-29106-29107-29113-29134",
    "28193-28953-29042-29239-29374-29468-29492-29505-29506-29514-29516-29517-29519-29524-29527-29530-29536-29541-29580-29581-29582-29583-29584-29585-29586-29587-29588-29589-29590-29591-29592-29593-29601-29606-29607-29608-29609-29614",
    "28239-28559-28676-28740-28778-28882-28929-28974-29143-29183-29254-29261-29263-29265-29269-29271-29278-29283-29284-29285-29286-29292-29294-29296-29298-29306-29307-29309-29311-29314-29317-29325-29328-29329-29332-29333-29335-29341-29360-29361-29365-29380-29382-29385-29393-29394-29398-29407",
    "28303-29891-30104-30376-31179-31197-31470-31497-31670-31725-31761-31810-31816-31932-31969-31995-32012-32020-32034-32037-32043-32047-32051-32053-32060-32067-32069-32070-32071-32090",
    "28325-29303-30160-30453-30461-30536-31314-31699-31705-31833-31836-31973-31976-32001-32025-32042-32044-32072-32088-32091-32099-32110-32111-32112-32113-32116-32117-32123-32129",
    "28330-28704-29021-29186-29425-29612-29712-29741-29745-29747-29750-29755-29759-29762-29776-29780-29791-29797-29804",
    "28354-28577-28689-28690-28786-28890-28908-29010-29095-29144-29145-29168-29185-29193-29202-29209-29211-29212-29213-29214-29217-29220-29222-29225-29226-29231-29233-29240",
    "28495-28520-28695-28947-28973-28981-28987-28988-28990-28998-28999-29007-29009-29014-29026-29031-29034-29038-29053-29054-29057",
    "28621-30517-30612-30991-30993-31214-31337-31344-31350-31365-31373-31374-31375-31381-31382",
    "28694-28833-29295-29477-30192-30201-30316-30380-30416-30440-30456-30470-30481-30489-30493-30500-30506-30507-30514-30515-30516-30524-30528-30545-30551-30552-30553-30554",
    "28956-29412-29452-29453-29467-29627-29657-29721-29724-29801-29810-29835-29851-29861-29879-29894-29896-29915-29995-30033-30036-30039-30041-30046-30054-30055-30056-30057-30058-30059-30062-30064-30067-30070-30075-30077-30079-30085-30086-30087-30088-30098-30100-30101-30103-30111-30118-30123-30126-30130",
    "29043-29375-29409-29427-29434-29579-29805-29867-30031-30073-30082-30097-30102-30133-30135-30139-30142-30145-30151-30152-30156-30159-30172-30173",
    "29061-29200-30043-30412-30678-30697-30758-30760-30781-30798-30836-30842-30847-30850-30851-30856-30857-30858-30861-30871-30873",
    "29110-29416-29739-29761-29778-29781-29782-29809-29842-29844-29871-29884-29885-29888-29893-29895-29904-29909-29916-29919",
    "29201-29475-29534-29537-30071-30168-30226-30266-30268-30276-30281-30284-30285-30287-30288-30289-30291-30295-30300-30302-30305-30307-30308-30311-30324-30330-30332-30333-30336-30339-30342-30343-30345-30353",
    "29207-29257-29299-29340-29395-29722-29732-29769-29785-29836-29839-29863-29864-29878-29883-29890-29912-29917-29920-29922-29923-29924-29925-29926-29927-29928-29929-29930-29931-29932-29934-29935-29936-29937-29938-29939-29940-29941-29942-29943-29944-29945-29946-29947-29948-29949-29950-29952-29964-29965-29966-29967-29968-29969-29970-29971-29972-29973-29975-29976-29977-29978-29979-29980-29981-29982-29983-29985-29989-29991-29992-30000-30003-30005-30006-30007-30008-30012-30018-30019-30022-30023-30024-30025-30028-30038-30042-30052-30053",
    "29243-29906-30004-30290-30468-30958-30970-30994-31013-31076-31098-31121-31128-31164-31165-31174-31208-31248-31270-31356-31368-31384-31387-31392-31394-31397-31403-31404-31405-31407-31412-31413-31424-31428-31431-31436-31444-31445-31447-31448-31449-31451-31453-31455-31456-31457-31458-31460-31461-31463-31464-31465-31467-31469-31475-31477-31486-31487-31489-31490-31491-31492-31493-31494-31499-31500-31501-31505-31508-31509-31512-31513-31520-31523-31531-31534-31544-31555-31556-31557",
    "29320-29359-29771-29789-29790-29792-29793-29794-29795-29803-29813-29814-29821-29825-29826-29832-29834-29847-29856",
    "29350-29777-30457-30460-30665-31088-31137-31194-31355-31929-31977-32022-32080-32108-32126-32131-32132-32138-32145-32147-32148-32149-32155-32175",
    "29633-30204-30265-30314-30531-30634-30642-30705-30713-30719-30742-30759-30771-30775-30776-30777-30780-30784-30789-30790-30793-30799-30801-30802-30820-30832-30837-30839",
    "29811-29857-29954-30272-30309-30312-30323-30338-30341-30356-30357-30358-30359-30366-30369-30370-30379-30389",
    "30140-30303-30344-30375-30573-30992-31084-31178-31195-31216-31224-31305-31378-31380-31409-31433-31515-31519-31537-31550-31551-31554-31558-31560-31564-31565-31568-31569-31571-31572-31574-31576-31579-31582-31584-31585-31592-31596-31601-31602-31603-31607-31612-31613-31614-31621-31622-31627-31634-31641-31648-31649-31651-31680-31690-31694",
    "30205-30241-30510-30535-30613-30711-30738-30800-30916-30917-30966-31066-31099-31100-31103-31134-31138-31143-31145-31146-31147-31148-31150-31152-31157-31158-31160-31162-31166-31168-31192-31198-31202-31204-31205-31209-31213-31229-31230-31231-31232-31233-31234-31235-31236-31237-31238-31239-31240-31241-31242-31244-31246-31247-31249-31253",
    "30240-30251-30654-30860-30896-30980-31001-31123-31154-31199-31299-31315-31440-31552-31587-31605-31610-31624-31633-31640-31646-31650-31660-31661-31662-31663-31664-31669-31674-31676-31697-31700-31702-31704-31706-31713-31716-31717-31719-31726",
    "30242-30459-31047-31806-31933-31974-31975-32153-32156-32164-32165-32166-32167-32176-32188-32190-32195-32198-32199-32203-32209-32211-32219-32220-32222-32231-32234",
    "30423-30547-30693-30710-30712-30728-30734-30736-30737-30746-30753-30754-30755-30756",
    "30454-30471-30563-30593-30628-30890-30900-30944-30963-30982-31034-31051-31054-31055-31058-31061-31062-31064-31065-31070-31071-31074-31077-31078-31091-31101",
    "30498-30920-30968-30995-31000-31002-31003-31005-31016-31018-31020-31021-31022-31029-31033-31043-31046",
    "30672-30778-31063-31082-31245-31264-31273-31274-31275-31276-31277-31278-31279-31280-31281-31282-31283-31284-31285-31286-31287-31288-31289-31290-31291-31292-31293-31294-31295-31296-31297-31298-31302-31304-31313-31318-31319-31323-31328-31334-31335-31336",
    "30925-31129-31895-31906-31921-31931-31934-31935-31952-31953-31955-31959-31963-31982-31998",
    "30959-31852-31871-31907-31978-31989-32004-32006-32007-32008-32010-32019-32030-32033-32039-32046",
    "30997-32066-32173-32191-32210-32236-32255-32265-32279-32280-32284-32293-32294-32295-32296-32297-32302-32305-32310-32321-32322-32324-32327-32335-32336-32337-32338-32339-32343-32344",
    "31312-31967-32163-32178-32183-32184-32312-32383",
]
for _ni in _BUNDLE_NIS_AZURERM:
    Instance.register("hashicorp", _ni)(TerraformProviderAzurerm)
