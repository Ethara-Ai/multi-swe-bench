import re
import json
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
    """Shared per-era base: OS toolchain + third-party test deps + a FULL clone.

    Deliberately does NOT check out any PR's base.sha and does NOT run the
    anti-reward-hack hardening. Both are per-PR concerns: the hardening pins
    HEAD to one sha and runs `git gc --prune=now`, so doing it here would prune
    the shared clone down to a single PR's commit and break every other PR in
    the era with "reference is not a tree". The project itself is installed in
    the per-PR layer too, so the installed package matches that PR's base.sha
    rather than whatever the default branch happened to be when the base built.

    The `# syntax` directive opts this out of DockerfileEnhancer, which would
    otherwise inject exactly the checkout+prune that must not happen here.
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

    def dependency(self) -> str:
        return "python:3.11-slim"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return "base-13237-to-11942"

    def workdir(self) -> str:
        return "base-13237-to-11942"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo

        return """# syntax=docker/dockerfile:1.6
FROM python:3.11-slim

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    PIP_DISABLE_PIP_VERSION_CHECK=1

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} shared base (13237-to-11942)" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'
RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

# Toolchain and third-party test dependencies only -- shared by every PR in
# this era. The project install lives in the per-PR layer, after the checkout.
RUN pip install 'pytest>=7.0.0,<8.2' 'pytest-xdist!=3.3.0'
RUN pip install mypy types-greenlet

CMD ["/bin/bash"]
""".format(org=org, repo=repo)


class ImageDefault(Image):
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
        return ImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "envagent"

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
                "prepare.sh",
                """ls
###ACTION_DELIMITER###
echo 'python -m pytest --no-header -rA --tb=no -p no:cacheprovider -v --rootdir . --log-info=sqlalchemy.testing' > test_commands.sh
###ACTION_DELIMITER###
pip install 'pytest>=7.0.0,<8.2' 'pytest-xdist!=3.3.0' && pip install .
###ACTION_DELIMITER###
cat test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
pip install mypy types-greenlet
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
echo 'python -m pytest --no-header -rA --tb=no -p no:cacheprovider -v --rootdir . --log-info=sqlalchemy.testing -n auto -m "not memory_intensive and not timing-intensive" -k "not aaa_profiling"' > test_commands.sh
###ACTION_DELIMITER###
cat test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
python -m pytest --no-header -rA --tb=no -p no:cacheprovider -v --rootdir . --log-info=sqlalchemy.testing -n auto -m "not memory_intensive and not timing-intensive" -k "not aaa_profiling"

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
python -m pytest --no-header -rA --tb=no -p no:cacheprovider -v --rootdir . --log-info=sqlalchemy.testing -n auto -m "not memory_intensive and not timing-intensive" -k "not aaa_profiling"

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn  /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
python -m pytest --no-header -rA --tb=no -p no:cacheprovider -v --rootdir . --log-info=sqlalchemy.testing -n auto -m "not memory_intensive and not timing-intensive" -k "not aaa_profiling"

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        repo = self.pr.repo

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # Thin per-PR layer on top of the era's shared base. The base already
        # holds the toolchain, the test deps and a FULL clone, so all that is
        # left here is: pin to this PR's base.sha, install the project from
        # THAT source (never the base's default-branch tree), copy the patches
        # in, and harden.
        #
        # The hardening runs LAST, pinned to the literal base sha. It strips
        # remotes/refs and prunes unreachable objects; because Docker layers are
        # copy-on-write those deletions live in THIS layer only, so the shared
        # base keeps full history for the era's other PRs while this graded
        # image can no longer reach any post-fix commit.
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return """# syntax=docker/dockerfile:1.6
FROM {base_image}

WORKDIR /home/{repo}
RUN git reset --hard
RUN git checkout {sha}

# Project install AFTER checkout, so the installed package is this PR's
# base.sha and not the base image's default-branch tree.
RUN pip install .

{copy_commands}

{hardening}

CMD ["/bin/bash"]
""".format(
            base_image=self.dependency().image_full_name(),
            repo=repo,
            sha=self.pr.base.sha,
            copy_commands=copy_commands,
            hardening=hardening,
        )


