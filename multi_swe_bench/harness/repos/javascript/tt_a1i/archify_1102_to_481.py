"""tt-a1i/archify -- raw single-PR records (PRs 481..1102, Sep-Oct 2026).

The records carry `repo: "archify"` and no `number_interval`, so Instance.create
routes them to `tt-a1i/archify`, which is what this module registers.

ERA: 30 PRs over 21 distinct base commits. Two repository layouts occur:
  * current layout (28 PRs): root package.json + package-lock.json and
    archify/package-lock.json; tests are test/*.test.mjs at the repo root, run by
    scripts/run-tests.mjs as `node --test --test-concurrency=N test/*.test.mjs` from the
    root. Upstream CI (full-regression job) installs with
    `npm ci && npm --prefix archify ci`.
  * older layout (#626 at 922c1cad, #914 at 58ab0b98): no root package.json; tests are
    archify/test/*.test.mjs, run by the same script from archify/; CI installs with
    `npm ci` in archify/.
  Both runners are thin wrappers around `node --test` over every `*.test.mjs` in their
  test directory, with ARCHIFY_UPDATE_CACHE_DIRECTORY pointed at a scratch directory.
  This config runs exactly that command, detecting the layout at run time (a test patch
  can add new test files, so the file list is taken after patches are applied).
  * Other suites, each run as upstream runs it, from the package that owns it:
    website/ (CI's website job: `npm ci`, `npm run build`, `npm test` = node --test
    test/*.test.mjs -- tests read the built dist/), integrations/<name>/test and
    archify/modules/<name>/test. Test patches of #626, #784, #819, #914, #939, #1007
    and #1102 change tests in these suites.
  * package.json `engines.node` is >=18 at every base; CI's canonical lane is Node 22.
  * Browser cases skip themselves unless ARCHIFY_CHROME is set; CI's regression job
    does not set it and neither does this config.

SHARED BASE architecture: image_tag() is PR-independent ("base-1102_to_481"), so one
base image serves all 30 PRs. The base keeps FULL history and never references
BASE_COMMIT; the pin lives in prepare.sh and the history scrub in the PR layer. Line 1
of the base Dockerfile is the `# syntax` directive, which keeps DockerfileEnhancer from
rewriting the clone into a pinned single-commit image.
"""

from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

__all__ = [
    "ARCHIFY_1102_TO_481",
    "ArchifyImageBase",
    "ArchifyImageDefault",
    "parse_archify_log",
]

# Shared base for this era-shard, named like the file: PRs 1102..481 (QC 2F).
BASE_TAG = "base-1102_to_481"

# engines.node >=18 at every base; upstream CI's canonical lane ("test (22)") is Node 22.
# Pinned to an exact release: scripts/build-zip.sh and the byte-for-byte archive tests
# require Node 22 with bundled zlib 1.3.1-e00f703 (what 22.23.4 ships); a floating tag
# could move zlib and change which of those tests run.
NODE_IMAGE = "node:22.23.4-bookworm"

# Unprivileged account the suite runs under, as on a CI runner. Root bypasses
# permission bits, which changes the outcome of file-permission cases. Not named
# "archify": its home would be /home/archify, which is where the repo is cloned.
TEST_USER = "archifyci"
TEST_UID = "1500"

# Per-test result lines written by the reporter below, and the per-file key.
RESULT_MARKER = "@@ARCHIFY@@"
FILE_TEST_SUFFIX = "<file>"

# Generous bound for one full suite run (~7 min alone, slower when stages run side by
# side). A run that hits it is killed and recorded as incomplete, never as passed.
SUITE_TIMEOUT = 3600

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.M)
_BINARY_STUB_RE = re.compile(r"^Binary files .* differ$")


def _patch_blocks(patch: str) -> list[tuple[str, str]]:
    blocks = []
    for block in re.split(r"(?m)^(?=diff --git )", patch or ""):
        m = _DIFF_HEADER_RE.match(block)
        if m:
            blocks.append((m.group(2), block))
    return blocks


def binary_stub_paths(pr: PullRequest) -> list[str]:
    """Paths whose diff block is a header-only binary stub.

    The records store binary changes as `Binary files a/X and b/X differ` without a
    `GIT binary patch` payload (29 of 30 fix patches: archify.zip, docs gif/png assets,
    and in #784/#914 archify/renderers/shared/diagnostics.mjs, which git classifies as
    binary). Such a block cannot be applied, and git apply is atomic, so one stub would
    stop the whole patch. The stubs are excluded by path so every text change still
    applies exactly as recorded; the content they omit is not in the record and cannot
    be reconstructed without leaking the solution.
    """
    paths = []
    for path, block in _patch_blocks(pr.test_patch) + _patch_blocks(pr.fix_patch):
        # Only the block HEADER decides: a text file may contain the words.
        header = block.split("\n@@", 1)[0].splitlines()
        if any(_BINARY_STUB_RE.match(l) for l in header) and not any(
            l == "GIT binary patch" for l in header
        ):
            paths.append(path)
    return sorted(set(paths))


