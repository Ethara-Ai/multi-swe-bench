"""validatorjs/validator.js config for the modern era: PR number 931..99999
(node:20-bookworm + npm + mocha with @babel/register).

Conformant with image.py: dependency() returns a base-image *string* and the
self-contained dockerfile() below mirrors the canonical single-level template
(see CrossplaneImageDefault) -- clone "${REPO_URL}" -> checkout "${BASE_COMMIT}"
-> prepare.sh -> verbatim Image._HARDENING_BLOCK -> CMD. Because dependency()
stays a string, DockerfileEnhancer still engages and prepends the # syntax +
ARG REPO_URL/BASE_COMMIT + ENV/label infra (its repo-fetch standardiser skips
"${REPO_URL}" clones, so the embedded clone/checkout + hardening survive). Each
PR builds its own image -- no shared base layer to pin+strip across commits.
"""

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.javascript.validatorjs.validator_js import (
    ValidatorJsImageBase,
)


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
        # Level 2: per-PR image FROM the shared ValidatorJsImageBase toolchain.
        # dependency() is an *Image* (not a string), so the DockerfileEnhancer
        # returns dockerfile() verbatim -- the clone/checkout + verbatim
        # Image._HARDENING_BLOCK below are kept exactly as written (and pinning
        # BASE_COMMIT here is correct: it is per-PR, not the shared base).
        return ValidatorJsImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def dockerfile(self) -> str:
        # Two-level per-PR Dockerfile (mirrors FastfetchImageDefault). The shared
        # toolchain base does NOT clone, so this image clones full history then
        # checks out ${BASE_COMMIT} inline. Because dependency() is an Image, the
        # DockerfileEnhancer returns this Dockerfile verbatim -- the clone +
        # hardening below are kept as written. Image._HARDENING_BLOCK is
        # concatenated raw (not via the f-string) so its ${BASE_COMMIT} /
        # %(refname) tokens stay literal. prepare.sh installs node_modules +
        # builds (network is available at build time, before the hardening
        # strip); node_modules is untracked so the history rewrite leaves it in
        # place for the offline eval runs.
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/{file.name}\n"

        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

"""

        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail

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
# Repo is already cloned + checked out at ${{BASE_COMMIT}} and hardened by
# Image.dockerfile(), so this script no longer performs any git checkout. It
# installs dependencies and builds so the eval runs don't need network.
set -e

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}
git reset --hard || true

npm install --legacy-peer-deps >/dev/null 2>&1 || true

# devDependencies introduced by the fix patch must be available to the test-only
# stage too (the test patch may import a devDep whose package.json entry lives in
# fix.patch). Apply just package.json from the fix, install, then revert the
# source -- node_modules is untracked so it survives the hardening pass.
if [ -f /home/fix.patch ]; then
    git apply --include=package.json --whitespace=nowarn --ignore-whitespace /home/fix.patch 2>/dev/null || true
    npm install --legacy-peer-deps >/dev/null 2>&1 || true
    git checkout -- package.json 2>/dev/null || true
fi

npm run build > /home/build.log 2>&1 || {{ cat /home/build.log; exit 1; }}
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}
./node_modules/.bin/mocha --require @babel/register --reporter spec --recursive

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}
git apply --whitespace=nowarn --ignore-whitespace --exclude=package-lock.json --exclude=yarn.lock --exclude=index.js --exclude=validator.js --exclude=validator.min.js --exclude='lib/*' --exclude='es/*' /home/test.patch
npm install --legacy-peer-deps >/dev/null 2>&1 || true
npm run build > /home/build.log 2>&1 || {{ cat /home/build.log; exit 1; }}
./node_modules/.bin/mocha --require @babel/register --reporter spec --recursive

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}
git apply --whitespace=nowarn --ignore-whitespace --exclude=package-lock.json --exclude=yarn.lock --exclude=index.js --exclude=validator.js --exclude=validator.min.js --exclude='lib/*' --exclude='es/*' /home/test.patch /home/fix.patch
npm install --legacy-peer-deps >/dev/null 2>&1 || true
npm run build > /home/build.log 2>&1 || {{ cat /home/build.log; exit 1; }}
./node_modules/.bin/mocha --require @babel/register --reporter spec --recursive

""".format(pr=self.pr),
            ),
        ]


