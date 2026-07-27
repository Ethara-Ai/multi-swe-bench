"""google/syzkaller harness for the GOPATH era (PR < 1700).

Covers number_interval: syzkaller_0_to_1699.

Pre-modules syzkaller had no go.mod (the file first appeared around
PR #1928). Sources had to live under
``$GOPATH/src/github.com/google/syzkaller`` and ``GO111MODULE=off``
was required. The base image is ``golang:1.13-buster`` because that's
the newest Go that still supports GOPATH-mode builds cleanly while
also being able to clone the older vendored dependencies. Debian
Buster is EOL so package archives are redirected to archive.debian.org.

Reference format (§3/§4): the shared base opts out of the Dockerfile
enhancer via the ``# syntax`` directive and keeps FULL history with
light hardening only; the PR layer applies the canonical hardening
block with the literal base.sha.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_GO_IMAGE = "golang:1.13-buster"
_TAG_SUFFIX = "gopath"
_GOPATH_PKG = "github.com/google/syzkaller"
_REPO_DIR = f"/go/src/{_GOPATH_PKG}"


class _ImageBase(Image):
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
        return f"""# syntax=docker/dockerfile:1.6
FROM {_GO_IMAGE}

ARG TARGETARCH
ARG REPO_URL="https://github.com/google/syzkaller.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    GO111MODULE=off \\
    GOPATH=/go \\
    PATH=/go/bin:/usr/local/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

LABEL org.opencontainers.image.title="google/syzkaller" \\
      org.opencontainers.image.description="google/syzkaller Docker image" \\
      org.opencontainers.image.source="https://github.com/google/syzkaller" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/
RUN sed -i 's|http://deb.debian.org/debian|http://archive.debian.org/debian|g' /etc/apt/sources.list && \\
    sed -i 's|http://security.debian.org/debian-security|http://archive.debian.org/debian-security|g' /etc/apt/sources.list && \\
    sed -i '/buster-updates/d' /etc/apt/sources.list && \\
    apt-get update && apt-get install -y --no-install-recommends \\
    git make patch gcc g++ pkg-config clang-format ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

# gcc/g++ wrappers: suppress the -Werror=array-bounds FALSE POSITIVE on syzkaller's
# generated USB-descriptor codegen (the code is correct; upstream suppresses it too).
# /usr/local/bin precedes /usr/bin so the target compiler picks these up.
RUN {{ echo '#!/bin/sh'; echo 'exec /usr/bin/gcc -Wno-array-bounds -Wno-error=array-bounds "$@"'; }} > /usr/local/bin/gcc && chmod +x /usr/local/bin/gcc
RUN {{ echo '#!/bin/sh'; echo 'exec /usr/bin/g++ -Wno-array-bounds -Wno-error=array-bounds "$@"'; }} > /usr/local/bin/g++ && chmod +x /usr/local/bin/g++

RUN mkdir -p {_REPO_DIR} && git clone "${{REPO_URL}}" {_REPO_DIR}

WORKDIR {_REPO_DIR}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

CMD ["/bin/bash"]
"""


class _ImageDefault(Image):
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

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _hardening_block(self) -> str:
        """Canonical Image._HARDENING_BLOCK with the literal base.sha baked in
        (§4/§9 — the PR layer performs the strict history scrub)."""
        return Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)

    def files(self) -> list[File]:
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
                "syz_setup.sh",
                """#!/bin/bash
# Best-effort: generate syscall descriptions. Older revisions (PR<1689)
# expose `make generate`; newer ones use `make descriptions`. Failures
# are tolerated because many unit tests don't actually need the
# generated code, and tests that do will simply fail to compile (and
# be reported as failed by the harness).
cd {repo_dir}
if grep -qE '^descriptions:' Makefile 2>/dev/null; then
  make descriptions 2>&1 | tail -30 || true
elif grep -qE '^generate:' Makefile 2>/dev/null; then
  make generate 2>&1 | tail -30 || true