# --- Stub content the records omit, pinned by the records' own blob ids -------------
# #784 and #914 change archify/renderers/shared/diagnostics.mjs, a JavaScript source
# file that git classifies as binary (line 37 holds literal NUL separators), so both
# fix patches record only `Binary files ... differ` with `index <a>..9fbd25d55`. That
# change is part of the fix itself: it adds `withIsolatedDiagnosticRecording`, which
# the rest of the fix and both test patches import. Without it neither fix can load.
# The content below is the upstream blob whose id the records name; fix-run.sh checks
# the file's blob id before (the record's `a` side) and after (the full `b` id) writing
# it, so it can only ever produce exactly the post-image the record declares. Applied
# in the fix stage only, exactly where the record's fix patch applies it.
SUPPLIED_STUB_BLOBS: dict[str, tuple[str, str]] = {
    "9fbd25d557f47f64b142b2d5393ee921d6f337cd": (
        "archify/renderers/shared/diagnostics.mjs",
        """\
aW1wb3J0IGZzIGZyb20gJ25vZGU6ZnMnOwppbXBvcnQgcGF0aCBmcm9tICdub2RlOnBhdGgnOwoK
Y29uc3QgRElBR05PU1RJQ19NT0RFID0gcHJvY2Vzcy5lbnYuQVJDSElGWV9ESUFHTk9TVElDX0ZP
Uk1BVCA9PT0gJ2pzb24nOwpsZXQgcmVjb3JkZWQgPSBbXTsKbGV0IHJlY29yZGVkS2V5cyA9IG5l
dyBTZXQoKTsKbGV0IHJlY29yZGVkQnlNZXNzYWdlID0gbmV3IE1hcCgpOwpjb25zdCBib3VuZGFy
eUtleSA9IFN5bWJvbC5mb3IoJ2FyY2hpZnkucmVuZGVyZXItZGlhZ25vc3RpYy1ib3VuZGFyeScp
OwpsZXQgcmVjb3JkaW5nU3VwcHJlc3Npb25EZXB0aCA9IDA7CgpmdW5jdGlvbiBwbGFpbk9iamVj
dCh2YWx1ZSkgewogIGlmICghdmFsdWUgfHwgdHlwZW9mIHZhbHVlICE9PSAnb2JqZWN0JyB8fCBB
cnJheS5pc0FycmF5KHZhbHVlKSkgcmV0dXJuIHt9OwogIHJldHVybiBPYmplY3QuZnJvbUVudHJp
ZXMoT2JqZWN0LmVudHJpZXModmFsdWUpLmZpbHRlcigoWywgZW50cnldKSA9PiBlbnRyeSAhPT0g
dW5kZWZpbmVkKSk7Cn0KCmZ1bmN0aW9uIG5vcm1hbGl6ZWREaWFnbm9zdGljKGRpYWdub3N0aWMp
IHsKICBjb25zdCBtZXNzYWdlID0gU3RyaW5nKGRpYWdub3N0aWM/Lm1lc3NhZ2UgfHwgJ0FyY2hp
ZnkgY291bGQgbm90IGNsYXNzaWZ5IHRoaXMgZmFpbHVyZS4nKS50cmltKCk7CiAgcmV0dXJuIHsK
ICAgIGNvZGU6IFN0cmluZyhkaWFnbm9zdGljPy5jb2RlIHx8ICdpbnRlcm5hbC91bmNsYXNzaWZp
ZWQnKSwKICAgIHNldmVyaXR5OiBkaWFnbm9zdGljPy5zZXZlcml0eSA9PT0gJ3dhcm5pbmcnID8g
J3dhcm5pbmcnIDogJ2Vycm9yJywKICAgIG1lc3NhZ2UsCiAgICBzdWJqZWN0OiBwbGFpbk9iamVj
dChkaWFnbm9zdGljPy5zdWJqZWN0KSwKICAgIGV2aWRlbmNlOiBwbGFpbk9iamVjdChkaWFnbm9z
dGljPy5ldmlkZW5jZSksCiAgICBzdXBwb3J0ZWRGaXhlczogQXJyYXkuaXNBcnJheShkaWFnbm9z
dGljPy5zdXBwb3J0ZWRGaXhlcykKICAgICAgPyBbLi4ubmV3IFNldChkaWFnbm9zdGljLnN1cHBv
cnRlZEZpeGVzLm1hcCgoZml4KSA9PiBTdHJpbmcoZml4KS50cmltKCkpLmZpbHRlcihCb29sZWFu
KSldCiAgICAgIDogW10sCiAgICAuLi4oQXJyYXkuaXNBcnJheShkaWFnbm9zdGljPy5zdXBwcmVz
c2VzKSA/IHsKICAgICAgc3VwcHJlc3NlczogWy4uLm5ldyBTZXQoZGlhZ25vc3RpYy5zdXBwcmVz
c2VzLm1hcCgoY29kZSkgPT4gU3RyaW5nKGNvZGUpLnRyaW0oKSkuZmlsdGVyKEJvb2xlYW4pKV0s
CiAgICB9IDoge30pLAogIH07Cn0KCmV4cG9ydCBmdW5jdGlvbiByZWNvcmREaWFnbm9zdGljKGRp
YWdub3N0aWMpIHsKICBpZiAoIURJQUdOT1NUSUNfTU9ERSB8fCByZWNvcmRpbmdTdXBwcmVzc2lv
bkRlcHRoID4gMCkgcmV0dXJuOwogIGNvbnN0IG5vcm1hbGl6ZWQgPSBub3JtYWxpemVkRGlhZ25v
c3RpYyhkaWFnbm9zdGljKTsKICAvLyBJZGVudGljYWwgbWVzc2FnZXMgY2FuIGRlc2NyaWJlIGRp
ZmZlcmVudCBzdWJqZWN0czsgb25seSBleGFjdCByZXBlYXRzIGRlZHVwZS4KICBjb25zdCBrZXkg
PSBgJHtub3JtYWxpemVkLmNvZGV9ACR7bm9ybWFsaXplZC5tZXNzYWdlfQAke0pTT04uc3RyaW5n
aWZ5KG5vcm1hbGl6ZWQuc3ViamVjdCl9YDsKICBpZiAocmVjb3JkZWRLZXlzLmhhcyhrZXkpKSBy
ZXR1cm47CiAgcmVjb3JkZWRLZXlzLmFkZChrZXkpOwogIHJlY29yZGVkLnB1c2gobm9ybWFsaXpl
ZCk7CiAgaWYgKCFyZWNvcmRlZEJ5TWVzc2FnZS5oYXMobm9ybWFsaXplZC5tZXNzYWdlKSkgcmVj
b3JkZWRCeU1lc3NhZ2Uuc2V0KG5vcm1hbGl6ZWQubWVzc2FnZSwgbm9ybWFsaXplZCk7Cn0KCmV4
cG9ydCBmdW5jdGlvbiB3aXRoRGlhZ25vc3RpY1JlY29yZGluZ1N1cHByZXNzZWQoY2FsbGJhY2sp
IHsKICByZWNvcmRpbmdTdXBwcmVzc2lvbkRlcHRoICs9IDE7CiAgdHJ5IHsKICAgIHJldHVybiBj
YWxsYmFjaygpOwogIH0gZmluYWxseSB7CiAgICByZWNvcmRpbmdTdXBwcmVzc2lvbkRlcHRoIC09
IDE7CiAgfQp9CgovLyBTcGVjdWxhdGl2ZSBzeW5jaHJvbm91cyB3b3JrIG5lZWRzIHR5cGVkIG1l
c3NhZ2UgbG9va3VwIHdpdGhvdXQgcHVibGlzaGluZwovLyBpdHMgZGlzY2FyZGVkIGRpYWdub3N0
aWNzIGludG8gdGhlIGNhbGxlcidzIGV2ZW50dWFsIHJlbmRlcmVyIGZhaWx1cmUuCmV4cG9ydCBm
dW5jdGlvbiB3aXRoSXNvbGF0ZWREaWFnbm9zdGljUmVjb3JkaW5nKGNhbGxiYWNrKSB7CiAgY29u
c3QgcHJldmlvdXNSZWNvcmRlZCA9IHJlY29yZGVkOwogIGNvbnN0IHByZXZpb3VzS2V5cyA9IHJl
Y29yZGVkS2V5czsKICBjb25zdCBwcmV2aW91c0J5TWVzc2FnZSA9IHJlY29yZGVkQnlNZXNzYWdl
OwogIHJlY29yZGVkID0gW107CiAgcmVjb3JkZWRLZXlzID0gbmV3IFNldCgpOwogIHJlY29yZGVk
QnlNZXNzYWdlID0gbmV3IE1hcCgpOwogIHRyeSB7CiAgICByZXR1cm4gY2FsbGJhY2soKTsKICB9
IGZpbmFsbHkgewogICAgcmVjb3JkZWQgPSBwcmV2aW91c1JlY29yZGVkOwogICAgcmVjb3JkZWRL
ZXlzID0gcHJldmlvdXNLZXlzOwogICAgcmVjb3JkZWRCeU1lc3NhZ2UgPSBwcmV2aW91c0J5TWVz
c2FnZTsKICB9Cn0KCmV4cG9ydCBmdW5jdGlvbiB0aHJvd0RpYWdub3N0aWNFcnJvcihtZXNzYWdl
LCBkaWFnbm9zdGljcykgewogIGZvciAoY29uc3QgZGlhZ25vc3RpYyBvZiBkaWFnbm9zdGljcyB8
fCBbXSkgcmVjb3JkRGlhZ25vc3RpYyhkaWFnbm9zdGljKTsKICBjb25zdCBlcnJvciA9IG5ldyBF
cnJvcihtZXNzYWdlKTsKICBlcnJvci5hcmNoaWZ5RGlhZ25vc3RpY3MgPSAoZGlhZ25vc3RpY3Mg
fHwgW10pLm1hcChub3JtYWxpemVkRGlhZ25vc3RpYyk7CiAgdGhyb3cgZXJyb3I7Cn0KCmV4cG9y
dCBmdW5jdGlvbiB0aHJvd0RpYWdub3N0aWNQcm9ibGVtcyhwcmVmaXgsIHByb2JsZW1zLCB7IGNv
ZGUgPSAnbGF5b3V0L2NvbnN0cmFpbnQnLCBzdWJqZWN0ID0ge30sIGRpYWdub3N0aWNzOiBkZXRh
aWxzID0gW10gfSA9IHt9KSB7CiAgY29uc3QgbWVzc2FnZXMgPSAocHJvYmxlbXMgfHwgW10pLm1h
cCgocHJvYmxlbSkgPT4gU3RyaW5nKHByb2JsZW0pKTsKICAvLyBDb25zdW1lIG1hdGNoaW5nIGRl
dGFpbHMgaW4gZW1pc3Npb24gb3JkZXI6IHJlcGVhdGVkIG1lc3NhZ2VzIG1hcCB0bwogIC8vIGRp
ZmZlcmVudCBzdWJqZWN0cywgc28gYSBzaW5nbGUgbG9va3VwIHdvdWxkIGF0dHJpYnV0ZSBldmVy
eSBvY2N1cnJlbmNlIHRvCiAgLy8gdGhlIGxhc3QgZGV0YWlsIGVudHJ5LgogIGNvbnN0IHF1ZXVl
c0J5TWVzc2FnZSA9IG5ldyBNYXAoKTsKICBmb3IgKGNvbnN0IGVudHJ5IG9mIGRldGFpbHMpIHsK
ICAgIGNvbnN0IHF1ZXVlID0gcXVldWVzQnlNZXNzYWdlLmdldChlbnRyeS5tZXNzYWdlKTsKICAg
IGlmIChxdWV1ZSkgcXVldWUucHVzaChlbnRyeSk7CiAgICBlbHNlIHF1ZXVlc0J5TWVzc2FnZS5z
ZXQoZW50cnkubWVzc2FnZSwgW2VudHJ5XSk7CiAgfQogIGNvbnN0IGRpYWdub3N0aWNzID0gbWVz
c2FnZXMubWFwKChtZXNzYWdlKSA9PiB7CiAgICBjb25zdCBxdWV1ZSA9IHF1ZXVlc0J5TWVzc2Fn
ZS5nZXQobWVzc2FnZSk7CiAgICAvLyBXaXRob3V0IGEgcXVldWVkIGRldGFpbCwgcmV1c2UgdGhl
IHN0cnVjdHVyZWQgZGlhZ25vc3RpYyBhbHJlYWR5CiAgICAvLyByZWNvcmRlZCB1bmRlciB0aGlz
IG1lc3NhZ2UgcmF0aGVyIHRoYW4gYXR0YWNoaW5nIGEgYmFyZSBmYWxsYmFjay4KICAgIGNvbnN0
IGVudHJ5ID0gcXVldWU/Lmxlbmd0aCA/IHF1ZXVlLnNoaWZ0KCkgOiByZWNvcmRlZEJ5TWVzc2Fn
ZS5nZXQobWVzc2FnZSk7CiAgICByZXR1cm4gbm9ybWFsaXplZERpYWdub3N0aWMoZW50cnkgfHwg
ewogICAgICBjb2RlLAogICAgICBzZXZlcml0eTogJ2Vycm9yJywKICAgICAgbWVzc2FnZSwKICAg
ICAgc3ViamVjdCwKICAgICAgZXZpZGVuY2U6IHt9LAogICAgICBzdXBwb3J0ZWRGaXhlczogW10s
CiAgICB9KTsKICB9KTsKICB0aHJvd0RpYWdub3N0aWNFcnJvcihgJHtwcmVmaXh9OlxuLSAke21l
c3NhZ2VzLmpvaW4oJ1xuLSAnKX1gLCBkaWFnbm9zdGljcyk7Cn0KCmZ1bmN0aW9uIGZhbGxiYWNr
RGlhZ25vc3RpYyhlcnJvcikgewogIGNvbnN0IGlucHV0ID0gcHJvY2Vzcy5hcmd2WzJdID8gcGF0
aC5yZXNvbHZlKHByb2Nlc3MuYXJndlsyXSkgOiB1bmRlZmluZWQ7CiAgcmV0dXJuIG5vcm1hbGl6
ZWREaWFnbm9zdGljKHsKICAgIGNvZGU6ICdpbnRlcm5hbC91bmNsYXNzaWZpZWQnLAogICAgc2V2
ZXJpdHk6ICdlcnJvcicsCiAgICBtZXNzYWdlOiBlcnJvcj8ubWVzc2FnZSB8fCAnUmVuZGVyZXIg
ZmFpbGVkIHdpdGhvdXQgYSBkaWFnbm9zdGljLicsCiAgICBzdWJqZWN0OiB7IGlucHV0IH0sCiAg
ICBldmlkZW5jZTogeyBlcnJvck5hbWU6IGVycm9yPy5uYW1lIHx8ICdFcnJvcicgfSwKICAgIHN1
cHBvcnRlZEZpeGVzOiBbXSwKICB9KTsKfQpleHBvcnQgZnVuY3Rpb24gcmVuZGVyZXJGYWlsdXJl
KGVycm9yKSB7CiAgY29uc3QgYXR0YWNoZWQgPSBBcnJheS5pc0FycmF5KGVycm9yPy5hcmNoaWZ5
RGlhZ25vc3RpY3MpCiAgICA/IGVycm9yLmFyY2hpZnlEaWFnbm9zdGljcy5tYXAobm9ybWFsaXpl
ZERpYWdub3N0aWMpCiAgICA6IFtdOwogIC8vIEVhcmxpZXIgZGlhZ25vc3RpY3MgZG8gbm90IGNs
YXNzaWZ5IGEgbGF0ZXIsIHVucmVsYXRlZCBpbXBsZW1lbnRhdGlvbiBlcnJvci4KICBjb25zdCBk
aWFnbm9zdGljcyA9IGF0dGFjaGVkLmxlbmd0aAogICAgPyAocmVjb3JkZWQubGVuZ3RoID8gcmVj
b3JkZWQgOiBhdHRhY2hlZCkKICAgIDogW2ZhbGxiYWNrRGlhZ25vc3RpYyhlcnJvcildOwogIHJl
dHVybiB7CiAgICBzY2hlbWFWZXJzaW9uOiAxLAogICAgb2s6IGZhbHNlLAogICAgc291cmNlOiAn
cmVuZGVyZXInLAogICAgZXJyb3I6IGVycm9yPy5tZXNzYWdlIHx8ICdSZW5kZXJlciBmYWlsZWQg
d2l0aG91dCBhIGRpYWdub3N0aWMuJywKICAgIGRpYWdub3N0aWNzLAogIH07Cn0KCi8vIE1hdGNo
IHRoZSBwdWJsaWMgQ0xJJ3MgdGV4dCBmb3JtYXQgd2l0aG91dCBtYWtpbmcgaXRzIHN0YW5kYWxv
bmUgZG9jdG9yCi8vIGJvb3RzdHJhcCBkZXBlbmQgb24gdGhpcyByZW5kZXJlciBydW50aW1lIGJl
aW5nIHByZXNlbnQuCmZ1bmN0aW9uIGZvcm1hdERpYWdub3N0aWNzKGVycm9yLCBkaWFnbm9zdGlj
cyA9IFtdKSB7CiAgaWYgKCFkaWFnbm9zdGljcy5sZW5ndGgpIHJldHVybiBlcnJvcjsKICByZXR1
cm4gWwogICAgZXJyb3IsCiAgICAuLi5kaWFnbm9zdGljcy5tYXAoKGVudHJ5KSA9PiB7CiAgICAg
IGNvbnN0IGZpeCA9IGVudHJ5LnN1cHBvcnRlZEZpeGVzPy5sZW5ndGggPyBgIEZpeDogJHtlbnRy
eS5zdXBwb3J0ZWRGaXhlcy5qb2luKCc7ICcpfS5gIDogJyc7CiAgICAgIHJldHVybiBgWyR7ZW50
cnkuY29kZX1dICR7ZW50cnkubWVzc2FnZX0ke2ZpeH1gOwogICAgfSksCiAgXS5qb2luKCdcbicp
Owp9Cgpjb25zdCByZWFkZXJTaWduYWwgPSBuZXcgSW50MzJBcnJheShuZXcgU2hhcmVkQXJyYXlC
dWZmZXIoNCkpOwoKZnVuY3Rpb24gd2FpdEZvclJlYWRlcigpIHsKICAvLyBTbGVlcCBpbnN0ZWFk
IG9mIHNwaW5uaW5nIG9uIEVBR0FJTi4gQSByZXRyeSBidWRnZXQgbG9va3MgbGlrZSBhIHNhZmVn
dWFyZAogIC8vIGFuZCBiZWhhdmVzIGxpa2UgYSB0cnVuY2F0aW9uIGdhdGU6IGEgc3Bpbm5pbmcg
bG9vcCBidXJucyB0aG91c2FuZHMgb2YKICAvLyBhdHRlbXB0cyBpbiBhIGZldyBtaWxsaXNlY29u
ZHMsIHNvIGEgcmVhZGVyIHRoYXQgaXMgbWVyZWx5IHNsb3cgdG8gc3RhcnQKICAvLyBleGhhdXN0
cyBpdCBhbmQgbG9zZXMgdGhlIHRhaWwgb2YgdGhlIHJlY2VpcHQuIFdhaXRpbmcgY29zdHMgbm90
aGluZyB3aGlsZQogIC8vIHRoZSByZWFkZXIgY2F0Y2hlcyB1cCwgYW5kIGEgcmVhZGVyIHRoYXQg
Z29lcyBhd2F5IHJhaXNlcyBFUElQRSwgd2hpY2ggdGhlCiAgLy8gY2FsbGVyIGFscmVhZHkgdHJl
YXRzIGFzIGEgcmVhbCB3cml0ZSBmYWlsdXJlLgogIEF0b21pY3Mud2FpdChyZWFkZXJTaWduYWws
IDAsIDAsIDEpOwp9CgpleHBvcnQgZnVuY3Rpb24gaW5zdGFsbFJlbmRlcmVyRGlhZ25vc3RpY0Jv
dW5kYXJ5KCkgewogIGlmIChnbG9iYWxUaGlzW2JvdW5kYXJ5S2V5XSkgcmV0dXJuOwogIGdsb2Jh
bFRoaXNbYm91bmRhcnlLZXldID0gdHJ1ZTsKICBpZiAoIURJQUdOT1NUSUNfTU9ERSkgewogICAg
cHJvY2Vzcy5vbmNlKCd1bmNhdWdodEV4Y2VwdGlvbicsIChlcnJvcikgPT4gewogICAgICAvLyBP
bmx5IGVycm9ycyBjbGFzc2lmaWVkIGF0IHRoZWlyIG9wZXJhdGlvbiBib3VuZGFyeSBhcmUgYXV0
aG9yLWZhY2luZy4KICAgICAgLy8gUHJlc2VydmUgTm9kZSdzIGRlYnVnZ2luZyBpbmZvcm1hdGlv
biBmb3IgdW5leHBlY3RlZCBpbXBsZW1lbnRhdGlvbiBlcnJvcnMuCiAgICAgIGlmICghZXJyb3I/
LmFyY2hpZnlEaWFnbm9zdGljcz8ubGVuZ3RoKSB7CiAgICAgICAgLy8gVGhlIG9uY2UtbGlzdGVu
ZXIgaXMgYWxyZWFkeSByZW1vdmVkLiBSZXRocm93IG91dHNpZGUgdGhlIGV4Y2VwdGlvbgogICAg
ICAgIC8vIGhhbmRsZXIgc28gTm9kZSByZXRhaW5zIGl0cyBub3JtYWwgc3RhY2sgYW5kIGV4aXQg
Y29kZSAobm90IGNvZGUgNykuCiAgICAgICAgcHJvY2Vzcy5uZXh0VGljaygoKSA9PiB7IHRocm93
IGVycm9yOyB9KTsKICAgICAgICByZXR1cm47CiAgICAgIH0KICAgICAgY29uc3QgcGF5bG9hZCA9
IGAke2Zvcm1hdERpYWdub3N0aWNzKGVycm9yLm1lc3NhZ2UsIGVycm9yLmFyY2hpZnlEaWFnbm9z
dGljcyl9XG5gOwogICAgICBwcm9jZXNzLnN0ZGVyci5vbmNlKCdlcnJvcicsICgpID0+IHByb2Nl
c3MuZXhpdCgxKSk7CiAgICAgIHByb2Nlc3Muc3RkZXJyLndyaXRlKHBheWxvYWQsICgpID0+IHBy
b2Nlc3MuZXhpdCgxKSk7CiAgICB9KTsKICAgIHJldHVybjsKICB9CiAgcHJvY2Vzcy5vbigndW5j
YXVnaHRFeGNlcHRpb24nLCAoZXJyb3IpID0+IHsKICAgIGNvbnN0IHBheWxvYWQgPSBgJHtKU09O
LnN0cmluZ2lmeShyZW5kZXJlckZhaWx1cmUoZXJyb3IpKX1cbmA7CiAgICB0cnkgewogICAgICAv
LyBzdGRlcnIgbWF5IGJlIGEgcGlwZS4gZnMud3JpdGVTeW5jIHBlcmZvcm1zIGEgUEFSVElBTCB3
cml0ZSBvbmNlIHRoZQogICAgICAvLyBwYXlsb2FkIGV4Y2VlZHMgdGhlIE9TIHBpcGUgYnVmZmVy
ICg4S0Igb24gbWFjT1MpIGFuZCByZXR1cm5zIHRoZSBieXRlCiAgICAgIC8vIGNvdW50IGFjdHVh
bGx5IHdyaXR0ZW4uIElnbm9yaW5nIHRoYXQgcmV0dXJuIHZhbHVlIHNpbGVudGx5IHRydW5jYXRl
ZAogICAgICAvLyBsYXJnZSBkaWFnbm9zdGljIHBheWxvYWRzIG1pZC1KU09OLCBzbyB0aGUgcGFy
ZW50IENMSSdzIEpTT04ucGFyc2UKICAgICAgLy8gZmFpbGVkIGFuZCB0aGUgZmFpbC1jbG9zZWQg
Ym91bmRhcnkgcmVwb3J0ZWQgaW50ZXJuYWwvdW5jbGFzc2lmaWVkCiAgICAgIC8vIGluc3RlYWQg
b2YgdGhlIGRpYWdub3N0aWNzIHdlIGhhZCBhbHJlYWR5IGNvbXB1dGVkLiBMb29wIHVudGlsIGRy
YWluZWQuCiAgICAgIC8vIEEgZnVsbCBwaXBlIGFsc28gbWFrZXMgd3JpdGVTeW5jIHRocm93IEVB
R0FJTjsgd2FpdCBmb3IgdGhlIHJlYWRlcgogICAgICAvLyByYXRoZXIgdGhhbiB0cmVhdCBpdCBh
cyBhIHN0cmVhbSBmYWlsdXJlLCBvdGhlcndpc2UgdGhlIHRhaWwgaXMKICAgICAgLy8gZHJvcHBl
ZCBqdXN0IHRoZSBzYW1lLgogICAgICBjb25zdCBidWZmZXIgPSBCdWZmZXIuZnJvbShwYXlsb2Fk
LCAndXRmOCcpOwogICAgICBsZXQgd3JpdHRlbiA9IDA7CiAgICAgIHdoaWxlICh3cml0dGVuIDwg
YnVmZmVyLmxlbmd0aCkgewogICAgICAgIHRyeSB7CiAgICAgICAgICB3cml0dGVuICs9IGZzLndy
aXRlU3luYyhwcm9jZXNzLnN0ZGVyci5mZCwgYnVmZmVyLCB3cml0dGVuLCBidWZmZXIubGVuZ3Ro
IC0gd3JpdHRlbik7CiAgICAgICAgfSBjYXRjaCAod3JpdGVFcnJvcikgewogICAgICAgICAgaWYg
KHdyaXRlRXJyb3I/LmNvZGUgPT09ICdFQUdBSU4nKSB7CiAgICAgICAgICAgIHdhaXRGb3JSZWFk
ZXIoKTsKICAgICAgICAgICAgY29udGludWU7CiAgICAgICAgICB9CiAgICAgICAgICB0aHJvdyB3
cml0ZUVycm9yOwogICAgICAgIH0KICAgICAgfQogICAgfSBjYXRjaCB7CiAgICAgIC8vIFRoZSBy
ZW5kZXJlciBpcyBhbHJlYWR5IGZhaWxpbmcuIEF2b2lkIHJlcGxhY2luZyBpdHMgcmVhbCBlcnJv
ciB3aXRoIGEKICAgICAgLy8gc2Vjb25kYXJ5IHN0cmVhbSBmYWlsdXJlOyB0aGUgcGFyZW50IENM
SSBzdGlsbCBoYXMgdGhlIGV4aXQgc3RhdHVzLgogICAgfQogICAgcHJvY2Vzcy5leGl0KDEpOwog
IH0pOwp9Cg==
""",
    ),
}

