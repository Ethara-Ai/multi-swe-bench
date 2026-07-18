import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
    """Shared per-era base: OS + toolchain + a FULL clone of the repo (all
    history, NO checkout, NO hardening). Built ONCE and reused by every PR in
    this era. The leading `# syntax=` directive makes DockerfileEnhancer return
    this Dockerfile verbatim (image.py: `if SYNTAX_DIRECTIVE in raw: return raw`)
    so the enhancer does NOT inject the ${BASE_COMMIT} hardening pass here — the
    base has no BASE_COMMIT and must keep full history so any PR's base.sha stays
    reachable. Per-PR checkout + hardening live in ImageDefault.
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
        return "python:3.9-bookworm"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return "base-py39-poetry"

    def workdir(self) -> str:
        return "base-py39-poetry"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return """# syntax=docker/dockerfile:1.6
FROM python:3.9-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends git build-essential patch libxml2-dev libxslt1-dev libsasl2-dev libldap2-dev libssl-dev

RUN printf 'setuptools<58\\nlxml<5\\nCython<3\\n' > /cons.txt
RUN PIP_CONSTRAINT=/cons.txt pip install --upgrade "pip<24" "setuptools<58" wheel
RUN pip install "poetry<1.6"

WORKDIR /home/
RUN git clone https://github.com/mealie-recipes/mealie.git /home/mealie
"""


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
                """ls -la
