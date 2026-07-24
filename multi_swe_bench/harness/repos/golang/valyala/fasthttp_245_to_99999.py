import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _sanitize_patch(patch: str) -> str:
    """Drop diff sections ``git apply`` cannot take cleanly (binary hunks emitted
    without a full index line, and go.sum/go.work.sum lock files whose hunks
    depend on the exact module graph). Both otherwise abort the WHOLE apply under
    ``set -e``. go.sum regenerates via ``go mod download``; binary test fixtures
    (e.g. *.zst bodies) do not affect compilation."""
    if not patch:
        return patch
    kept = []
    for sec in re.split(r"(?m)(?=^diff --git )", patch):
        if not sec:
            continue
        if "Binary files " in sec or "GIT binary patch" in sec:
            continue
        m = re.match(r"diff --git a/\S+ b/(\S+)", sec)
        if m and m.group(1).rsplit("/", 1)[-1] in ("go.sum", "go.work.sum"):
            continue
        kept.append(sec)
    return "".join(kept)


class FasthttpImageBase(Image):
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
        # Modules era (PR #245..#2175). go.mod `go` directives span the first
        # version-less module file up to `go 1.24.0`. A recent toolchain plus
        # GOTOOLCHAIN=auto lets Go fetch whatever newer toolchain a given
        # snapshot pins while still compiling the 2018-era go.mod files, so a
        # SINGLE modern base serves the whole 1.11-1.24 spread -- no per-era bases.
        return "golang:1.26-bookworm"

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

        # `# syntax` = the sanctioned enhancer opt-out (PIPELINE §2): without it
        # the enhancer injects `git checkout ${BASE_COMMIT}` + the destructive
        # prune into this SHARED base, pinning it to whichever PR built it first
        # and breaking every other modules-era record. Strict per-PR hardening
        # is applied in FasthttpImageDefault instead.
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

{self.global_env}

RUN git config --global --add safe.directory '*'

WORKDIR /home/

{code}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class FasthttpImageDefault(Image):
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
        return FasthttpImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", _sanitize_patch(self.pr.fix_patch)),
            File(".", "test.patch", _sanitize_patch(self.pr.test_patch)),
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
set -e

git config --global --add safe.directory '*'
cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

go mod download || true
go test -count=1 ./... || true

