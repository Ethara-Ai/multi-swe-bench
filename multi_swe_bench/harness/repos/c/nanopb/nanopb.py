import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")

# GitHub's exact casing; also the path the clone + WORKDIR create.
REPO_DIR = "nanopb"

# ---------------------------------------------------------------------------
# Emit `number_interval` on the OUTPUT (resolved jsonl) rows for nanopb/nanopb.
#
# Every record is a release-interval BUNDLE (base.label like "0.4.8..0.4.9").
# The required output format is the dash-JOINED EXPLICIT list of the bundled PR
# numbers -- "707-887-924-927-..." -- and NOT a "start-end" range like
# "707-1010", which would wrongly imply that every PR number in between belongs
# to the bundle. Only the numbers actually present are emitted.
#
# Where the bundle list comes from: this raw dump has NO `prs_in_bundle` key
# (unlike the AFFiNE lht dumps). The bundled PRs are carried in
# `resolved_issues`, which IS a declared PullRequest field (pull_request.py:178)
# and therefore survives the dataclass-json schema loader. So, unlike the AFFiNE
# registry, NO `PullRequest.from_json` monkeypatch is needed to recover them. A
# `prs_in_bundle` attribute is still preferred when present, so a future re-dump
# that adds one is honoured without touching this file again.
#
# Why patch Dataset.build rather than set pr.number_interval at load time:
# `number_interval` is the ROUTING key -- Instance.create (instance.py:41-48)
# builds the registry name as f"{org}/{number_interval}" whenever it is
# non-empty, and only "nanopb/nanopb" is registered. Setting it on the
# PullRequest would make every instance unroutable ("Instance ... not
# registered"). gen_report builds every resolved-jsonl row through
# Dataset.build(raw_dataset[id], report) (gen_report.py:599), so stamping it
# there lands it on the OUTPUT only and leaves routing untouched.
# ---------------------------------------------------------------------------
from multi_swe_bench.harness.dataset import Dataset as _Dataset


def _nanopb_bundle_numbers(pr) -> list[int]:
    """The PR numbers bundled into this release-interval record, ascending."""
    source = getattr(pr, "prs_in_bundle", None) or [
        getattr(issue, "number", issue) for issue in (pr.resolved_issues or [])
    ]

    numbers: list[int] = []
    seen: set[int] = set()
    for entry in source:
        if isinstance(entry, dict):
            entry = entry.get("number")
        if entry is None:
            continue
        try:
            number = int(entry)
        except (TypeError, ValueError):
            continue
        if number not in seen:
            seen.add(number)
            numbers.append(number)

    # Sorted so the value is deterministic: `resolved_issues` is NOT stored in
    # numeric order in this dump (e.g. the 0.4.8..0.4.9 bundle lists 707 and 887
    # after 932), and an unsorted interval would differ run-to-run if the raw
    # ordering ever changed.
    return sorted(numbers)


def nanopb_number_interval(pr) -> str:
    """Dash-joined explicit bundle list, e.g. "146-147-150-155-157"."""
    return "-".join(str(number) for number in _nanopb_bundle_numbers(pr))


# NOTE: Dataset subclasses PullRequest, so a plain getattr() flag check would
# see a flag inherited from another registry's patch and wrongly skip this one;
# check the class's OWN __dict__. Chaining is safe in either import order --
# each registry's wrapper is scoped to its own org/repo and delegates onward.
if not _Dataset.__dict__.get("_nanopb_build_patched", False):
    _nanopb_orig_build = _Dataset.build.__func__

    def _nanopb_build(cls, pr, report):
        ds = _nanopb_orig_build(cls, pr, report)
        # Never clobber a value the raw dump already supplied (it would be
        # routing-relevant); only fill the empty string this dump carries.
        if pr.org == "nanopb" and pr.repo == "nanopb" and not ds.number_interval:
            ds.number_interval = nanopb_number_interval(pr)
        return ds

    _Dataset.build = classmethod(_nanopb_build)
    _Dataset._nanopb_build_patched = True


