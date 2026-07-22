from __future__ import annotations

import re
import json
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class StarshipEra2ImageBase(Image):
    """Shared TOOLCHAIN-ONLY base for the 1336..85 era (ubuntu:20.04 + rust 1.47).

    Deliberately contains NO ``git clone``. That is the safety property that makes
    a shared base possible here: DockerfileEnhancer._inject_final_sanitize() only
    injects the history-stripping hardening when the Dockerfile mentions
    git clone/fetch/remote add. With no clone, this image is never pinned to a
    BASE_COMMIT and never has its origin removed, so it can be reused by every PR
    in the era. (Putting the clone here — as PyO3/k3s/processing do — would pin the
    SHARED base to one commit and strand every other PR on a pruned, remote-less
    repo.)

    The per-PR image below therefore does clone + checkout + hardening itself.
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
        return "ubuntu:20.04"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return "base-era2"

    def workdir(self) -> str:
        return "base-era2"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return """
FROM ubuntu:20.04

## Set noninteractive
ENV DEBIAN_FRONTEND=noninteractive

# Install basic requirements and system dependencies for starship
RUN apt-get update && apt-get install -y git curl cmake pkg-config libssl-dev build-essential

# Ensure bash is available
RUN if [ ! -f /bin/bash ]; then         if command -v apk >/dev/null 2>&1; then             apk add --no-cache bash;         elif command -v apt-get >/dev/null 2>&1; then             apt-get update && apt-get install -y bash;         elif command -v yum >/dev/null 2>&1; then             yum install -y bash;         else             exit 1;         fi     fi

# Install Rust 1.47.0 via rustup (last stable version compatible with uom 0.23.x/0.26.x)
# This is the expensive step the shared base exists to amortise.
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain 1.47.0
ENV PATH="/root/.cargo/bin:$PATH"

WORKDIR /home/
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
        return StarshipEra2ImageBase(self.pr, self._config)

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
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard

source /root/.cargo/env && cargo test || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
source /root/.cargo/env && cargo test

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
source /root/.cargo/env && cargo test

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn  /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
source /root/.cargo/env && cargo test

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        base_ref = f"{base.image_name()}:{base.image_tag()}"

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # This image's dependency() is an Image, so DockerfileEnhancer.enhance()
        # returns this Dockerfile VERBATIM (no ARG/infra injection) and
        # build_dataset.py does NOT pass REPO_URL/BASE_COMMIT build-args. So the
        # clone URL and the commit must both be baked in literally, and the
        # hardening block must be embedded by hand with ${BASE_COMMIT}
        # substituted for this PR's actual sha.
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)

        return f"""FROM {base_ref}

WORKDIR /home/
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard
RUN git checkout {self.pr.base.sha}

{copy_commands}
{hardening}
"""


@Instance.register("starship", "starship_1336_to_85")
class STARSHIP_1336_TO_85(Instance):
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
        # Parse the log content and extract test execution results.
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Regex patterns
        passed_pattern = re.compile(r"test (.*) \.\.\. ok")
        failed_pattern = re.compile(r"    (.*)")
        skipped_pattern = re.compile(r"test (.*) \.\.\. ignored")
        in_failures_section = False
        for line in log.splitlines():
            if "failures:" in line:
                in_failures_section = True
                continue
            if in_failures_section:
                if line.strip() == "":
                    in_failures_section = False
                    continue
                match = failed_pattern.match(line)
                if match:
                    failed_tests.add(match.group(1).strip())
                    continue
            if "test result: FAILED" in line:
                in_failures_section = False  # Reset after summary line
                continue
            match = passed_pattern.match(line)
            if match:
                passed_tests.add(match.group(1).strip())
                continue
            match = skipped_pattern.match(line)
            if match:
                skipped_tests.add(match.group(1).strip())
                continue
        # Remove failed tests from passed tests
        passed_tests = passed_tests - failed_tests
        parsed_results = {
            "passed_tests": passed_tests,
            "failed_tests": failed_tests,
            "skipped_tests": skipped_tests,
        }

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# --- LHT bundle routing (ubuntu20.04+rust1.47) ---------------------------------------
# Each dataset record's number_interval is the dash-joined prs_in_bundle
# (derived from prs_in_bundle by the from_json shim in __init__.py).
# Instance.create looks up f"starship/{number_interval}", so every bundle
# in this toolchain era is registered here against STARSHIP_1336_TO_85. (39 bundles)
_STARSHIP_1336_TO_85_INTERVALS = [
    "85-136-137-139-142",
    "96-98-99-101-102-103-105",
    "111-114-115-116-119-120-121",
    "127-133-134",
    "138-306-317-318-321-322-328-333-334-336-338-341-343-347-348-349-355",
    "140-143",
    "144-147-150-151-152-153-155",
    "163-164",
    "165-166-167-169-171-175-176-177-178-180-181-183-184-186-187-189",
    "168-195-197-200",
    "173-234-236-237-239-242-245",
    "191-207-213-214-216-217-219-221-223-224-228",
    "233-240-247-248-249-252-254-256-259-260-262-264-265-266-268-269-271-274-275-277-278-281-282-286-287-288-291",
    "244-642-643-673-683-688-689-693-695",
    "276-294-296-297",
    "285-299-307-309-310-311",
    "314-316-332-335-339-340-356-357-358-359-360-361-363-366-367-369-371-372-374-376-377-379-381-385-388-390-395-396-399",
    "378-383-403-404-409-414-416-426-430-432-433-438-439-440-441-445-446-451-452-453-454-455-466-478",
    "380-405-406-408-410-411-413-419-421-425-427-429",
    "398-507-515-517-571-572-573-574-577-581-586",
    "434-551-556-575-584-589-592-596-599-604-610-612",
    "460-470-485-534-535-538-541",
    "469-483-490-491-492-493-494-495",
    "482-526-529-531-533",
    "514-547-548-552-558-563-566",
    "546-782-819-832-843-844-847-848-850-853-854-855-856-857-858-859-860-861-862-863-864-865-867-871-872-876-878-880-883-884-889-893-897-898",
    "569-583-619-660-663-669-672-676-679-684-685",
    "598-699-791-795-797-799-803-804-805-806-807-809",
    "605-606-628-630-633-635-636-638",
    "644-694-707-708-710",
    "646-696-714-739-756-763-764-765",
    "662-936-948",
    "692-1033-1046-1095-1097-1098-1101-1103-1104-1109-1113-1121-1122-1125-1129-1132-1135-1136-1140-1141-1147-1150-1151",
    "738-908-911-914-915-923-924-925-926-927-929-930-931",
    "750-768-792-812-813-815-817-820-821-825-826-836-837",
    "881-958-992-1170-1183-1189-1216-1218-1227-1231-1234-1237-1238-1239-1242-1243-1249-1253-1255-1256-1257-1263-1264-1267-1269-1270-1282-1284-1289-1290-1291-1292-1293-1294-1301",
    "916-997-1021-1035-1047-1054-1055-1058-1067-1069-1076-1077-1079-1082",
    "1185-1262-1280-1297-1299-1300-1303-1305-1306-1307-1308-1309-1311-1314-1317-1325-1326-1329-1330-1331-1332-1333-1335-1339-1348-1361-1367-1376-1377-1378-1383-1389-1390-1391",
    "1336-1398-1402-1405-1406-1407-1415-1418-1421-1422-1423-1424-1428-1433-1436-1437-1438-1439-1440",
]
for _iv in _STARSHIP_1336_TO_85_INTERVALS:
    Instance.register("starship", _iv)(STARSHIP_1336_TO_85)