_INDEX_RE = re.compile(r"^index ([0-9a-f]+)\.\.([0-9a-f]+)", re.M)


def supplied_stubs(pr: PullRequest) -> list[tuple[str, str, str]]:
    """(path, a-side blob prefix, full b-side blob id) for fix-patch stubs whose
    post-image is in SUPPLIED_STUB_BLOBS."""
    out = []
    stubs = set(binary_stub_paths(pr))
    for path, block in _patch_blocks(pr.fix_patch):
        if path not in stubs:
            continue
        m = _INDEX_RE.search(block.split("\n@@", 1)[0])
        if not m:
            continue
        for full, (known_path, _) in SUPPLIED_STUB_BLOBS.items():
            if known_path == path and full.startswith(m.group(2)):
                out.append((path, m.group(1), full))
    return out


# --- Browser lane ------------------------------------------------------------------
# Browser cases skip unless ARCHIFY_CHROME names a browser. For #626 they are the
# whole signal: its test patch changes two *-browser tests, plus a website test that
# the base already passes. Only for the PRs listed here, the PR layer installs
# Debian's chromium and every stage runs with ARCHIFY_CHROME set (and the launcher's
# own ARCHIFY_CHROME_NO_SANDBOX opt-out, required inside a container). The shared base
# and every other PR are unchanged. #481, #808, #899, #908, #911, #928, #1051 and #1093
# are the same case: every test their test patch changes is a *-browser test (plus,
# for some, scripts/browser-test-inventory.mjs).
BROWSER_PRS = frozenset({481, 626, 808, 899, 908, 911, 928, 1051, 1093})
CHROMIUM = "/usr/bin/chromium"
CHROMIUM_BIN = "/usr/lib/chromium/chromium"  # real binary behind the Debian wrapper


