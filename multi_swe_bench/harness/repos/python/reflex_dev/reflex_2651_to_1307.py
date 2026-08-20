import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _strip_binary_diffs(patch: str) -> str:
    """Remove binary diff hunks from a unified diff so `git apply` never aborts
    on a binary hunk with no full-index line (e.g. `docs/.DS_Store`, images).
    Safe: binary hunks touch no Python source and never affect test outcomes."""
    sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
    return "".join(
        s for s in sections
        if s and "Binary files " not in s and "GIT binary patch" not in s
    )



def parse_pytest_log(log: str) -> TestResult:
    """Parse pytest -v -rA output. Verbose result lines look like:

        tests/test_var.py::test_fstring_roundtrip PASSED [ 12%]

    Test names are kept as full pytest node ids (`path::test`)."""
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    # Anchor on the trailing "<STATUS> [ NN%]" so node ids containing spaces
    # (parametrized tests, e.g. `test_x[append then pop]`) are captured whole.
    re_line = re.compile(
        r"^(.+?::.+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+\[\s*\d+%\]\s*$"
    )

    for raw in log.splitlines():
        line = ANSI_ESCAPE.sub("", raw).strip()
        m = re_line.match(line)
        if not m:
            continue
        nodeid, status = m.group(1).strip(), m.group(2)
        if status in ("PASSED", "XPASS"):
            passed_tests.add(nodeid)
        elif status in ("FAILED", "ERROR"):
            failed_tests.add(nodeid)
        else:  # SKIPPED, XFAIL
            skipped_tests.add(nodeid)

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


class ReflexEra2ImageBase(Image):
    """reflex era 2 — post-rename, still poetry (PRs 1307-2651, v0.2->0.4):
    package dir `reflex/`, deps via poetry, pytest unit suite under `tests/`
    (top-level `integration/` harness tests are skipped). Python 3.10."""

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
        return "python:3.10-slim"

    def image_tag(self) -> str:
        return "base-era2"

    def workdir(self) -> str:
        return "base-era2"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo
        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV TZ=UTC
ENV http_proxy=${{http_proxy}}
ENV https_proxy=${{https_proxy}}
ENV HTTP_PROXY=${{HTTP_PROXY}}
ENV HTTPS_PROXY=${{HTTPS_PROXY}}
ENV no_proxy=${{no_proxy}}
ENV NO_PROXY=${{NO_PROXY}}
ENV SSL_CERT_FILE=${{CA_CERT_PATH}}
ENV REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}}
ENV CURL_CA_BUNDLE=${{CA_CERT_PATH}}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git build-essential curl ca-certificates && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN git config --global --add safe.directory '*'
{code}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class ReflexEra2ImageDefault(Image):
    """Per-PR image: checkout base commit, `poetry install`, run the targeted
    pytest unit tests."""

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
        return ReflexEra2ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", _strip_binary_diffs(self.pr.fix_patch)),
            File(".", "test.patch", _strip_binary_diffs(self.pr.test_patch)),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}
pip install --no-cache-dir "poetry<2" || true
poetry config virtualenvs.create false || true
poetry install --no-interaction || pip install --no-cache-dir -e . || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
TEST_FILES=$({{ grep -E '^diff --git a/tests/' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | grep -E '\\.py$' | grep -vE 'conftest\\.py|__init__\\.py' | sort -u; }} || true)
EXIST=""
for f in $TEST_FILES; do if [ -f "$f" ]; then EXIST="$EXIST $f"; fi; done
if [ -z "$EXIST" ]; then echo "NO_BASELINE_TEST_FILES"; exit 0; fi
python -m pytest $EXIST -v --no-header -rA --tb=no \
    -p no:cacheprovider --continue-on-collection-errors -o 'addopts=' 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
EXCLUDES=(--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \
    --exclude='*.ico' --exclude='*.pdf' --exclude='*.woff*' --exclude='*.svg' \
    --exclude='*.lockb' --exclude='*.webp' --exclude='*.mp3')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
if grep -qE '^diff --git a/(pyproject\\.toml|poetry\\.lock)' /home/test.patch 2>/dev/null; then
    poetry install --no-interaction || pip install --no-cache-dir -e . || true
fi
TEST_FILES=$({{ grep -E '^diff --git a/tests/' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | grep -E '\\.py$' | grep -vE 'conftest\\.py|__init__\\.py' | sort -u; }} || true)
EXIST=""
for f in $TEST_FILES; do if [ -f "$f" ]; then EXIST="$EXIST $f"; fi; done
if [ -z "$EXIST" ]; then echo "NO_TEST_FILES"; exit 0; fi
python -m pytest $EXIST -v --no-header -rA --tb=no \
    -p no:cacheprovider --continue-on-collection-errors -o 'addopts=' 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
EXCLUDES=(--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \
    --exclude='*.ico' --exclude='*.pdf' --exclude='*.woff*' --exclude='*.svg' \
    --exclude='*.lockb' --exclude='*.webp' --exclude='*.mp3')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null || true
if grep -qhE '^diff --git a/(pyproject\\.toml|poetry\\.lock)' /home/test.patch /home/fix.patch 2>/dev/null; then
    poetry install --no-interaction || pip install --no-cache-dir -e . || true
fi
TEST_FILES=$({{ grep -E '^diff --git a/tests/' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | grep -E '\\.py$' | grep -vE 'conftest\\.py|__init__\\.py' | sort -u; }} || true)
EXIST=""
for f in $TEST_FILES; do if [ -f "$f" ]; then EXIST="$EXIST $f"; fi; done
if [ -z "$EXIST" ]; then echo "NO_TEST_FILES"; exit 0; fi
python -m pytest $EXIST -v --no-header -rA --tb=no \
    -p no:cacheprovider --continue-on-collection-errors -o 'addopts=' 2>&1
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

        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("reflex-dev", "reflex_2651_to_1307")
