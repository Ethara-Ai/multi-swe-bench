"""Waypoint harness for the Go 1.13 era (PRs 0-2907).

Uses golang:1.14 as the base image because the codebase calls
testing.T.Cleanup(), introduced in Go 1.14, even though go.mod declares
go 1.13.

Note: PR#26 has a private dependency (github.com/hashicorp/securetunnel)
that cannot be resolved publicly.  It will fail at `go mod download`.

Two-level image layout:

  level 1  _ImageBase     golang:1.14 + apt packages, NO repo.  Tag is
                          "base-go1_14", so every PR in this range shares
                          one base image.
  level 2  _ImageDefault  clones the repo, checks out this PR's base SHA,
                          warms the caches, then applies the hardening block.

The repo is deliberately NOT cloned into the base: a Docker layer is
immutable, so history baked into a shared parent layer can never be removed
by pruning in a child layer, and `docker save` would hand back every fix
commit. Cloning per-PR keeps the fix out of the shared layer.

Because level 2 returns an Image (not a str) from dependency():
  * DockerfileEnhancer.enhance() returns the Dockerfile untouched
    (image.py: it bails when the dependency is not a str), and
  * build_dataset/run_evaluation do not pass the REPO_URL / BASE_COMMIT
    build args (both gate on isinstance(dep, str)).
So this file writes the clone/checkout/hardening itself and interpolates the
base SHA literally. Image._HARDENING_BLOCK is read verbatim from image.py so
the two never drift.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_GO_IMAGE = "golang:1.14"
_TAG_SUFFIX = "go1_14"

_PATCH_EXCLUDES = (
    "--exclude='vendor/*' --exclude='website/*' "
    "--exclude='*.png' --exclude='*.jpg' --exclude='*.pdf' --exclude='*.gz' "
    "--exclude='*.idx' --exclude='*.pack' --exclude='*.tar' --exclude='*.gif' "
    "--exclude='*.xz' --exclude='*.lzma' --exclude='*.enc'"
)

_DEFAULT_PACKAGES = [
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

# org/repo and commit charsets. Validated before interpolation into a generated
# RUN/WORKDIR/clone URL so a crafted value cannot inject build commands. This
# mirrors image.py's own guard, which no longer runs for a chained image.
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9._-]+$")
_SAFE_SHA = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _safe_component(value: str, kind: str) -> str:
    if not value or not _SAFE_COMPONENT.match(str(value)):
        raise ValueError(f"unsafe {kind} for Dockerfile interpolation: {value!r}")
    return str(value)


def _safe_sha(value: str) -> str:
    if not value or not _SAFE_SHA.match(str(value)):
        raise ValueError(f"unsafe base commit for Dockerfile interpolation: {value!r}")
    return str(value)


class _ImageBase(Image):
    """Level 1: toolchain + apt packages, shared by every PR in this range."""

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
        return _GO_IMAGE

    def image_tag(self) -> str:
        return f"base-{_TAG_SUFFIX}"

    def workdir(self) -> str:
        return f"base-{_TAG_SUFFIX}"

    def files(self) -> list[File]:
        return []

    def _get_apt_update_command(self, packages_str: str, base_img: str) -> str:
        # golang:1.14 is built on Debian buster, whose apt repos have moved to
        # archive.debian.org. The base image name ("golang:1.14") doesn't match
        # DEPRECATED_DEBIAN_IMAGES, so apt-get update would 404 against the live
        # mirrors. Force the archive-rewrite branch by handing the parent a
        # buster tag, which triggers the sources.list fixup before installing.
        return super()._get_apt_update_command(packages_str, "debian:buster")

    def dockerfile(self) -> str:
        base_img = self.dependency()
        packages_str = " \\\n    ".join(_DEFAULT_PACKAGES)
        apt_command = self._get_apt_update_command(packages_str, base_img)

        org = _safe_component(self.pr.org, "org")
        repo = _safe_component(self.pr.repo, "repo")

        # Written out here rather than left to DockerfileEnhancer: overriding
        # dockerfile() means the enhancer returns this content untouched.
        sections = [
            "# syntax=docker/dockerfile:1.6",
            f"FROM {base_img}",
            "ARG TARGETARCH",
            "ENV DEBIAN_FRONTEND=noninteractive \\\n    LANG=C.UTF-8 \\\n    TZ=UTC",
            (
                f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
                f'      org.opencontainers.image.description="{org}/{repo} base image" \\\n'
                f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
                f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
            ),
        ]

        if self.global_env:
            sections.append(self.global_env)

        sections.append(apt_command)
        sections.append("WORKDIR /home/")

        if self.clear_env:
            sections.append(self.clear_env)

        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


class _ImageDefault(Image):
    """Level 2: per-PR clone, checkout, cache warm, hardening."""

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
        return _ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _get_test_packages(self) -> str:
        """Extract Go test package paths from test_patch diff headers."""
        packages = set()
        for match in re.finditer(r"diff --git a/(.+?) b/", self.pr.test_patch):
            fpath = match.group(1).strip()
            if fpath.endswith("_test.go"):
                parts = fpath.rsplit("/", 1)
                pkg_dir = parts[0] if len(parts) > 1 else "."
                packages.add(f"./{pkg_dir}/...")
        if not packages:
            return "./..."
        return " ".join(sorted(packages))

    def files(self) -> list[File]:
        test_packages = self._get_test_packages()
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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
# The repo is already cloned and checked out at the PR base commit by the
# Dockerfile; this only verifies the tree is clean and warms the module/build
# cache so the evaluation run doesn't resolve deps from scratch.
set -e

cd /home/{repo}
bash /home/check_git_changes.sh

go mod download || true
go test -v -count=1 {test_packages} || true

""".format(repo=self.pr.repo, test_packages=test_packages),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
go mod tidy
go mod vendor || true
go test -v -count=1 {test_packages}