@Instance.register("validatorjs", "validator_js_931_to_99999")
class ValidatorJs931To99999(Instance):
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

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")

        lines = test_log.splitlines()
        current_path = []
        indentation_to_level = {}

        for line in lines:
            line = ansi_escape.sub("", line)

            match = re.match(
                r"^(\s*)(?:([✓✔]|[0-9]+\))\s+)?(.*?)(?:\s+\([0-9]+ms\))?$", line
            )

            if not match or not match.group(3).strip():
                continue

            spaces, status, name = match.groups()
            name = name.strip()
            indent = len(spaces)

            if indent not in indentation_to_level:
                if not indentation_to_level:
                    indentation_to_level[indent] = 0
                else:
                    prev_indents = sorted(
                        [i for i in indentation_to_level.keys() if i < indent]
                    )
                    if prev_indents:
                        closest_indent = prev_indents[-1]
                        indentation_to_level[indent] = (
                            indentation_to_level[closest_indent] + 1
                        )
                    else:
                        indentation_to_level[indent] = 0

            level = indentation_to_level[indent]
            current_path = current_path[:level]
            current_path.append(name)

            if status:
                full_path = ":".join(current_path)
                if status in ("✓", "✔"):
                    passed_tests.add(full_path)
                elif status.endswith(")"):
                    failed_tests.add(full_path)

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# Route bundled PRs that carry a dash-joined number_interval (the list of
# prs_in_bundle) to this 931..99999-era config. Instance.create() looks up
# f"{org}/{number_interval}", so the interval string must be registered.
_NUMBER_INTERVALS = [
    "562-1732-1971-2028-2214-2291-2294-2323-2325-2328-2332-2333-2339-2350-2359-2362-2392-2394-2395-2399-2404-2406-2408-2409-2411-2413-2415-2418-2419-2423-2427-2437-2439-2440-2442-2474-2481-2482-2492-2493-2500-2518-2534-2536",
    "813-1507-1565-1570-1584-1639-1646-1653-1655-1656-1657-1658-1670-1672-1680-1681-1682-1686-1697-1699-1706-1708-1709-1716-1718-1720-1721-1724-1730-1731-1738-1745-1746-1747-1761-1770-1772-1777-1778-1786-1788-1790-1799-1806-1807-1825-1827-1836-1837-1838-1845-1846-1848-1851",
    "917-931-932-933-946-950",
    "954-1047-1072-1081-1117-1141-1159-1163-1211-1238-1243-1244-1246-1250-1251-1260-1265-1267-1268",
    "975-991-1024-1035-1040-1041-1048-1049-1054-1056-1057",
    "1066-1479-1665-1678-1761-1814-1859-1860-1861-1865-1867-1868-1887-1888-1892-1896-1897-1909-1910-1916-1920-1922-1924-1925-1939-1940-1942-1944-1945-1946-1951-1952-1956-1957-1962-1964-1965-1967-1974-1975-1983-1985-1986-1989-1992-1993-1995-1996-1997-1998-2001-2002-2004-2007-2008-2010-2011-2014-2024-2045-2055-2075-2084-2085-2091-2107-2109-2111-2113-2115-2118-2119-2121-2129-2132-2133-2135-2137-2138-2142-2148-2149-2157-2163-2164-2165-2167-2168-2169-2173",
    "1114-1200-1207-1213-1217-1226-1233-1234",
    "1301-1356-1357-1367-1370-1371-1373-1376-1383-1384-1388-1391-1394-1397-1408-1411-1418-1420-1425-1428-1439-1440",
    "2025-2110-2117-2144-2175-2176-2178-2188-2189-2196-2202-2203-2209-2217-2218-2222-2226-2229-2231-2235-2246",
    "2123-2585-2616-2620-2627",
    "2183-2383-2591-2592-2639-2643-2645-2658-2660-2663-2676-2682-2695",
    "2535-2633-2634-2640",
    "2556-2563-2573-2576-2581-2582-2603",
]
for _interval in _NUMBER_INTERVALS:
    Instance.register("validatorjs", _interval)(ValidatorJs931To99999)