###ACTION_DELIMITER###
apt-get update && apt-get install -y libxml2-dev libxslt1-dev
###ACTION_DELIMITER###
printf 'setuptools<58\\nlxml<5\\nCython<3\\n' > /cons.txt
###ACTION_DELIMITER###
PIP_CONSTRAINT=/cons.txt pip install "pip<24" "setuptools<58" wheel
###ACTION_DELIMITER###
pip install "poetry<1.6"
###ACTION_DELIMITER###
poetry export -f requirements.txt --without-hashes --dev -o /req.txt || poetry export -f requirements.txt --without-hashes --with dev -o /req.txt
###ACTION_DELIMITER###
PIP_CONSTRAINT=/cons.txt pip install -r /req.txt
###ACTION_DELIMITER###
PIP_CONSTRAINT=/cons.txt pip install -e . --no-deps
###ACTION_DELIMITER###
if [ -d tests ]; then TP=tests; elif [ -d mealie/tests ]; then TP=mealie/tests; else TP=.; fi; python -m pytest "$TP" --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors -o addopts=""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if [ -d tests ]; then TP=tests; elif [ -d mealie/tests ]; then TP=mealie/tests; else TP=.; fi; python -m pytest "$TP" --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors -o addopts=

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
git -C /home/{pr.repo} apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.webp' --exclude='*.bmp' --exclude='*.svg' --exclude='*.zip' --exclude='*.gz' --exclude='*.tar' --exclude='*.tgz' --exclude='*.pdf' --exclude='*.db' --exclude='*.sqlite' --exclude='*.sqlite3' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='*.mo' --exclude='*.jar' --exclude='*.so' --exclude='*.pyc' /home/test.patch || git -C /home/{pr.repo} apply --whitespace=nowarn --3way --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.webp' --exclude='*.bmp' --exclude='*.svg' --exclude='*.zip' --exclude='*.gz' --exclude='*.tar' --exclude='*.tgz' --exclude='*.pdf' --exclude='*.db' --exclude='*.sqlite' --exclude='*.sqlite3' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='*.mo' --exclude='*.jar' --exclude='*.so' --exclude='*.pyc' /home/test.patch || ( cd /home/{pr.repo} && patch -p1 --forward --fuzz=3 < /home/test.patch ) || true
( PIP_CONSTRAINT=/cons.txt pip install -e . || PIP_CONSTRAINT=/cons.txt pip install -e . --no-build-isolation ) || true
if [ -d tests ]; then TP=tests; elif [ -d mealie/tests ]; then TP=mealie/tests; else TP=.; fi; python -m pytest "$TP" --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors -o addopts=

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
git -C /home/{pr.repo} apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.webp' --exclude='*.bmp' --exclude='*.svg' --exclude='*.zip' --exclude='*.gz' --exclude='*.tar' --exclude='*.tgz' --exclude='*.pdf' --exclude='*.db' --exclude='*.sqlite' --exclude='*.sqlite3' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='*.mo' --exclude='*.jar' --exclude='*.so' --exclude='*.pyc' /home/test.patch || git -C /home/{pr.repo} apply --whitespace=nowarn --3way --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.webp' --exclude='*.bmp' --exclude='*.svg' --exclude='*.zip' --exclude='*.gz' --exclude='*.tar' --exclude='*.tgz' --exclude='*.pdf' --exclude='*.db' --exclude='*.sqlite' --exclude='*.sqlite3' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='*.mo' --exclude='*.jar' --exclude='*.so' --exclude='*.pyc' /home/test.patch || ( cd /home/{pr.repo} && patch -p1 --forward --fuzz=3 < /home/test.patch ) || true
git -C /home/{pr.repo} apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.webp' --exclude='*.bmp' --exclude='*.svg' --exclude='*.zip' --exclude='*.gz' --exclude='*.tar' --exclude='*.tgz' --exclude='*.pdf' --exclude='*.db' --exclude='*.sqlite' --exclude='*.sqlite3' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='*.mo' --exclude='*.jar' --exclude='*.so' --exclude='*.pyc' /home/fix.patch || git -C /home/{pr.repo} apply --whitespace=nowarn --3way --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.webp' --exclude='*.bmp' --exclude='*.svg' --exclude='*.zip' --exclude='*.gz' --exclude='*.tar' --exclude='*.tgz' --exclude='*.pdf' --exclude='*.db' --exclude='*.sqlite' --exclude='*.sqlite3' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='*.mo' --exclude='*.jar' --exclude='*.so' --exclude='*.pyc' /home/fix.patch || ( cd /home/{pr.repo} && patch -p1 --forward --fuzz=3 < /home/fix.patch ) || true
( PIP_CONSTRAINT=/cons.txt pip install -e . || PIP_CONSTRAINT=/cons.txt pip install -e . --no-build-isolation ) || true
if [ -d tests ]; then TP=tests; elif [ -d mealie/tests ]; then TP=mealie/tests; else TP=.; fi; python -m pytest "$TP" --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors -o addopts=

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        # Two-stage: chain to the shared ImageBase *Image*. Because dependency()
        # returns an Image (not a str), DockerfileEnhancer returns this verbatim
        # and supplies neither ARG BASE_COMMIT nor the hardening pass — so we set
        # BASE_COMMIT and embed Image._HARDENING_BLOCK ourselves. The base holds
        # a full clone + /cons.txt + poetry; here we check out THIS PR's base.sha,
        # export+install deps against it, then the hardening block prunes every
        # other ref/commit (reward-hack defense). `hardening` is inserted as a
        # plain value so its ${...}/$(...) tokens stay byte-identical.
        base = self.dependency()
        name = base.image_name()
        tag = base.image_tag()
        base_sha = self.pr.base.sha
        repo = self.pr.repo
        hardening = Image._HARDENING_BLOCK

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{base_sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

WORKDIR /home/{repo}
RUN git checkout {base_sha}
RUN poetry export -f requirements.txt --without-hashes --dev -o /req.txt || poetry export -f requirements.txt --without-hashes --with dev -o /req.txt
RUN PIP_CONSTRAINT=/cons.txt pip install -r /req.txt
RUN PIP_CONSTRAINT=/cons.txt pip install -e . --no-deps

{copy_commands}
{hardening}
CMD ["/bin/bash"]
"""


@Instance.register("mealie-recipes", "mealie_v0_poetry")
class MEALIE_V0_POETRY(Instance):
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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # pytest `-rA` short test summary lines:
        #   PASSED tests/unit_tests/test_config.py::test_name[a b]
        #   FAILED tests/unit_tests/test_config.py::test_name - AssertionError: ...
        #   ERROR  tests/unit_tests/test_x.py::test_y - ...
        summary_pattern = re.compile(
            r"^(PASSED|FAILED|ERROR|XFAIL|XPASS)\s+(.+?)\s*$", re.MULTILINE
        )
        for status, name in summary_pattern.findall(log):
            if status in ("FAILED", "ERROR"):
                name = re.sub(r"\s+-\s.*$", "", name).strip()
                failed_tests.add(name)
            elif status == "PASSED":
                passed_tests.add(name.strip())
            # XFAIL / XPASS: expected-fail bookkeeping, not real pass/fail

        # Grouped skip summary: SKIPPED [6] tests/unit_tests/test_x.py:18: reason
        for m in re.finditer(
            r"^SKIPPED\s+\[\d+\]\s+(\S+?):(\d+):", log, re.MULTILINE
        ):
            skipped_tests.add(f"{m.group(1)}:{m.group(2)}")

        # Defensive fallback: verbose per-test lines `nodeid STATUS [ 12%]`
        verbose_pattern = re.compile(
            r"^(.+?::.+?)\s+(PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)"
            r"(?:\s+\[\s*\d+%\])?\s*$",
            re.MULTILINE,
        )
        for name, status in verbose_pattern.findall(log):
            name = name.strip()
            if status == "PASSED":
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            elif status == "SKIPPED":
                skipped_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )

# Route bundled PRs by their dash-joined `prs_in_bundle` interval to this era.
# Instance.create() looks up f"{org}/{number_interval}", so every bundle whose
# base.sha matches this era (poetry era, pyproject python ^3.8/^3.9 — python:3.9-bookworm)
# must be registered here. Era was derived from the repo state at each base.sha
# (packaging files), not from PR-number ranges — routing is NOT monotonic in PR
# number (e.g. bundle 5883 is uv-era while the higher 6128/6268 are poetry-era).
# 9 bundle(s); intervals come from the lht dataset's prs_in_bundle.
_NUMBER_INTERVALS = [
    "101-125-137-143",
    "146-168-176",
    "148-157-158-159",
    "177-181-188-236",
    "232-240-244-245-246-250-251-252-253-256-257-258-259-260-262-263-264-265-266-269-278-285-286-287-288-289-292-296-298-299-305-319-322-323-324-327-328-330-334-335-339-340-341-342-343-344-345-346-347-348-349-351-354-355-358-359-360-361-362-363-364-365-366-367-368-369-370-371-372-373-374-376-377-378-379-380-381-382-387-388-389-390-391-392-393-394-395-398-399-400-401-402-403-404-406-407-408-410-413-414-416-417-420-422-423-424-425-426-430-431-432-436-437-443-444-445-447-448-449-450-452-455-462-466-467-468-470-471-473-474-476-479-481-482-483-494-496-497-498-499-500-501-502-503-505-506-508-510-511-512-513-514-518-524-525-526-528-529-530-531-537-540-543-544-545-547-555-557-559-561-563-564-565-568-569-570-571-572-574-575-576-577-580-582-583-584-587-589-591-592-594-596-600-602-603-607-608-609-611-618-621-624-626-629-631-632-664-666-667-668-669-670-673-680-683-687-715-716-717-718-719-720-721-725-726-727-731-744-747-749-750-760-764-765-775-778-785-786-787-789-794-807-810-825-826-838-841-845-864-866-867-873-875-876-877-883-888-905-906-907-910-911-914-918-919-923-925-927-928-939-954-968-969-979-980-984-987-989-990-993-1002-1005-1006-1008-1015-1021-1026-1040-1051-1052-1055-1056-1059-1060-1064-1069-1071-1075-1076-1084-1086-1087-1088-1093-1095-1096-1097-1098-1099-1101-1102-1104-1107-1111-1116-1119-1120-1125-1126-1130-1142-1143-1146-1147-1149-1150-1151-1152-1153-1155-1157-1158-1160-1168-1169-1170-1172-1173-1174-1175-1176-1178-1182-1188-1191-1200-1204-1206-1207-1209-1210-1212-1213-1214-1216-1228-1233-1234-1235-1245-1247-1248-1250-1251-1252-1254-1257-1258-1259-1260-1263-1265-1267-1268-1271-1272-1275-1279-1280-1281-1282-1283-1284-1285-1286-1287-1288-1290-1293-1294-1295-1296-1298-1299-1300-1302-1303-1304-1305-1307-1308-1310-1312-1313-1314-1315-1316-1325-1329-1332-1333-1338-1339-1340-1341-1345-1346-1347-1348-1349-1351-1354-1355-1356-1362-1364-1365-1368-1369-1370-1371-1372-1375-1376-1379-1383-1388-1392-1393-1394-1395-1403-1405-1406-1417-1418-1423-1424-1426-1427-1428-1437-1439-1448-1452-1453-1455-1461-1464-1468-1480-1483-1487-1488-1497-1506-1508-1511-1512-1515-1519-1520-1522-1523-1524-1526-1527-1528-1532-1533-1535-1538-1539-1540-1541-1542-1543-1544-1545-1546-1547-1548-1549-1550-1551-1552-1553-1555-1556-1557-1558-1559-1560-1561-1562-1565-1566-1567-1574-1575-1577-1578-1579-1580-1581-1583-1584-1586-1587-1589-1590-1591-1592-1595-1604-1606-1608-1609-1610-1611-1613-1614-1617-1618-1619-1623-1624-1628-1631-1633-1635-1636-1637-1638-1639-1642-1643-1645-1648-1650-1651-1652-1653-1654-1655-1657-1661-1664-1665-1667-1669-1670-1671-1672-1673-1675-1677-1679-1683-1686-1694-1695-1696-1699-1701-1702-1708-1712-1716-1717-1718-1731-1732-1734-1735-1736-1739-1740-1744-1745-1746-1747-1748-1749-1752-1754-1755-1756-1757-1758-1759-1760-1762-1766-1769",
    "239-243-267",
    "338-509-532",
    "635-636",
    "777-780-781-793-803-818-853",
]
for _interval in _NUMBER_INTERVALS:
    Instance.register("mealie-recipes", _interval)(MEALIE_V0_POETRY)
