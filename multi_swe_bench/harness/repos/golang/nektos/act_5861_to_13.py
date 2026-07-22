import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_SYNTAX_DIRECTIVE = "# syntax=docker/dockerfile:1.6"

# NOTE: GOPROXY is intentionally NOT pinned here. This ENV applies to BOTH
# the build (prepare.sh warms the cache and needs the real proxy) and the
# offline eval. GOPROXY=off is therefore set per-script: real proxy in
# prepare.sh, "off" in the run/test/fix eval scripts.
_ENV_BLOCK = (
    "ENV DEBIAN_FRONTEND=noninteractive \\\n"
    "    LANG=C.UTF-8 \\\n"
    "    GOFLAGS=-mod=mod \\\n"
    "    GOTOOLCHAIN=local \\\n"
    "    TZ=UTC"
)

# Hardening block for per-PR images when dep=Image (BASE_COMMIT not injected
# by the harness — it is only injected when dep is a string). We use HEAD
# instead of ${BASE_COMMIT} because prepare.sh already checked out the exact
# base commit; detach + strip all refs so agents cannot navigate git history.
_CUSTOM_HARDENING_BLOCK = """\
RUN set -eux; \\
    git checkout --detach HEAD; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
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
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""


class ActImageBase(Image):
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
        return "golang:1.25-bookworm"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo
        github_repo = repo[: -len("_root")] if repo.endswith("_root") else repo
        repo_url = f"https://github.com/{org}/{github_repo}.git"

        label_block = (
            f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
            f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{org}/{github_repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        return f"""{_SYNTAX_DIRECTIVE}

FROM golang:1.25-bookworm

ARG TARGETARCH
ARG REPO_URL="{repo_url}"

{_ENV_BLOCK}

{label_block}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    build-essential \\
    git \\
    gnupg \\
    make \\
    python3 \\
    sudo \\
    wget \\
    docker.io \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{github_repo}

CMD ["/bin/bash"]
"""


class ActImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> "ActImageBase":
        return ActImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        repo = self.pr.repo
        github_repo = repo[: -len("_root")] if repo.endswith("_root") else repo

        copy_lines = "\n".join(
            f"COPY {f.name} /home/" for f in self.files()
        )

        return f"""{_SYNTAX_DIRECTIVE}

FROM {name}:{tag}

ARG TARGETARCH

WORKDIR /home/{github_repo}

{copy_lines}

RUN bash /home/prepare.sh

{_CUSTOM_HARDENING_BLOCK}CMD ["/bin/bash"]
"""

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
set -e

# Build time: network IS available, so use a real proxy to warm the cache.
export GOPROXY=https://proxy.golang.org,direct
export GOFLAGS=-mod=mod

cd /home/{pr.repo}

# Checkout exact PR base commit (hardcoded because BASE_COMMIT build-arg is
# not injected by the harness when dependency() returns an Image object).
git reset --hard
git checkout {pr.base.sha}

# 1) Base graph
go mod download -x 2>&1 || true
go build -buildvcs=false -mod=mod ./... 2>&1 || true

# 2) Post-patch graph (network available at build time): refresh go.mod/go.sum
#    and pull every module the test+fix build needs, then restore the base tree.
git apply --whitespace=nowarn /home/test.patch 2>&1 || true
git apply --whitespace=nowarn /home/fix.patch 2>&1 || true
go mod download all 2>&1 || true
go mod tidy 2>&1 || true
git checkout -- . 2>&1 || true
git clean -fd 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

# Eval is offline: force module resolution to the build-warmed cache only.
export GOPROXY=off

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}

# Start Docker daemon if socket available, otherwise skip
if [ -e /var/run/docker.sock ]; then
  echo "Docker socket detected"
fi

go test -buildvcs=false -mod=mod -count=1 -short -timeout 300s -vet=off ./... 2>&1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

# Eval is offline: force module resolution to the build-warmed cache only.
export GOPROXY=off

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}

# Reset go.mod/go.sum to the clean base before applying patches
git checkout -- go.mod go.sum 2>/dev/null || true

# Apply test patch with fallbacks
git apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.gif' --exclude='*.ico' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' /home/test.patch 2>&1 || \\
  git apply --whitespace=nowarn --reject --exclude='*.png' --exclude='*.jpg' --exclude='*.gif' --exclude='*.ico' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' /home/test.patch 2>&1 || true

# Patches carry a complete, consistent go.mod/go.sum (verified: the new direct
# deps are already hashed in the base go.sum). Do NOT rm go.sum + `go mod tidy`
# here -- the eval is offline, tidy fails silently and leaves an incomplete
# go.sum, which makes `go test` report "[build failed]".

# Start Docker daemon if socket available
if [ -e /var/run/docker.sock ]; then
  echo "Docker socket detected"
fi

go test -buildvcs=false -mod=mod -count=1 -short -timeout 300s -vet=off ./... 2>&1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

# Eval is offline: force module resolution to the build-warmed cache only.
export GOPROXY=off

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}

# Reset go.mod/go.sum to the clean base before applying patches
git checkout -- go.mod go.sum 2>/dev/null || true

# Apply test patch with fallbacks
git apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.gif' --exclude='*.ico' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' /home/test.patch 2>&1 || \\
  git apply --whitespace=nowarn --reject --exclude='*.png' --exclude='*.jpg' --exclude='*.gif' --exclude='*.ico' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' /home/test.patch 2>&1 || true

# Apply fix patch with fallbacks
git apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.gif' --exclude='*.ico' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' /home/fix.patch 2>&1 || \\
  git apply --whitespace=nowarn --reject --exclude='*.png' --exclude='*.jpg' --exclude='*.gif' --exclude='*.ico' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' /home/fix.patch 2>&1 || true

# Patches carry a complete, consistent go.mod/go.sum (verified: the new direct
# deps are already hashed in the base go.sum). Do NOT rm go.sum + `go mod tidy`
# here -- the eval is offline, tidy fails silently and leaves an incomplete
# go.sum, which makes `go test` report "[build failed]".

# Start Docker daemon if socket available
if [ -e /var/run/docker.sock ]; then
  echo "Docker socket detected"
fi

go test -buildvcs=false -mod=mod -count=1 -short -timeout 300s -vet=off ./... 2>&1

""".format(pr=self.pr),
            ),
        ]


