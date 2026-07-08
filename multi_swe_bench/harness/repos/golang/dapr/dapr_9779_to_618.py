import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _filter_binary_patches(patch: str) -> str:
    """Remove binary diff sections from a patch string."""
    if not patch:
        return patch
    filtered_lines = []
    skip = False
    for line in patch.split("\n"):
        if line.startswith("diff --git"):
            skip = False
            filtered_lines.append(line)
        elif skip:
            continue
        elif "GIT binary patch" in line or "Binary files" in line:
            # Remove this entire diff section (back to last diff --git)
            while filtered_lines and not filtered_lines[-1].startswith("diff --git"):
                filtered_lines.pop()
            if filtered_lines:
                filtered_lines.pop()  # Remove the diff --git line too
            skip = True
        else:
            filtered_lines.append(line)
    return "\n".join(filtered_lines)


class DaprImageBase(Image):
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
        return "golang:1.26-bookworm"

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
            code = (
                f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo} && "
                f"cd /home/{self.pr.repo} && "
                f"git fetch origin '+refs/pull/*/head:refs/pull/*/head'"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

{self.global_env}

WORKDIR /home/

ENV DEBIAN_FRONTEND=noninteractive
ENV CGO_ENABLED=0

{code}

{self.clear_env}

"""


class DaprImageDefault(Image):
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
        return DaprImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                f"{_filter_binary_patches(self.pr.fix_patch)}",
            ),
            File(
                ".",
                "test.patch",
                f"{_filter_binary_patches(self.pr.test_patch)}",
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

""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Download dependencies
go mod download -x 2>&1 || true

# Warm build cache
go build -buildvcs=false -mod=mod ./pkg/... ./utils/... ./cmd/... 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}

go test -buildvcs=false -mod=mod -tags=unit,allcomponents -count=1 -short -timeout 300s ./pkg/... ./utils/... ./cmd/... 2>&1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}

# Reset go.mod/go.sum to clean state before applying patches
git checkout -- go.mod go.sum 2>/dev/null || rm -f go.mod go.sum

# Apply test patch
git apply --whitespace=nowarn /home/test.patch 2>&1 || \\
  git apply --whitespace=nowarn --reject /home/test.patch 2>&1 || true

# Tidy modules after patch (may add new deps)
go mod tidy 2>&1 || true

go test -buildvcs=false -mod=mod -tags=unit,allcomponents -count=1 -short -timeout 300s ./pkg/... ./utils/... ./cmd/... 2>&1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}

# Reset go.mod/go.sum to clean state before applying patches
git checkout -- go.mod go.sum 2>/dev/null || rm -f go.mod go.sum

# Apply test patch then fix patch
git apply --whitespace=nowarn /home/test.patch 2>&1 || \\
  git apply --whitespace=nowarn --reject /home/test.patch 2>&1 || true

git apply --whitespace=nowarn /home/fix.patch 2>&1 || \\
  git apply --whitespace=nowarn --reject /home/fix.patch 2>&1 || true

# Tidy modules after patches (may add new deps)
go mod tidy 2>&1 || true

go test -buildvcs=false -mod=mod -tags=unit,allcomponents -count=1 -short -timeout 300s ./pkg/... ./utils/... ./cmd/... 2>&1

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

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard && git checkout ${{BASE_COMMIT}}

{copy_commands}
{prepare_commands}

RUN git for-each-ref --format='%(refname)' refs/pull | xargs -r -n1 git update-ref -d 2>/dev/null || true

{Image._HARDENING_BLOCK}
CMD ["/bin/bash"]
"""