# `go mod download`/GOTOOLCHAIN can rewrite go.mod/go.sum (e.g. add a
# `toolchain` directive); restore the committed state so fix patches that edit
# go.mod still apply cleanly at eval time.
git checkout -- go.mod go.sum 2>/dev/null || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "common.sh",
                """#!/bin/bash
# Resilient apply: plain, then 3-way, then reject-tolerant. Patches are already
# binary/go.sum-stripped (see _sanitize_patch), so this only handles residual
# whitespace/context drift without aborting the stage.
apply_patch() {
  local f="$1"
  [ -s "$f" ] || return 0
  git apply --whitespace=nowarn "$f" \\
    || git apply --whitespace=nowarn --3way "$f" \\
    || git apply --whitespace=nowarn --reject "$f" \\
    || true
}
""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
source /home/common.sh
git checkout -- go.mod go.sum 2>/dev/null || true
apply_patch /home/test.patch
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
source /home/common.sh
git checkout -- go.mod go.sum 2>/dev/null || true
apply_patch /home/test.patch
apply_patch /home/fix.patch
go test -v -count=1 ./...

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

        # Strict per-PR hardening (PIPELINE §2/§4): detach onto the LITERAL
        # base.sha and prune every other ref so the fix cannot be recovered from
        # git history. Applied here because the Image-typed base opts the
        # enhancer out of auto-injecting it.
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


@Instance.register("valyala", "fasthttp_245_to_99999")
class Fasthttp245To99999(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FasthttpImageDefault(self.pr, self._config)

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
        # `go test` is not colorized by default, but strip ANSI escapes
        # defensively in case the log was captured through a colorizing tee.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        re_pass = re.compile(r"^--- PASS: (\S+)")
        re_fail = re.compile(r"^--- FAIL: (\S+)")
        re_skip = re.compile(r"^--- SKIP: (\S+)")
        re_pkg = re.compile(r"^(?:ok|FAIL|\?)\s+(\S+/\S+)")

        pending_pass: set[str] = set()
        pending_fail: set[str] = set()
        pending_skip: set[str] = set()

        def flush(pkg: str) -> None:
            for t in pending_pass:
                passed_tests.add(f"{pkg}::{t}")
            for t in pending_fail:
                failed_tests.add(f"{pkg}::{t}")
            for t in pending_skip:
                skipped_tests.add(f"{pkg}::{t}")
            pending_pass.clear()
            pending_fail.clear()
            pending_skip.clear()

        for raw_line in test_log.splitlines():
            line = raw_line.strip()
            m = re_pass.match(line)
            if m:
                pending_pass.add(m.group(1)); continue
            m = re_fail.match(line)
            if m:
                pending_fail.add(m.group(1)); continue
            m = re_skip.match(line)
            if m:
                pending_skip.add(m.group(1)); continue
            m = re_pkg.match(line)
            if m:
                flush(m.group(1))
        flush("unknown")

        passed_tests -= failed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === bundle number_interval routing (prs_in_bundle dash-joined, PIPELINE §11b) ===
# Modules-era bundles (lead PR >= 245). One key per bundle. Data-derived from
# valyala__fasthttp_lht_final.jsonl -- regenerate if bundles change.
_BUNDLE_NIS_FASTHTTP_MODERN = [
    "245-356-378-402-428-442-447-452-454-458-460-462-464-467-472-475-476-484-485-488-490",
    "477-550-551-564-567-571",
    "507-522-523-531-532-543-544",
    "575-577-579-581-589-596",
    "598-610-614-621-628-631-634-637-638-640-645-647",
    "649-654-655-657-658-662-663-666-672-673-674-677-678-679-680-682-683",
    "685-687-688-689-696-697-699-702-703-708",
    "713-720-725",
    "731-735-736",
    "738-741-747-755-758-762-764-765-770-772-774-778",
    "787-789-790-796-800-802-810",
    "817-820",
    "821-822-823-825-827-828-834-842",
    "851-855-858-859-864",
    "866-880-881-885-889-890-897",
    "903-907-909-914-918-925",
    "911-942-950-956-960",
    "967-969-970",
    "989-990-991-994-995-997-999-1000-1001",
    "1009-1010",
    "1015-1021-1022-1023-1024-1028-1029",
    "1027-1034-1036",
    "1045-1046-1047-1049",
    "1056-1058-1061-1064-1069",
    "1074-1076-1077-1079-1081-1082-1085-1086-1087-1088-1092-1093-1095",
    "1097-1099-1105-1106-1107-1116-1117",
    "1126-1127-1128-1130-1135-1137-1143-1145-1148-1150-1151-1154-1155-1162-1165-1169-1175-1176-1183-1184-1185-1188-1189",
    "1194-1199-1201-1202",
    "1203-1204-1208-1212-1214-1216-1218-1221-1224-1228-1230-1234-1235-1237",
    "1233-1238-1243-1248-1249-1250-1253-1254-1255-1260-1262",
    "1308-1310-1311-1313-1317-1324-1328",
    "1330-1331-1336-1346-1351-1355",
    "1356-1360-1365",
    "1375-1377-1379-1381-1387-1394-1403",
    "1383-1398-1405-1406-1410-1415-1417-1423-1432-1433-1434-1436-1437",
    "1414-1520-1523-1526-1532-1533-1534-1535-1536-1538-1539",
    "1443-1665-1666-1669-1672-1673-1674-1676-1677-1678-1684-1685-1686-1687-1688-1689-1690-1695-1702-1704-1707-1710-1711-1718-1719",
    "1444-1446",
    "1449-1452-1453-1454-1456-1457-1461-1466-1467-1470",
    "1471-1476-1478-1480-1481-1482-1483-1484-1485-1486-1487-1488-1489-1491-1492-1495-1496-1497-1498-1502-1503-1505-1508-1510-1511-1512-1514-1515-1516",
    "1525-1701-1720-1721-1722-1725-1727-1728-1729-1738-1741-1742-1746-1747-1748-1752-1757-1759-1761-1763-1767-1769-1774",
    "1542-1543-1545-1546",
    "1550-1552-1555-1558-1559-1562-1565-1573-1576",
    "1582-1585-1586-1589-1595-1597-1602-1607-1609",
    "1603-1612-1613-1614",
    "1621-1623-1626-1629-1634-1638-1640-1642-1643-1644-1645-1649-1650-1651-1656-1658",
    "1776-1781-1783-1784-1787-1788-1789-1790-1792",
    "1778-1779",
    "1791-1796-1800-1801-1802-1806-1809-1810-1813-1814-1818-1819-1820-1821-1823-1825-1826-1828-1829-1831-1832-1833-1835-1837-1842-1843-1844-1846-1847-1848-1849-1850-1851-1855-1857-1858-1860-1861-1863-1865-1866-1870-1871",
    "1864-1872-1874-1878-1880-1881-1883-1884-1885-1886-1887-1890",
    "1893-1895-1896-1897-1899-1902-1908",
    "1910-1915-1918-1919-1920-1925-1927-1928-1929-1931-1932-1933-1934-1935-1936-1937-1940-1941-1947-1950-1951-1952-1955-1956",
    "1945-1953-1958-1959-1962-1963-1968-1971-1972-1980-1983-1986",
    "1988-1989-1990-1991-1993-1995-1996",
    "1999-2000-2001-2002-2003-2005",
    "2007-2008-2011-2012-2013-2018-2022-2023-2025-2029",
    "2027-2031-2034-2035-2036",
    "2030-2038-2039-2042-2043-2046-2048-2049-2052-2055",
    "2054-2056-2057-2058-2060-2061-2065",
    "2072-2073-2075-2076-2077-2078-2079-2080-2081-2084",
    "2092-2094-2095-2096-2097-2098-2099-2101-2103-2109-2110-2111-2114-2118-2122",
    "2123-2125-2128-2129-2135-2137-2138-2139-2140-2142-2144-2145-2146-2147-2149-2152-2158-2161-2162-2163-2164-2165-2166-2167-2168-2169-2170-2172-2173-2174",
    "2175-2176-2180-2181-2182-2183-2184-2185-2186-2187-2188-2190-2192-2193-2195-2196",
]
for _ni in _BUNDLE_NIS_FASTHTTP_MODERN:
    Instance.register("valyala", _ni)(Fasthttp245To99999)
