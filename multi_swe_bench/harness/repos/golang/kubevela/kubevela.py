import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# kubevela/kubevela — an open application delivery platform built on Kubernetes (Go).
#
# Discovery (dataset analysis):
#  - 70-PR Go dataset #314..#7054 across master + release branches.
#    Single Go module `github.com/oam-dev/kubevela`.
#  - Each record's number_interval is derived from its prs_in_bundle field,
#    giving a per-record explicit PR-list NI (never a range).
#  - Test files live under pkg/, references/, e2e/, cmd/. Standard `go test`
#    per package; suite_test.go files use Ginkgo BDD via the same
#    `--- PASS:/FAIL:/SKIP:` entry-point format.
#  - go.mod goes from go 1.16 (early PRs) to go 1.23.8 (latest); GOTOOLCHAIN=auto
#    lets each PR self-select. No cgo in the codebase, but CGO_ENABLED=1 is
#    harmless and matches the rest of the golang registry family.
#  - Per-PR: the test_patch's `*_test.go` files identify the Go packages to
#    exercise; `go test` runs each. Runs are fenced with `### KVPKG ###`
#    markers so test ids stay unique across packages. envtest-dependent
#    suites (pkg/controller/.../suite_test.go) fail without kubebuilder
#    assets — they show as a single TestSuite FAIL; unit tests in
#    non-suite packages are the resolvable signal.