class NanopbImageBase(Image):
    """Level 1: toolchain + source base image (shared by every PR in the dataset).

    This Dockerfile is written out IN FULL, starting with the
    `# syntax=docker/dockerfile:1.6` directive. That directive is the documented
    enhancer opt-out: DockerfileEnhancer.enhance() returns the content verbatim
    the moment it sees it (image.py: `if cls.SYNTAX_DIRECTIVE in raw: return
    raw`). Everything the enhancer would otherwise inject -- ARG TARGETARCH /
    ARG REPO_URL / the ENV block / the ethara LABEL -- is therefore emitted by
    hand below, and must stay in sync with the reference format.

    Taking the opt-out is what lets a SHARED base clone the repository at all.
    Without it, _standardize_repo_fetch() rewrites any `git clone ... /home/repo`
    line into the `${REPO_URL}` / `${BASE_COMMIT}` form followed by the full
    Image._HARDENING_BLOCK -- which would force-pin this one shared image to a
    single commit and delete every other ref, breaking `git checkout` for the
    other six PRs that share the tag.

    So: clone here (once, full history, light hardening only); pin to
    ${BASE_COMMIT} and run the canonical hardening per-PR in
    NanopbImageDefault. That also means one clone for the whole dataset instead
    of one per PR image.

    debian:bullseye, NOT gcc:9 (the previous pin) and NOT debian:latest:
      * gcc:9 is Debian *buster*, which has moved to archive.debian.org. It is
        not in Image.DEPRECATED_DEBIAN_IMAGES (that list stops at gcc:8), so
        _get_apt_update_command does not rewrite its sources and `apt-get
        update` fails with 404 -- the base image cannot build at all.
      * bullseye still has live repositories and ships the era-appropriate
        toolchain for this dataset (nanopb 0.4.1 .. 0.4.9): GCC 10, Python 3.9,
        SCons 4.0, protoc 3.12 and python3-protobuf 3.12. Bookworm's protobuf
        4.21 runtime rejects the `*_pb2.py` files that the older nanopb
        releases in this range ship pre-generated ("Descriptors cannot not be
        created directly"), so it is not a safe single pin across the span.
    """

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
        return "debian:bullseye"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        # nanopb's test suite is driven by SCons and needs protoc plus the
        # Python protobuf runtime for the generator. build-essential, git,
        # make and python3 come from the default package set.
        #
        # python-is-python3 is load-bearing, not cosmetic: the generator
        # wrapper in the older releases (e.g. 0.4.1, the 486 bundle) starts
        # with `#!/usr/bin/env python`, and bullseye ships ONLY `python3` --
        # no bare `python`. Without this package the very first codegen step
        # dies with `/usr/bin/env: 'python': No such file or directory` ->
        # `scons: *** [build/alltypes/alltypes.pb.c] Error 127`, so the run and
        # test stages capture ZERO tests while the fix stage (whose patch
        # rewrites the shebang to python3) captures the whole suite. That is a
        # pure environment artefact and it mis-reports the entire suite as n2p.
        return [
            "scons",
            "protobuf-compiler",
            "python3-protobuf",
            "python3-pip",
            "python3-setuptools",
            "python-is-python3",
            "pkg-config",
        ]

    def dockerfile(self) -> str:
        base_img = self.dependency()
        repo_url = f"https://github.com/{self.pr.org}/{self.pr.repo}.git"

        # Mirrors Image.dockerfile()'s default package set.
        default_packages = [
            "ca-certificates",
            "curl",
            "build-essential",
            "git",
            "gnupg",
            "make",
            "python3",
            "sudo",
            "wget",
        ]
        all_packages = default_packages + self.extra_packages()
        packages_str = " \\\n    ".join(all_packages)
        apt_command = self._get_apt_update_command(packages_str, base_img)

        # Resilient in-container clone. A first multi-arch build of this image
        # spent 1h40m on the emulated apt steps and then died on the very last
        # layer with `gnutls_handshake() failed: Error decoding the received TLS
        # packet` -- the same BuildKit failure the toeverything/AFFiNE registry
        # documents. Contributing causes are HTTP/2 multiplexing, a too-small
        # http.postBuffer, and libcurl's low-speed timeout killing a briefly
        # stalled transfer; the arm64 half runs under QEMU here, where stalls
        # are far likelier. Configure git GLOBALLY *before* cloning.
        git_resiliency = (
            "RUN git config --global http.version HTTP/1.1 \\\n"
            "    && git config --global http.postBuffer 1048576000 \\\n"
            "    && git config --global http.lowSpeedLimit 0 \\\n"
            "    && git config --global http.lowSpeedTime 999999 \\\n"
            "    && git config --global core.compression 0 \\\n"
            "    && git config --global submodule.fetchJobs 1"
        )

        if self.config.need_clone:
            # Retry x3: one flaky handshake must not discard the whole build.
            # The bare `git clone "${REPO_URL}" /home/<repo>` form is preserved
            # inside the loop so the reference-format marker still matches.
            fetch = (
                "RUN for i in 1 2 3; do \\\n"
                f'        git clone "${{REPO_URL}}" /home/{REPO_DIR} && break; \\\n'
                f"        echo \"clone attempt $i failed; retrying\"; \\\n"
                f"        rm -rf /home/{REPO_DIR}; \\\n"
                "        sleep 10; \\\n"
                "    done; \\\n"
                f"    test -d /home/{REPO_DIR}/.git"
            )
        else:
            fetch = f"COPY {self.pr.repo} /home/{REPO_DIR}"

        # Light base hardening ONLY: drop the origin remote so the image carries
        # no upstream to re-fetch from, and stop submodule recursion. The
        # canonical Image._HARDENING_BLOCK (detach at ${BASE_COMMIT}, delete
        # every ref, expire reflog, gc) deliberately does NOT run here -- this
        # image is shared, so it must retain full history for every PR's
        # checkout. Per-PR pinning + full hardening happen in
        # NanopbImageDefault.
        light_hardening = (
            "RUN git remote remove origin 2>/dev/null || true; \\\n"
            "    git config --local fetch.recurseSubmodules false; \\\n"
            "    git config --local gc.auto 0"
        )

        # NOTE: hand-written infra block -- the `# syntax` directive opts this
        # Dockerfile out of DockerfileEnhancer entirely (see class docstring),
        # so nothing below is injected for us.
        sections = [
            DockerfileEnhancer.SYNTAX_DIRECTIVE,
            f"FROM {base_img}",
            'ARG TARGETARCH\n'
            f'ARG REPO_URL="{repo_url}"\n'
            "ARG BASE_COMMIT",
            "ENV DEBIAN_FRONTEND=noninteractive \\\n"
            "    LANG=C.UTF-8 \\\n"
            "    LC_ALL=C.UTF-8 \\\n"
            "    TZ=UTC",
            f'LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"',
        ]

        if self.global_env:
            sections.append(self.global_env)

        sections.append("WORKDIR /home/")
        sections.append(apt_command)
        sections.append(git_resiliency)
        sections.append(fetch)
        sections.append(f"WORKDIR /home/{REPO_DIR}")
        sections.append(light_hardening)

        if self.clear_env:
            sections.append(self.clear_env)

        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


