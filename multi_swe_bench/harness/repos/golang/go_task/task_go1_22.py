"""go-task/task harness for Go 1.22-1.24 era.

Covers number_intervals: task_go1_22, task_go1_23, task_go1_24.

Uses golang:1.24 as the Docker base image for all.
GOTOOLCHAIN=auto allows patches that bump go.mod beyond 1.24.
Tests require the ``task`` binary in PATH, so ``go install`` runs first.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_GO_IMAGE = "golang:1.24"
_TAG_SUFFIX = "go1_22"

# Toolchain env this era needs, emitted into the shared base image.
_ERA_ENV = """ENV GOTOOLCHAIN=auto"""

# Package set copied from the default list in Image.dockerfile() (image.py) so
# this hand-written base provisions exactly what the shared build provisions.
# `apt-get update` falls back to archive.debian.org because the older golang
# images ride Debian releases whose mirrors have been retired -- the same fix
# Image._get_apt_update_command() applies, keyed off reachability rather than
# the fixed DEPRECATED_DEBIAN_IMAGES list (which does not name golang tags).
_APT_INSTALL = (
    "RUN { apt-get update 2>/dev/null || "
    "{ sed -i 's|deb.debian.org/debian|archive.debian.org/debian|g' /etc/apt/sources.list && "
    "sed -i 's|security.debian.org/debian-security|archive.debian.org/debian-security|g' /etc/apt/sources.list && "
    "sed -i '/-updates/d' /etc/apt/sources.list && "
    "apt-get update; }; } && \\\n"
    "    apt-get install -y --no-install-recommends \\\n"
    "    ca-certificates \\\n"
    "    curl \\\n"
    "    build-essential \\\n"
    "    git \\\n"
    "    gnupg \\\n"
    "    make \\\n"
    "    python3 \\\n"
    "    sudo \\\n"
    "    wget \\\n"
    "    && rm -rf /var/lib/apt/lists/*"
)


def _checkout_dir(pr: PullRequest) -> str:
    """Path the repo is cloned to inside the image."""
    return f"/home/{pr.repo}"


class _ImageBase(Image):
    """Level 1: toolchain-only base image, shared by every PR of the era.

    ``dependency()`` returns a *string*, so ``DockerfileEnhancer.enhance()``
    (image.py) engages and prepends the ``# syntax`` directive, the
    ``REPO_URL``/``BASE_COMMIT`` ARGs, the proxy/TZ/cert ENV block, the OCI
    labels and the CA-cert symlinks.

    This image deliberately does NOT clone the repository. A shared image that
    clones would be rewritten by ``DockerfileEnhancer._standardize_repo_fetch``
    into a ``${BASE_COMMIT}`` checkout plus ``Image._HARDENING_BLOCK``, pinning
    the whole era to whichever PR happened to build the base first and deleting
    the history every other PR needs. The clone therefore lives in
    ``_ImageDefault``, per PR.
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
        return _GO_IMAGE

    def image_tag(self) -> str:
        return f"base-{_TAG_SUFFIX}"

    def workdir(self) -> str:
        return f"base-{_TAG_SUFFIX}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        sections = [f"FROM {_GO_IMAGE}", "WORKDIR /home/", _APT_INSTALL]

        if _ERA_ENV:
            sections.append(_ERA_ENV)
        if self.global_env:
            sections.append(self.global_env)
        if self.clear_env:
            sections.append(self.clear_env)

        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


