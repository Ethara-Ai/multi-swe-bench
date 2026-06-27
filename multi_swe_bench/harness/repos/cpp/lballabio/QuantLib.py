from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class QuantLibImageBase(Image):
    """Level 1: toolchain-only base image, shared by every PR.

    dependency() returns a *string* (gcc:10) and this Dockerfile carries NO
    "# syntax" directive, so DockerfileEnhancer engages and prepends the
    "# syntax"/ARG/ENV/LABEL infra block. CRITICAL: this image must NOT clone the
    repo. A shared string-dependency image that clones gets rewritten by the
    enhancer into a per-${BASE_COMMIT} checkout + hardening (which asserts
    HEAD == ${BASE_COMMIT}); built as a commit-agnostic base with no real
    BASE_COMMIT, that assertion fails and the base never forms (the previous
    QuantLib bug). So the clone + checkout + hardening live per-PR in
    QuantLibImageDefault; this image only provides the gcc toolchain +
    CMake/Boost apt deps and is reused across the dataset.
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
        # gcc:10 (Debian bullseye, Boost 1.74) spans this dataset (QuantLib
        # v1.12.1 .. v1.41). Not a deprecated-Debian image, so apt works without
        # an archive rewrite.
        return "gcc:10"

    def image_tag(self) -> str:
        # One shared toolchain base, reused by every PR. Commit-agnostic (no
        # clone/checkout/hardening), so it is shareable across the dataset.
        return "base-gcc10"

    def workdir(self) -> str:
        return "base-gcc10"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        # QuantLib is a CMake/Boost project. build-essential, git, make and
        # python3 are already in the default package set below.
        return [
            "cmake",
            "libboost-dev",
            "libboost-test-dev",
            "libboost-thread-dev",
            "libboost-timer-dev",
        ]

    def dockerfile(self) -> str:
        # Toolchain ONLY -- deliberately no git clone/checkout/hardening here so
        # the image stays commit-agnostic and shareable. No "# syntax" directive,
        # so DockerfileEnhancer injects the ARG/ENV/LABEL infra block (but no
        # clone/hardening, since this Dockerfile has no clone).
        base_img = self.dependency()

        default_packages = [
            "ca-certificates", "curl", "build-essential", "git", "gnupg",
            "make", "python3", "sudo", "wget",
        ]
        all_packages = default_packages + self.extra_packages()
        packages_str = " \\\n    ".join(all_packages)
        apt_command = self._get_apt_update_command(packages_str, base_img)

        return f"""FROM {base_img}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

{apt_command}

CMD ["/bin/bash"]
"""


class QuantLibImageDefault(Image):
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
        # Chain to the shared toolchain base. Because this returns an Image (not
        # a string), DockerfileEnhancer returns this Dockerfile verbatim, so the
        # clone + ${BASE_COMMIT} checkout + Image._HARDENING_BLOCK below are kept
        # exactly as written (per-PR pinning is correct -- it is not the shared
        # base).
        return QuantLibImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def dockerfile(self) -> str:
        # Per-PR image: the shared base has no repo, so clone full history here,
        # check out ${BASE_COMMIT}, stage scripts/patches + run prepare.sh, then
        # apply Image._HARDENING_BLOCK (detach at ${BASE_COMMIT}, remove origin,
        # delete every other ref, reflog expire, gc/repack, drop alternates, and
        # assert HEAD == ${BASE_COMMIT} with no extra commits) so the fix cannot
        # be recovered from git history.
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo

        copy_files = " ".join(file.name for file in self.files())

        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/
RUN git clone https://github.com/{self.pr.org}/{repo}.git /home/{repo}

WORKDIR /home/{repo}
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

COPY {copy_files} /home/

RUN bash /home/prepare.sh

"""

        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail

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
                """#!/bin/bash
# Repo is already cloned + checked out at ${{BASE_COMMIT}} and hardened by
# Image.dockerfile(), so this script no longer performs any git checkout. It
# only resets the tree and creates the CMake build dir.
set -e

cd /home/{pr.repo}
git reset --hard || true

mkdir -p build

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

# Skip ARM-incompatible Bessel function test (floating-point precision issue on aarch64)
if [ -f test-suite/functions.cpp ]; then
    sed -i '/testWeightedModifiedBesselFunctions.*{{/a\\    return;' test-suite/functions.cpp
fi

mkdir -p build
cd build
cmake ..
cmake --build . -j$(nproc)

# ctest location varies by era: try build/test-suite/ first, fall back to build/
if [ -d test-suite ] && [ -f test-suite/CTestTestfile.cmake ]; then
    cd test-suite
fi
ctest --output-on-failure
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi

# Skip ARM-incompatible Bessel function test (floating-point precision issue on aarch64)
if [ -f test-suite/functions.cpp ]; then
    sed -i '/testWeightedModifiedBesselFunctions.*{{/a\\    return;' test-suite/functions.cpp
fi

mkdir -p build
cd build
cmake .. || true
cmake --build . -j$(nproc) 2>&1 || true

# ctest location varies by era: try build/test-suite/ first, fall back to build/
if [ -d test-suite ] && [ -f test-suite/CTestTestfile.cmake ]; then
    cd test-suite
fi
ctest --output-on-failure 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
if [ -s /home/fix.patch ]; then
  git apply --whitespace=nowarn --reject /home/fix.patch 2>/dev/null || true
fi

# Skip ARM-incompatible Bessel function test (floating-point precision issue on aarch64)
if [ -f test-suite/functions.cpp ]; then
    sed -i '/testWeightedModifiedBesselFunctions.*{{/a\\    return;' test-suite/functions.cpp
fi

mkdir -p build
cd build
cmake .. || true
cmake --build . -j$(nproc) 2>&1 || true

# ctest location varies by era: try build/test-suite/ first, fall back to build/
if [ -d test-suite ] && [ -f test-suite/CTestTestfile.cmake ]; then
    cd test-suite
fi
ctest --output-on-failure 2>&1 || true

""".format(pr=self.pr),
            ),
        ]


