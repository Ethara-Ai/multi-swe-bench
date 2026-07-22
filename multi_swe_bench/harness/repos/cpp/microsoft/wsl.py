import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# microsoft/WSL is a Windows-only C++ project: its top-level CMakeLists.txt
# hard-fails on non-Windows toolchains (requires the Windows SDK build 26100 and
# MSVC) and its test suite under test/windows/ uses TAEF (`te.exe`), which only
# runs on Windows. The Linux-runnable subset (test/linux/unit_tests) only
# exercises live WSL-guest kernel/interop behaviour and cannot run in a plain
# container either. This makes a real build/test impossible on the Linux Docker
# harness. We therefore follow the established convention already used in this
# codebase for sibling Microsoft Windows repos (c/microsoft/ebpf_for_windows.py,
# c/microsoft/xdp_for_windows.py): a deterministic patch-applicability smoke
# test that yields a valid f2p signal (`fix_patch_applied` fails before the fix
# and passes after it) and a p2p signal (`test_patch_applied`).
#
# Several PRs in this dataset carry fix patches with binary-file diff markers
# generated without `--binary` (no full index line), which `git apply` rejects
# outright. filter_binary.awk strips those binary-only diff blocks so the
# textual source changes — the part that actually matters — apply cleanly and
# uniformly across every release line (2.5 - 2.8).