class ArchifyImageBase(Image):
    """Shared base: toolchain + full-history clone, pinned to nothing."""

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
        return NODE_IMAGE

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo
        # ARG BASE_COMMIT is DECLARED (the harness passes it to every build) but never
        # REFERENCED below. Consuming it would pin this shared image to one PR's commit.
        return f"""# syntax=docker/dockerfile:1.6
FROM {NODE_IMAGE}
ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT
ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"
ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}} \\
    CI=true \\
    npm_config_fund=false \\
    npm_config_audit=false \\
    npm_config_update_notifier=false
LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"
RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt
WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates unzip \\
    && rm -rf /var/lib/apt/lists/*
RUN git config --global --add safe.directory '*'
RUN useradd -m -u {TEST_UID} {TEST_USER}
RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null
{self.clear_env}
CMD ["/bin/bash"]
"""


CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
set -e

git rev-parse --is-inside-work-tree > /dev/null 2>&1 || {
  echo "check_git_changes: Not inside a git repository"
  exit 1
}

test -z "$(git status --porcelain)" || {
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
}

echo "check_git_changes: No uncommitted changes"
exit 0
"""


# ---------------------------------------------------------------------------
# prepare.sh -- three sections: PIN, PROVISION, GATE.
# ---------------------------------------------------------------------------
PREPARE_SH = r"""#!/bin/bash
set -euo pipefail