class _ImageDefault(Image):
    """Level 2: per-PR image built on the shared era base.

    ``dependency()`` returns an ``Image``, so ``DockerfileEnhancer.enhance()``
    returns this Dockerfile verbatim -- which is why the clone, the
    ``${BASE_COMMIT}`` checkout and the history strip are spelled out here. The
    strip is the canonical ``Image._HARDENING_BLOCK`` (concatenated raw so its
    ``${BASE_COMMIT}`` and ``%(refname)`` tokens stay literal), so the fix
    cannot be recovered from git history while ``base.sha`` stays reachable as
    HEAD.
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

    def dependency(self) -> Image | None:
        return _ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        checkout_dir = _checkout_dir(self.pr)

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

# The Dockerfile already cloned the repo and checked out ${{BASE_COMMIT}}, so
# this only asserts a clean tree and warms the module/build caches.
cd {checkout_dir}
git reset --hard
bash /home/check_git_changes.sh

go install -v ./... || true
go test -v -count=1 ./... || true

# Warming the caches can rewrite go.mod/go.sum; restore the tracked tree so the
# image ships base.sha byte-for-byte and the eval patches apply cleanly.
git reset --hard || true

""".format(checkout_dir=checkout_dir),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd {checkout_dir}
go install -v ./... || true
# The package list is derived from the patch diff headers, but a patch adds,
# renames and deletes files: a directory named there may not exist at THIS
# stage (created by the fix, or removed by it). `go test` treats a missing
# package as a fatal error and aborts before running anything, so keep only
# the directories that exist right now and still hold .go files.
PKGS=""
for d in $(cat /home/test.patch /home/fix.patch 2>/dev/null | grep '^diff --git' | sed 's|diff --git a/||;s| b/.*||' | grep '\\.go$' | xargs -I{{}} dirname {{}} | sort -u | sed 's|^|./|' | grep -v '^\\.$'); do
  # vendor/ holds third-party code: not ours to test, and in module mode it is
  # not part of this module at all.
  case "$d" in ./vendor/*) continue;; esac
  # Let the toolchain decide what is a real package. A directory can exist and
  # hold .go files yet still not be a package -- e.g. every file excluded by a
  # build constraint such as `//+build ignore` -- and `go test` treats that as
  # a fatal "cannot find module for path" before running anything.
  go list "$d" >/dev/null 2>&1 || continue
  PKGS="$PKGS $d"
done
if [ -z "$PKGS" ]; then
  PKGS="./..."
fi
go test -v -count=1 -timeout 15m $PKGS

""".format(checkout_dir=checkout_dir),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd {checkout_dir}
git apply /home/test.patch || {{ echo "Warning: git apply test.patch failed, retrying with --reject..."; git apply --reject /home/test.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
go install -v ./... || true
# The package list is derived from the patch diff headers, but a patch adds,
# renames and deletes files: a directory named there may not exist at THIS
# stage (created by the fix, or removed by it). `go test` treats a missing
# package as a fatal error and aborts before running anything, so keep only
# the directories that exist right now and still hold .go files.
PKGS=""
for d in $(cat /home/test.patch /home/fix.patch 2>/dev/null | grep '^diff --git' | sed 's|diff --git a/||;s| b/.*||' | grep '\\.go$' | xargs -I{{}} dirname {{}} | sort -u | sed 's|^|./|' | grep -v '^\\.$'); do
  # vendor/ holds third-party code: not ours to test, and in module mode it is
  # not part of this module at all.
  case "$d" in ./vendor/*) continue;; esac
  # Let the toolchain decide what is a real package. A directory can exist and
  # hold .go files yet still not be a package -- e.g. every file excluded by a
  # build constraint such as `//+build ignore` -- and `go test` treats that as
  # a fatal "cannot find module for path" before running anything.
  go list "$d" >/dev/null 2>&1 || continue
  PKGS="$PKGS $d"
done
if [ -z "$PKGS" ]; then
  PKGS="./..."
fi
go test -v -count=1 -timeout 15m $PKGS

""".format(checkout_dir=checkout_dir),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd {checkout_dir}
git apply /home/test.patch /home/fix.patch || {{ echo "Warning: git apply failed, retrying with --reject..."; git apply --reject /home/test.patch 2>&1 || true; git apply --reject /home/fix.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
go install -v ./... || true
# The package list is derived from the patch diff headers, but a patch adds,
# renames and deletes files: a directory named there may not exist at THIS
# stage (created by the fix, or removed by it). `go test` treats a missing
# package as a fatal error and aborts before running anything, so keep only
# the directories that exist right now and still hold .go files.
PKGS=""
for d in $(cat /home/test.patch /home/fix.patch 2>/dev/null | grep '^diff --git' | sed 's|diff --git a/||;s| b/.*||' | grep '\\.go$' | xargs -I{{}} dirname {{}} | sort -u | sed 's|^|./|' | grep -v '^\\.$'); do
  # vendor/ holds third-party code: not ours to test, and in module mode it is
  # not part of this module at all.
  case "$d" in ./vendor/*) continue;; esac
  # Let the toolchain decide what is a real package. A directory can exist and
  # hold .go files yet still not be a package -- e.g. every file excluded by a
  # build constraint such as `//+build ignore` -- and `go test` treats that as
  # a fatal "cannot find module for path" before running anything.
  go list "$d" >/dev/null 2>&1 || continue
  PKGS="$PKGS $d"
done
if [ -z "$PKGS" ]; then
  PKGS="./..."
fi
go test -v -count=1 -timeout 15m $PKGS

""".format(checkout_dir=checkout_dir),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        checkout_dir = _checkout_dir(self.pr)
        copy_files = " ".join(file.name for file in self.files())

        header = f"""FROM {base.image_name()}:{base.image_tag()}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/
RUN mkdir -p {checkout_dir} && git clone https://github.com/{self.pr.org}/{self.pr.repo}.git {checkout_dir}

WORKDIR {checkout_dir}
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

COPY {copy_files} /home/

RUN bash /home/prepare.sh || true

"""

        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""

        return header + Image._HARDENING_BLOCK + tail


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


class _TaskInstanceBase(Instance):
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


@Instance.register("go-task", "task_go1_22")
class TaskGo1_22(_TaskInstanceBase):
    pass


_GROUPED_VERSIONS = ["1.23", "1.24"]

for _gv in _GROUPED_VERSIONS:
    _reg_key = f"task_go{_gv.replace('.', '_')}"
    _cls = type(
        f"_Task_{_reg_key}",
        (_TaskInstanceBase,),
        {},
    )
    Instance.register("go-task", _reg_key)(_cls)


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# Every record in go-task__task_lht_final.jsonl carries a `number_interval` that
# is its `prs_in_bundle` joined by "-" (e.g. "101-204-305-409"). Instance.create()
# prefers that key, looking up f"go-task/{number_interval}", so each delivered
# bundle whose toolchain requirement lands in this era (golang:1.24) is registered
# to TaskGo1_22. A bundle sits in the era of the highest Go version it needs -- the
# `go` directive at its base sha, or a higher one introduced by its patches.
_BUNDLE_NIS_TASK_GO1_22 = [
    "1157-1633-1704-1710-1711-1713-1715-1717-1719-1730-1747-1751-1752-1754-1758-1760-1762-1764-1765-1767-1776-1778-1779-1780-1782-1784-1789-1790-1791",
    "1652-1716-1757-1771-1810-1815-1823-1824-1827-1830-1833-1834-1835-1839-1842-1849-1851-1852-1857-1866-1874-1884-1885-1886-1890-1891-1893-1904-1905",
    "1783-1792-1793-1806-1809-1811-1814",
    "1797-1859-1869-1872-1879-1882-1895-1896-1897-1898-1899-1907-1921-1935-1941-1942-1949-1960-1961-1962-1963-1971-1972-1974-1976-1980-1981-1983-1984-1985-1989-1992-2002",
    "1798-1982-2007-2011-2017-2018-2020-2021-2028-2031-2033-2038-2049-2050-2052-2054-2055-2059-2060-2064-2068-2069-2082-2084-2086-2088-2092-2093-2097",
    "1808-2235-2246-2351-2354-2358-2359-2360-2362-2363-2364-2369-2371-2372-2375-2377-2378-2380-2381-2383-2386-2387-2389-2391-2394-2398-2399-2410-2414-2415-2417",
    "1844-2403-2433-2489-2491-2495-2502-2507-2511-2512-2513-2515-2519-2523-2524-2525-2526-2527-2532-2536-2540-2550-2552-2553-2554-2555-2556-2557-2566-2568-2569-2571-2572-2573-2574-2576-2577-2580-2586",
    "1883-1910-1913-1914-1915-1917-1926-1927-1928-1934",
    "2042-2048-2075-2081-2085-2110-2112-2113-2115-2121-2123-2125-2126-2127-2130-2131-2134-2137-2141-2144-2147-2148-2151-2152-2157-2165-2166-2167-2169-2173-2176-2178-2186-2188",
    "2053-2168-2286-2350-2393-2418-2421-2430-2431-2432-2434-2435-2436-2438-2448-2449-2454-2456-2457-2461-2463-2472-2490-2492-2494-2500-2501-2506",
    "2140-2196-2200-2211-2214-2216-2219-2220-2223-2225-2233-2236-2237-2256-2260-2270-2271-2281",
    "2234-2564-2578-2579-2584-2602-2611-2628-2633-2637-2638-2646-2651-2653",
    "2265-2289-2291-2297-2298-2308-2311-2316-2319-2322-2323-2326-2331-2333",
    "2428-2607-2632-2656-2661-2662-2665-2669-2672-2673-2682-2684-2686-2693-2705-2706-2709-2712-2713-2714-2719",
    "2537-2635-2640-2657-2660",
    "2670-2678-2724-2728-2729-2730-2738-2744-2748-2755-2756-2759",
    "2718-2723",
]
for _ni in _BUNDLE_NIS_TASK_GO1_22:
    Instance.register("go-task", _ni)(TaskGo1_22)