class NanopbImageDefault(Image):
    """Level 2: per-PR image. Clones, pins ${BASE_COMMIT}, then hardens.

    dependency() returns an Image, so DockerfileEnhancer.enhance() returns this
    Dockerfile verbatim -- the clone, checkout and Image._HARDENING_BLOCK below
    are emitted exactly as written. Pinning to a single commit is correct here
    because this image is per-PR, unlike the shared base.
    """

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
        return NanopbImageBase(self.pr, self._config)

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
                "compat.sh",
                """#!/bin/bash
# Python-3 compatibility shims for the OLDER nanopb releases in this dataset
# (0.4.1 .. 0.4.3 still carry Python-2 print statements and tab/space-mixed
# SCons files). Sourced by every stage script so the three stages stay
# byte-identical in how they prepare the tree -- an asymmetry here would show
# up as a spurious base-vs-fix delta.
#
# Runs AFTER any patch has been applied, and only rewrites files that actually
# need it, so it is a no-op on the newer releases.
set -u

if grep -q "^[[:space:]]*print '" tests/site_scons/site_init.py 2>/dev/null; then
  sed -i "s/print '\\(.*\\)'/print('\\1')/g" tests/site_scons/site_init.py
  sed -i 's/print "\\(.*\\)"/print("\\1")/g' tests/site_scons/site_init.py
fi

if [ -f tests/SConstruct ]; then
  expand -t 4 tests/SConstruct > tests/SConstruct.tmp && mv tests/SConstruct.tmp tests/SConstruct
fi
if [ -f tests/site_scons/site_init.py ]; then
  expand -t 4 tests/site_scons/site_init.py > tests/site_scons/site_init.py.tmp \\
    && mv tests/site_scons/site_init.py.tmp tests/site_scons/site_init.py
fi

""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
# Build-time only. The shared base image cloned the repo with full history;
# this pins the working tree to THIS PR's ${{BASE_COMMIT}} and asserts the tree
# is clean, before the hardening block strips the git history.
set -e

cd /home/{repo_dir}
git reset --hard
git checkout ${{BASE_COMMIT}}
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"

""".format(repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
# Stage 1 (baseline): no patches applied.
set -uo pipefail

cd /home/{repo_dir}
source /home/compat.sh

cd tests
# -k (keep going): one unbuildable target must not abort the whole suite.
# The lht test patches reference binary fixtures (e.g. tests/fuzztest/
# regressions.zip) that are recorded only as "Binary files ... differ" with NO
# `GIT binary patch` payload, so `git apply` cannot materialise them. Without
# -k, SCons hits `Source 'build/fuzztest/regressions.zip' not found` and stops
# building EVERY remaining target -- the 554 bundle lost ~68 otherwise-passing
# tests that way, which read as a post-fix regression rather than an abort.
scons -j1 -k NODEFARGS=1 2>&1 || true

""".format(repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
# Stage 2: gold test patch only.
set -uo pipefail

cd /home/{repo_dir}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi

source /home/compat.sh

cd tests
# -k (keep going): one unbuildable target must not abort the whole suite.
# The lht test patches reference binary fixtures (e.g. tests/fuzztest/
# regressions.zip) that are recorded only as "Binary files ... differ" with NO
# `GIT binary patch` payload, so `git apply` cannot materialise them. Without
# -k, SCons hits `Source 'build/fuzztest/regressions.zip' not found` and stops
# building EVERY remaining target -- the 554 bundle lost ~68 otherwise-passing
# tests that way, which read as a post-fix regression rather than an abort.
scons -j1 -k NODEFARGS=1 2>&1 || true

""".format(repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
# Stage 3: gold test patch + fix patch. Patch order and flags are identical to
# test-run.sh so the only difference between the two stages is fix.patch.
set -uo pipefail

cd /home/{repo_dir}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
if [ -s /home/fix.patch ]; then
  git apply --whitespace=nowarn --reject /home/fix.patch 2>/dev/null || true
fi

source /home/compat.sh

cd tests
# -k (keep going): one unbuildable target must not abort the whole suite.
# The lht test patches reference binary fixtures (e.g. tests/fuzztest/
# regressions.zip) that are recorded only as "Binary files ... differ" with NO
# `GIT binary patch` payload, so `git apply` cannot materialise them. Without
# -k, SCons hits `Source 'build/fuzztest/regressions.zip' not found` and stops
# building EVERY remaining target -- the 554 bundle lost ~68 otherwise-passing
# tests that way, which read as a post-fix regression rather than an abort.
scons -j1 -k NODEFARGS=1 2>&1 || true

""".format(repo_dir=REPO_DIR),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # The shared base already cloned full history, so this layer only pins
        # it: prepare.sh does `git reset --hard` -> `git checkout ${BASE_COMMIT}`
        # and the canonical hardening below then strips everything else.
        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/{REPO_DIR}

{copy_commands}
RUN bash /home/prepare.sh

"""

        # Anti-reward-hacking hardening -- the canonical Image._HARDENING_BLOCK
        # (detach at ${BASE_COMMIT}, remove origin, delete every ref, expire the
        # reflog, gc/repack, drop alternates, then assert; plus the submodule
        # strip). Concatenated raw rather than interpolated through the f-string
        # above so its ${BASE_COMMIT} and %(refname) tokens stay literal.
        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail


@Instance.register("nanopb", "nanopb")
class Nanopb(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return NanopbImageDefault(self.pr, self._config)

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

    @staticmethod
    def _normalize_test_name(raw: str) -> Optional[str]:
        """Canonical test id, or None if this line is not a scorable test.

        Two nanopb-specific corrections, both load-bearing:

        1. DROP absolute in-checkout paths. nanopb's SConstruct runs pattern
           checks over its OWN SOURCE headers, emitting lines like
           ``[ OK ] All patterns found in /home/nanopb/pb.h``. Those assert on
           the very files a fix patch edits, so crediting them creates the
           self-satisfying loop that report.py's check 5 exists to reject --
           and since essentially every nanopb fix touches pb.h / pb_decode.c /
           pb_encode.c, it invalidated otherwise-good instances wholesale
           ("Fix patch modified file(s) containing credited test(s)"). They are
           source-file lints, not tests; test artifacts all live under build/.

        2. STABILISE the fuzzer corpus ids. The runner prints
           ``Ran ['build/fuzztest/fuzztest'] against build/fuzztest/corpus.zip
           (3671 entries)``; the entry count is data-dependent, so leaving it in
           the id makes the same test look like a different one whenever the
           corpus changes and it can never match across stages.
        """
        name = raw.strip()

        # "['bin'] against corpus.zip (N entries)" -> "bin against corpus.zip"
        bracketed = re.match(
            r"^\[\s*['\"](?P<bin>[^'\"]+)['\"]\s*\]\s+against\s+(?P<target>\S+)", name
        )
        if bracketed:
            name = f"{bracketed.group('bin')} against {bracketed.group('target')}"
        else:
            # Trailing "(N entries)" on any other form.
            name = re.sub(r"\s*\(\d+\s+entries\)\s*$", "", name)

        if name.startswith("/"):
            return None

        # Normalize test name: strip build/ prefix if present
        return re.sub(r"(?:^|(?<=\s))build/", "", name).strip() or None

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass_tests = [
            re.compile(r"\[\s*OK\s*\]\s+Ran\s+(.+)"),
            re.compile(r"\[\s*OK\s*\]\s+Files equal:\s+(.+?)\s+and\s+.+"),
            re.compile(r"\[\s*OK\s*\]\s+All patterns found in\s+(.+)"),
        ]

        re_fail_tests = [
            re.compile(r"\[\s*FAIL\s*\]\s+Program\s+(.+?)\s+returned\s+.+"),
            re.compile(r"\[\s*FAIL\s*\]\s+Files differ:\s+(.+?)\s+and\s+.+"),
            re.compile(r"\[\s*FAIL\s*\]\s+Pattern not found in\s+(.+?):\s+.+"),
            re.compile(
                r"\[\s*FAIL\s*\]\s+Pattern should not exist.*in\s+(.+?):\s+.+"
            ),
        ]

        for line in test_log.splitlines():
            line = ANSI_ESCAPE.sub("", line).strip()
            if not line:
                continue

            for re_pass in re_pass_tests:
                match = re_pass.search(line)
                if match:
                    test_name = self._normalize_test_name(match.group(1))
                    if test_name:
                        passed_tests.add(test_name)
                    break

            for re_fail in re_fail_tests:
                match = re_fail.search(line)
                if match:
                    test_name = self._normalize_test_name(match.group(1))
                    if test_name:
                        failed_tests.add(test_name)
                    break

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === bundle number_interval routing (resolved_issues dash-joined) ===
# JSONL + registry ship together; Instance.create() resolves
# nanopb/<number_interval> whenever that field is non-empty, so every DELIVERED
# dash-joined bundle value must also be a registered routing key -> Nanopb.
# Without these, re-ingesting the delivered jsonl fails with
# "Instance nanopb/<interval> not registered" for every record.
#
# Single-era repo (0.4.1 .. 0.4.9 all build on the one bullseye toolchain), so
# every key maps to the single Nanopb class. Bundle-level: one key per
# delivered bundle. Data-derived from the 7 delivered bundles -- regenerate if
# the delivered set changes.
_BUNDLE_NIS = [
    "296-554-556-560-563-568-577-580-581",
    "326-646-805-844-845-846-847-865-867-868-873-875-882-888-890-893-912-913-919",
    "360-799-803-814-815-818-822-834-837-838-839-840",
    "486-492-496-497-502-506-508-515-516-518-530-532-534-535-537",
    "589-652-656-669-676-677-681-685-686-690-718-720-721-722-723-725-727-730-731-734-736-751-756-764-771-772",
    "596-603-604-614",
    "707-887-924-927-928-931-932-939-942-946-953-959-962-965-968-971-972-974-978-986-987-988-1003-1010",
]
for _ni in _BUNDLE_NIS:
    Instance.register("nanopb", _ni)(Nanopb)