_BUNDLE_NIS = [
    '13-15-20-23-25-27',
    '30-34-35-36-38-40',
    '46-48',
    '55-56-57-65-66-68',
    '77-78',
    '135-136-137-138-142-143-144-147-152-154',
    '155-157-163-183-184-190-194-201',
    '207-221-222-223-225-229-231-238-240-241-243-252',
    '256-259-267-274-283-288',
    '287-290-304-308-309-313-314-318-320-326-327-334-343-347',
    '293-524-525-529-530-531-533-539-541-544-552-560-562-563-567-570-571-574-575-577-578-579-580-585-591',
    '349-352-354',
    '371-375-381-383-386',
    '382-395-399-404-406-408',
    '411-412-413-415-417-424-426-429-433-434-459-460-461-462-463',
    '423-470-471-472-474-476-477-478-483-485-486-488-494-495',
    '500-502-503-508-511-512-513-515-523',
    '514-537-566-593-594-595-596-600-601-604-605-606-608-619-620-624-628-630-633-635-637-643-648-650-654-658-659-660-661-662-663-665-667-670-672-674-675',
    '629-719-743-746-749-753-761-762',
    '668-679-680-681-685-686-687-688-690-691-693-698-699-700-701-702-712-714-716-722-723-725-726-727-731',
    '677-752-763-766-767-768-771-772-774-776-786-787-789-791-797-802-803-809-810-815-819-821-822-823-824-825-828-830-832-838-846-848-849-853-854-855-856-859-860-862-864-870-871-872-873-876-877-878-882-883-886-890-893-894',
    '793-840-868-887-889-891-898-903-906-908-911-920-922-923-924-929-930-931-937-938-942-945-948-950-954-955-956-957-958-962-964-965-966-968-971-972-984-986-988-990-993-994-997-1003-1009-1011-1013-1014-1016-1017-1018-1019-1024-1026-1027-1028-1030-1036-1037-1038-1039-1040-1041-1049-1056-1061-1062-1066-1067-1068',
    '1004-1048-1057-1070-1074-1076-1077-1079-1080-1081-1082-1083-1084-1085-1086-1087-1088-1089-1093-1094-1095-1100-1101-1102-1104-1106-1107-1111-1116-1117-1118-1119-1120-1121-1123-1124-1126-1127-1128-1129-1135-1136-1137-1139-1141-1142-1145-1147-1148-1150-1153-1154-1155-1156-1157-1163-1164-1167-1168-1169-1170-1171-1173-1179-1180-1186-1189-1197-1198-1199-1200-1201-1202-1203-1204-1208-1211-1212-1213-1214-1215-1217-1224-1225',
    '1222-1238-1239-1240-1243-1247-1249-1250-1252-1260-1268-1273-1277-1282-1286',
    '1293-1391-1414-1422-1425-1426-1427-1428-1432-1436-1437-1446-1447-1456-1464-1471-1475',
    '1328-1331-1335-1339-1340-1341-1348-1349-1352-1354-1355-1357-1359-1360-1362-1364-1365',
    '1345-1351-1363-1370-1371-1372-1374-1375-1376-1378-1379-1380-1381-1385-1388-1392-1393-1394-1395-1397-1403-1404-1405-1406-1407-1408-1416-1417-1418-1419-1420',
    '1423-1457-1458-1462-1463-1466-1472-1476-1480-1493-1496-1497-1506-1510-1514-1515-1516-1517-1534-1535',
    '1494-1954-2023-2123-2128-2133-2140-2148-2149-2153-2156-2162-2164-2167-2168-2169-2170-2171-2172-2174-2175-2186-2187-2188-2189',
    '1501-1507-1519-1520-1524-1531-1532-1539-1540-1541-1547-1550-1554-1557-1560-1562',
    '1503-1521-1582-1596-1597-1599-1606-1607-1610-1611-1612-1613-1614-1616-1618-1619-1622-1623-1624-1625-1626-1629-1630-1631-1635-1636-1637-1646-1648-1649',
    '1525-1530-1566-1569-1572-1573-1574-1576-1577-1580-1585-1587-1589-1590',
    '1645-1650-1651-1656-1660-1661-1664-1665-1666-1667-1669-1670-1671-1672-1674-1675-1679-1686-1688-1689-1699-1700-1701-1702-1703-1706-1707-1708',
    '1691-1693-1705-1710-1711-1712-1719-1720-1721-1722-1723-1724-1725-1727-1728-1729-1732-1733-1735-1739-1743-1745-1751-1752-1753-1754-1755-1758-1759-1763-1770',
    '1804-1868-1888-1892-1898-1904-1905-1906-1911-1917-1920-1925-1932-1933-1934',
    '1833-1834-1838-1839-1840-1841-1842-1843-1845-1854-1857-1858-1859-1863-1864-1869-1870-1871-1872-1880-1881-1882-1883',
    '1879-1884-1893-1895',
    '1912-1913-1914-1931-1939-1941-1942-1947-1948-1955-1957-1959-1961-1965-1970-1973-1978-1985-1986',
    '1940-1949-2018-2020-2022-2032-2036-2037-2045-2059-2060-2065-2066-2067-2068-2069-2070',
    '1958-1988-1992-1999-2000-2007-2008-2009-2010-2011-2012',
    '2015-2054-2062-2071-2075-2079-2087-2088-2089-2091',
    '2155-2173-2181-2198-2208-2213-2214-2223-2226-2228-2229',
    '2163-2215-2216-2253-2269-2274-2276-2278-2279-2284-2288-2289-2291-2293-2294-2295-2296-2299-2302-2306-2307-2308-2309-2310',
    '2224-2272-2311-2316-2317-2318-2319-2323-2324-2325-2326-2327-2332-2333-2337-2340-2341',
    '2235-2236-2240-2242-2244-2252-2256-2259-2265-2266-2267',
    '2248-2354-2355-2372-2373-2387-2389-2394-2398-2403-2405',
    '2281-2346-2348-2349-2350-2358-2360-2365-2366',
    '2352-2449-2454-2458-2468-2477',
    '2402-2442-2446',
    '2416-2421-2422-2430-2436',
    '2473-2474-2479-2480-2484-2485-2496-2498-2505-2506-2507',
    '2490-2552-2557-2638-2644',
    '2495-2513-2524-2611-2664-2665-2669-2670-2673-2674-2675-2676-2677-2682-2683-2687',
    '2502-2540-2547-2575-2580-2589-2590-2593-2594-2595',
    '2588-2602-2603-2604-2612-2622-2623-2624-2634-2635',
    '2651-2652-2653-2655-2656',
    '2678-2742-2745-2753-2755-2762-2767-2875-2924',
    '2690-2692-2693-2700-2703-2705-2706-2714-2715',
    '2717-2761-3225-4986-5026-5028-5276-5297-5326-5837',
    '5294-5295-5296-5891-5899',
    '5861-5884',
]


class Act(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ActImageDefault(self.pr, self._config)

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
        # Strip ANSI escape codes
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

        # Failed takes priority over passed/skipped
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


for _ni in _BUNDLE_NIS:
    Instance._registry[f"nektos/{_ni}"] = Act
