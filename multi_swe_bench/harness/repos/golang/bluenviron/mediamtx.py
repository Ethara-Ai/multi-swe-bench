import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image, _safe_path_component
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Single-era config covering PR #471 -> #5722 (release 0.16 -> 1.18).
# Although go.mod ranges from `go 1.16` to `go 1.26.0` and the module path
# was renamed twice (`aler9/rtsp-simple-server` -> `aler9/mediamtx` ->
# `bluenviron/mediamtx`), one config suffices because:
#   - GOTOOLCHAIN=auto on the golang:1.22 host downloads any newer toolchain
#     declared in go.mod at runtime (verified for go 1.24, 1.25, 1.26).
#   - GitHub auto-redirects the legacy aler9 clone URL to bluenviron/mediamtx.
#   - The test framework (Go built-in `testing`) and output format are stable
#     across the entire history, so parse_log() is unchanged.
#
# Two-level build. ImageBase (tag "base") is the toolchain layer: golang:1.22 +
# apt packages + GOTOOLCHAIN=auto, and nothing PR-specific. Image identity is
# f"{image_name()}:{image_tag()}", so every PR's ImageDefault depends on the
# same "…_m_mediamtx:base" name and build_dataset's dependency graph (which
# dedups images by full name) builds it exactly ONCE for the whole dataset --
# the apt/ffmpeg install is paid once instead of 73 times.
#
# The repo is deliberately NOT cloned in the base. The base image is shared, so
# it cannot carry a checkout: the hardening pass pins .git to one BASE_COMMIT
# and deletes every other ref, which would leave the other PRs unable to check
# out their own base sha. Clone + checkout + hardening therefore stay in the
# per-PR layer, exactly as before.

# Mirrors the default list in Image.dockerfile(). Duplicated rather than
# imported because that method also owns the clone/checkout/hardening sections,
# which the base image must not have -- see the note above.
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