fi
""".format(repo_dir=_REPO_DIR),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash

cd {repo_dir}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

# Test-env setup: TestGitRepo needs a git identity; TestBisectionResults
# sandboxes commands as user 'syzkaller'. Use --system so the identity applies
# to EVERY user (incl. the sandboxed syzkaller), and give the user a shell+home.
git config --system user.email "syzkaller@syzkaller.test" 2>/dev/null || true
git config --system user.name "syzkaller" 2>/dev/null || true
git config --system safe.directory '*' 2>/dev/null || true
id syzkaller >/dev/null 2>&1 || useradd -m -s /bin/bash syzkaller 2>/dev/null || true
chmod 0777 /tmp 2>/dev/null || true

bash /home/syz_setup.sh || true
# Warm the compile cache only (no test execution at build time; the
# run scripts target just the packages the patches touch).
go build ./... 2>&1 | tail -20 || true

""".format(repo_dir=_REPO_DIR, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd {repo_dir}
bash /home/syz_setup.sh || true
PKGS=$(grep '^diff --git' /home/test.patch 2>/dev/null | sed 's|diff --git a/||;s| b/.*||' | grep '_test\\.go$' | xargs -I{{}} dirname {{}} | sort -u | sed 's|^|./|' | grep -v '^\\.$')
PKGS=$(for p in $PKGS; do [ -d "${{p#./}}" ] && echo "$p"; done)
if [ -z "$PKGS" ]; then
PKGS=$(cat /home/test.patch /home/fix.patch 2>/dev/null | grep '^diff --git' | sed 's|diff --git a/||;s| b/.*||' | grep '\\.go$' | xargs -I{{}} dirname {{}} | sort -u | sed 's|^|./|' | grep -v '^\\.$')
PKGS=$(for p in $PKGS; do [ -d "${{p#./}}" ] && echo "$p"; done)
fi
if [ -z "$PKGS" ]; then
  PKGS=$(go list ./... 2>/dev/null)
fi
if [ -z "$PKGS" ]; then
  PKGS="./..."
fi
# Run tests per package so a single package's (pre-vendor) dependency
# load failure cannot abort the whole invocation; broken cloud/VM-layer
# packages log their error while core-package tests still execute.
for p in $PKGS; do
  go test -short -v -count=1 -timeout 20m "$p" 2>&1 || true
done

""".format(repo_dir=_REPO_DIR),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd {repo_dir}
git apply /home/test.patch 2>/dev/null || \
git apply --ignore-whitespace /home/test.patch 2>/dev/null || {{ \
echo "Warning: strict apply failed; using fuzzy patch(1)"; git checkout -- .; \
patch -p1 -N -f --no-backup-if-mismatch --fuzz=3 < /home/test.patch 2>&1 | tail -15 || true; \
find . -name '*.rej' -delete 2>/dev/null || true; }}
bash /home/syz_setup.sh || true
PKGS=$(grep '^diff --git' /home/test.patch 2>/dev/null | sed 's|diff --git a/||;s| b/.*||' | grep '_test\\.go$' | xargs -I{{}} dirname {{}} | sort -u | sed 's|^|./|' | grep -v '^\\.$')
PKGS=$(for p in $PKGS; do [ -d "${{p#./}}" ] && echo "$p"; done)
if [ -z "$PKGS" ]; then
PKGS=$(cat /home/test.patch /home/fix.patch 2>/dev/null | grep '^diff --git' | sed 's|diff --git a/||;s| b/.*||' | grep '\\.go$' | xargs -I{{}} dirname {{}} | sort -u | sed 's|^|./|' | grep -v '^\\.$')
PKGS=$(for p in $PKGS; do [ -d "${{p#./}}" ] && echo "$p"; done)
fi
if [ -z "$PKGS" ]; then
  PKGS=$(go list ./... 2>/dev/null)
fi
if [ -z "$PKGS" ]; then
  PKGS="./..."
fi
# Run tests per package so a single package's (pre-vendor) dependency
# load failure cannot abort the whole invocation; broken cloud/VM-layer
# packages log their error while core-package tests still execute.
for p in $PKGS; do
  go test -short -v -count=1 -timeout 20m "$p" 2>&1 || true
done

""".format(repo_dir=_REPO_DIR),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd {repo_dir}
apply_one() {{
  git apply "$1" 2>/dev/null && return 0
  git apply --ignore-whitespace "$1" 2>/dev/null && return 0
  echo "strict apply failed for $1; fuzzy patch(1) fallback"
  patch -p1 -N -f --no-backup-if-mismatch --fuzz=3 < "$1" 2>&1 | tail -10 || true
  find . -name '*.rej' -delete 2>/dev/null || true
}}
apply_one /home/fix.patch
apply_one /home/test.patch
bash /home/syz_setup.sh || true
PKGS=$(grep '^diff --git' /home/test.patch 2>/dev/null | sed 's|diff --git a/||;s| b/.*||' | grep '_test\\.go$' | xargs -I{{}} dirname {{}} | sort -u | sed 's|^|./|' | grep -v '^\\.$')
PKGS=$(for p in $PKGS; do [ -d "${{p#./}}" ] && echo "$p"; done)
if [ -z "$PKGS" ]; then
PKGS=$(cat /home/test.patch /home/fix.patch 2>/dev/null | grep '^diff --git' | sed 's|diff --git a/||;s| b/.*||' | grep '\\.go$' | xargs -I{{}} dirname {{}} | sort -u | sed 's|^|./|' | grep -v '^\\.$')
PKGS=$(for p in $PKGS; do [ -d "${{p#./}}" ] && echo "$p"; done)
fi
if [ -z "$PKGS" ]; then
  PKGS=$(go list ./... 2>/dev/null)
fi
if [ -z "$PKGS" ]; then
  PKGS="./..."
fi
# Run tests per package so a single package's (pre-vendor) dependency
# load failure cannot abort the whole invocation; broken cloud/VM-layer
# packages log their error while core-package tests still execute.
for p in $PKGS; do
  go test -short -v -count=1 -timeout 20m "$p" 2>&1 || true
done

""".format(repo_dir=_REPO_DIR),
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
        hardening = self._hardening_block()

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
{prepare_commands}

WORKDIR {_REPO_DIR}

{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


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


@Instance.register("google", "syzkaller_0_to_1699")
class Syzkaller_0_to_1699(Instance):
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


# === bundle number_interval routing (prs_in_bundle dash-joined) ============
# §11b: every dash-joined bundle value must be a registered routing key.
# Era-0 (GOPATH, lead PR < 1700) bundles point to Syzkaller_0_to_1699.
# Keys are data-derived from Dataset/google__syzkaller_lht_final.jsonl —
# REGENERATED-BY-SCRIPT; do not hand-edit.
_BUNDLE_NIS_ERA0: list[str] = [
    "1028-1033-1036-1037-1038-1039-1040-1041-1042-1045-1046-1048-1050-1052-1053-1055-1056-1057-1058-1060-1063-1064-1065-1066-1067-1068-1069-1071-1072-1075-1077-1078-1081-1082-1089-1090-1091-1092-1093-1095-1096-1098-1099-1101-1102-1103-1104-1105-1106-1107-1109-1110-1111",
    "138-141-144-145-146",
    "1467-1536-1543-1546-1548-1549-1550-1551-1553-1554-1555-1556-1558-1560-1561-1562-1563-1564-1565-1566-1567-1568-1569-1570-1571",
    "1639-1647-1648-1649-1650-1652-1653-1654-1657-1659-1660-1661-1663-1664-1666-1668-1669-1672-1673-1674-1675-1680-1681-1682-1683-1685-1686-1687-1688-1691-1692",
    "512-517-519-521-524-525-528",
    "555-557-562-566-570-571-572-575-578-582-584-587",
    "654-655-656-658-659-660-661-663-665-666-667-668",
    "777-778-779-780-781-782-784-785-789-791-794-795-796-798-799-800-802-803-804-805-807-808-809-810-811-812-813-814-815-816-817-818-819-820-821-822-824-825-826-827",
    "848-908-909-910-912-913-914-916-918-919-920-921-922-923-924-925-926-927-928-929-930-931-932-934-935-936-937-938-939-940-941-943-944-945-948-950-951-952-953-954-956-958-959-960-961-962-963-964-965-966-967-968-969-970",
    "972-974-975-976-979-980-981-985-986-987-988-989-990-992-997-999-1001-1002-1004-1005-1007-1008-1010-1016-1017-1018-1019-1021-1022-1023-1024-1025-1026-1027-1029-1031-1034",
    "983-1306-1317-1330-1331-1332-1333-1334-1335-1336-1337-1338-1339-1341-1343-1345-1347-1349-1350-1351-1353-1354-1355-1356-1357-1358-1360-1361-1362-1363-1368-1369-1372-1373-1374-1376-1377-1378-1386-1387-1388-1390-1392-1394",
    "1015-1047-1149-1150-1154-1160-1161-1163-1164-1165-1166-1167-1168-1169-1170-1171-1172-1173-1174-1175-1178-1179-1181-1183-1184-1185-1186-1188-1189-1190-1191-1193-1194-1196-1199-1200-1202-1203-1205-1206-1207-1208-1209-1210-1211-1212-1213-1214-1215-1219-1220-1222",
    "107-108-110-111-112-113-114-115-116-117-119-120-121-122-124-126-127-128-129-130-131-132-133-134-135-136-137",
    "1218-1223-1224-1226-1227-1228-1229-1230-1231-1233-1236-1237-1238-1239-1241-1242-1243-1244-1245-1246-1248-1249-1251-1253-1256-1257-1258-1259-1260-1261-1262-1264-1266-1268-1269-1270",
    "1240-1263-1273-1274-1275-1279-1280-1281-1282-1284-1285-1286-1287-1289-1290-1292-1293-1294-1295-1296-1297-1298-1299-1300-1301-1303-1304-1307-1308-1309-1310-1311-1312-1313-1314-1315-1316-1319-1320-1321-1323-1324-1325-1326-1328",
    "1272-1494-1497-1498-1499-1500-1501-1503-1504-1505-1506-1507-1509-1510-1513-1514-1515-1516-1517-1518-1519-1520-1521-1524-1525-1526-1528-1529-1530-1531-1533-1538-1539-1540-1544",
    "1342-1370-1375-1379-1383-1385-1389-1391-1393-1395-1397-1398-1399-1400-1401-1402-1404-1405-1406-1407-1408-1409-1410-1411-1412-1414-1415-1418-1419-1420-1421-1422-1423-1425-1426-1427-1428-1431-1433-1434-1435-1437-1438-1439-1440-1442-1443-1445-1447-1451-1453-1455-1457",
    "1382-1430-1449-1458-1459-1462-1463-1464-1465-1466-1468-1470-1471-1472-1473-1474-1475-1476-1477-1478-1480-1482-1483-1485-1487-1488-1489-1490-1491-1492-1493-1495-1496",
    "150-151-154-159-160-161-165-166-167",
    "1572-1576-1577-1579-1581-1585-1586-1587-1588-1590-1592-1595-1596-1597-1598-1600-1602-1605-1607",
    "1583-1589-1613-1615-1618-1620-1625-1626-1628-1629-1631-1632-1633-1634-1637-1638-1640-1641-1642-1643-1645",
    "1612-1644-1768-1769-1771-1772-1773-1775-1779-1780-1781-1782-1783-1784-1785-1786-1787-1788-1789-1790-1791-1793-1794-1795-1796-1797-1798-1799-1803-1804-1805-1806-1807-1809-1810-1811-1812-1813-1814-1815-1817-1818-1819-1822-1823-1825-1826-1827-1828-1829-1830-1831-1833-1835-1836-1838-1839-1840-1841-1842-1843-1844-1845-1846-1847-1848-1849-1850-1851-1853-1854-1855-1856-1857-1858-1859-1860-1861-1862-1865-1866-1867-1868-1870-1873",
    "1689-1872-1874-1875-1877-1880-1881-1882-1886-1888-1889-1892-1893-1894-1897-1898-1899-1900-1901-1902-1903-1904-1906-1907-1908-1909-1910-1911-1912-1914-1915-1916-1917-1919-1920-1921-1922-1924-1925-1926-1927-1929-1930-1931-1932-1933-1934-1935-1936-1937-1938-1939-1942-1943-1946-1947-1949-1953-1954-1955-1958-1959-1960-1961-1964-1966-1967-1969-1972-1973-1974",
    "169-170-171-172-175-181-185-186-190-194-196-198-201-204-206-207-209-210-212-213",
    "1694-1695-1696-1697-1700-1702-1703-1705-1706-1707-1708-1711-1712-1713-1714-1715-1716-1717-1718-1719-1720-1721-1722-1724-1725-1727-1728-1729-1730-1733-1735-1736-1737-1738-1739-1740-1742-1744-1745-1746-1747-1748-1749-1752-1753-1754-1755-1756-1757-1760-1761-1762-1763-1767",
    "180-195-219-221-222-223-224-225-226-227-228-229-230-231-232-233-234-235-236-237-239-240-241-244-247-248-252-253-254-255-256-259-260-265-266-267-268-271-272-273",
    "274-276-279-280-284-285-286-287-297-298-299-300-301-302-304-305-308-309-314-315-317-319-320-321-323-326-327-328-329-331-334-335-337-338-340-341-343",
    "344-345-347-348-349-350-351-353-356-357-358-359-360-362-363-364-365-366-367-368-369-370-371-372-373",
    "374-375-376-378-381-382-383-388-389-390-392-393-394",
    "395-396-397-398-400-401-403-404-405-407-408-409-412-413-415-416-418-420-424-425-426-427-429-430-431",
    "433-436-437-440-441-442-443-444-447-448-449-450-451-452-453-454-455-456-458-459-461-463-465-467-469-470-474-476-479-483-485-486-489",
    "492-493-495-496-500-503-504-506-509-510",
    "530-531-532-535-537-539-540-541-543-547-551-553",
    "588-591-593-598-601-602-609-611-617-618-620-621-622-623-624-625-626",
    "628-630-633-635-642-648-649-651",
    "678-683-684-685-687-689-690-693-695-696-697-699-700-701-702-703-704-705-708-709-711-713-715-717-718-719-720-722",
    "721-729-730-732-733-734-735-736-737-741-743-744-745-746-751-752-753-754-755-756-757-759-762-764-766-768-769-770-773-774-776",
    "767-830-831-832-834-835-836-837-838-840-842-843-844-846-847-849-850-851-852-853-854-855-856-857-858-859-860-861-862-863-865-866-867-868-869-870-871-872-873-874-877-879-880-882-883-886-887-889-890-892-893-894-895-898-901-902-903-904-905-907",
    "982-1112-1113-1114-1116-1117-1118-1121-1122-1123-1125-1128-1129-1130-1131-1132-1133-1135-1138-1139-1140-1141-1142-1145-1146-1147-1151-1152-1153-1155-1156-1157-1158",
]
for _ni in _BUNDLE_NIS_ERA0:
    Instance.register("google", _ni)(Syzkaller_0_to_1699)