""".format(repo=self.pr.repo, test_packages=test_packages),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git apply {excludes} /home/test.patch
go mod tidy
go mod vendor || true
go test -v -count=1 {test_packages}

""".format(
                    repo=self.pr.repo,
                    test_packages=test_packages,
                    excludes=_PATCH_EXCLUDES,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git apply {excludes} /home/test.patch /home/fix.patch
go mod tidy
go mod vendor || true
go test -v -count=1 {test_packages}

""".format(
                    repo=self.pr.repo,
                    test_packages=test_packages,
                    excludes=_PATCH_EXCLUDES,
                ),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        org = _safe_component(self.pr.org, "org")
        repo = _safe_component(self.pr.repo, "repo")
        sha = _safe_sha(self.pr.base.sha)

        # No REPO_URL / BASE_COMMIT build args reach a chained image, so the
        # SHA is interpolated literally -- including into the hardening block,
        # which is read verbatim from image.py so the two cannot drift.
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", sha)

        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        sections = [
            "# syntax=docker/dockerfile:1.6",
            f"FROM {base.image_name()}:{base.image_tag()}",
        ]

        if self.global_env:
            sections.append(self.global_env)

        sections.append("WORKDIR /home/")
        sections.append(f'RUN git clone "https://github.com/{org}/{repo}.git" /home/{repo}')
        sections.append(f"WORKDIR /home/{repo}")
        sections.append(f"RUN git reset --hard\nRUN git checkout {sha}")
        sections.append(copy_commands.rstrip("\n"))
        sections.append("RUN bash /home/prepare.sh")
        sections.append(hardening.rstrip("\n"))

        if self.clear_env:
            sections.append(self.clear_env)

        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


def _parse_go_test_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    re_pass = re.compile(r"--- PASS: (\S+)")
    re_fail = [
        re.compile(r"--- FAIL: (\S+)"),
        re.compile(r"FAIL:?\s?(.+?)\s"),
    ]
    re_skip = re.compile(r"--- SKIP: (\S+)")

    for line in test_log.splitlines():
        line = line.strip()

        m = re_pass.match(line)
        if m:
            name = m.group(1)
            if name not in failed_tests:
                skipped_tests.discard(name)
                passed_tests.add(name)

        for rp in re_fail:
            m = rp.match(line)
            if m:
                name = m.group(1)
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)

        m = re_skip.match(line)
        if m:
            name = m.group(1)
            if name not in passed_tests and name not in failed_tests:
                skipped_tests.add(name)

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("hashicorp", "waypoint_0_to_2907")
class WaypointGo1_14(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return _ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd if run_cmd else "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd if test_patch_run_cmd else "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd if fix_patch_run_cmd else "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return _parse_go_test_log(test_log)


# ---------------------------------------------------------------------------
# number_interval routing (dash-joined prs_in_bundle)
# ---------------------------------------------------------------------------
# Dataset rows are PR bundles: `prs_in_bundle` [2908, 2915, 2917, ...] with
# `number_interval` "2908-2915-2917-..." -- the EXPLICIT member list, never a
# first-to-last range. A range would be actively wrong for this repo: the
# bundles starting at 1598 (..1760) and 1751 (..1918) interleave, so "1598-1760"
# would claim PRs that belong to the other bundle.
#
# Instance.create() routes on f"{org}/{number_interval}" whenever it is set, so
# every delivered bundle value has to resolve to an era class. Three pieces,
# all idempotent -- __init__.py imports the three waypoint era modules together
# and each one carries this same block:
#
#   1. A PullRequest.from_json shim. `prs_in_bundle` is not a PullRequest field,
#      so it is dropped at parse time and never reaches the emitted dataset. For
#      hashicorp/waypoint rows whose number_interval is empty, fill it from the
#      raw line's prs_in_bundle -- older records without the column then route,
#      and serialize, exactly like current ones.
#   2. The delivered bundle keys below, registered to this era's class.
#   3. An Instance.create fallback mapping any *unregistered* waypoint
#      number_interval to the era owning its first PR number, so regenerating
#      the dataset with new bundles needs no edit to the list below.
#
# Sentinels are checked against each class's OWN __dict__: Dataset subclasses
# PullRequest and would otherwise inherit the from_json sentinel.
# ---------------------------------------------------------------------------
import json as _wp_json  # noqa: E402

# Delivered bundles whose first PR falls in this era. Data-derived from
# hashicorp/dataset/hashicorp__waypoint_lht_final.jsonl (prs_in_bundle);
# regenerate if the delivered set changes.
_BUNDLE_NUMBER_INTERVALS = [
    "1296-1300-1307-1311-1321-1324-1327-1328-1332-1338-1345-1347-1351-1356-1359-1363-1365",
    "1374-1379-1381-1405-1406-1408-1411-1422-1423-1427-1428-1429-1430-1433-1440-1442-1444-1448-1449-1451-1455-1456-1459-1460-1464",
    "1598-1599-1602-1607-1618-1625-1627-1629-1630-1634-1635-1636-1638-1639-1647-1660-1662-1667-1670-1681-1686-1688-1691-1695-1696-1698-1702-1706-1717-1718-1725-1727-1733-1741-1742-1743-1746-1747-1749-1753-1757-1758-1760",
    "1751-1767-1769-1772-1775-1776-1777-1778-1779-1781-1782-1785-1786-1789-1790-1792-1793-1798-1799-1806-1810-1811-1812-1820-1832-1833-1835-1837-1838-1841-1842-1850-1856-1857-1859-1861-1869-1870-1874-1877-1882-1884-1890-1896-1898-1899-1901-1905-1906-1913-1914-1917-1918",
]

for _ni in _BUNDLE_NUMBER_INTERVALS:
    Instance.register("hashicorp", _ni)(WaypointGo1_14)

_WP_ERA_KEY = re.compile(r"^hashicorp/waypoint_(\d+)_to_(\d+)$")


def _wp_number_interval(bundle) -> str:
    """Dash-join a prs_in_bundle list, order preserved, duplicates dropped."""
    seen = set()
    members = []
    for n in bundle:
        if n not in seen:
            seen.add(n)
            members.append(str(n))
    return "-".join(members)


if "_waypoint_ni_shim" not in PullRequest.__dict__:
    _wp_orig_from_json = PullRequest.from_json.__func__

    @classmethod
    def _wp_from_json(cls, json_str):
        pr = _wp_orig_from_json(cls, json_str)
        try:
            if (
                getattr(pr, "org", "") == "hashicorp"
                and getattr(pr, "repo", "") == "waypoint"
                and not getattr(pr, "number_interval", "")
            ):
                bundle = (_wp_json.loads(json_str) or {}).get("prs_in_bundle") or []
                if bundle:
                    pr.number_interval = _wp_number_interval(bundle)
        except Exception:
            pass
        return pr

    PullRequest.from_json = _wp_from_json
    PullRequest._waypoint_ni_shim = True


if "_waypoint_era_route_shim" not in Instance.__dict__:
    _wp_orig_create = Instance.create.__func__

    @classmethod
    def _wp_create(cls, pr, config, *args, **kwargs):
        try:
            return _wp_orig_create(cls, pr, config, *args, **kwargs)
        except ValueError:
            if (
                getattr(pr, "org", "") != "hashicorp"
                or getattr(pr, "repo", "") != "waypoint"
            ):
                raise
            # Unregistered bundle: route on the era owning its first PR number.
            head = str(getattr(pr, "number_interval", "")).split("-")[0]
            first = int(head) if head.isdigit() else pr.number
            for key, era_cls in cls._registry.items():
                m = _WP_ERA_KEY.match(key)
                if m and int(m.group(1)) <= first <= int(m.group(2)):
                    return era_cls(pr, config, *args, **kwargs)
            raise

    Instance.create = _wp_create
    Instance._waypoint_era_route_shim = True