class ImageBase(Image):
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
        return "golang:1.22"

    def extra_packages(self) -> list[str]:
        # build-essential (a default package) already provides make/gcc/libc6-dev.
        # ffmpeg is the only extra: mediamtx's integration tests shell out to it
        # to produce RTSP/RTMP/HLS source streams.
        return ["ffmpeg"]

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # Hand-written instead of inheriting Image.dockerfile() so the build stops
        # after the apt layer: no clone, no `git checkout ${BASE_COMMIT}`, no
        # hardening block -- that is what makes this image reusable across every
        # PR.
        #
        # The leading syntax directive is load-bearing beyond BuildKit: because
        # dependency() is a str, DockerfileEnhancer would otherwise inject its
        # infrastructure block here, and its early-return is
        # `if SYNTAX_DIRECTIVE in raw`. Emitting it ourselves means this file is
        # the final content and the MITM/proxy plumbing is never added -- no
        # http_proxy/https_proxy/no_proxy ARGs, no SSL_CERT_FILE /
        # REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE overrides, no CA-bundle symlink
        # farm. Nothing in the harness passes those build args or a mitm_ca
        # secret, and the golang:1.22 (Debian) CA store at
        # /etc/ssl/certs/ca-certificates.crt already works for git and go. The
        # labels below are kept because they carry image provenance.
        #
        # Opting out also skips _standardize_repo_fetch / _inject_final_sanitize,
        # which is correct here: they exist to force a clone+checkout+hardening
        # tail onto an image, and this one deliberately has no repo. The per-PR
        # layer still does all three -- see ImageDefault.dockerfile().
        base_img = self.dependency()
        org = _safe_path_component(self.pr.org, "org")
        repo = _safe_path_component(self.pr.repo)

        packages_str = " \\\n    ".join(_DEFAULT_PACKAGES + self.extra_packages())
        apt_command = self._get_apt_update_command(packages_str, base_img)

        sections = ["# syntax=docker/dockerfile:1.6", f"FROM {base_img}"]

        sections.append(
            f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
            f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        if self.global_env:
            sections.append(self.global_env)

        sections.append(
            "WORKDIR /home/\n"
            "ENV DEBIAN_FRONTEND=noninteractive\n"
            "ENV LANG=C.UTF-8\n"
            "ENV TZ=UTC"
        )
        sections.append(apt_command)
        # Set as an ENV (not just exported inside the scripts) so it is inherited
        # by every PR image and also persists into the eval container.
        sections.append("ENV GOTOOLCHAIN=auto")

        if self.clear_env:
            sections.append(self.clear_env)

        # Declared, unused, and deliberately last. build_dataset passes
        # REPO_URL/BASE_COMMIT as build args to every image whose dependency() is
        # a str, so declaring them silences the "build-args were not consumed"
        # warning; keeping them after the apt layer means their per-PR values
        # cannot invalidate its cache. The clone that consumes them lives in the
        # per-PR layer.
        sections.append("ARG REPO_URL\nARG BASE_COMMIT")

        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


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

    def dependency(self) -> Optional[Image]:
        return ImageBase(self.pr, self.config)

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

""".format(),
            ),
            File(
                ".",
                "run_tests.sh",
                """#!/bin/bash
# Single-module Go repo. GOTOOLCHAIN=auto lets the host golang:1.22 download
# the toolchain version declared by go.mod (mediamtx spans go 1.16 -> 1.26+).
set -eo pipefail
export CI=true
export GOTOOLCHAIN=auto
cd /home/{pr.repo}
# `go generate ./...` produces embed assets (hls.min.js, mtxrpicam binaries,
# VERSION file) required for compilation. Without it, packages with
# `//go:embed` directives fail with "pattern X: no matching files found" and
# `go test` exits before printing any --- PASS/FAIL lines. This mirrors the
# repo's own scripts/test.mk which runs `go generate` before `go test`.
# Wrapped in `|| true` because some PRs may have transient network failures
# downloading external assets (hls.js CDN, GitHub releases for rpicam) — the
# test compile will surface any real missing-embed error.
go generate ./... || true
# Fallback for the `//go:embed VERSION` target. `go generate` runs
# ./internal/core/versiongetter, which derives the version from git tags. The
# hardening block strips every ref from .git, so on older revisions the getter
# aborts ("ERR: EOF") without writing VERSION and every package that embeds it
# fails with "pattern VERSION: no matching files found" — no test in the repo
# runs, at any stage. Newer revisions already fall back to v0.0.0 themselves
# (visible as "WARN: cannot get tag from .git folder"); this does the same for
# the ones that do not. Writes only when the file is absent, so a successful
# `go generate` is never overwritten.
#
# The trailing `|| true` is load-bearing: this script runs under
# `set -eo pipefail`, and grep exits 1 when it matches nothing. Revisions that
# predate the //go:embed directive (the rtsp-simple-server era) match nothing,
# so without it the pipeline fails the whole script here and every stage
# produces an empty log.
grep -rl 'go:embed VERSION' --include='*.go' . 2>/dev/null | while read -r f; do
  d=$(dirname "$f")
  [ -s "$d/VERSION" ] || echo "v0.0.0" > "$d/VERSION"
done || true
# `go test` always writes per-package result lines (`--- PASS:` / `--- FAIL:` /
# `--- SKIP:`) before exiting, so the script's exit status does not affect what
# parse_log sees. Capture status without `|| true`, then exit 0 so the harness
# always reaches parse_log even when some packages fail.
status=0
# `-p=1` runs packages serially; `-parallel=1` runs subtests within a package
# serially. Both are needed because mediamtx integration tests bind real ports
# (RTSP/RTMP/HLS/SRT/WebRTC) — concurrent runs cause port-conflict flakes that
# show up as Rule 2 (PASS->FAIL between test/fix stages) in the harness.
#
# `-vet=off` disables the vet pass `go test` runs implicitly before compiling.
# GOTOOLCHAIN=auto honours the `go` directive in go.mod (up to 1.26 in this
# dataset), and Go 1.24 added the printf check for non-constant format strings.
# That check fires on pre-existing code such as
#   internal/staticsources/rpicamera/camera_arm_.go: fmt.Errorf(string(buf[1:]))
# which makes `go test` report `FAIL <pkg> [build failed]` and skip that
# package's tests entirely — a toolchain-version artifact, not a defect in the
# PR under evaluation, and identical in the test and fix stages. Leaving vet on
# silently drops real test coverage and injects the package path into
# failed_tests as a pseudo-test name. Tests themselves still compile and run.
#
# `-run '^Test'` excludes fuzz targets. A plain `go test` also replays every
# file in testdata/fuzz/<Target>/, reporting each as a subtest named after the
# file — and those names are content hashes ("FuzzUnmarshal/297c43aee5d30530").
# They are corpus entries, not tests: they enter f2p/n2p, and since eval freezes
# the run/test stages and only re-runs fix, an agent whose corpus differs by one
# file can never reproduce them, making the instance unsolvable regardless of
# its patch. Every real test in this repo is named Test*, so the filter costs
# nothing (verified: 1428 Test names, 138 Fuzz, no Example/Benchmark).
go test -v -count=1 -p=1 -parallel=1 -vet=off -timeout=20m -run '^Test' ./... > /tmp/gotest.out 2>&1 || status=$?
cat /tmp/gotest.out

# Retry once when the run captured nothing at all.
#
# The recovery block below keys off `FAIL <pkg> [build failed]` lines, which go
# test emits per package once compilation has begun. A module-resolution failure
# happens earlier: go prints one error ("no required module provides package
# ...") and exits without naming a package, so there is nothing to recover from
# and the stage reports zero tests. This is what a test patch importing a module
# whose go.mod entry lives in the fix patch looks like. Resolve and retry.
#
# Guarded on zero captured results, so a healthy stage never reaches it.
if ! grep -q '^--- \(PASS\|FAIL\|SKIP\):' /tmp/gotest.out; then
  echo "run_tests.sh: no test lines captured; running go mod tidy and retrying"
  go mod tidy || true
  go test -v -count=1 -p=1 -parallel=1 -vet=off -timeout=20m -run '^Test' ./... > /tmp/gotest.out 2>&1 || status=$?
  cat /tmp/gotest.out
fi

# Synthesize per-test FAIL lines for packages that never compiled.
#
# When a package fails to build, `go test` emits only a package-level line
#   FAIL  github.com/org/repo/internal/conf [setup failed]
# and never a `--- FAIL: TestX` line, because no test binary was produced.
# parse_log() derives failed_tests from `--- FAIL:` lines, so those tests are
# absent from the test-patch stage entirely. f2p is
# (failed with test patch) INTERSECT (passed after fix) -- an empty left side
# yields an empty f2p, and the instance "resolves" vacuously with nothing graded.
#
# This bites whenever the test patch imports a module the base go.mod lacks and
# the dependency bump lives in the fix patch (e.g. PR 2183: new tests import
# gortsplib/v4 while base go.mod pins v3). Semantically every test in an
# unbuildable package IS failing, so we enumerate the package's Test functions
# from source and emit the lines go test could not. Tests that do not pass after
# the fix are filtered out by the intersection anyway, so this cannot invent f2p
# entries -- it only restores ones that were lost.
MOD=$(go list -m 2>/dev/null || echo "")
if [ -n "$MOD" ]; then
  grep -E '^FAIL[[:space:]]+[^[:space:]]+[[:space:]]+\[(setup|build) failed\]' /tmp/gotest.out \
    | awk '{{print $2}}' | sort -u \
    | while read -r pkg; do
        case "$pkg" in
          "$MOD")   dir="." ;;
          "$MOD"/*) dir="${{pkg#$MOD/}}" ;;
          *)        continue ;;
        esac
        [ -d "$dir" ] || continue
        grep -hoE '^func[[:space:]]+Test[A-Za-z0-9_]*' "$dir"/*_test.go 2>/dev/null \
          | awk '{{print $2}}' | sort -u \
          | while read -r t; do echo "--- FAIL: $t (0.00s)"; done
      done
fi
echo "run_tests.sh: go test exited with status=$status"
exit 0
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

export GOTOOLCHAIN=auto
go mod download || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
bash /home/run_tests.sh
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch || {{ echo "Warning: git apply test.patch failed, retrying with --reject..."; git apply --reject --whitespace=nowarn /home/test.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
bash /home/run_tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
# Applied as two invocations, not one. `git apply a b` is all-or-nothing across
# both files, so a single unappliable hunk anywhere aborts everything and drops
# BOTH patches into the --reject fallback, which applies hunks loosely and then
# deletes the .rej evidence. Observed on PR 1502: a logo.png in fix.patch failed
# to apply, and the fallback left conn_test.go with a duplicate switch case, so
# internal/rtmp never compiled and its tests were recorded as failing for a
# reason unrelated to the patch. Split, test.patch still applies down the strict
# path when only fix.patch is at fault.
git apply --whitespace=nowarn /home/test.patch || {{ echo "Warning: git apply test.patch failed, retrying with --reject..."; git apply --reject --whitespace=nowarn /home/test.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
git apply --whitespace=nowarn /home/fix.patch || {{ echo "Warning: git apply fix.patch failed, retrying with --reject..."; git apply --reject --whitespace=nowarn /home/fix.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
bash /home/run_tests.sh

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        # DockerfileEnhancer.enhance() returns the raw content unchanged whenever
        # dependency() is an Image, so this layer must emit everything the
        # single-level build used to get for free: the clone, the checkout and the
        # hardening block. The proxy/cert ENV, the CA symlinks and the labels are
        # not repeated -- they are baked into ImageBase and inherited.
        #
        # REPO_URL/BASE_COMMIT are declared as ARGs with literal defaults because
        # build_dataset only passes those build args to images whose dependency()
        # is a string (i.e. only to ImageBase). The defaults keep `${BASE_COMMIT}`
        # resolvable inside the hardening block, which is written against that
        # variable.
        base = self.dependency()
        repo = _safe_path_component(self.pr.repo)
        org = _safe_path_component(self.pr.org, "org")

        # The staged helper scripts + patches live outside /home/{repo}, so the
        # hardening pass leaves them untouched.
        copy_commands = "\n".join(f"COPY {f.name} /home/{f.name}" for f in self.files())

        sections = [f"FROM {base.image_full_name()}"]

        if self.global_env:
            sections.append(self.global_env)

        sections.append(
            f'ARG REPO_URL="https://github.com/{org}/{repo}.git"\n'
            f'ARG BASE_COMMIT="{self.pr.base.sha}"'
        )
        sections.append("WORKDIR /home/")
        sections.append(f'RUN git clone "${{REPO_URL}}" /home/{repo}')
        sections.append(f"WORKDIR /home/{repo}")
        sections.append("RUN git reset --hard\nRUN git checkout ${BASE_COMMIT}")
        sections.append(f"{copy_commands}\nRUN bash /home/prepare.sh")
        # Strips every other ref/commit so the fix can't be read out of git history.
        sections.append(Image._HARDENING_BLOCK)

        if self.clear_env:
            sections.append(self.clear_env)

        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


@Instance.register("bluenviron", "mediamtx")
class Mediamtx(Instance):
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

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_pass_tests = [re.compile(r"--- PASS: (\S+)")]
        re_fail_tests = [
            re.compile(r"--- FAIL: (\S+)"),
            re.compile(r"FAIL:?\s?(.+?)\s"),
        ]
        re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

        def get_base_name(test_name: str) -> str:
            return test_name

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    test_name = pass_match.group(1)
                    if test_name in failed_tests:
                        continue
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    passed_tests.add(get_base_name(test_name))

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    test_name = fail_match.group(1)
                    if test_name in passed_tests:
                        passed_tests.remove(test_name)
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    failed_tests.add(get_base_name(test_name))

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    test_name = skip_match.group(1)
                    if test_name in passed_tests:
                        continue
                    if test_name not in failed_tests:
                        continue
                    skipped_tests.add(get_base_name(test_name))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# Bundled-PR routing. Every record in dataset2/bluenviron__mediamtx_lht_final.jsonl
# carries a `number_interval` -- the hyphen-joined `prs_in_bundle` list for that
# bundle (e.g. "1003-1039-1047"). Instance.create() routes on
# f"{org}/{number_interval}" whenever number_interval is non-empty, falling back
# to f"{org}/{repo}" only when it is empty. So every interval a record can carry
# must be registered to this same Mediamtx config, in addition to the plain
# "bluenviron/mediamtx" registration above (which still serves any record that
# omits the field) -- otherwise create() raises "Instance ... is not registered"
# before any image is built.
#
# One config covers all 73 bundles: see the module docstring at the top -- the Go
# toolchain, test framework and log format are stable across the whole range, and
# GOTOOLCHAIN=auto resolves whatever go.mod asks for.
_NUMBER_INTERVALS = [
    "471-473-474-483",
    "509-515-543-551",
    "846-858-871-883-909",
    "970-1417-1436-1463-1467",
    "1003-1039-1047",
    "1179-1198",
    "1212-1218-1219",
    "1233-1234-1235-1239-1245-1249",
    "1242-1301",
    "1323-1333-1342-1345",
    "1383-1384-1394-1395-1398-1405-1408-1409-1411",
    "1490-1566-1567-1571-1574-1583-1585-1587-1592-1593-1594-1595-1596-1598-1599-1601-1602-1604-1610-1611-1614-1626-1629-1633-1636-1637-1638-1639-1640-1641-1642",
    "1502-1503-1504-1505-1506-1510-1511-1528-1530-1542-1545-1547-1548-1549-1556-1557-1560-1561-1563-1564-1565",
    "1546-1721-1727-1732-1736-1737-1738-1740-1741-1746-1751-1754-1755-1756-1763-1768-1772-1774-1775-1776-1778-1779-1780-1781-1783-1784-1786-1789-1797-1798-1799-1800-1801-1803-1804-1805-1806-1807-1808-1809",
    "1653-1655-1661-1663-1666-1668-1671-1672-1678-1682-1683-1684-1687-1688-1689-1690-1698-1699-1700",
    "1810-1811-1815-1821-1822-1823-1824-1825-1826-1828-1829-1830-1831-1832-1833-1834-1835",
    "1854-1855-1856-1861",
    "1862-1867-1868-1869-1870-1871-1872-1873-1874-1878-1879-1880-1885-1886-1887-1897-1898-1899-1900-1905",
    "1924-1932-1944-1945-1946-1947-1956-1964-1965-1966-1967-1968-1969",
    "1972-1973-1975-1976-1978-1983-1984-1985-1989-1990-1991-1995-1997-1998-2000-2002-2006",
    "2010-2015-2016-2020-2021-2022-2025-2027-2031-2040-2044-2050-2051-2057-2064-2065-2071-2072-2073-2074-2075-2079-2080-2081-2082-2083-2086-2087",
    "2068-2089-2091-2092-2093-2096-2097-2098-2099-2100-2101-2105-2106-2107-2110-2112-2113-2117-2120-2121-2122-2123-2124-2125-2126-2127-2129-2130-2134-2138-2139-2140-2141-2142-2144-2145-2146-2147-2148-2150-2151-2152-2153-2154-2155-2157-2158-2159-2160-2161-2162-2163-2164-2169-2170-2171-2174-2176-2177-2178-2179",
    "2183-2184-2185-2189-2194-2195-2196-2201-2211-2212-2213-2216-2220-2228-2229-2232-2234-2236-2237-2238-2239-2243-2244-2245-2247-2248-2249-2250-2251-2252-2253-2256-2262-2263-2264-2265-2266-2267",
    "2255-2277-2283-2292-2293-2294-2298-2299-2300-2301-2321-2322-2328-2329-2332-2337-2339-2340-2344-2354-2355-2356-2357-2359-2360",
    "2269-2273-2278-2279",
    "2362-2366-2367-2368-2369-2376-2381-2385-2389-2390-2391-2399-2400-2401-2405",
    "2409-2413-2414-2415-2421-2425-2426-2428-2431-2434-2435-2437-2441-2443-2444-2446-2447-2450-2453-2455-2456-2457-2460-2465-2468-2470-2471-2472-2475-2476-2479-2482-2493-2501-2503-2505-2506",
    "2507-2515-2520-2522-2530-2532-2544-2546-2550-2552-2553-2559-2560-2569-2570-2571-2572-2580-2582-2583-2586-2590-2591-2592-2594-2595-2596-2597",
    "2584-2607-2611-2612-2613-2614-2616-2622-2626-2627-2628-2629-2630-2631-2634-2637-2638-2644-2645-2646-2648-2652-2662-2663-2667-2668-2669-2671-2673-2674-2675-2676-2677-2681-2685-2686-2687",
    "2656-2691-2692-2695-2702-2705-2706-2711-2714-2715-2722-2736-2740-2744-2745",
    "2746-2751-2761-2764-2770-2771-2772-2773-2774-2775-2776-2777-2782-2783-2785-2791-2792-2794-2795-2798-2799-2800",
    "2801-2804-2806-2808-2811-2814-2815-2817-2820-2821-2823-2825",
    "2828-2842-2843-2844-2852-2853-2862-2868-2870-2873-2884-2885",
    "2888-2891-2896-2897-2900-2903-2906-2907-2909-2910-2911-2912-2913-2918-2919-2920-2924-2925-2928-2934-2935",
    "2939-2945-2948-2952-2953-2954-2956-2957-2958-2959-2964-2968-2978-2979-2980-2981-2987-2988-2989",
    "2962-2993-2999-3002-3007-3008-3011-3013-3014-3015-3016-3017-3018-3019-3020-3025-3030-3035-3037-3038-3040-3042-3043-3044-3049-3053-3069-3071-3072-3078-3081-3087-3089-3090-3096",
    "3098-3099-3100-3102-3103-3104-3105-3110-3113-3120-3124-3128-3132-3133-3136-3139-3149-3150-3151-3153-3154-3164-3168-3171-3178-3179-3180-3189-3191-3192-3197-3198-3199-3200-3202-3203-3204-3205-3209-3210-3211-3212-3213-3215-3216-3217-3222-3228-3229-3231-3232-3233-3240-3241",
    "3245-3249-3250-3253-3254-3255-3259-3260-3261-3262-3263-3264-3265-3272-3274-3276-3277-3278-3279-3280-3281",
    "3288-3300-3302-3316-3318-3323-3325",
    "3314-3329-3330-3334-3341-3356-3362-3363-3364-3367-3368-3369-3370",
    "3374-3375-3385-3388-3402-3406-3407-3410-3414-3416-3417-3421-3423-3424-3425-3426-3427-3436-3437-3441-3442-3443-3444-3445-3446-3447-3448-3449-3450-3456-3457-3458-3459-3460",
    "3409-3673-3781-3787-3793-3794-3795-3798-3806-3807-3815-3817-3824-3825-3827-3828-3830-3831-3833-3834-3835-3836-3837-3839-3840-3843-3844",
    "3431-4435-4436-4438-4440-4442-4453-4455-4456-4461-4462-4464-4465-4470-4471-4472-4475-4478-4479-4480-4484-4487-4488-4489",
    "3464-3470-3484-3486-3492-3504-3530-3532-3533-3534-3535-3537",
    "3515-3619-3622-3624-3626-3627-3628-3643-3647-3650-3651-3655-3656-3665-3667-3670-3672-3674-3679-3681-3692-3693-3697-3698",
    "3538-3540-3546-3549-3559-3594-3595-3596-3597-3598-3604-3605-3607-3608-3609-3610-3611-3612",
    "3699-3721-3725-3734-3735-3741-3744-3746-3750-3751-3752-3753-3757-3760-3763-3767-3771-3772-3773-3775-3776-3779-3780",
    "3702-3896-3899-3900-3920-3921-3923-3925-3926-3927-3930-3938-3940-3946-3947-3948-3950-3955-3979-3988-3996-3997-4003-4004",
    "3761-3939-3981-4051-4108-4110-4112-4113-4115-4116-4117-4119-4122-4125-4126-4129-4130-4131-4132-4133-4134-4135-4140-4141-4144-4145-4146",
    "3847-3849-3850-3851-3852-3853-3856-3857-3869-3870-3872-3876-3885-3891-3893-3894-3895",
    "3992-4007-4008-4010-4016-4018-4019-4026-4029-4033-4034-4039-4044-4045-4047-4048-4052-4062-4068-4070-4071-4072-4073-4074-4075-4076-4078-4080-4085-4087-4088-4090-4091-4094-4095-4096-4097-4099-4100-4102",
    "4152-4154-4155-4156-4162-4169-4171-4173-4174-4175-4176-4177-4181-4185-4190-4193-4194",
    "4205-4208-4212-4213-4216-4221-4223-4224-4231-4232-4236",
    "4218-4241-4767-4774-4783-4784-4785-4786-4789-4790-4793-4794-4795-4796-4797-4800-4801-4802-4808-4809-4813-4814-4816-4818-4820-4823-4824-4825-4828-4830-4833-4834-4835-4836-4837-4838-4839-4840-4841-4842-4843-4844-4845-4846-4849-4850-4852-4853-4854-4855-4856-4857-4859-4860-4861-4862-4863-4866",
    "4245-4246-4249-4254-4255-4259-4260-4261-4264-4266-4267-4271-4279-4280-4286-4287-4291-4293-4294-4295-4296-4298-4299-4301-4302-4303-4308-4312-4314-4322-4331-4332-4333-4334-4335-4337-4338-4345-4348-4355-4356-4360-4363-4364-4366-4367-4369-4370-4372-4374-4375-4376-4378-4382-4387-4394-4395-4397-4398-4399-4401-4404-4406-4411-4414-4415-4416-4417-4418-4419-4420-4422-4423-4425-4426-4427-4428-4429-4430",
    "4297-4463-4467-4496-4498-4503-4511-4512-4513-4514-4515-4516-4517-4518-4519-4520-4522-4523-4530-4532-4538-4539-4541-4542-4543-4549-4550-4552-4553-4555-4556-4557-4558-4562-4563-4564-4566-4567-4568-4569-4570-4571",
    "4575-4576-4581-4582-4583-4584-4586-4590-4597-4598-4600-4601-4602-4603-4604-4606-4609-4620-4621-4622-4626-4627-4628-4629-4632-4633-4634-4638-4639-4645-4646-4653-4654-4661-4667-4670-4671-4677-4679-4683-4689-4690-4691-4692-4693-4695",
    "4668-5440-5441-5444-5445-5446-5447-5449-5452-5454-5456-5459-5460-5461-5462-5465-5469-5470-5471-5474-5475-5478-5479-5480-5481-5483-5484-5486-5488-5489-5490-5492-5493-5495-5497-5498-5499-5500-5501-5502-5503-5504-5506-5507-5508",
    "4696-4697-4700-4703-4706-4707-4708-4713-4715-4718-4721-4723-4727-4728-4729-4731-4738-4758-4759-4760-4763-4764-4766",
    "4722-5308-5309-5310-5311-5312-5313-5314-5315-5316-5319-5322-5323-5324-5326-5328-5329-5331-5335-5337-5338-5340-5341-5344-5350-5353-5354-5357-5361-5367-5368-5370-5371-5372-5373-5374-5375-5376-5377-5379-5380-5385-5388-5389-5390-5391-5392-5394-5395-5396-5397",
    "4867-4869-4870-4876-4877-4883-4887-4893-4894-4898-4901-4903-4905-4907-4909-4910-4912-4914-4915-4918-4920-4921-4922-4929-4930-4932-4933-4936-4940-4941-4945-4946-4947-4952-4954-4956-4957-4962-4965-4966-4967-4968-4969-4973-4974-4975-4978-4979-4980-4985-4986-4990-4991-4992-4993-4994-4995",
    "5003-5006-5007-5009-5010-5011-5012-5015-5017-5018-5021-5022-5023-5024-5025-5026-5028-5031-5032",
    "5035-5036-5037-5039-5045-5047-5048-5050-5051-5056-5061-5062-5065-5067-5070-5072-5077-5078-5079-5080-5081-5082-5083-5084-5086-5088-5089",
    "5063-5125-5126-5129-5130-5131-5134-5138-5139-5141-5142-5147-5148-5150-5152-5158-5173-5174-5175-5176-5177-5179-5182-5184-5185-5186-5187-5190-5191-5193-5194-5203-5204-5205-5211-5212-5213-5216-5222-5223",
    "5166-5172-5210-5228-5237-5239-5244-5247-5249-5250-5251-5252-5254-5255-5257-5258-5259",
    "5219-5261-5265-5268-5269-5271-5272-5277-5278-5282-5283-5284-5285-5286-5287-5288-5289-5290-5295-5296-5297-5298-5299-5300-5301",
    "5402-5404-5408-5417-5418-5419-5420-5422-5424-5425-5429-5430-5432-5433",
    "5476-5544-5547-5552-5554-5555-5557-5560-5561-5563-5564-5565-5566-5567-5568-5569-5570-5571-5573-5575-5576-5577-5579-5580-5581-5582-5583-5585",
    "5509-5511-5515-5517-5523-5524-5526-5527-5528-5529-5530-5533-5534-5535-5536-5537-5538-5539-5541-5542-5543",
    "5586-5595-5597-5598-5601-5602-5603-5605-5607-5609-5615-5616-5619-5620-5621-5622-5623-5625-5626-5628-5629-5630",
    "5604-5699-5701-5702-5703-5704-5705-5706-5707-5708-5709-5710-5711-5713-5715-5716-5717-5719-5720-5721",
    "5624-5631-5635-5636-5637-5638-5639-5640-5641-5644-5645-5646-5647-5648-5649-5650-5651-5654-5655-5656-5658-5659-5660-5661-5662-5663-5665-5666-5667-5673-5674-5675-5676-5677-5683-5684-5685-5686-5687-5688-5689-5690-5691-5692-5693-5695-5696-5697-5698",
    "5722-5723-5725-5726-5727-5731-5735-5738-5743-5746-5747-5748-5749-5751-5752-5756-5757-5758-5760-5761-5768-5769-5770-5771-5772-5773",
]
for _interval in _NUMBER_INTERVALS:
    Instance.register("bluenviron", _interval)(Mediamtx)