# ========== Section 1: PIN ==========
cd [[REPO_DIR]]

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach [[SHA]]
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "[[SHA]]"

# ========== Section 2: PROVISION ==========
node --version
npm --version

# Every npm package of this commit, exactly as upstream CI installs it: `npm ci`
# against its own package-lock.json -- the root and archify/ (CI's test jobs; only
# archify/ in the older layout), website/ (CI's website job) and, where present, a
# module under archify/modules/. Fixture directories carry no lockfile and are not
# packages. Not tolerated: a failed install is a failed build.
for _lock in $(git ls-files '*package-lock.json' | grep -v '/node_modules/'); do
    npm --prefix "$(dirname "${_lock}")" ci
done

# ========== Section 3: GATE (non-tolerant, last) ==========
# The main suite exists where this commit's runner looks for it.
compgen -G 'test/*.test.mjs' >/dev/null || compgen -G 'archify/test/*.test.mjs' >/dev/null
# Every declared dependency of every installed package is present.
for _lock in $(git ls-files '*package-lock.json' | grep -v '/node_modules/'); do
    (cd "$(dirname "${_lock}")" && node -e "const p = require('./package.json'); const fs = require('fs'); const deps = Object.keys(Object.assign({}, p.dependencies, p.devDependencies)); for (const d of deps) { if (!fs.existsSync('node_modules/' + d + '/package.json')) { throw new Error('missing dependency ' + d + ' in ' + process.cwd()); } } console.log(process.cwd() + ' deps: ' + deps.length);")
