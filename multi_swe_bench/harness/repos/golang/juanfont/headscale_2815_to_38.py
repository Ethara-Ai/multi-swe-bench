import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class HeadscaleImageDefault(Image):
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
        # Returning a string (rather than a chained Image) lets the shared
        # Image.dockerfile() in image.py own the build: it clones "${REPO_URL}",
        # checks out "${BASE_COMMIT}", and appends the _HARDENING_BLOCK that
        # strips every other ref/remote/commit so the fix can't be read out of
        # git history (reward hacking). DockerfileEnhancer then injects the
        # proxy/cert infra and the final sanitize pass. None of that fires when
        # dockerfile() is overridden, which is why the previous two-stage
        # base+default build bypassed the hardening entirely.
        return "golang:1.25-bookworm"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def extra_setup(self) -> str:
        # Runs after "git checkout ${BASE_COMMIT}" and before the hardening
        # block. We stage the runtime helper scripts + patches into /home/ and
        # warm the Go module + build caches so the eval scripts run offline.
        # The copied files live outside /home/{repo}, so the hardening pass
        # (which only operates inside the git tree) leaves them untouched.
        return (
            "COPY fix.patch /home/fix.patch\n"
            "COPY test.patch /home/test.patch\n"
            "COPY run.sh /home/run.sh\n"
            "COPY test-run.sh /home/test-run.sh\n"
            "COPY fix-run.sh /home/fix-run.sh\n"
            "COPY prepare.sh /home/prepare.sh\n"
            "RUN bash /home/prepare.sh"
        )

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
# Warm the Go module + build caches at image-build time so the eval runs
# don't need network. The repo is already checked out at ${{BASE_COMMIT}}
# and hardened by Image.dockerfile(), so this script no longer performs any
# git checkout itself. The build/test steps are allowed to fail (|| true)
# because their only purpose here is to populate caches; the real pass/fail
# signal comes from the run/test-run/fix-run scripts.
set -e

export ASSUME_NO_MOVING_GC_UNSAFE_RISK_IT_WITH=go1.25

cd /home/{pr.repo}
git reset --hard || true

go mod download -x 2>&1 || true
go build -buildvcs=false -mod=mod ./... 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export ASSUME_NO_MOVING_GC_UNSAFE_RISK_IT_WITH=go1.25

cd /home/{pr.repo}

go test -mod=mod -count=1 -short -timeout 300s ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export ASSUME_NO_MOVING_GC_UNSAFE_RISK_IT_WITH=go1.25

cd /home/{pr.repo}

git checkout -- go.mod go.sum 2>/dev/null || rm -f go.mod go.sum

git apply --whitespace=nowarn /home/test.patch 2>&1 || \\
  git apply --whitespace=nowarn --reject /home/test.patch 2>&1 || true

go mod tidy 2>&1 || true

go test -mod=mod -count=1 -short -timeout 300s ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export ASSUME_NO_MOVING_GC_UNSAFE_RISK_IT_WITH=go1.25

cd /home/{pr.repo}

git checkout -- go.mod go.sum 2>/dev/null || rm -f go.mod go.sum

git apply --whitespace=nowarn /home/test.patch 2>&1 || \\
  git apply --whitespace=nowarn --reject /home/test.patch 2>&1 || true

git apply --whitespace=nowarn /home/fix.patch 2>&1 || \\
  git apply --whitespace=nowarn --reject /home/fix.patch 2>&1 || true

go mod tidy 2>&1 || true

go test -mod=mod -count=1 -short -timeout 300s ./...