# Per-record number_interval strings derived from prs_in_bundle.
# Key = primary PR number (the `number` field in the JSONL).
# Value = dash-joined sorted list of all PRs in that record's bundle.
_BUNDLE_NIS: dict[int, str] = {
    314: "314-318-319-325-326-328-329-331-332-333-337-342-343-345-351-352-356-359-360-363-364-366-367-368-372-373-379-380-381-382-383-384-385-386-387-388-389-390-392-393-394-395-397-398-401-402-403-408-409-410-414-415",
    421: "421-422-423-426-428-429-431-434-435-436-437-438-439-440-442-445-446-447-449-450-452-454-455-456-457-458-459-467-469-470-471-472-475-476-477-478-480-481-482-483-484-485-486-487-488-489-490-493-495-496-497-498-500-501-502-504-505-506-507-508-509-510-511-512-513-514-515-516-517-518-519-520-523-524-525-527-530",
    554: "554-556-558-559-560-561-562-563-566-567-569-570-571-573-579-580-581-582-583-584-586-587-591-592-593-594-596-598-599-600-603-604-605-606-607-608-610-611-612-613-615-616-617-618-619-620-621",
    614: "614-628-631-639-640-642-643-648-649-650-653-659-660-663-664-667-669-670-673-678-683-687-689-690-691-697-702-703-706-711-713-716-717-725-726-729",
    684: "684-686-728-742-743-747-750-752-753-754-756-757-758-761-762-764-766-767-769-771-773-779-785-786-790-791-797",
    765: "765-774-776-781-784-798-800-801-802-803-805-807-808-809-810-811-812-815-824-825-828-831-832-833-834-841-842-843-844-846-847-853-854-858-868-869-871-875-881-887-888",
    787: "787-850-852-857-861-863-886-890-891-892-894-900-901-902-912-916-917-920-921-923-924-926-927-930-934-935-936-937-938-940-945-946-948-952-953-954-955-958-962-963-965-967-971-973-974",
    838: "838-943-947-960-972-975-983-985-986-989-990-991-994-1000-1001-1003-1004-1008-1009-1012-1014-1015-1017-1020-1021-1024-1032",
    982: "982-1034-1094-1095-1096-1099-1101-1109-1111-1114",
    1136: "1136-1152-1156-1169",
    1162: "1162-1175-1185",
    1192: "1192-1301-1395-1398-1401-1402-1405-1408-1410-1412-1414-1417-1420-1421-1426-1427-1430-1431-1433-1434",
    1267: "1267-1359-1362-1364-1366-1370-1371-1373-1374-1375-1376-1377-1380-1382-1383-1384-1385-1386-1393-1396",
    1413: "1413-1415-1419-1436-1440-1441-1442-1444-1445-1446-1447-1449-1450-1452-1453-1458-1459-1460-1461-1462-1464-1465-1466-1467-1469-1470-1471-1472-1474-1475-1477-1478-1479-1480-1481-1482-1483-1485-1488-1490-1492-1494-1496-1497-1498-1500-1501-1503-1505-1507-1508-1509-1510-1511-1512-1513-1515",
    1463: "1463-1514-1517-1519-1521-1523-1524-1525-1526-1531-1532-1533-1535-1538-1539-1540-1541-1542-1543-1545-1548-1550-1553-1554-1556-1557-1558-1561-1565-1567-1568-1569-1571-1575-1579-1582-1583-1584-1585-1586-1587",
    1489: "1489-1506-1527-1589-1590-1591-1592-1669-1676",
    1528: "1528-1739-1742",
    1743: "1743-1753-1772-1776",
    1969: "1969-2231-2270-2327-2335-2336-2337-2339-2340-2341-2343-2345-2348-2350-2351-2355-2358-2362-2368-2369-2376-2378-2379-2387-2388-2389-2391-2392-2397-2398-2399",
    2413: "2413-2414-2415-2418-2422-2424-2428-2431-2435",
    2440: "2440-2443-2446-2447-2450-2452-2453-2469-2470-2471",
    2482: "2482-2483-2487-2490-2498-2500-2506-2516-2517-2524-2525",
    2529: "2529-2552-2553-2560-2564-2565",
    2555: "2555-2579-2582-2586-2603-2610-2613-2614-2617-2621-2628",
    2633: "2633-2638-2639-2649-2652-2653-2654-2657-2690-2692-2696-2704-2705-2709-2713",
    2725: "2725-2735-2736-2745-2753",
    3079: "3079-3095-3096-3099-3100-3103-3106-3107-3109",
    3114: "3114-3115-3116-3117-3119-3120-3129-3134-3137-3146-3147-3148-3151-3153-3155-3158-3159-3161",
    3164: "3164-3165-3170-3194-3201-3202-3207-3209-3212-3213-3218-3221",
    3230: "3230-3234-3235-3239-3240-3241-3243-3247-3249-3255-3258-3265-3269-3272-3277-3286-3288-3290-3294-3295-3297-3301-3303-3306-3307-3313-3314-3315-3316-3322-3325",
    3335: "3335-3348-3349-3357-3380-3383-3405",
    3422: "3422-3843",
    3571: "3571-3574-3576-3579-3582-3586-3591-3594-3604-3608-3617-3620-3626-3631-3632-3633-3640-3643-3645-3646-3647-3649-3654-3656-3660-3661-3662",
    3668: "3668-3684-3685-3695-3697-3712-3720-3723-3728-3733-3735",
    3739: "3739-3740-3746-3748-3756-3757-3760-3762-3766-3777-3779-3784-3785-3793",
    3796: "3796-3819-3833-3842-3844-3851-3852-3857-3858-3863-3864",
    3880: "3880-3942",
    4177: "4177-4178-4190-4191-4195-4208-4211",
    4216: "4216-4229-4241-4247-4249-4254-4262-4263-4266-4269",
    4330: "4330-4333-4338-4343-4353-4354-4355-4361",
    4461: "4461-4476-4480",
    4557: "4557-4562-4564-4566-4574-4575-4579-4584-4586-4589-4591-4598-4608-4609-4610",
    4692: "4692-4698-4712-4722-4723-4726-4735-4738-4748-4749-4750-4754",
    4711: "4711-4724-4747",
    4764: "4764-4767-4768-4777-4789",
    4788: "4788-4858",
    4798: "4798-4800-4805-4812-4813-4832-4834-4835-4855-4859-4865-4867",
    4987: "4987-4988-4991-4997-5004-5012-5013-5015-5018-5022-5023-5025-5028-5033-5035-5036",
    5042: "5042-5049-5068-5074-5075-5076-5079-5080",
    5098: "5098-5104-5107-5112-5115-5119-5120",
    5135: "5135-5141-5147-5150-5154-5155-5156-5159",
    5164: "5164-5165-5167-5170-5177-5181-5188",
    5283: "5283-5289-5296-5306-5309-5311",
    5329: "5329-5339",
    5338: "5338-5340-5341-5391-5409-5559-5617-5672",
    5356: "5356-5381-5386-5390-5394-5395-5396",
    5461: "5461-5462-5473-5474-5475-5478-5486-5508-5509",
    5573: "5573-5588-5591-5599-5618-5619-5660-5673-5696",
    5874: "5874-5915-5925-5935",
    6135: "6135-6153-6155-6156-6157-6158-6159-6160",
    6170: "6170-6177-6185-6187-6192-6193-6194-6195-6196-6197-6198-6201",
    6173: "6173-6174-6175-6179-6180-6182",
    6207: "6207-6217-6228-6229-6234",
    6215: "6215-6248-6256-6266-6273-6274-6275-6277-6278-6279-6280-6281-6282-6283-6284-6288-6290-6294-6300-6303-6306-6307-6308-6309-6310-6311-6316-6322-6323-6326-6329-6330-6334-6336-6337-6338-6340-6344-6346-6347-6348-6349-6350-6351-6352-6354-6355-6356-6357-6360-6363-6386",
    6239: "6239-6245-6246-6247-6250-6254-6262-6264-6267",
    6453: "6453-6457-6474-6476-6477-6479",
    6528: "6528-6638-6711-6714-6720-6721-6723-6726-6728-6733-6735-6738-6739-6740-6747-6749-6755-6757-6759-6761-6762-6764-6766-6767-6768-6770-6771-6773-6774-6775",
    6967: "6967-6971",
    7029: "7029-7040",
    7054: "7054-7062-7063-7086",
}