done
# The test account and the tools the suite shells out to.
id -u [[USER]]
git --version
unzip -v > /dev/null && echo "unzip: ok"
[[BROWSER_GATE]]echo DEPS_OK
"""


# The one test command every stage runs.
TEST_CMD = r"""# A patch that changes dependencies leaves node_modules from the base commit stale.
# For every package with a (tracked, after staging) package-lock.json: reinstall
# exactly when the applied patches changed or added that lockfile, or changed a
# dependency section of its package.json. Patches that leave dependencies alone -- and
# the baseline -- skip this. Not tolerated: a failed install fails the stage.
for _lock in $(git ls-files '*package-lock.json' | grep -v '/node_modules/'); do
    _dir="$(dirname "${_lock}")"
    [ -f "${_dir}/package.json" ] || continue
    _changed="$(cd "${_dir}" && node -e "
const cp = require('child_process');
const keys = ['dependencies', 'devDependencies', 'optionalDependencies', 'peerDependencies', 'overrides'];
const pick = (j) => JSON.stringify(keys.map((k) => j[k] || {}));
let base = null;
try { base = JSON.parse(cp.execSync('git show HEAD:./package.json', { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] })); } catch (e) {}
const now = require('./package.json');
const lock = cp.execSync('git status --porcelain -- package-lock.json', { encoding: 'utf8' }).trim();
const fresh = !require('fs').existsSync('node_modules');
console.log(lock || fresh ? 'lock' : (base === null || pick(base) !== pick(now)) ? 'deps' : 'no');
")"
    if [ "${_changed}" = "lock" ]; then
        npm --prefix "${_dir}" ci
    elif [ "${_changed}" = "deps" ]; then
        npm --prefix "${_dir}" install
    fi
done

# Run as the unprivileged test account. Patches were applied as root, so ownership is
# handed over first; this happens identically in all three stages.
chown -R [[USER]]:[[USER]] [[REPO_DIR]]

# Suites, each run exactly as upstream runs it: `node --test` over its directory's
# *.test.mjs, from the directory that owns it.
#   main     -- scripts/run-tests.mjs: test/ from the root, or (older layout)
#               archify/test/ from archify/.
#   website  -- website/ `npm test` (= node --test test/*.test.mjs), after
#               `npm run build`, as CI's website job does: several tests read dist/.
#   integrations/<name>, archify/modules/<name> -- each package's own test/.
# The suite list is taken after patches are applied, so suites a patch adds are run.
# Two reporters per suite: `spec` (readable log) and /home/archify_reporter.mjs (one
# marker line per test, which parse_log reads). Everything goes to files and is
# printed after the run -- the container's stdout pipe can silently drop the tail of a
# large live stream. Every exit status is kept and returned, never swallowed.
_cache=/tmp/archify-update-cache
rm -rf /tmp/archify-suite-* "${_cache}"
install -d -o [[USER]] -g [[USER]] "${_cache}"
_rc=0
_as_user() {
    runuser -u [[USER]] -- env HOME=/home/[[USER]] CI=true ASTRO_TELEMETRY_DISABLED=1 \
        ARCHIFY_UPDATE_CACHE_DIRECTORY="${_cache}" \
        ARCHIFY_REPO_ROOT=[[REPO_DIR]] ARCHIFY_RESULT_MARKER="[[MARKER]]" [[BROWSER_ENV]]"$@"
}
_suites=()
if compgen -G '[[REPO_DIR]]/test/*.test.mjs' >/dev/null; then
    _suites+=("[[REPO_DIR]]")
else
    _suites+=("[[REPO_DIR]]/archify")
