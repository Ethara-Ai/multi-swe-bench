import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class GolangciLintVersionBase(Image):
    """Version-bucketed base image: FROM golang:{version} + clone repo.

    Shared across all PRs in the same Go version bucket.
    image_tag = "base-{interval_name}" (e.g., "base-golangci-lint_558_to_0").
    """

    def __init__(self, pr: PullRequest, config: Config, go_version: str, interval_name: str = ""):
        self._pr = pr
        self._config = config
        self._go_version = go_version
        self._interval_name = interval_name or f"go{go_version}"

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    @property
    def go_version(self) -> str:
        return self._go_version

    def dependency(self) -> Union[str, "Image"]:
        return f"golang:{self._go_version}"

    def image_tag(self) -> str:
        return f"base-{self._interval_name}"

    def workdir(self) -> str:
        return f"base-{self._interval_name}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()
        org = self.pr.org
        repo = self.pr.repo

        if self.config.need_clone:
            fetch = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            fetch = f"COPY {repo} /home/{repo}"

        # Old Go images are Debian buster/stretch, whose apt repos are archived;
        # rewrite sources to archive.debian.org so `apt-get install git` works.
        debian_fix = ""
        if self._go_version in ("1.13", "1.14", "1.15", "1.16", "1.17", "1.18", "1.19", "1.20"):
            debian_fix = (
                "RUN if grep -q 'buster\\|stretch' /etc/apt/sources.list 2>/dev/null; then \\\n"
                "        sed -i 's|deb.debian.org|archive.debian.org|g' /etc/apt/sources.list && \\\n"
                "        sed -i 's|security.debian.org|archive.debian.org|g' /etc/apt/sources.list && \\\n"
                "        sed -i '/stretch-updates/d' /etc/apt/sources.list && \\\n"
                "        sed -i '/buster-updates/d' /etc/apt/sources.list && \\\n"
                "        echo 'Acquire::Check-Valid-Until \"false\";' > /etc/apt/apt.conf.d/99no-check-valid; \\\n"
                "    fi\n\n"
            )

        # `# syntax` opts this shared version-bucketed base out of the
        # DockerfileEnhancer, which would otherwise rewrite the `git clone` line
        # into clone + `git checkout ${BASE_COMMIT}` + `git gc --prune`. Every PR
        # in a Go-version bucket shares ONE base tag, so pruning the base to a
        # single PR's base.sha breaks every other PR ("reference is not a tree").
        # The base keeps full history; the strict anti-reward-hack hardening runs
        # per-PR (see GolangciLintImageDefault).
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/
{debian_fix}RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates && rm -rf /var/lib/apt/lists/* || true

RUN git config --global --add safe.directory '*'
{fetch}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

CMD ["/bin/bash"]
"""


_COMMON_ENV = """export CI=true
export GOTOOLCHAIN=auto
export GL_TEST_RUN=1
export GOLANGCI_LINT_INSTALLED=true
export CGO_ENABLED=1
export GO111MODULE=auto"""


_GOPATH_SETUP = """# Pre-modules era: stage the repo under GOPATH so Go can resolve imports.
export GOPATH=/go
REPO_PATH="$GOPATH/src/github.com/{pr.org}/{pr.repo}"
mkdir -p "$(dirname "$REPO_PATH")"
if [ ! -e "$REPO_PATH" ]; then
    ln -s /home/{pr.repo} "$REPO_PATH"
fi
cd "$REPO_PATH" """


class GolangciLintImageDefault(Image):
    """Per-PR image: FROM version-base -> checkout + patches + prepare.

    prepare_style:
      - "modules" (default): cd into clone, go mod download, go test ./...
      - "gopath":  pre-modules era; symlink into $GOPATH/src/<org>/<repo>, use vendor/
    """

    def __init__(
        self,
        pr: PullRequest,
        config: Config,
        go_version: str = "1.25",
        interval_name: str = "",
        prepare_style: str = "modules",
    ):
        self._pr = pr
        self._config = config
        self._go_version = go_version
        self._interval_name = interval_name or f"go{go_version}"
        self._prepare_style = prepare_style

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image:
        return GolangciLintVersionBase(self.pr, self.config, self._go_version, self._interval_name)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _modules_files(self) -> list[File]:
        env = _COMMON_ENV
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -e

{env}

cd /home/{self.pr.repo}
git reset --hard
git checkout {self.pr.base.sha}

# Vendor-mode short-circuits the network and skips modern Go's strict
# pseudo-version validation that rejects pre-2019 go.mod entries.
if [ -d ./vendor ]; then
    export GOFLAGS="-mod=vendor"
fi

go mod tidy 2>&1 || echo "go mod tidy failed (non-fatal)"
go mod download 2>&1 || echo "go mod download failed (non-fatal)"

if [ -d ./cmd/golangci-lint ]; then
    go build -o golangci-lint ./cmd/golangci-lint 2>&1 || echo "go build failed (non-fatal)"
fi

go test -v -count=1 -timeout 20m ./... || true
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail

{env}

cd /home/{self.pr.repo}
if [ -d ./vendor ]; then
    export GOFLAGS="-mod=vendor"
fi
if [ -d ./cmd/golangci-lint ]; then
    go build -o golangci-lint ./cmd/golangci-lint 2>&1 || echo "go build failed (non-fatal)"
fi
go test -v -count=1 -timeout 20m ./...
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail

{env}

cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch || {{ echo "Warning: git apply test.patch failed, retrying with --reject..."; git apply --reject --whitespace=nowarn /home/test.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
if [ -d ./vendor ]; then
    export GOFLAGS="-mod=vendor"
fi
if [ -d ./cmd/golangci-lint ]; then
    go build -o golangci-lint ./cmd/golangci-lint 2>&1 || echo "go build failed (non-fatal)"
fi
go test -v -count=1 -timeout 20m ./...
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail

{env}

cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ echo "Warning: git apply failed, retrying with --reject..."; git apply --reject --whitespace=nowarn /home/test.patch 2>&1 || true; git apply --reject --whitespace=nowarn /home/fix.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
if [ -d ./vendor ]; then
    export GOFLAGS="-mod=vendor"
fi
if [ -d ./cmd/golangci-lint ]; then
    go build -o golangci-lint ./cmd/golangci-lint 2>&1 || echo "go build failed (non-fatal)"
fi
go test -v -count=1 -timeout 20m ./...
""",
            ),
        ]

    def _gopath_files(self) -> list[File]:
        env = _COMMON_ENV
        gopath_setup = _GOPATH_SETUP.format(pr=self.pr)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -e

{env}

# Initial checkout uses the real clone path (where git history lives).
cd /home/{self.pr.repo}
git reset --hard
git checkout {self.pr.base.sha}

{gopath_setup}

# Pre-modules golangci-lint ships its deps under vendor/.
# Make sure GOFLAGS uses vendor mode when present.
if [ -d ./vendor ]; then
    export GOFLAGS="-mod=vendor"
fi

if [ -d ./cmd/golangci-lint ]; then
    go build -o golangci-lint ./cmd/golangci-lint 2>&1 || echo "go build failed (non-fatal)"
fi

go test -v -count=1 -timeout 20m ./... || true
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail

{env}

{gopath_setup}

if [ -d ./vendor ]; then
    export GOFLAGS="-mod=vendor"
fi
if [ -d ./cmd/golangci-lint ]; then
    go build -o golangci-lint ./cmd/golangci-lint 2>&1 || echo "go build failed (non-fatal)"
fi
go test -v -count=1 -timeout 20m ./...
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail

{env}

{gopath_setup}

git apply --whitespace=nowarn /home/test.patch || {{ echo "Warning: git apply test.patch failed, retrying with --reject..."; git apply --reject --whitespace=nowarn /home/test.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
if [ -d ./vendor ]; then
    export GOFLAGS="-mod=vendor"
fi
if [ -d ./cmd/golangci-lint ]; then
    go build -o golangci-lint ./cmd/golangci-lint 2>&1 || echo "go build failed (non-fatal)"
fi
go test -v -count=1 -timeout 20m ./...
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail

{env}

{gopath_setup}

git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ echo "Warning: git apply failed, retrying with --reject..."; git apply --reject --whitespace=nowarn /home/test.patch 2>&1 || true; git apply --reject --whitespace=nowarn /home/fix.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
if [ -d ./vendor ]; then
    export GOFLAGS="-mod=vendor"
fi
if [ -d ./cmd/golangci-lint ]; then
    go build -o golangci-lint ./cmd/golangci-lint 2>&1 || echo "go build failed (non-fatal)"
fi
go test -v -count=1 -timeout 20m ./...
""",
            ),
        ]

    def files(self) -> list[File]:
        if self._prepare_style == "gopath":
            return self._gopath_files()
        return self._modules_files()

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        # Anti-cheat hardening runs in the PR layer (the shared base keeps full
        # history so every PR's base.sha is reachable). prepare.sh checks out
        # this PR's base.sha, then the canonical hardening block detaches at that
        # literal sha and strips every other ref/reflog so later commits (the
        # fix) are unreachable.
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_RE_PASS = re.compile(r"--- PASS: (\S+)")
_RE_FAIL = re.compile(r"--- FAIL: (\S+)")
_RE_SKIP = re.compile(r"--- SKIP: (\S+)")
# Package summary lines produced by `go test`:
#   "ok      github.com/foo/bar/pkg  0.123s"
#   "FAIL    github.com/foo/bar/pkg  0.123s"
#   "?       github.com/foo/bar/pkg  [no test files]"
_RE_PKG_OK = re.compile(r"^ok\s+(\S+)")
_RE_PKG_FAIL = re.compile(r"^FAIL\s+(\S+)")
_RE_PKG_NOTEST = re.compile(r"^\?\s+(\S+)")


def golangci_lint_parse_log(test_log: str) -> TestResult:
    """Shared parse_log for all golangci-lint instances.

    Go subtest names (e.g. `TestX/case1`) are not globally unique — the same
    name can appear in multiple packages. To avoid collisions, we buffer test
    names per package and prefix each one with the package path emitted on the
    trailing `ok|FAIL pkg` summary line.
    """
    test_log = _ANSI_RE.sub("", test_log)

    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    # Buffer test outcomes for the current package; flush when we see `ok pkg`,
    # `FAIL pkg`, or `? pkg`. Buffer holds (status, raw_name) tuples.
    buf: list[tuple[str, str]] = []

    def flush(pkg: str) -> None:
        for status, name in buf:
            qualified = f"{pkg}::{name}"
            if status == "PASS":
                if qualified not in failed_tests:
                    skipped_tests.discard(qualified)
                    passed_tests.add(qualified)
            elif status == "FAIL":
                passed_tests.discard(qualified)
                skipped_tests.discard(qualified)
                failed_tests.add(qualified)
            elif status == "SKIP":
                if qualified not in passed_tests and qualified not in failed_tests:
                    skipped_tests.add(qualified)
        buf.clear()

    for line in test_log.splitlines():
        line = line.strip()

        m = _RE_PASS.match(line)
        if m:
            buf.append(("PASS", m.group(1)))
            continue
        m = _RE_FAIL.match(line)
        if m:
            buf.append(("FAIL", m.group(1)))
            continue
        m = _RE_SKIP.match(line)
        if m:
            buf.append(("SKIP", m.group(1)))
            continue

        m = _RE_PKG_OK.match(line) or _RE_PKG_FAIL.match(line) or _RE_PKG_NOTEST.match(line)
        if m:
            flush(m.group(1))

    # Trailing tests with no package summary (rare — `go test` crashed mid-run).
    # Flush under a synthetic package so they're still counted.
    if buf:
        flush("<unknown-package>")

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("golangci", "golangci-lint")
class GolangciLint(Instance):
    """Default golangci-lint instance - for PRs without number_interval."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GolangciLintImageDefault(
            self.pr,
            self._config,
            go_version="1.25",
            interval_name="golangci-lint",
            prepare_style="modules",
        )

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
        return golangci_lint_parse_log(test_log)


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# Registered so delivered records (which carry the dash-joined number_interval)
# resolve to this class (PIPELINE §11/§11b). The bare "golangci-lint" key above
# still routes the build-time dataset, whose number_interval is empty.
_BUNDLE_NIS_GOLANGCI = [
    "8-9-10-11-15-16-18",  # pr-8 (7 PRs)
    "19-31-35-47-48-53-55-56-57-58-59-61-62",  # pr-19 (13 PRs)
    "24-27-29",  # pr-24 (3 PRs)
    "38-39-43-44",  # pr-38 (4 PRs)
    "42-46",  # pr-42 (2 PRs)
    "64-69-70-71",  # pr-64 (4 PRs)
    "74-77-79-81-82",  # pr-74 (5 PRs)
    "83-84-89-91-95-101-103-112-113-114-115-117",  # pr-83 (12 PRs)
    "119-120-135-136",  # pr-119 (4 PRs)
    "128-133-134",  # pr-128 (3 PRs)
    "140-141-142-144-145-147",  # pr-140 (6 PRs)
    "149-176-179-180-185-188-189-192-197",  # pr-149 (9 PRs)
    "151-152-163-169-171",  # pr-151 (5 PRs)
    "172-173-174-175",  # pr-172 (4 PRs)
    "200-201-202-224-226-227-236-240-244-245-246-248-249-251-252-254-256-258",  # pr-200 (18 PRs)
    "210-211",  # pr-210 (2 PRs)
    "262-273-274-275-278-279",  # pr-262 (6 PRs)
    "268-269-270-271",  # pr-268 (4 PRs)
    "285-286-332-336-342-351-352-353-363-364",  # pr-285 (10 PRs)
    "290-297-303-305",  # pr-290 (4 PRs)
    "306-309-311-317-319-321-327-328-329-330",  # pr-306 (10 PRs)
    "390-392-394-396-398-399",  # pr-390 (6 PRs)
    "406-417-434-435-438-442-443-444-445-448-459-474",  # pr-406 (12 PRs)
    "480-487-498-501-502-504-505-507-515-524-525-533-548-555-557-559-560-561",  # pr-480 (18 PRs)
    "558-565-585-589-591-594-601-603-605-607-610-613-625-626-630-632-636-640-644-662-666-667-668-670-672",  # pr-558 (25 PRs)
    "621-673-674-676-679-680-681-684-689-691-693-694-695-697-698-699-703-704-705-713-714-716-717-720-723-724-725-726-727-728",  # pr-621 (30 PRs)
    "729-735-736",  # pr-729 (3 PRs)
    "744-745-746-747-751-756-757-758-759-760-763-764-765-766-767-769-771-772-774-775-776-777-778-779-783-784-786-787-788-789-790-792-793-794-797-798",  # pr-744 (36 PRs)
    "831-834-842-844-845-848-851-856-859-863-868-871-874-875-880-883-890-891-894-899-900",  # pr-831 (21 PRs)
    "837-1019-1050",  # pr-837 (3 PRs)
    "841-849-850-905-906-907-917-920-921",  # pr-841 (9 PRs)
    "852-951-982-993-1013-1017-1029-1031-1032-1038",  # pr-852 (10 PRs)
    "904-927",  # pr-904 (2 PRs)
    "922-930-947-953-964",  # pr-922 (5 PRs)
    "929-933-936-944-946-952",  # pr-929 (6 PRs)
    "937-983-984-987-989-992",  # pr-937 (6 PRs)
    "969-975-976",  # pr-969 (3 PRs)
    "997-1056-1057-1058-1059-1060-1061-1062-1063-1064-1065-1066-1067-1068-1069-1070-1072-1073-1074-1077-1079-1084-1089",  # pr-997 (23 PRs)
    "1000-1022-1036-1044-1045-1049",  # pr-1000 (6 PRs)
    "1094-1095-1096-1097-1098-1099-1101-1104-1112-1114-1116-1118-1120-1126-1127-1129-1131-1134-1136-1137-1139-1140-1143-1145-1146-1147-1148-1150-1151-1154-1155-1158-1159-1160-1161-1162-1163-1164-1166-1167-1171-1173-1174-1177-1179-1181-1183-1192-1196-1198-1203-1205",  # pr-1094 (52 PRs)
    "1201-1352-1357-1358-1363-1364-1365-1366-1367-1368-1369-1376-1377-1378-1380-1381-1383-1384-1385-1386-1387-1388-1389-1390-1394-1396-1397-1400-1401-1402-1405-1406-1407-1408-1410-1411-1412-1413-1414-1415-1417-1418-1419-1420-1422-1423-1424-1447-1448-1449-1451-1457-1458-1459",  # pr-1201 (54 PRs)
    "1206-1207-1209-1212",  # pr-1206 (4 PRs)
    "1214-1218-1228-1234-1236-1238-1239-1240-1242-1243-1244-1246-1247-1250-1253",  # pr-1214 (15 PRs)
    "1221-1223-1224-1226-1229-1231",  # pr-1221 (6 PRs)
    "1252-1421-1476-1491-1494-1497-1498-1500-1501-1503-1514",  # pr-1252 (11 PRs)
    "1254-1256-1260-1261-1266-1267-1273-1277-1280-1281-1282",  # pr-1254 (11 PRs)
    "1265-1623-1876-1877-1879-1880-1881-1882-1883-1886-1889-1890-1891-1899-1901-1906-1907-1908-1911-1917-1918-1919-1922-1925-1926-1927-1929-1930-1933-1934-1935-1938-1946-1947-1948-1949-1950-1951-1952-1955-1956-1960",  # pr-1265 (42 PRs)
    "1279-1283-1284-1285-1286-1287-1288-1289-1290-1292-1295-1296-1297-1298-1299-1300-1302-1303-1305-1307-1309-1310-1311-1312-1313-1315-1321-1325-1326-1327-1328-1329-1330-1332-1333-1334-1336-1337-1339-1340-1341-1342-1344-1345-1347-1350-1353-1354-1355-1356",  # pr-1279 (50 PRs)
    "1293-1520-1521-1526-1527-1528-1529-1531-1532-1533-1534-1536-1540-1541-1544-1548-1553-1554-1558-1560-1562-1567-1569-1571-1572-1573-1578-1584-1585",  # pr-1293 (29 PRs)
    "1319-1360-1460-1576-1651-1660-1732-1750-1751-1752-1755-1756-1757-1758-1759-1760-1761-1762-1763-1764-1765-1766-1772-1773-1775-1780-1781-1783-1785-1786-1788-1789-1790-1791-1793-1794-1795-1796-1797-1798-1799-1800-1801-1802-1803-1804-1805-1806-1807",  # pr-1319 (49 PRs)
    "1467-1469-1471-1472",  # pr-1467 (4 PRs)
    "1563-1591-1593-1601-1602-1603-1604-1605-1606-1607-1608-1609-1610-1611-1612-1613-1614-1615-1616-1617-1618-1619-1620-1621-1622-1624-1625-1626-1627-1628-1629-1630-1631",  # pr-1563 (33 PRs)
    "1583-1815-1816-1817-1819-1821-1822-1823-1827-1829-1830-1831-1834-1837-1838-1840-1842-1843-1844-1845-1847-1854-1861-1862-1863-1864-1865-1866-1867-1869-1870-1871",  # pr-1583 (32 PRs)
    "1587-1595-1643-1648-1654-1664-1665-1667",  # pr-1587 (8 PRs)
    "1637-1638-1639",  # pr-1637 (3 PRs)
    "1647-1663-1666-1668-1669-1670-1671-1672-1673-1674-1677-1678-1679-1680-1688-1694-1696-1697-1698-1699-1700-1701-1702-1703-1704-1705-1706-1707-1708-1709-1710-1711-1712-1713-1714-1715-1716-1717-1718-1719-1720-1721-1722-1723-1729-1731-1733-1734-1735-1736-1738-1739-1740",  # pr-1647 (53 PRs)
    "1742-1743-1744-1746-1747",  # pr-1742 (5 PRs)
    "1963-1964-1965-1966-1967-1968-1971-1975-1976-1979",  # pr-1963 (10 PRs)
    "1983-1985-1990-1991-1992-1994-1995-1996-1997-2000-2003-2005-2006-2010-2013-2017-2018-2019-2020-2023-2024-2025-2027-2028-2029-2030-2031-2032-2033-2034-2035-2036-2037-2039-2040-2042-2043-2044-2045-2052-2053-2055",  # pr-1983 (42 PRs)
    "2041-2216-2219-2221-2224-2225-2226-2230-2236-2237-2240-2243-2244-2245-2246-2247-2248-2249-2250-2252-2259-2260-2262-2264-2265-2266-2267-2269-2270-2271-2272-2275-2276-2277-2278-2289-2295-2299-2303-2304-2306-2308-2309-2310-2317-2318-2319-2320-2321-2322-2323-2324-2325-2326-2327-2330-2331-2332-2333-2334-2335-2336-2338-2340-2341",  # pr-2041 (65 PRs)
    "2066-2069-2071-2072-2073-2075-2077-2078-2080-2081-2082-2083-2084-2085-2086-2089-2091-2092-2094-2095-2096-2098-2100-2101-2102-2103-2105-2106-2107-2109-2110-2112-2113-2117-2122-2123-2124-2125-2128-2129-2131-2145-2147-2148-2149-2150-2151-2152-2153-2154-2155-2165-2166-2168-2169-2174-2179",  # pr-2066 (57 PRs)
    "2342-2344-2347-2348-2350-2351-2352-2353-2354-2358-2359-2360-2362-2364-2366-2369-2370-2371-2372-2373-2379-2380-2382-2383-2384-2385-2386-2388-2389-2390-2391-2392-2396-2397-2398-2405-2412-2413-2424-2425-2426-2427-2435-2436-2437-2441-2442-2443-2444-2446-2447-2448-2450-2453-2454-2455-2456-2457-2458-2459-2460-2461-2463-2467-2471-2472-2473-2474-2476-2482-2483-2484-2487-2490-2491-2492-2493-2494-2495-2496-2497-2498-2499-2500-2501-2502-2503-2506-2507-2508-2509-2510-2511-2514-2516-2517-2518-2519-2520",  # pr-2342 (99 PRs)
    "2387-3130-3134-3139-3142-3144-3147-3148-3150-3151-3152-3153-3154-3155-3156-3157-3158-3159-3160-3161-3162-3163-3164-3165-3167-3168-3169-3170-3173-3174-3179-3180-3181-3182-3186-3187-3188-3189-3190-3192-3194-3196-3198-3202-3204-3205-3206-3207-3208-3209-3210-3211-3212-3215-3220-3226-3233-3234-3237-3238-3239-3241-3242-3243-3254-3255-3256-3257-3260-3261-3262-3263-3265-3266-3267",  # pr-2387 (75 PRs)
    "2438-2584-2585-2594-2596-2599-2602-2603-2607-2611-2614-2616-2617-2618-2620-2621-2622-2623-2624-2625-2627-2628-2629-2630-2631-2632-2633-2634-2635-2636-2640-2641-2642-2643-2644-2646-2652",  # pr-2438 (37 PRs)
    "2521-2522-2531-2532-2534-2535-2536-2537-2538-2539-2540-2541-2542-2545-2546-2547-2548-2551-2552-2553-2554-2556-2557-2558-2559-2560-2564-2567-2570-2571-2572-2576-2578",  # pr-2521 (33 PRs)
    "2655-2656-2659-2660-2661-2665-2666-2669-2672-2674",  # pr-2655 (10 PRs)
    "2657-2667-2677-2682-2684-2685-2686-2687-2688-2691-2693-2694-2695-2696-2697-2698-2699-2700-2701-2703-2704-2705-2713-2714-2715-2716-2717-2718-2719-2720-2721-2722-2723-2724-2725-2726-2727-2728-2729-2730-2731-2732-2733-2734-2735-2736-2737-2738-2739-2740-2741-2742-2743-2744-2746-2749-2753-2754-2755-2756-2757-2758-2759-2760-2761-2763-2770-2772-2776-2780-2781-2782-2789-2791-2792-2793-2794-2795-2796-2797-2798-2800-2801-2802-2803-2804-2805-2806-2807-2808-2809-2810-2811-2812-2813-2814-2815-2816-2817-2818-2819-2820-2821-2822-2829",  # pr-2657 (105 PRs)
    "2828-2860-2865-2867-2870-2871-2872-2873-2874-2875-2876-2880-2882-2884-2885-2886-2887-2888-2889-2890-2891-2892-2893-2894-2895-2896-2897-2898-2899-2900-2902-2904-2905-2906-2907-2908-2909-2913-2916-2917-2918-2921-2925-2926-2927-2928-2929-2932-2933-2934-2935-2936-2937-2938-2939-2942-2943-2944-2945-2949-2950-2951-2952-2953-2957-2958-2959-2961-2962-2965-2966-2967-2968-2971-2973-2974-2976-2978-2979",  # pr-2828 (79 PRs)
    "2833-2836-2837-2838-2841-2842-2845",  # pr-2833 (7 PRs)
    "2846-2850-2853-2854-2855-2857-2858",  # pr-2846 (7 PRs)
    "2981-2984-2988-2989-2991-2992-2994-2995",  # pr-2981 (8 PRs)
    "3000-3001-3003-3007-3008-3009-3010-3012-3013-3019-3025-3028-3029-3030-3031-3032-3033",  # pr-3000 (17 PRs)
    "3002-3016-3034-3035-3036-3037-3038-3039-3040-3041-3042-3043-3044-3045-3046-3047-3050-3052-3054-3055-3058-3059",  # pr-3002 (22 PRs)
    "3024-3062-3064-3065-3067-3072-3074-3075-3078-3089-3090-3091-3092-3093-3097-3099-3100-3102-3104-3106-3113-3117-3118-3119-3120-3122-3123-3124-3125-3128",  # pr-3024 (30 PRs)
    "3274-3278-3284-3287-3288-3294-3295-3296-3298-3300-3302-3306-3309-3310-3312",  # pr-3274 (15 PRs)
    "3307-3311-3314-3315-3317-3318-3321-3323-3330-3331-3332-3333-3334-3340-3341-3342-3343-3344-3345-3347-3349-3350-3352-3353-3355-3358-3360-3367-3368-3369-3372-3373-3377-3378-3379-3380-3381-3386-3389-3392-3393-3394-3397-3405-3407-3411-3412-3414-3415-3416-3417-3418-3422-3423-3427-3429-3434-3436-3442-3443-3444-3445-3446-3447-3448-3452-3459-3463-3465-3468-3482-3483-3496-3497-3499-3500-3501-3508-3510-3513-3514-3516-3517-3518-3519-3520-3522-3537",  # pr-3307 (88 PRs)
    "3316-4335-4365-4366-4378-4382-4386-4392-4394-4396-4397-4398-4400-4401-4402-4403-4404-4406-4407-4408-4409-4410-4412-4414-4416-4418-4419-4420-4422-4428-4429-4430-4431-4435-4436-4437-4438-4439-4440-4441-4443-4444-4446-4447-4448-4449-4451-4452-4455-4456-4457-4460-4461-4462-4464-4465-4466-4467-4468-4469-4470-4472-4473-4474-4477-4478-4479-4480-4481-4483-4484-4487-4488-4489-4491-4492-4493-4494-4495-4496-4497-4498-4499-4500-4502-4503-4505-4507-4508-4509-4510-4513-4514-4515-4516-4517-4518-4519-4520-4521-4523-4524-4525-4526-4527-4528-4530-4531-4532-4533-4534-4535-4536-4537-4538-4539-4540-4542",  # pr-3316 (118 PRs)
    "3458-3612-3617-3622-3709-3726-3729-3730-3731-3732-3733-3734-3735-3740-3741-3742-3744-3750-3753-3754-3755-3756-3760-3761-3762-3765-3770-3771-3772-3773-3774-3777-3779-3780-3781-3782-3790-3791-3792-3794-3795-3797-3799-3800-3805-3810-3811-3812-3816-3817-3818-3821-3822-3823-3824-3825-3830-3831-3832-3834-3835-3836-3837-3838-3841-3842-3843-3844-3845-3847-3851-3852-3853-3857-3858-3859-3860",  # pr-3458 (77 PRs)
    "3506-3521-3571-3572-3604-3606-3619-3624-3625-3632-3636-3637-3639-3640-3641-3642-3643-3645-3646-3647-3648-3651-3655-3657-3658-3659-3660-3661-3664-3665-3667-3672-3675-3676-3677-3679-3680-3681-3684-3685-3686-3687-3688-3689-3691-3692-3693-3694-3695-3696-3697-3698-3699-3701-3702-3704",  # pr-3506 (56 PRs)
    "3671-3714-3793-4035-4036-4042-4043-4044-4046-4048-4055-4056-4063-4064-4065-4066-4068-4069-4070-4071-4077-4078-4079-4080-4081-4083-4086-4087-4090-4093-4094-4095-4096-4101-4102-4103-4104-4105-4107-4110-4111-4112-4114-4115-4116-4117-4119-4120-4122-4124-4125-4127-4129-4130-4131-4133-4135-4139-4140-4141-4142-4143-4144-4145-4146",  # pr-3671 (65 PRs)
    "3700-3705-3707-3710",  # pr-3700 (4 PRs)
    "3882-3884-3885-3886-3888-3889-3890-3892-3896-3898-3899-3900-3901-3902-3903-3904-3905-3907",  # pr-3882 (18 PRs)
    "3887-3909-3911-3914-3917-3918-3920-3922-3923-3924-3929-3930-3935-3936-3937-3942-3943-3944-3945-3946-3947-3948-3949-3950-3956-3959-3961-3962-3963-3970-3972-3978-3979-3980-3985-3988-3989-3991-3992-3994-3995",  # pr-3887 (41 PRs)
    "4003-4157-4166-4167-4173-4181-4183-4185-4186-4190-4191-4192-4193-4194-4195-4199-4200-4201-4203-4207-4213-4214-4215-4221-4222-4223-4225-4232-4233-4234-4235-4236-4245-4248-4249-4250-4251-4256-4257-4258-4259-4260-4261-4266-4269-4271-4272-4274-4275-4277-4279-4280-4282-4284-4285-4288-4289-4290-4291-4292-4293-4295-4296-4297-4299-4302-4304-4305-4306-4307-4308-4309-4314-4315-4316-4317-4319-4320-4326-4327-4329-4330-4333-4334-4337-4338-4339-4340-4341-4344-4346-4348-4352",  # pr-4003 (93 PRs)
    "4005-4006-4008-4009-4014-4015-4016-4017-4018-4019-4022-4024-4026-4028-4029-4030-4034",  # pr-4005 (17 PRs)
    "4149-4153-4154-4155",  # pr-4149 (4 PRs)
    "4354-4355-4357-4358-4359",  # pr-4354 (5 PRs)
    "4362-4367-4370-4371-4372-4373-4374-4377-4379-4380-4387-4388-4389-4390",  # pr-4362 (14 PRs)
    "4522-4557-4562-4567-4572-4579-4583-4585-4588-4589-4590-4591-4592-4594-4595-4597-4598-4599-4600-4601-4602-4603-4605-4607-4610-4611-4612-4613-4614-4615-4617-4619-4620-4621-4624-4625-4626-4628-4631-4632-4633-4636-4637-4638-4640-4641-4642-4643-4644-4645-4647-4649-4650-4652-4653-4655-4656-4657-4660-4663-4664-4665-4666-4667-4668-4669-4670-4671-4672-4673-4674-4675-4676-4679-4681-4682-4684-4685-4686-4688-4689-4690",  # pr-4522 (82 PRs)
    "4545-4546-4547-4548-4549-4552",  # pr-4545 (6 PRs)
    "4553-4555-4560-4564-4565-4566-4568-4569-4570-4571-4573-4574-4576-4577-4578-4580-4581-4584-4587",  # pr-4553 (19 PRs)
    "4692-4693-4694-4698-4700-4701-4702-4705-4706-4707",  # pr-4692 (10 PRs)
    "4718-4723-4726-4729-4731-4732-4734-4737-4738-4739-4740-4742-4745-4746-4747-4748",  # pr-4718 (16 PRs)
    "4749-4750-4756-4757-4758-4759-4761-4763-4766-4768-4771-4775-4778-4782-4784-4785-4786-4788-4790-4797-4801-4802-4804",  # pr-4749 (23 PRs)
    "4760-4781-4783-4798-4799-4805-4806-4807-4808-4809-4810-4811-4812-4814-4815-4817-4820-4821-4822-4823-4826-4831-4833-4836-4838-4839-4840-4843-4844-4846-4847-4848-4849-4851-4852-4855-4857-4860-4861-4862-4863-4868-4870-4881-4882-4884-4886-4887-4888-4889-4890-4892-4893-4898-4899",  # pr-4760 (55 PRs)
    "4871-4968-4998-4999-5000-5001-5002-5009-5011-5014-5016-5017-5022-5024-5025-5026-5027-5028-5029-5034-5035-5036-5038-5039-5040-5041-5045-5046-5047-5048-5049-5050-5053-5054-5056-5057-5058-5059-5061-5062-5066-5071-5074-5075-5076-5079-5081-5083-5085-5086-5087-5088-5090-5093-5094-5098-5099-5100-5101-5102-5104-5106-5107-5109-5110-5112-5113-5114-5116-5118-5119-5120-5121",  # pr-4871 (73 PRs)
    "4901-4902-4903-4906-4907-4911-4916-4921-4922-4923-4926-4927",  # pr-4901 (12 PRs)
    "4910-4918-4944-4945-4949-4951-4952-4954-4955-4960-4961-4967-4971-4973-4975-4977-4978-4979-4981-4982-4983-4985-4992-4993-4996-4997",  # pr-4910 (26 PRs)
    "4929-4930-4931-4938-4943",  # pr-4929 (5 PRs)
    "5077-5138-5155-5157-5158-5160-5161-5162-5163-5164-5166-5167-5168-5169-5170-5171-5173-5174-5175-5176-5177-5178-5179-5180-5181-5182-5186-5187-5188-5189-5192-5193-5194-5195-5196-5197-5198-5199-5200-5201-5202-5203-5204-5205-5206-5207-5209-5211-5212-5213-5216-5217-5219-5222-5226-5228-5230-5231-5232-5233-5234-5238-5239-5240-5241-5242-5243-5244-5245-5246-5247-5248-5250-5251-5253-5254-5255-5256-5258-5259-5260-5261-5262-5263-5264-5265-5266-5267-5268",  # pr-5077 (89 PRs)
    "5122-5123-5124-5125-5126-5128-5129-5130-5131-5132-5133-5134-5135-5136-5139-5140-5145-5146-5148-5150-5151-5152-5153-5154",  # pr-5122 (24 PRs)
    "5224-5292-5293-5295-5301-5302-5304-5305-5306-5307-5308-5309-5311-5312-5316-5318-5319-5320-5321-5322-5324-5325-5327-5328-5329-5330-5332-5333-5335-5336-5337-5338-5339-5341-5346-5347-5350-5351-5355-5358-5359-5360-5361-5362-5363-5365-5366-5367-5368-5372-5373-5375-5376-5377-5380-5382-5383-5384-5386-5387-5388-5389-5390",  # pr-5224 (63 PRs)
    "5280-5281-5282-5283-5286-5287",  # pr-5280 (6 PRs)
    "5288-5291",  # pr-5288 (2 PRs)
    "5357-5385-5423-5431-5432-5436-5439-5440-5446-5450-5451-5453-5454-5462-5464-5465-5468-5470-5472-5474-5475-5481-5483-5487-5505-5506-5511-5516-5517-5518-5519-5520-5522-5523-5524-5525-5526-5529-5530-5531-5532-5533-5534-5535-5536-5537-5538-5541-5542-5543-5544-5545-5549-5550-5551-5552-5553-5554-5556-5559-5560-5561-5562-5565-5573-5576-5577-5578-5579-5584-5585-5586-5587-5588-5589-5592-5594",  # pr-5357 (77 PRs)
    "5413-5414-5415-5418-5419-5420-5421-5422-5424-5426-5429-5430-5433-5434-5435-5441-5445-5447-5448-5455-5456-5463-5467-5469-5471-5476-5477-5479-5482-5484-5485-5486",  # pr-5413 (32 PRs)
    "5595-5598-5599-5606-5607-5609",  # pr-5595 (6 PRs)
    "5630-5632-5634-5635-5636-5638-5639-5643-5650-5651-5652-5653-5654-5656-5657-5658-5659-5661-5662-5663-5665-5668-5669-5673-5676-5677-5680-5681-5682-5685-5686-5687-5688-5689-5690-5691-5692-5693-5694-5695-5696-5697-5698-5699-5704-5705-5706-5708-5710-5711-5712-5713-5715-5717-5721",  # pr-5630 (55 PRs)
    "5718-5743-5749-5761-5771-5780-5781-5782-5783-5784-5785-5786-5788-5792-5796-5798-5799-5801-5802-5803-5804-5809-5810-5811-5812-5813-5814-5817-5818-5820-5821-5822-5825-5826-5827-5828-5829-5835-5836-5837-5838-5839-5841-5843-5846-5848-5849-5850-5851-5852-5853-5854-5860-5861-5864-5866-5867-5868-5869-5876-5877-5878-5880-5884-5885-5886-5888-5889-5893-5895-5896-5900-5902",  # pr-5718 (73 PRs)
    "5872-5962-5965-5967-5970-5971-5977-5980-5981-5982-5983-5984-5986-5988-5989-5990-5991-5992-5994-5997-5998-5999",  # pr-5872 (22 PRs)
    "5915-5916-5917-5932-5933-5934-5935-5936-5939-5944-5947-5948-5949-5950",  # pr-5915 (14 PRs)
    "5966-5993-6001-6002-6006-6007-6008-6009-6010-6011-6012-6013-6014-6015-6016-6018-6023-6028-6031-6032-6033-6034-6035-6039-6040-6042-6044-6046-6047-6048-6051-6054-6055-6060-6061-6062-6063-6064-6065-6067-6068-6071-6072-6073-6074-6075-6076-6077-6078-6079-6084-6085-6086-6087-6089-6093",  # pr-5966 (56 PRs)
    "6094-6095-6100-6101-6102-6103-6104-6105-6106-6111-6112-6113-6116-6117-6118-6119-6121-6123-6124-6125-6126-6128-6129-6130-6131-6132-6133-6134-6135-6136-6139-6141-6142-6143-6148-6149-6150-6152-6155",  # pr-6094 (39 PRs)
    "6162-6177-6184-6191-6192-6193-6194-6195-6196-6197-6199-6203-6206-6207-6210-6211-6221-6222-6228-6229",  # pr-6162 (20 PRs)
    "6172-6175-6179-6181-6182-6183-6185-6186-6187",  # pr-6172 (9 PRs)
    "6235-6238-6239-6240",  # pr-6235 (4 PRs)
    "6241-6243-6245-6247-6248-6252-6253-6254-6255-6256-6257-6258-6270-6275-6276-6277-6278-6279-6280-6282-6284-6286-6288-6289-6290-6295-6299-6300",  # pr-6241 (28 PRs)
    "6271-6301-6302-6304-6305-6306-6307-6308-6314-6317-6318-6319-6320-6323-6324-6325-6326-6328-6330-6332-6333-6335-6338-6342-6343-6346-6349-6350-6352-6354-6355-6356-6357-6358-6359",  # pr-6271 (35 PRs)
    "6360-6361-6362-6364-6366-6367-6368-6369-6372",  # pr-6360 (9 PRs)
    "6373-6374-6376-6377",  # pr-6373 (4 PRs)
    "6385-6421-6443-6445-6453-6454-6457-6460-6461-6462-6463-6465-6472-6474-6476-6477-6480-6482-6483-6484-6485-6491-6492-6493-6494-6495-6497-6500-6502-6503-6504-6506-6507-6510-6511-6512-6515-6516-6519-6524-6525-6526-6527-6529-6530-6531-6532-6533-6534-6535-6536",  # pr-6385 (51 PRs)
]

for _ni in _BUNDLE_NIS_GOLANGCI:
    Instance.register("golangci", _ni)(GolangciLint)