def _test_pkgs(patch: str) -> list[str]:
    """Go package directories owning the `*_test.go` files in a patch."""
    pkgs: set[str] = set()
    for line in (patch or "").splitlines():
        if not line.startswith("diff --git a/"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        path = parts[2][2:] if parts[2].startswith("a/") else parts[2]
        if path.endswith("_test.go"):
            pkgs.add(path.rsplit("/", 1)[0] if "/" in path else ".")
    return sorted(pkgs)


class KubevelaImageBase(Image):
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
        return "golang:1.23-bookworm"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()

        if self.config.need_clone:
            code = f"RUN git clone --no-single-branch https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV TZ=UTC

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

ENV GOTOOLCHAIN=auto
ENV GOFLAGS=-mod=mod
ENV CGO_ENABLED=1
WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git curl ca-certificates build-essential pkg-config \\
    && rm -rf /var/lib/apt/lists/*

{code}

CMD ["/bin/bash"]
"""


class KubevelaImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return KubevelaImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        pkgs = _test_pkgs(self.pr.test_patch)
        pkg_list = " ".join(pkgs) if pkgs else "."

        prepare = f"""#!/bin/bash
set -e
cd /home/{repo}
go mod download 2>/dev/null || true
"""

        run_tests = f"""#!/bin/bash
set -uo pipefail
cd /home/{repo}
go mod download 2>/dev/null || true

for pkg in {pkg_list}; do
  [ -d "$pkg" ] || continue
  echo "### KVPKG: $pkg ###"
  go test -v -count=1 -vet=off -timeout=20m "./$pkg/" 2>&1 || true
done
"""

        run_sh = f"""#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
bash /home/run_tests.sh
"""

        excludes = (
            "--exclude=*.png --exclude=*.jpg --exclude=*.jpeg --exclude=*.gif "
            "--exclude=*.ico --exclude=*.svg --exclude=*.pdf --exclude=*.zip "
            "--exclude=*.gz --exclude=*.tar --exclude=*.bin"
        )

        test_run = f"""#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
git apply --3way --whitespace=nowarn {excludes} /home/test.patch \\
  || git apply --whitespace=nowarn --reject {excludes} /home/test.patch \\
  || echo "git apply test.patch failed (continuing)"
bash /home/run_tests.sh
"""

        fix_run = f"""#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
git apply --3way --whitespace=nowarn {excludes} /home/test.patch \\
  || git apply --whitespace=nowarn --reject {excludes} /home/test.patch \\
  || echo "git apply test.patch failed (continuing)"
git apply --3way --whitespace=nowarn {excludes} /home/fix.patch \\
  || git apply --whitespace=nowarn --reject {excludes} /home/fix.patch \\
  || echo "git apply fix.patch failed (continuing)"
bash /home/run_tests.sh
"""

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "prepare.sh", prepare),
            File(".", "run_tests.sh", run_tests),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""# syntax=docker/dockerfile:1.6

FROM {name}:{tag}

{copy_commands}
WORKDIR /home/{repo}

ARG BASE_COMMIT="{sha}"

{Image._HARDENING_BLOCK}

RUN bash /home/prepare.sh
"""


class Kubevela(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return KubevelaImageDefault(self.pr, self._config)

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
        ansi = re.compile(r"\x1B\[[0-9;]*[a-zA-Z]")
        clean = ansi.sub("", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # `go test -v` per-test result lines (possibly indented for subtests):
        #   --- PASS: TestRenderOAM (0.01s)
        #   --- FAIL: TestApplyTrait (0.02s)
        #   --- SKIP: TestE2E (0.00s)
        # Fenced by `### KVPKG: <pkg> ###` so ids stay unique across packages.
        res_re = re.compile(r"^\s*--- (PASS|FAIL|SKIP):\s+(\S+)")
        pkg_re = re.compile(r"^### KVPKG:\s+(\S+)\s+###")

        pkg = ""
        for line in clean.splitlines():
            line = line.rstrip()
            pm = pkg_re.match(line.strip())
            if pm:
                pkg = pm.group(1)
                continue
            m = res_re.match(line)
            if not m:
                continue
            status, name = m.group(1), m.group(2)
            tid = f"{pkg}::{name}" if pkg and pkg != "." else name
            if status == "PASS":
                passed_tests.add(tid)
            elif status == "FAIL":
                failed_tests.add(tid)
            elif status == "SKIP":
                skipped_tests.add(tid)

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


# Register Kubevela under every per-record bundle NI.
# Each record in the dataset has its own number_interval derived from
# prs_in_bundle, so we need one registry entry per unique NI string.
for _ni in _BUNDLE_NIS.values():
    Instance._registry[f"kubevela/{_ni}"] = Kubevela