fi
for _d in [[REPO_DIR]]/website [[REPO_DIR]]/integrations/* [[REPO_DIR]]/archify/modules/*; do
    compgen -G "${_d}/test/*.test.mjs" >/dev/null && _suites+=("${_d}")
done
_n=0
for _d in "${_suites[@]}"; do
    _n=$((_n + 1))
    _log="/tmp/archify-suite-${_n}"
    cd "${_d}"
    if [ "${_d}" = "[[REPO_DIR]]/website" ] && [ -f package.json ]; then
        _as_user timeout --kill-after=30 [[TIMEOUT]] npm run build > "${_log}.build" 2>&1 || _rc=$?
    fi
    _as_user timeout --kill-after=30 [[TIMEOUT]] node --test --test-concurrency=2 \
        --test-reporter=spec --test-reporter-destination="${_log}.spec" \
        --test-reporter=/home/archify_reporter.mjs --test-reporter-destination="${_log}.res" \
        test/*.test.mjs > "${_log}.out" 2>&1 || _rc=$?
done
cd [[REPO_DIR]]
_n=0
for _d in "${_suites[@]}"; do
    _n=$((_n + 1))
    _log="/tmp/archify-suite-${_n}"
    echo "===== suite ${_d#[[REPO_DIR]]/} ====="
    cat "${_log}.build" "${_log}.out" "${_log}.spec" 2>/dev/null || true
done
echo "test runner exit status: ${_rc}"
_n=0
for _d in "${_suites[@]}"; do
    _n=$((_n + 1))
    echo "[[MARKER]] SUITE ${_n}"
    cat "/tmp/archify-suite-${_n}.res" 2>/dev/null || true
done
rm -rf "${_cache}"
exit "${_rc}"
"""


# node:test custom reporter. Prints one line per test:
#   <marker> PASS|FAIL|SKIP <repo-relative file>::<parent> > <child> > <test>
# then `<file>::<file>` per test file (FAIL if the file failed to load or any of its
# top-level tests failed) and a final `<marker> END <n>`. A subtest's result arrives
# before its parent's, so children are collected per nesting level and emitted under
# their parent's full title.
REPORTER_MJS = r"""import path from 'node:path';

const MARKER = process.env.ARCHIFY_RESULT_MARKER || '@@ARCHIFY@@';
const ROOT = process.env.ARCHIFY_REPO_ROOT || process.cwd();
const one = (s) => String(s).replace(/\s+/g, ' ').trim();

export default async function* archifyReporter(source) {
  const pending = new Map();
  const fileFailed = new Map();
  let total = 0;
  const rel = (f) => (f ? path.relative(ROOT, f).split(path.sep).join('/') : '<unknown>');
  const lines = (file, node, prefix, out) => {
    const full = prefix ? `${prefix} > ${node.name}` : node.name;
    out.push(`${MARKER} ${node.status} ${file}::${full}\n`);
    for (const child of node.children) lines(file, child, full, out);
  };
  for await (const event of source) {
    if (event.type !== 'test:pass' && event.type !== 'test:fail') continue;
    const data = event.data;
    const file = rel(data.file);
    if (!pending.has(file)) pending.set(file, new Map());
    const levels = pending.get(file);
    const nesting = data.nesting ?? 0;
    const children = levels.get(nesting + 1) || [];
    levels.delete(nesting + 1);
    const skipped = (data.skip !== undefined && data.skip !== false)
      || (data.todo !== undefined && data.todo !== false);
    const status = skipped ? 'SKIP' : event.type === 'test:pass' ? 'PASS' : 'FAIL';
    if (!fileFailed.has(file)) fileFailed.set(file, false);
    // A file that fails as a whole (load error, crash) is reported as a test named
    // after the file itself: that is the file's own verdict, not a test.
    if (nesting === 0 && data.file && (data.name === data.file || rel(data.name) === file)) {
      if (status === 'FAIL') fileFailed.set(file, true);
      continue;
    }
    const node = { name: one(data.name), status, children };
    if (nesting > 0) {
      if (!levels.has(nesting)) levels.set(nesting, []);
      levels.get(nesting).push(node);
      continue;
    }
    if (status === 'FAIL') fileFailed.set(file, true);
    const out = [];
    lines(file, node, '', out);
    total += out.length;
    yield out.join('');
  }
  const out = [];
  for (const [file, failed] of [...fileFailed].sort()) {
    out.push(`${MARKER} ${failed ? 'FAIL' : 'PASS'} ${file}::<file>\n`);
  }
  out.push(`${MARKER} END ${total}\n`);
  yield out.join('');
}
"""


class ArchifyImageDefault(Image):
    """Per-PR layer: patches, scripts, the pin (via prepare.sh), and the scrub."""

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
        return ArchifyImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    @property
    def repo_dir(self) -> str:
        return f"/home/{self.pr.repo}"

    def _expand(self, template: str) -> str:
        return (
            template.replace("[[REPO_DIR]]", self.repo_dir)
            .replace("[[SHA]]", self.pr.base.sha)
            .replace("[[USER]]", TEST_USER)
            .replace("[[MARKER]]", RESULT_MARKER)
            .replace("[[TIMEOUT]]", str(SUITE_TIMEOUT))
            .replace(
                "[[BROWSER_ENV]]",
                f"ARCHIFY_CHROME={CHROMIUM} ARCHIFY_CHROME_NO_SANDBOX=1 "
                if self.pr.number in BROWSER_PRS
                else "",
            )
            .replace(
                "[[BROWSER_GATE]]",
                # Gate on the installed binary + package without starting it: Debian's
                # /usr/bin/chromium wrapper exits 1 when /proc/cpuinfo lacks sse3, which is
                # the case for the amd64 half of a multi-arch build under QEMU on an arm64
                # host. Tests still launch ARCHIFY_CHROME=/usr/bin/chromium at run time.
                f"# Browser lane (BROWSER_PRS).\ntest -x {CHROMIUM_BIN}\ndpkg-query -W chromium\n"
                if self.pr.number in BROWSER_PRS
                else "",
            )
        )

    def _apply(self, *patches: str) -> str:
        excludes = "".join(f" --exclude='{p}'" for p in binary_stub_paths(self.pr))
        return f"git apply --whitespace=nowarn{excludes} {' '.join(patches)}\n"

    # Every stage stages its working tree after applying: upstream, a PR's files are
    # committed, and the suite (scripts/stage-clean-skill.mjs, the archive tests, the
    # test-inventory checks) selects files from the git index. A file the patch adds
    # but leaves untracked would otherwise be invisible to them. In the baseline this
    # is a no-op.
    _STAGE_TREE = "git add -A\n"

    # archify.zip is a deterministic build of the archify/ tree (scripts/build-zip.sh,
    # Node 22 + bundled zlib). Every fix patch that changes it records only a binary
    # stub, so the stub is excluded and the archive is rebuilt from the patched tree --
    # the same build upstream commits. Verified: for 27 of the 29 such PRs the rebuilt
    # archive's blob equals the `index ..<b>` of the record's own archify.zip block
    # (#784/#914 differ only because their diagnostics.mjs change is itself a stub).
    _REBUILD_ZIP = "bash scripts/build-zip.sh\ngit add archify.zip\n"

    def files(self) -> list[File]:
        fix_step = self._apply("/home/test.patch", "/home/fix.patch")
        for path, a_idx, full in supplied_stubs(self.pr):
            fix_step += (
                f'case "$(git hash-object {path})" in {a_idx}*) ;; '
                f'*) echo "unexpected pre-image of {path}"; exit 1 ;; esac\n'
                f"base64 -d /home/stub-{full}.b64 > {path}\n"
                f'test "$(git hash-object {path})" = "{full}"\n'
            )
        fix_step += self._STAGE_TREE
        if "archify.zip" in binary_stub_paths(self.pr):
            fix_step += self._REBUILD_ZIP
        stage_patches = {
            "run.sh": self._STAGE_TREE,
            "test-run.sh": self._apply("/home/test.patch") + self._STAGE_TREE,
            "fix-run.sh": fix_step,
        }
        stages = [
            File(
                ".",
                name,
                self._expand(
                    "#!/bin/bash\nset -eo pipefail\nexport CI=true\n\n"
                    "cd [[REPO_DIR]]\n\n" + patch_step + "\n" + TEST_CMD
                ),
            )
            for name, patch_step in stage_patches.items()
        ]
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._expand(PREPARE_SH)),
            File(".", "archify_reporter.mjs", REPORTER_MJS),
        ] + [
            File(".", f"stub-{full}.b64", SUPPLIED_STUB_BLOBS[full][1])
            for _, _, full in supplied_stubs(self.pr)
        ] + stages

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        sha = self.pr.base.sha
        repo_dir = self.repo_dir
        copies = "".join(f"COPY {f.name} /home/\n" for f in self.files())
        browser = (
            "RUN apt-get update && apt-get install -y --no-install-recommends chromium \\\n"
            "    && rm -rf /var/lib/apt/lists/*\n"
            if self.pr.number in BROWSER_PRS
            else ""
        )

        # PR images are never touched by DockerfileEnhancer (it returns raw the moment
        # dependency() is an Image), so everything this layer needs is written out
        # here. The scrub lives here rather than in the shared base: the base must keep
        # full history so every PR of the dataset can reach its own commit.
        #
        # The scrub contains no `git reset`, no `git clean` and no path-scoped
        # checkout -- any of those would silently revert prepare.sh's work. The opening
        # commit-scoped `--detach` is a same-tree no-op that preserves edits.
        return f"""FROM {name}:{tag}
{self.global_env}
{copies}{browser}RUN bash /home/prepare.sh
WORKDIR {repo_dir}
RUN set -eux; \\
    git checkout --detach {sha}; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates .git/ORIG_HEAD .git/FETCH_HEAD; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"
RUN if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
{self.clear_env}
"""


# ---------------------------------------------------------------------------
# parse_log
# ---------------------------------------------------------------------------
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_RESULT_RE = re.compile(
    r"^" + re.escape(RESULT_MARKER) + r" (PASS|FAIL|SKIP) (\S+?)::(.+?)\s*$"
)
_END_RE = re.compile(r"^" + re.escape(RESULT_MARKER) + r" END \d+\s*$")
_SUITE_RE = re.compile(r"^" + re.escape(RESULT_MARKER) + r" SUITE \d+\s*$")
# Volatile tokens a test title could embed, which differ on every run of the SAME
# commit and would otherwise split one test into a different name per stage.
_VOLATILE_RES = [
    (re.compile(r"\b1[5-9]\d{11}\b"), "<epoch-ms>"),
    (re.compile(r"/tmp/([A-Za-z0-9._-]*?-)?[A-Za-z0-9]{6}(?=[/\s:]|$)"), "/tmp/<tmp>"),
]


def _name(path: str, title: str) -> str:
    for rx, repl in _VOLATILE_RES:
        title = rx.sub(repl, title)
    return f"{path}::{title.strip()}"


# Tests that check the BYTES of a file a fix patch records only as a binary stub. The
# stub is excluded (see binary_stub_paths), so the fix stage still has the old bytes
# while the fix's text side (e.g. a receipt with the new sha256) is applied: the test
# then fails after the fix for a reason that is an artifact of the record, not of the
# change. Such a test is dropped from every stage of the PRs whose patch stubs that
# file, and so is its file's `<file>` verdict (which it would otherwise decide).
#   docs/assets/archify-live-proof.gif -- generated with a real browser + ffmpeg
#   (scripts/build-readme-showcase.mjs), so it cannot be rebuilt in the image; the
#   test compares it with docs/assets/archify-live-proof.json (sha256, size, frames).
STUB_BOUND_TESTS: dict[str, list[tuple[str, str]]] = {
    "docs/assets/archify-live-proof.gif": [
        (
            "readme-showcase.test.mjs",
            "README motion proof is compact, looping, and backed by current gallery artifacts",
        ),
    ],
}


# The same receipt test also hashes the gallery's source artifacts (archify/examples/*)
# against docs/assets/archify-live-proof.json. The fix patches of the PRs below
# regenerate those artifacts but do not refresh that receipt (upstream refreshes it
# in a later, separate showcase rebuild), so the test fails after the fix with
# "source artifact drift; rebuild README showcase" -- unrelated to the change under
# test. It is dropped, with its file's `<file>` verdict, from every stage of these PRs.
RECEIPT_DRIFT_PRS = frozenset({911, 928, 1055})
RECEIPT_DRIFT_TESTS = STUB_BOUND_TESTS["docs/assets/archify-live-proof.gif"]


# Browser-lane timing flake: this layout case (and the parent test that aggregates
# it) passes or fails at random on unchanged code under the browser lane -- seen
# failing in the run stage of #911 and #1093 and in the test stage of #808 and #899,
# passing in the other stages of the same images. Its verdict would therefore be
# noise (a false fixed or a false regression), so it is dropped, with its file's
# `<file>` verdict, from every stage of every BROWSER_PRS PR.
_READER_LAYOUT_PARENT = "Reader Layout preserves final-artifact behavior across its ownership boundaries"
BROWSER_FLAKY_TESTS: list[tuple[str, str]] = [
    ("reader-layout-browser.test.mjs", _READER_LAYOUT_PARENT),
    (
        "reader-layout-browser.test.mjs",
        _READER_LAYOUT_PARENT
        + " > compact automatic Sequence fits the initial desktop stage without shrinking source text",
    ),
]


def ignored_tests(pr: PullRequest) -> list[tuple[str, str]]:
    stubs = set(binary_stub_paths(pr))
    ignored = [t for path, tests in STUB_BOUND_TESTS.items() if path in stubs for t in tests]
    if pr.number in RECEIPT_DRIFT_PRS:
        ignored += [t for t in RECEIPT_DRIFT_TESTS if t not in ignored]
    if pr.number in BROWSER_PRS:
        ignored += [t for t in BROWSER_FLAKY_TESTS if t not in ignored]
    return ignored


def parse_archify_log(
    test_log: str, ignored: list[tuple[str, str]] | tuple = ()
) -> TestResult:
    """Parse the reporter's marker lines (see REPORTER_MJS).

    Names are `<repo-relative file>::<title path>` -- the same title can appear in
    more than one file, and subtests are qualified by their parents. Each file also
    gets `<file>::<file>`, its own verdict; if any suite's reporter did not print its END
    line (the run was killed), no file verdict counts as passed. Within one file a title
    that occurs twice collapses to one name, failed if either occurrence failed.
    """
    passed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()
    suites = 0
    ends = 0

    for raw in _ANSI_RE.sub("", test_log).splitlines():
        line = raw.rstrip("\r")
        if _SUITE_RE.match(line):
            suites += 1
            continue
        if _END_RE.match(line):
            ends += 1
            continue
        m = _RESULT_RE.match(line)
        if not m:
            continue
        status, path, title = m.groups()
        if any(
            path.endswith("/" + suffix) and title in (FILE_TEST_SUFFIX, ignored_title)
            for suffix, ignored_title in ignored
        ):
            continue
        name = _name(path, title)
        if title == FILE_TEST_SUFFIX:
            (passed if status == "PASS" else failed).add(name)
        elif status == "PASS":
            passed.add(name)
        elif status == "SKIP":
            skipped.add(name)
        else:
            failed.add(name)

    # Complete only if every suite's reporter printed its END line; a killed or crashed
    # suite leaves no file verdict standing as passed.
    if suites == 0 or ends < suites:
        file_keys = {n for n in passed if n.endswith(f"::{FILE_TEST_SUFFIX}")}
        passed -= file_keys
        failed |= file_keys

    passed -= failed
    skipped -= passed
    skipped -= failed

    return TestResult(
        passed_count=len(passed),
        failed_count=len(failed),
        skipped_count=len(skipped),
        passed_tests=passed,
        failed_tests=failed,
        skipped_tests=skipped,
    )


# The records carry `repo: "archify"` and no `number_interval`, so Instance.create
# looks up `tt-a1i/archify` -- that key routes this dataset. `archify_1102_to_481`
# (this file's stem) is the interval-style name for tools that address the range.
@Instance.register("tt-a1i", "archify_1102_to_481")
@Instance.register("tt-a1i", "archify")
class ARCHIFY_1102_TO_481(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ArchifyImageDefault(self.pr, self._config)

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
        return parse_archify_log(test_log, ignored_tests(self.pr))