class REFLEX_2651_TO_1307(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ReflexEra2ImageDefault(self.pr, self._config)

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
        return parse_pytest_log(log)


# ---------------------------------------------------------------------------
# number_interval bundle routing (prs_in_bundle dash-joined)  -- PIPELINE 11b
# ---------------------------------------------------------------------------
# Raw dataset leaves number_interval empty; delivery sets it to
# "-".join(prs_in_bundle). Register REFLEX_2651_TO_1307 (this era) under every bundle key so
# delivered records resolve to pubkey/<bundle>. Original era-key registration
# above is kept.
_BUNDLE_NIS_REFLEX_ERA2 = [
    "1307-1312-1313-1316-1321-1322-1326-1333-1335-1336-1337-1338-1339-1341-1345-1348-1353-1355-1358-1359-1362",
    "1360-1361-1385-1387-1390-1394-1398-1399-1400-1403-1404-1405-1416-1418-1419-1428-1430-1437-1442-1444-1447-1452-1454-1455-1457-1458-1462-1467",
    "1369-1370-1374-1375-1382-1389",
    "1379-1542-1606-1607-1629-1636-1640-1647-1652-1653-1665-1668-1669-1670-1679-1681-1682-1685-1695-1700-1701-1703-1704-1706-1708-1714-1716-1717-1723-1724-1725-1726-1728-1729-1733-1738-1739-1744-1745-1750-1757-1758-1761-1765-1767-1769-1770-1773-1774-1780-1781",
    "1383-1460-1463-1469-1471-1478-1480-1481-1483-1484-1491-1492-1493-1496-1498-1499-1501-1502-1504-1510-1516-1521-1524-1533-1537-1538-1547",
    "1517-1520-1535-1541-1543-1548-1550-1552-1553-1554-1557-1559-1566-1567-1568-1569-1570-1571-1572-1573-1575-1580-1590-1591-1599-1600-1602-1603-1608-1609-1612-1613-1614-1616-1619-1623-1624-1625-1626-1627-1634-1645-1650-1651",
    "1674-1676-1713-1748-1751-1764-1768-1782-1785-1786-1788-1792-1797-1799-1806-1807-1815-1816-1817-1820-1821-1822-1827-1837-1839-1842-1843-1844-1845-1846-1848-1849-1853",
    "1810-1861-1867-1921-1926-1937-1939-1941-1942-1943-1945-1946-1947-1950-1953-1954-1956-1961-1963-1967-1974-1975-1978-1982-1984-1986-1987-1990-1992-1993-1994-1998-2001-2002-2003-2005-2009-2010-2011-2014-2015-2016-2018-2019-2021-2022-2023-2025-2026-2027-2030-2033-2034-2039-2040-2044-2045-2046-2048-2049-2050-2051-2053-2055-2056-2059-2060-2061",
    "1828-1851-1852-1855-1859-1860-1866-1868-1869-1871-1873-1875-1876-1878-1879-1887-1889-1897-1898-1902-1904-1905-1906-1915-1917-1920-1922-1924-1925-1927-1928-1930-1934-1936-1938",
    "1847-2013-2029-2067-2146-2149-2150-2159-2164-2165-2171-2172-2185-2192-2194-2195-2198-2201-2202-2205-2206-2208-2210-2212-2214-2218-2219-2224-2225-2228-2230-2231-2234-2237-2240-2241-2247-2254-2255-2256-2258-2259-2261",
    "1891-2028-2062-2068-2069-2072-2076-2082-2084-2085-2086-2089-2090-2092-2094-2095-2097-2099-2102-2104-2109-2116-2117-2118-2121-2122-2124-2125-2126-2128",
    "1899-2041-2178-2179-2181-2182-2183-2184-2187-2193",
    "2006-2012-2037-2106-2129-2130-2133-2138-2139-2142-2143-2144-2152-2153-2154-2160-2162-2167-2168-2169-2170",
    "2223-2236-2262-2263-2266-2267-2270-2271-2276-2281-2283-2284-2296-2298",
    "2246-2279-2285-2288-2291-2292-2303-2306-2308-2311-2312-2315-2316-2318-2320-2326-2327-2328-2330-2336-2338-2341-2343-2348-2350-2352-2353-2358",
    "2310-2351-2369-2370-2371-2373-2374-2375-2377-2379-2380-2383-2385-2386-2389-2391-2395-2400-2401-2402-2403-2406-2408-2409-2417-2419-2420-2421-2424-2434-2435",
    "2331-2405-2407-2410-2422-2423-2425-2430-2431-2432-2436-2439-2440-2442-2443-2444-2445-2446-2447-2448-2450-2451-2452-2453-2457-2459-2460-2463-2465-2466-2467-2468-2469-2470-2472-2474-2477-2479-2480-2481-2485-2486-2487-2488-2489-2491-2494-2497-2498-2499-2503-2504-2507-2508-2512-2514-2515-2521-2522",
    "2359-3006-3026-3049-3051-3058-3062-3071-3073-3076-3077",
    "2533-2550-2574-2617-2618-2654-2662-2663-2664-2668-2675-2677-2679-2680-2682-2684-2688-2692-2695-2696-2699",
    "2651-2666-2669-2672",
]
for _ni in _BUNDLE_NIS_REFLEX_ERA2:
    Instance.register("reflex-dev", _ni)(REFLEX_2651_TO_1307)