@Instance.register("lballabio", "QuantLib")
class QuantLib(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return QuantLibImageDefault(self.pr, self._config)

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

        re_pass_tests = [
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s+Passed\s+.*$"),
        ]
        re_fail_tests = [
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Failed\s+.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+.*\*\*\*Exception.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Not Run\s+.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Timeout\s+.*$"),
        ]

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    test = pass_match.group(1).strip()
                    passed_tests.add(test)

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    test = fail_match.group(1).strip()
                    failed_tests.add(test)

        # Deduplicate: if a test appears in both passed and failed, treat as failed
        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# Route the dash-joined number_interval (canonical prs_in_bundle from each
# record's resolved_issues + own PR number, sorted, "-"-joined) of the
# release-bundled valid21 dataset to the single QuantLib config. Instance.create()
# looks up f"{org}/{number_interval}"; Instance.register returns the class unchanged,
# so it answers to every key (the repo-level "QuantLib" key above is kept as the
# back-compat fallback used when number_interval is empty).
_BUNDLE_NUMBER_INTERVALS = [
    "727-2297-2311-2314-2342-2344-2345-2347-2348-2349-2350-2351-2352-2354-2355-2356-2357-2359-2360-2362-2363-2365-2367-2370-2372-2373-2374-2376-2380-2381-2383-2384-2385-2386-2387-2388-2389-2390-2392-2394-2396-2397-2400-2401-2402-2404-2405-2406-2407-2408-2409-2412-2416-2417-2419-2420-2422",
    "1186-1251-2176-2183-2249-2260-2262-2263-2264-2268-2269-2270-2271-2272-2273-2274-2275-2277-2279-2280-2282-2283-2287-2290-2291-2292-2293-2294-2295-2296-2298-2299-2300-2302-2303-2308-2309-2315-2318-2322-2323-2325-2329-2330-2332-2333-2334-2335-2336-2337-2338-2341-2343",
    "1681-1695-2192-2195-2198-2203-2204-2205-2206-2207-2208-2209-2212-2215-2217-2218-2219-2221-2223-2225-2226-2227-2228-2229-2232-2233-2234-2235-2237-2238-2240-2242-2243-2244-2245-2259",
    "1973-1986-2122-2131-2140-2141-2145-2146-2149-2150-2151-2153-2156-2157-2158-2163-2164-2166-2170-2171-2177-2178-2179-2180-2181-2182-2185-2189-2190-2193-2196-2200",
    "1274-2071-2085-2087-2090-2091-2092-2094-2096-2097-2098-2099-2100-2101-2103-2104-2106-2107-2108-2110-2114-2115-2116-2117-2119-2120-2121-2125-2126-2127-2128-2129-2132-2134-2135",
    "1462-1954-2020-2022-2024-2028-2029-2030-2031-2032-2034-2036-2037-2038-2039-2040-2042-2043-2045-2046-2048-2050-2052-2054-2056-2057-2058-2061-2062-2063-2064-2067-2070-2073-2076-2077-2078-2080-2081-2084-2086",
    "374-375-405-455-457-499-500-509-518-789-839-1079-1080-1083-1780-1801-1807-1808-1809-1814-1815-1816-1818-1819-1822-1824-1826-1828-1830-1831-1832-1833-1834-1836-1837-1838-1840-1841-1842-1843-1844-1846-1847-1848-1849-1850-1851-1854-1857-1858-1859-1860-1861-1864-1865-1867-1868-1870-1871-1873-1876-1878-1881-1884-1885-1887-1889",
    "1422-1781-1855-1952-1953-1957-1959-1962-1963-1965-1967-1971-1972-1975-1976-1977-1978-1980-1981-1982-1984-1988-1994-1995-1997-1998-1999-2000-2003-2004-2005-2006-2007-2010-2011-2018-2021-2023-2025-2026-2027",
    "230-1163-1588-1593-1775-1813-1855-1875-1877-1886-1891-1893-1896-1897-1899-1900-1901-1903-1906-1907-1908-1909-1910-1911-1913-1916-1918-1919-1920-1922-1923-1926-1927-1930-1931-1934-1935-1936-1939-1942-1943-1944-1948-1949-1951",
    "1305-1339-1346-1347-1349-1350-1351-1352-1353-1354-1355-1356-1357-1359-1361-1362-1363-1365-1368-1369-1371-1372-1373-1380-1381-1382-1383-1386-1387-1390-1391-1395-1397-1400-1401-1403-1408-1409-1410-1411-1413-1416-1419-1420-1421-1426",
    "405-455-457-499-500-509-518-717-789-1306-1324-1388-1423-1433-1434-1435-1438-1439-1440-1446-1447-1448-1450-1452-1453-1454-1455-1458-1461-1466-1468-1470-1471-1475-1477-1478-1479-1481-1482-1485-1486-1487-1488-1491-1492-1493-1494-1496-1500",
    "293-628-662-689-1082-1083-1084-1099-1143-1255-1270-1287-1290-1292-1293-1294-1295-1296-1297-1298-1300-1301-1302-1303-1304-1308-1309-1310-1315-1316-1322-1325-1326-1329-1330-1331-1332-1333-1338-1339",
    "455-996-1050-1164-1207-1209-1210-1212-1214-1216-1217-1221-1223-1224-1225-1227-1230-1232-1235-1236-1237-1242-1244-1245-1249-1251-1254-1256-1257-1258-1261-1263-1264-1266-1267-1271-1275-1276-1277-1280-1281-1283-1285",
    "868-1068-1073-1085-1086-1087-1090-1092-1093-1096-1097-1098-1100-1101-1103-1105-1106-1107-1110-1111-1117-1118-1120-1126-1127-1130-1131-1132-1133-1134-1137-1139",
    "405-455-457-499-500-509-518-789-839-859-880-1050-1051-1339-1377-1429-1437-1495-1497-1499-1501-1502-1503-1504-1506-1507-1508-1511-1512-1513-1515-1516-1517-1518-1519-1520-1521-1522-1523-1524-1525-1526-1527-1529-1530-1531-1533-1534-1535-1536-1538-1539-1542-1543-1545-1547-1549-1551-1552",
    "727-909-913-919-929-931-934-935-941-942-943-944-945-946-948-949-951-952-953-955-957-958-961-962-963-964-966-967",
    "453-785-786-787-788-790-793-796-797-800-802-803-805-806-809-810-811-812-813-816-820-821-823-824-826-829-830-831-832-834-835-836-838-840-842-844-845-848-849-850-851-854",
    "335-648-655-656-660-665-666-667-668-674-676-678-679-681-686-690-694-699-700-701-704-706-711-716-718-719-725-726",
    "59-589-593-595-596-601-602-603-607-610-612-615-617-618-620-621-622-624-625-627-629-630-636-638-644-650-651-654-658-664",
    "315-518-533-541-546-548-550-551-555-557-559-560-561-562-564-566-569-570-572-573-575-576-577-581-582-583-585-586-590-591-592-594",
    "323-332-361-365-406-413-422-475-478-480-481-483-484-485-488-489-490-491-492-499-500-501-506-509-512-513-515-517-519-521-522-529-530-534-536-537-538-539-542-543-545",
]

for _ni in _BUNDLE_NUMBER_INTERVALS:
    Instance.register("lballabio", _ni)(QuantLib)