@Instance.register("dapr", "618-619-620-621-622-623-624-628-629-631-634-638-640-642-646-649-650-651-655-660-661-662-664-666-667-668-669-670-671-672-673-674-676-680-681-682-686-690-691-692-694-695-696-698-700-701-707-710-713-716-721-722-723-727-730-735-737-748-752-754-756-757-758-759-761-765-766-768-773-775-776-782-783-786-787-788-789-791-792-795-796-1282-1285-1286-1351-1364-1366-1367-1368-1375-1379-1392-1393-1397-1398-1401-1403-1405-1407-1412-1413-1415-1416-1417-1423-1424-1426-1428-1430-1432-1433-1434-1435-1436-1438-1443-1445-1447-1448-1451-1454-1456-1459-1460-1461-1462-1466-1470-1471-1472-1475-1476-1477-1480-1481-1483-1485-1487-1489-1491-1492-1494-1496-1498-1520-1522-1544-1548-1549-1638-1651-1673-1674-1680-1681-1682-1683-1687-1690-1692-1693-1696-1697-1701-1706-1712-1713-1714-1715-1718-1719-1722-1725-1727-1731-1733-1734-1744-1745-1749-1750-1752-1754-1766-1771-1774-1777-1778-1783-1785-1792-1794-1795-1796-1799-1800-1801-1802-1803-1816-1821-1832-1835-1836-1838-1839-1843-1845-1847-1848-1851-1853-1854-1856-1858-1862-1863-1864-1865-1866-1872-1874-1875-1879-1880-1882-1885-1886-1887-1888-1889-1892-1897-1898-1899-1901-1903-1904-1905-1908-1910-1911-1916-1918-1921-1922-1923-1924-1925-1928-1929-1930-1931-1932-1934-1936-1937-1938-1940-1941-1943-1946-1947-1948-1949-1956-1957-1960-1962-1965-1968-1970-1971-1975-1977-1978-1979-1980-1981-1982-1984-1985-1987-1989-1990-1992-1993-1994-1995-1999-2001-2003-2004-2006-2008-2010-2012-2013-2015-2018-2020-2021-2022-2023-2024-2027-2028-2029-2031-2032-2035-2036-2038-2039-2040-2041-2043-2044-2046-2048-2051-2053-2054-2055-2057-2058-2060-2064-2065-2066-2071-2072-2074-2078-2085-2090-2091-2092-2096-2097-2098-2099-2100-2104-2106-2110-2112-2115-2116-2118-2119-2120-2122-2123-2125-2130-2131-2132-2134-2141-2143-2150-2151-2152-2154-2161-2162-2164-2307-2318-2834-2873-2874-2877-2879-2883-2904-2927-2991-2994-3001-3008-3010-3017-3019-3020-3034-3037-3038-3041-3043-3044-3050-3052-3053-3061-3064-3072-3073-3075-3080-3083-3087-3088-3089-3093-3094-3097-3099-3102-3105-3106-3108-3109-3110-3111-3112-3118-3121-3125-3129-3139-3140-3141-3143-3145-3150-3152-3153-3161-3171-3174-3180-3182-3183-3184-3185-3188-3191-3202-3203-3204-3210-3212-3214-3222-3682-3703-3732-3739-3746-3887-3889-3893-3919-3923-3924-3958-3959-3963-3964-3966-3967-3971-3974-3986-3989-3990-3998-3999-4000-4013-4019-4155-4161-4193-4215-4216-4316-4430-4431-4435-4436-4452-4519-4520-4521-4522-4525-4526-4532-4538-4539-4545-4547-4642-4643-4645-4646-4686-5352-5353-5354-5417-5419-5534-5539-5582-5584-5588-5941-5952-5961-5992-5993-5994-6016-6039-6041-6056-6057-6080-6086-6090-6112-6130-6153-6159-6162-6204-6220-6284-6295-6315-6329-6335-6431-6452-6492-6501-6504-6513-6525-6529-6545-6547-6552-6553-6554-6555-6556-6559-6561-6562-6563-6575-6576-6577-6598-6661-6687-6697-6934-7002-7018-7057-7068-7091-7117-7118-7144-7165-7192-7204-7208-7210-7213-7242-7245-7305-7310-7327-7333-7341-7349-7387-7389-7398-7516-7519-7525-7526-7532-7660-7661-7662-7668-7670-7726-7748-7847-7864-7870-7914-7996-7998-7999-8019-8021-8022-8030-8038-8042-8050-8058-8059-8062-8083-8090-8091-8092-8093-8094-8095-8096-8097-8098-8099-8105-8106-8107-8108-8110-8126-8136-8137-8149-8155-8221-8307-8325-8351-8367-8370-8392-8394-8396-8397-8409-8423-8430-8488-8503-8505-8539-8541-8542-8543-8546-8547-8549-8555-8588-8619-8620-8625-8630-8633-8636-8640-8643-8645-8687-8690-8704-8717-8756-8766-8769-8771-8776-8777-8778-8781-8788-8791-8799-8802-8806-8807-8809-8811-8812-8825-8857-8858-8864-8881-8899-8906-8946-8983-8989-8990-9019-9023-9066-9068-9071-9072-9073-9078-9093-9094-9098-9099-9100-9102-9103-9104-9145-9152-9198-9204-9206-9209-9210-9220-9222-9229-9232-9251-9269-9271-9275-9279-9296-9564-9585-9586-9587-9594-9595-9600-9601-9602-9621-9622-9623-9624-9633-9634-9643-9645-9647-9649-9650-9652-9658-9659-9662-9673-9678-9681-9682-9684-9686-9687-9690-9695-9697-9698-9702-9703-9707-9708-9713-9715-9716-9718-9740-9741-9761-9764-9773-9774-9777-9779-9795")
class Dapr(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DaprImageDefault(self.pr, self._config)

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
