import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


REPO_DIR = "slidev"


class SlidevImageBase(Image):
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
        return "node:20-bookworm"

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

        if self.config.need_clone:
            code = "RUN git clone --no-single-branch https://github.com/{org}/{repo}.git /home/{repo_dir}".format(
                org=self.pr.org, repo=self.pr.repo, repo_dir=REPO_DIR
            )
        else:
            code = "COPY {repo} /home/{repo_dir}".format(
                repo=self.pr.repo, repo_dir=REPO_DIR
            )

        return """# syntax=docker/dockerfile:1.6

FROM {image_name}

LABEL org.opencontainers.image.title="slidevjs/slidev" \\
      org.opencontainers.image.description="slidevjs/slidev Docker image" \\
      org.opencontainers.image.source="https://github.com/slidevjs/slidev" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/

ENV DEBIAN_FRONTEND=noninteractive
ENV CI=true

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    && rm -rf /var/lib/apt/lists/*

RUN corepack disable 2>/dev/null || true
RUN npm install -g pnpm@10.33.2

{code}

CMD ["/bin/bash"]
""".format(
            image_name=image_name,
            code=code,
        )


class SlidevImageDefault(Image):
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
        return SlidevImageBase(self.pr, self.config)

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
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo_dir}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

# Strip packageManager field so global pnpm 10 is used
python3 -c "
import json
with open('package.json') as f:
    d = json.load(f)
d.pop('packageManager', None)
# Allow esbuild postinstall to run (pnpm 10 security policy)
d.setdefault('pnpm', {{}})['onlyBuiltDependencies'] = ['esbuild']
with open('package.json', 'w') as f:
    json.dump(d, f, indent=2)
"

pnpm install --no-frozen-lockfile 2>&1 || true