@Instance.register("sqlalchemy", "sqlalchemy_13237_to_11942")
class SQLALCHEMY_13237_TO_11942(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

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
        # Parse the log content and extract test execution results.
        passed_tests: set[str] = set()  # Tests that passed successfully
        failed_tests: set[str] = set()  # Tests that failed
        skipped_tests: set[str] = set()  # Tests that were skipped

        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        # pytest states the verdict in two different orders, and this suite
        # emits both. Matching only one order silently drops most results on
        # any era whose run.sh omits -rA:
        #   "test/foo.py::bar PASSED [ 12%]"        plain -v progress line
        #   "PASSED test/foo.py::bar"               -rA short summary
        #   "[gw3] [ 12%] PASSED test/foo.py::bar"  xdist worker line
        #   "test/foo.py::bar <- <string> PASSED"   location-indirection form,
        #                                           emitted for generated tests
        _status = r"PASSED|FAILED|SKIPPED"
        _verdict = re.compile(
            rf"(?:^|\s)(?P<s1>{_status})\s+(?P<t1>test/\S+)"
            rf"|(?:^|\s)(?P<t2>test/\S+)(?:\s+<-\s+\S+)?\s+(?P<s2>{_status})(?=\s|$)",
            re.MULTILINE,
        )
        for _m in _verdict.finditer(log):
            _status_hit = _m.group("s1") or _m.group("s2")
            _name = (_m.group("t1") or _m.group("t2")).strip()
            if _status_hit == "PASSED":
                passed_tests.add(_name)
            elif _status_hit == "FAILED":
                failed_tests.add(_name)
            else:
                skipped_tests.add(_name)

        # Skip reasons quote the id instead of listing it bare, e.g.
        #   SKIPPED [1] lib/.../config.py:420: 'test/x.py::Y::z (call)' : no cython
        for _name in re.findall(r"SKIPPED \[\d+\][^']*'(test/[^']+)'", log):
            skipped_tests.add(
                re.sub(r"\s*\((?:call|setup|teardown)\)$", "", _name.strip())
            )

        # One test id can carry different verdicts across backend variants
        # (PASSED on sqlite, SKIPPED where the backend is absent). TestResult
        # rejects ALL THREE pairwise overlaps, so resolve deterministically as
        # FAILED > SKIPPED > PASSED: never credit a pass for something that
        # also failed or was skipped somewhere. Without this the harness dies
        # with "Passed tests and skipped tests should not have common items".
        skipped_tests -= failed_tests
        passed_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# ---------------------------------------------------------------------------
# number_interval for sqlalchemy bundles -- REGISTRY-SCOPED shim.
#
# The delivered JSONL carries no `number_interval`; it carries exact bundle
# membership in `prs_in_bundle`. The required value is the EXACT PRs joined
# with '-', never a range:
#
#     prs_in_bundle: [146, 147, 150, 155, 157]
#     number_interval: "146-147-150-155-157"      (NOT "146-157")
#
# A range would claim every PR in between, which is wrong -- these bundles are
# sparse (e.g. anchor 6875 bundles 6875-8595-8924-9037, skipping ~1700
# intervening PRs).
#
# `Dataset.build()` copies number_interval straight off the loaded PullRequest
# into the resolved output jsonl, so filling it at load time is what makes it
# appear downstream. As this must live ONLY in the registry, two small,
# idempotent, sqlalchemy-scoped shims are installed at import time (this file
# is already imported by the package __init__, so nothing else is touched):
#
#   1. PullRequest.from_json / .from_dict -- for sqlalchemy/sqlalchemy records
#      whose number_interval is EMPTY, fill it from the raw record's
#      prs_in_bundle. Only empty values are filled, so an explicitly-set
#      number_interval (e.g. an era key) is never overwritten, and other repos
#      are untouched.
#   2. Instance.create -- routing looks up `sqlalchemy/<number_interval>`, and
#      a dash-joined bundle list is not a registered key. On the resulting
#      ValueError, fall back to the era class owning the bundle's ANCHOR PR
#      (pr.number) -- the PR whose base.sha the image is actually built at.
#
# Deriving the era from pr.number instead of hardcoding the 24 delivered bundle
# strings means a regenerated dataset with different bundles still routes
# without editing this file.
# ---------------------------------------------------------------------------

_SA_ORG = "sqlalchemy"
_SA_REPO = "sqlalchemy"

# (lo, hi, era) windows from the era module names, ascending. First match wins,
# so a PR exactly on a boundary belongs to the older era (5062 ->
# sqlalchemy_5062_to_4514), matching how the era files are cut.
# NOTE: 5233..5546 is a genuine gap in the era set; a PR there routes nowhere
# and surfaces as the original ValueError rather than being silently misfiled.
_SA_ERAS = (
    (4514, 5062, "sqlalchemy_5062_to_4514"),
    (5062, 5232, "sqlalchemy_5232_to_5062"),
    (5547, 7381, "sqlalchemy_7381_to_5547"),
    (7381, 7443, "sqlalchemy_7443_to_7381"),
    (7443, 7601, "sqlalchemy_7601_to_7443"),
    (7601, 8496, "sqlalchemy_8496_to_7601"),
    (8496, 11942, "sqlalchemy_11942_to_8496"),
    (11942, 13237, "sqlalchemy_13237_to_11942"),
)


def sqlalchemy_number_interval(prs_in_bundle) -> str:
    """Dash-join a bundle's PR numbers: [146, 147, 150] -> '146-147-150'."""
    if not prs_in_bundle:
        return ""
    return "-".join(str(p) for p in prs_in_bundle)


def sqlalchemy_era_for_number(number) -> str:
    """Return the era registry key owning this anchor PR, or '' if none."""
    try:
        n = int(number)
    except (TypeError, ValueError):
        return ""
    for lo, hi, era in _SA_ERAS:
        if lo <= n <= hi:
            return era
    return ""


def _sa_fill_number_interval(pr, raw) -> None:
    if not isinstance(raw, dict):
        return
    if getattr(pr, "org", "") != _SA_ORG or getattr(pr, "repo", "") != _SA_REPO:
        return
    if getattr(pr, "number_interval", ""):
        return
    interval = sqlalchemy_number_interval(raw.get("prs_in_bundle"))
    if interval:
        pr.number_interval = interval


if not getattr(PullRequest, "_sqlalchemy_ni_shim", False):
    _sa_orig_from_json = PullRequest.from_json.__func__
    _sa_orig_from_dict = PullRequest.from_dict.__func__

    # Signature-transparent (*args/**kwargs): the @dataclass_json decorator
    # REPLACES the class-body from_dict/from_json, so the live signatures are
    # dataclass_json's -- from_dict(cls, kvs, *, infer_missing=False) and
    # from_json(cls, s, *, parse_float=..., **kw). Its from_json delegates to
    # cls.from_dict(kvs, infer_missing=...), so a fixed 2-arg shim here breaks
    # every repo's loader, not just sqlalchemy's.
    def _sa_from_json(cls, *args, **kwargs):
        pr = _sa_orig_from_json(cls, *args, **kwargs)
        try:
            if args:
                _sa_fill_number_interval(pr, json.loads(args[0]))
        except Exception:
            pass
        return pr

    def _sa_from_dict(cls, *args, **kwargs):
        pr = _sa_orig_from_dict(cls, *args, **kwargs)
        try:
            raw = args[0] if args else kwargs.get("kvs")
            _sa_fill_number_interval(pr, raw)
        except Exception:
            pass
        return pr

    PullRequest.from_json = classmethod(_sa_from_json)
    PullRequest.from_dict = classmethod(_sa_from_dict)
    PullRequest._sqlalchemy_ni_shim = True


if not getattr(Instance, "_sqlalchemy_route_shim", False):
    _sa_orig_create = Instance.create.__func__

    def _sa_create(cls, pr, config, *args, **kwargs):
        try:
            return _sa_orig_create(cls, pr, config, *args, **kwargs)
        except ValueError:
            if (
                getattr(pr, "org", "") == _SA_ORG
                and getattr(pr, "repo", "") == _SA_REPO
            ):
                era = sqlalchemy_era_for_number(getattr(pr, "number", None))
                key = f"{_SA_ORG}/{era}" if era else ""
                if key and key in cls._registry:
                    return cls._registry[key](pr, config, *args, **kwargs)
            raise

    Instance.create = classmethod(_sa_create)
    Instance._sqlalchemy_route_shim = True