class WSLImageBase(Image):
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
        return "ubuntu:22.04"

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

        repo = self.pr.repo
        org = self.pr.org

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        # SHARED base (tag "base", ONE image reused by every PR). Keeps full git
        # history — no gc/prune here — so each PR's prepare.sh can still
        # `git checkout <base.sha>`: dropping origin unreferences the release-line
        # refs but leaves their objects intact, and the strict per-PR hardening in
        # WSLImageDefault only prunes AFTER detaching onto base.sha. A shared base
        # cannot pin to one commit, so the strict pass cannot live here. The
        # `# syntax` directive makes DockerfileEnhancer.enhance() skip this file,
        # which is what prevents it from auto-injecting a ${BASE_COMMIT} checkout
        # and turning the shared base into a per-PR one.
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    TZ=UTC \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
        git gawk ca-certificates && \\
    rm -rf /var/lib/apt/lists/*

{code}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class WSLImageDefault(Image):
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
        return WSLImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
            File(
                ".",
                "filter_binary.awk",
                r"""/^diff --git / { if (buf != "") { if (isbin == 0) printf "%s", buf } buf = $0 ORS; isbin = 0; next }
/^Binary files / || /^GIT binary patch/ { isbin = 1 }
{ buf = buf $0 ORS }
END { if (buf != "" && isbin == 0) printf "%s", buf }
""",
            ),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain --ignore-submodules=all) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0

""".format(),
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

bash /home/filter_patches.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "filter_patches.sh",
                """#!/bin/bash
# Strip binary-only diff blocks (generated without --binary, so they carry no
# full index line and `git apply` rejects them outright) leaving the textual
# source changes, which apply cleanly across every release line (2.5 - 2.8).
#
# Re-derived at RUN time, never only at image-build time. At evaluation the
# agent's fix patch is bind-mounted over /home/fix.patch, but run_evaluation's
# human_mode path does NOT replay prepare.sh -- so a fix_src.patch baked into the
# image during the build would still hold the GOLD fix and every submission,
# including an empty one, would score [PASS] while the agent's actual patch was
# never read. Regenerating here keeps the graded artifact tied to whatever is
# really mounted at /home/fix.patch.
set -e
awk -f /home/filter_binary.awk /home/test.patch > /home/test_src.patch
awk -f /home/filter_binary.awk /home/fix.patch  > /home/fix_src.patch
""".format(),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git checkout -f {pr.base.sha} >/dev/null 2>&1
git clean -dfx >/dev/null 2>&1
bash /home/filter_patches.sh

echo "[PASS] repo_checkout"

# At base state the fix patch is not applied -> expected FAIL.
if git apply --reverse --check --whitespace=nowarn /home/fix_src.patch >/dev/null 2>&1; then
  echo "[PASS] fix_patch_applied"
else
  echo "[FAIL] fix_patch_applied"
fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git checkout -f {pr.base.sha} >/dev/null 2>&1
git clean -dfx >/dev/null 2>&1
bash /home/filter_patches.sh

if git apply --whitespace=nowarn /home/test_src.patch 2>/dev/null || git apply --whitespace=nowarn --3way /home/test_src.patch 2>/dev/null; then
  echo "[PASS] test_patch_applied"
else
  echo "[FAIL] test_patch_applied"
fi

# Only the test patch is applied; the fix patch is still absent -> FAIL.
if git apply --reverse --check --whitespace=nowarn /home/fix_src.patch >/dev/null 2>&1; then
  echo "[PASS] fix_patch_applied"
else
  echo "[FAIL] fix_patch_applied"
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git checkout -f {pr.base.sha} >/dev/null 2>&1
git clean -dfx >/dev/null 2>&1
bash /home/filter_patches.sh

if git apply --whitespace=nowarn /home/test_src.patch 2>/dev/null || git apply --whitespace=nowarn --3way /home/test_src.patch 2>/dev/null; then
  echo "[PASS] test_patch_applied"
else
  echo "[FAIL] test_patch_applied"
fi

git apply --whitespace=nowarn /home/fix_src.patch 2>/dev/null || git apply --whitespace=nowarn --3way /home/fix_src.patch 2>/dev/null || true

# The fix patch is now applied -> expected PASS.
if git apply --reverse --check --whitespace=nowarn /home/fix_src.patch >/dev/null 2>&1; then
  echo "[PASS] fix_patch_applied"
else
  echo "[FAIL] fix_patch_applied"
fi

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        # Per-PR anti-reward-hacking hardening. dependency() returns an Image, so
        # DockerfileEnhancer emits this Dockerfile verbatim (it only auto-injects
        # into str-dependency images) — so the canonical block from image.py is
        # embedded here, after prepare.sh has checked out base.sha, with
        # ${BASE_COMMIT} bound to the literal sha. Referenced rather than copied so
        # it cannot drift from the source of truth.
        hardening = self._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("microsoft", "WSL")
class WSL(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return WSLImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass = re.compile(r"^\[PASS\]\s+(\S+)")
        re_fail = re.compile(r"^\[FAIL\]\s+(\S+)")

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            m = re_pass.match(line)
            if m:
                passed_tests.add(m.group(1))
                continue
            m = re_fail.match(line)
            if m:
                failed_tests.add(m.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
_BUNDLE_NIS_WSL = [
    "14416-14547-40041-40046-40065-40078-40081-40083-40084-40085-40086-40087-40088-40089-40091-40093-40094-40097-40098-40100-40101-40102-40104-40108-40122-40123-40124-40125-40128-40129-40130-40131-40132-40134-40137-40143-40144-40146-40147-40148-40149-40150-40151-40152-40153-40163-40165",
    "40074-40077-40080",
    "40121-40434-40456-40466-40473-40476-40485-40500-40513-40515-40527-40528-40529-40533-40536",
    "40277-40278-40305",
    "12284-12865-12868-12872-12917-12918-12919-12921-12924-12932-12944-12946-12947-12952-12957-12958-12966-12977-12981-12996-12997-12998-12999-13003-13005-13007-13010-13016-13017-13023-13034-13036-13046-13052-13054",
    "12928-12954-12955-12982-13002-13008-13076-13078-13079-13080-13085-13087-13089-13090-13095-13101-13103-13109-13118-13123-13130-13134-13139-13140-13149-13150-13157-13161-13176-13177-13178-13182-13183-13201-13208-13212-13229-13238-13240-13257-13264-13265-13267-13271-13280-13289-13290-13298-13300-13305-13314-13316-13318-13319-13340",
    "13350-13352-13355-13361-13371-13379-13383-13392-13405-13412-13418-13419-13442-13447-13450-13467-13470-13475-13481-13483-13488-13489-13493-13499-13500-13506-13510-13512-13515-13516-13517-13537-13545-13546-13547-13549-13552-13555-13561-13567-13568-13569-13570-13574-13579",
    "13834-13836-13843-13861-13877-13879-13890-13892-13896-13898-13905-13914-13921-13926-13927-13928-13929-13930-13933-13939-13942-13950-13953-13961-13964-13971-14003-14011-14021-14022-14024-14032-14042-14048-14049-14057-14059-14066-14071-14073-14076-14083-14087-14089-14091-14093-14099-14110-14111-14114-14119-14122-14127-14133-14140-14143-14146-14149-14151-14156-14160-14169-14186-14187-14198-14202-14206-14210-14215-14227-14268-14272-14276-14283-14285-14286-14289-14293-14297-14298-14323-14324-14333-14343-14345-14350-14352-14353-14360-14374-14380-14386-14387-14404-14421-14423-14424-14426-14459-14460-14461-14463-14466-14477",
    "14425-14447-14455-14490-14492-14497-14500-14522-14524-14525-14531-14532-14538-14542-14550-14553-14554-14556-14574-40036-40048-40050-40055-40059-40060-40075-40079-40092-40099-40113-40116-40127-40136-40140-40155-40156-40159-40160-40162-40170-40185-40187-40197-40207-40208-40223-40225-40226-40227-40228-40229-40235-40249-40253-40257-40258-40263-40271-40273-40274-40276",
    "40037-40095-40139-40172-40173-40174-40179-40180-40181-40183-40186-40190-40191-40192-40193-40194-40202-40203-40204-40205-40210-40211-40216-40217-40232-40236-40237-40239-40248-40250-40251-40254-40255-40265-40266-40267-40268-40279-40281-40288-40290-40298",
]
for _ni in _BUNDLE_NIS_WSL:
    Instance.register("microsoft", _ni)(WSL)