# Build workspace packages (vitest-era PRs need compiled deps)
pnpm -r --filter=./packages/** run build 2>&1 || true

""".format(repo_dir=REPO_DIR, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo_dir}
if [ -d node_modules/vitest ]; then
    pnpm test -- --reporter=verbose 2>&1
else
    pnpm test 2>&1
fi

""".format(repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo_dir}

# Apply test patch
git apply --whitespace=nowarn /home/test.patch 2>&1 || \\
  git apply --whitespace=nowarn --reject /home/test.patch 2>&1 || true

# Remove lockfile to avoid patch conflicts — pnpm install regenerates it
rm -f pnpm-lock.yaml

# Reinstall in case patch adds/changes dependencies
pnpm install --no-frozen-lockfile 2>&1 || true

# Rebuild workspace packages
pnpm -r --filter=./packages/** run build 2>&1 || true

if [ -d node_modules/vitest ]; then
    pnpm test -- --reporter=verbose 2>&1
else
    pnpm test 2>&1
fi

""".format(repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo_dir}

# Apply test patch then fix patch
git apply --whitespace=nowarn /home/test.patch 2>&1 || \\
  git apply --whitespace=nowarn --reject /home/test.patch 2>&1 || true

git apply --whitespace=nowarn /home/fix.patch 2>&1 || \\
  git apply --whitespace=nowarn --reject /home/fix.patch 2>&1 || true

# Remove lockfile to avoid patch conflicts — pnpm install regenerates it
rm -f pnpm-lock.yaml

# Reinstall in case patches add/change dependencies
pnpm install --no-frozen-lockfile 2>&1 || true

# Rebuild workspace packages
pnpm -r --filter=./packages/** run build 2>&1 || true

if [ -d node_modules/vitest ]; then
    pnpm test -- --reporter=verbose 2>&1
else
    pnpm test 2>&1
fi

""".format(repo_dir=REPO_DIR),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""# syntax=docker/dockerfile:1.6

FROM {name}:{tag}

{copy_commands}
WORKDIR /home/{REPO_DIR}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=$BASE_COMMIT

RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}
"""


class Slidev(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SlidevImageDefault(self.pr, self._config)

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

        # Vitest output patterns (strips timing and test count metadata for cross-stage consistency)
        re_vitest_pass = re.compile(r"^\s*[✓✔√]\s+(.+?)(?:\s+\([^)]*\))?(?:\s+\d+(?:\.\d+)?\s*m?s)?(?:\s+\[skipped\])?$")
        re_vitest_fail = re.compile(r"^\s*[❯×✗]\s+(.+?)(?:\s+\([^)]*\))?(?:\s+\d+(?:\.\d+)?\s*m?s)?(?:\s+\[skipped\])?$")
        re_vitest_skip = re.compile(r"^\s*[↓]\s+(.+?)(?:\s+\([^)]*\))?(?:\s+\d+(?:\.\d+)?\s*m?s)?(?:\s+\[skipped\])?$")

        # Jest output patterns (individual tests)
        re_jest_pass = re.compile(r"^\s*[✓✔√]\s+(.+?)(?:\s+\([^)]*\))?(?:\s+\d+(?:\.\d+)?\s*m?s)?(?:\s+\[skipped\])?$")
        re_jest_fail = re.compile(r"^\s*[✕✗×]\s+(.+?)(?:\s+\([^)]*\))?(?:\s+\d+(?:\.\d+)?\s*m?s)?(?:\s+\[skipped\])?$")
        re_jest_skip = re.compile(r"^\s*○\s+(.+)$")

        # Jest file-level patterns (PASS/FAIL test/file.ts)
        re_jest_file_pass = re.compile(r"^PASS\s+(.+)$")
        re_jest_file_fail = re.compile(r"^FAIL\s+(.+)$")

        # Vitest summary line: Tests  4 passed (4)
        re_vitest_summary = re.compile(
            r"^\s*Tests?\s+(\d+)\s+passed\s*(?:\|\s*(\d+)\s+failed)?"
        )

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            # Vitest patterns
            match = re_vitest_pass.match(line)
            if match:
                passed_tests.add(match.group(1).strip())
                continue

            match = re_vitest_fail.match(line)
            if match:
                failed_tests.add(match.group(1).strip())
                continue

            match = re_vitest_skip.match(line)
            if match:
                skipped_tests.add(match.group(1).strip())
                continue

            # Jest patterns
            match = re_jest_pass.match(line)
            if match:
                passed_tests.add(match.group(1).strip())
                continue

            match = re_jest_fail.match(line)
            if match:
                failed_tests.add(match.group(1).strip())
                continue

            match = re_jest_skip.match(line)
            if match:
                skipped_tests.add(match.group(1).strip())
                continue

            match = re_jest_file_pass.match(line)
            if match:
                passed_tests.add(match.group(1).strip())
                continue

            match = re_jest_file_fail.match(line)
            if match:
                failed_tests.add(match.group(1).strip())
                continue

        # Remove any test that shows up in both pass and fail (fail wins)
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


_BUNDLE_NIS = [
    "3-5",
    "111-112",
    "191-192",
    "225-228",
    "264-265-269",
    "276-321-326-335-337-342",
    "311-313-315",
    "383-389",
    "438-451",
    "482-546-547",
    "513-531-536-541",
    "549-550-551",
    "552-566-567-568",
    "598-601-604",
    "620-621-623-624",
    "633-638-647-650",
    "783-784-787",
    "796-797",
    "813-819-825-826-832-835",
    "840-845-846",
    "844-885-891-893-895-904-908-909-910-913",
    "867-872-874-876-879-881-882",
    "1001-1005-1006",
    "1023-1025-1029-1031",
    "1032-1033-1036-1046-1060-1063",
    "1058-1059-1090-1101",
    "1143-1146-1147",
    "1153-1155",
    "1186-1189-1191-1192",
    "1199-1201-1202-1205",
    "1209-1210-1212-1218",
    "1220-1228",
    "1222-1265-1266-1267",
    "1247-1273-1279-1286-1289-1290-1291-1293-1294-1295-1299-1300-1301-1302-1305-1306-1308-1311-1312-1313-1314-1315-1317-1318-1319-1321-1322-1326-1327-1328-1330-1331-1332-1334-1336-1337-1340-1342-1343-1344-1345-1346-1347-1348-1350-1352-1353-1354-1356-1357-1359-1362-1363-1365-1367-1368-1370-1372-1376-1377-1378-1379-1380-1382-1383-1384-1387-1388-1389-1393-1394-1395-1396-1397-1400-1403-1404",
    "1402-1435-1464-1475-1508-1512-1516-1517-1518-1521-1523-1526-1529-1530-1534-1535-1536-1543-1544-1545-1546-1548-1549-1553-1556-1557-1559-1562-1564-1566-1571-1576-1578-1581",
    "1588-1589-1595-1596-1598",
    "1645-1682-1683-1685-1687-1688-1692-1693-1698-1699",
    "1673-1708-1736-1737-1739-1740-1741-1744-1747-1755-1758-1760",
    "1700-1812-1842-1843-1846-1849-1854-1857-1858-1869-1877-1879-1884-1886-1890-1891-1895-1896-1898-1902-1905-1908-1909-1913-1916-1922-1926-1928-1933-1936-1937-1942-1948-1951-1952-1954-1963-1964-1965-1969-1971-1972-1973-1974-1980",
    "1743-1761-1762-1766-1767-1769",
    "1782-1788-1789",
    "1804-1838-1840-1841",
    "1982-2016-2024-2025-2027-2028-2029",
    "2026-2320-2329-2343-2344-2345-2347-2348-2349-2350-2351-2354-2355-2358-2359-2360-2361-2362-2369-2370",
    "2089-2094-2096-2099-2107-2112-2116-2117-2118-2136-2139",
    "2098-2100-2101-2102-2103",
    "2175-2176-2178-2179",
    "2185-2187-2189-2190-2191",
    "2309-2313",
    "2317-2318-2319-2321-2323-2324-2330-2333-2334",
    "2400-2401-2404-2410-2411",
    "2403-2414-2418-2419-2423-2424",
    "2425-2451-2452-2453-2457-2458-2459",
    "2465-2472-2473-2474",
    "2466-2471",
]
for _ni in _BUNDLE_NIS:
    Instance._registry[f"slidevjs/{_ni}"] = Slidev