""".format(pr=self.pr),
            ),
        ]


@Instance.register("juanfont", "headscale_2815_to_38")
class Headscale(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return HeadscaleImageDefault(self.pr, self._config)

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
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass_tests = [
            re.compile(r"^\s*--- PASS: (\S+)"),
            re.compile(r"^ok\s+(\S+)\s+"),
        ]
        re_fail_tests = [
            re.compile(r"^\s*--- FAIL: (\S+)"),
            re.compile(r"^FAIL\s+(\S+)"),
        ]
        re_skip_tests = [
            re.compile(r"^\s*--- SKIP: (\S+)"),
        ]

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass in re_pass_tests:
                match = re_pass.match(line)
                if match:
                    passed_tests.add(match.group(1))

            for re_fail in re_fail_tests:
                match = re_fail.match(line)
                if match:
                    failed_tests.add(match.group(1))

            for re_skip in re_skip_tests:
                match = re_skip.match(line)
                if match:
                    skipped_tests.add(match.group(1))

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


# Route bundled PRs that carry a dash-joined number_interval (the list of
# prs_in_bundle, e.g. "2815-2823-2827-...-2878") to this Go 1.25-era config.
# Instance.create() looks up f"{org}/{number_interval}", so each bundle's
# interval string must be registered against this class. One entry per
# resolved instance in data/dataset/juanfont__headscale_dataset.jsonl (30
# bundles, representatives 44..2815); intervals are the dash-joined
# prs_in_bundle taken from the Fabric juanfont__headscale_lht_final.jsonl.
_NUMBER_INTERVALS = [
    "44-46-47",
    "51-55",
    "54-59-60-61-62-66-67-69",
    "63-72",
    "84-92-94",
    "96-98",
    "99-101-102-103",
    "126-199-200-204-206-207-212-213-214-223-224-225-227-229-230-236-237-238-239-241-242-243-245-247-248-250-255-256",
    "128-131-134-135-136-139-140-141-142-144",
    "129-145",
    "150-165-166-167-169-171",
    "155-156-159",
    "172-174-176-177",
    "184-190-191-193",
    "208-303-305-306-312-313-314-315-316-317-321-324",
    "258-261-262-263-267-268",
    "297-320-325-330-332-333-334-336-338-345-348-349-351",
    "311-344-346-347-350-357-359-366-368-369-371-373-375-376-377-379-380-381-384-387-388-389-392-393-394-484-486-488-495-496-499-500-505-507-509-511-512-513-514-517",
    "482-510-518-519-521-528-531-534-537-538-540-541-542-544-545-551-553-556-557-558-559-560-565-566-568-569-570-573-574-576-578-590-591-593-594-595-596-597-598-599-601-602-607-608-609-610-611-612-615-616-618-619-624-628-630-632-636-638-639-641-642-643-646-648-651-654-656-658-665-667-668-675-676-677-683-684-686-687",
    "670-680-685-688-689-692-698-703-707-708-710-711-713-720-722-729",
    "738-756-760-763-767-768-771-772-778-780-781-784-786-788-790-791-793-795-797-801-811-815-823-829-830-831-837-840-843-844-847-852-853-854-855-856-859-862-863-865-871-874-875-884-887-888-889-890-891-892-895-896-897-899-900-905-906-913-914-915-920-921-926-927-928-931-936-940-943-944-945-946-951-953-955-957-959-960-962-964-971-972-975-977-979-981-984-985-988-989-990-995-998-1003-1005-1008-1013-1019",
    "1028-1030-1031-1036-1038-1042",
    "1107-1144-1147-1148-1150-1151-1163-1164-1167-1170-1171",
    "1159-1162-1177-1181-1183-1184-1185-1191-1192",
    "1278-1279-1281-1286-1288-1293-1297-1299-1304-1315-1317-1323-1325-1328-1330-1335-1339-1346-1349-1350-1352-1355-1360-1362",
    "1338-1374-1376-1377-1379-1381-1382-1391-1397-1398-1403-1412-1414-1417-1425-1426-1428-1429-1437-1439-1442-1448-1450",
    "1420-1421-1446-1458-1476-1477-1484-1487-1491-1492-1495-1497-1524-1535-1546-1553-1555-1556-1557-1558-1559-1560-1562-1563-1564-1565-1566-1575-1583-1587-1588-1589-1592-1597-1598-1603-1609-1610-1611-1612-1618-1622-1628-1632-1639-1640-1641-1642-1644-1646-1647-1649-1652-1657-1658-1663-1666-1668-1669-1670-1672-1673-1692-1697-1700-1701-1702-1707-1716-1719-1723-1728-1729-1730-1733-1734-1742-1744-1746-1751-1754-1756-1762-1763-1765-1766-1773-1781-1783-1790-1791-1792-1799-1802-1807-1809-1832-1833-1834-1843-1848-1853-1854-1860-1864-1866-1869-1872-1877-1878-1882-1886-1889-1890-1892-1893-1895-1897-1899-1901-1902-1903-1905-1906-1907-1908-1909-1912-1915-1917-1918-1919-1920-1922-1924-1927-1931-1939-1943-1944-1945-1946-1947-1948-1950-1952-1958-1959-1960-1975-1976-1979-1985-1986-1987-1989-1991-2000-2008-2009-2010-2011-2013-2014-2015-2017-2018-2019-2021-2022-2023-2030-2034-2035-2040-2041-2042-2051-2052-2059-2060-2066-2069-2071-2075-2076-2077-2078-2080-2081-2083-2086-2088-2089-2091-2092-2093-2095-2096-2098-2100-2102-2104-2105-2106-2107-2109-2111-2112-2113-2114-2116-2117-2122-2124-2125-2126-2127-2135-2136-2138",
    "1444-1463-1465",
    "1681-1823-2005-2020-2038-2046-2132-2134-2143-2145-2148-2149-2150-2154-2155-2156-2158-2161-2163-2165-2167-2170-2173-2179-2187-2191-2195-2198-2199-2202-2205-2206-2207-2212-2216-2217-2222-2225-2226-2227-2232-2235-2239-2240-2242-2243-2247-2248-2252-2254-2255-2258-2261-2265-2266-2269-2270-2271-2273-2279-2285-2286-2292-2294-2296-2297-2298-2306-2308-2309-2313-2314-2320-2321-2324-2328-2331-2338-2339-2340-2342-2347",
    "2815-2823-2827-2828-2842-2843-2844-2848-2854-2855-2859-2861-2865-2872-2873-2874-2875-2878",
]
for _interval in _NUMBER_INTERVALS:
    Instance.register("juanfont", _interval)(Headscale)
