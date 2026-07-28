import re
import json
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


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

    def dependency(self) -> str:
        return "rust:1.60"

    def image_prefix(self) -> str:
        return "mswebench"

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

apt-get update && apt-get install -y protobuf-compiler libssl-dev

cd /home/anki
git reset --hard
git checkout {pr.base.sha}
git submodule update --init --recursive

export PROTOC=/usr/local/bin/protoc
# anki's .cargo/config sets STRINGS_JSON=out/rslib/i18n/strings.json; the i18n
# build.rs does fs::write(that path) and panics if the dir is absent. The normal
# ninja build creates it -- `cargo test` alone does not, so make it here.
mkdir -p out/rslib/i18n
cargo test -p anki --locked || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/anki
export PROTOC=/usr/local/bin/protoc
# anki's .cargo/config sets STRINGS_JSON=out/rslib/i18n/strings.json; the i18n
# build.rs does fs::write(that path) and panics if the dir is absent. The normal
# ninja build creates it -- `cargo test` alone does not, so make it here.
mkdir -p out/rslib/i18n
cargo test -p anki --locked

""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/anki
if ! git -C /home/anki apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
export PROTOC=/usr/local/bin/protoc
# anki's .cargo/config sets STRINGS_JSON=out/rslib/i18n/strings.json; the i18n
# build.rs does fs::write(that path) and panics if the dir is absent. The normal
# ninja build creates it -- `cargo test` alone does not, so make it here.
mkdir -p out/rslib/i18n
cargo test -p anki --locked

""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/anki
if ! git -C /home/anki apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
export PROTOC=/usr/local/bin/protoc
# anki's .cargo/config sets STRINGS_JSON=out/rslib/i18n/strings.json; the i18n
# build.rs does fs::write(that path) and panics if the dir is absent. The normal
# ninja build creates it -- `cargo test` alone does not, so make it here.
mkdir -p out/rslib/i18n
cargo test -p anki --locked

""",
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
FROM rust:1.60

## Set noninteractive
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git protobuf-compiler libssl-dev
# apt protobuf-compiler (3.12) is too old for anki's proto3 `optional` fields
# (rslib/build/protobuf.rs panics). Install a modern protoc (25.x), arch-aware
# so multiarch amd64+arm64 both work; the run scripts point PROTOC at it.
RUN set -e; PV=25.1; case "$(uname -m)" in x86_64) PA=x86_64;; aarch64) PA=aarch_64;; *) PA=x86_64;; esac; \
    apt-get install -y unzip curl >/dev/null 2>&1 || true; \
    curl -fsSL -o /tmp/protoc.zip https://github.com/protocolbuffers/protobuf/releases/download/v${{PV}}/protoc-${{PV}}-linux-${{PA}}.zip; \
    unzip -oq /tmp/protoc.zip -d /usr/local; chmod +x /usr/local/bin/protoc; rm -f /tmp/protoc.zip

RUN if [ ! -f /bin/bash ]; then \
        if command -v apk >/dev/null 2>&1; then \
            apk add --no-cache bash; \
        elif command -v apt-get >/dev/null 2>&1; then \
            apt-get update && apt-get install -y bash; \
        elif command -v yum >/dev/null 2>&1; then \
            yum install -y bash; \
        else \
            exit 1; \
        fi \
    fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/ankitects/anki.git /home/anki

WORKDIR /home/anki
RUN git reset --hard
RUN git checkout {pr.base.sha}
RUN git submodule update --init --recursive
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("ankitects", "anki_2485_to_846")
class ANKI_2485_TO_846(Instance):
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
        # Cargo test output format (rust:1.60 era):
        #   test decks::name::test::parent ... ok
        #   test some::test ... FAILED
        #   test some::test ... ignored
        #   test result: ok. 116 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        for line in test_log.splitlines():
            if line.startswith("test "):
                match = re.search(r"test (.*) \.\.\. ok", line)
                if match:
                    passed_tests.add(match.group(1).strip())
                match = re.search(r"test (.*) \.\.\. ignored", line)
                if match:
                    skipped_tests.add(match.group(1).strip())
                match = re.search(r"test (.*) \.\.\. FAILED", line)
                if match:
                    failed_tests.add(match.group(1).strip())
        if "failures:" in test_log:
            match = re.search(r"failures:\n([\s\S]*?)\n\n", test_log)
            if match:
                for test in match.group(1).splitlines():
                    if test.strip() and not test.strip().startswith("----"):
                        failed_tests.add(test.strip())

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# PIPELINE.md §11b: every dash-joined bundle value must be a registered key
# in addition to the era key, else Instance.create() raises "not registered".
_BUNDLE_NIS_ANKI_2485_TO_846 = [
    "846-847-848-849-850-851-853-854-855-857-859",
    "1325-1330-1331-1333-1335-1337-1340-1343-1344-1345",
    "1743-1779-1802-1813-1815-1816-1817-1818-1819-1820-1821-1830-1833-1836-1837-1838-1840-1842-1843-1844-1845-1846-1847-1848-1851-1852-1853-1854-1855",
    "1850-1889-1890-1898-1905-1906-1908-1912-1913-1914-1915-1918-1919-1920-1922-1923-1925-1928-1929-1930",
    "1931-1932-1935-1937-1943-1946-1949-1950-1953-1955-1956-1957-1960-1967-1968-1970-1971-1972-1973-1975-1976-1977-1978-1982-1987-1990-1992-1995-1998-1999-2000-2001-2002-2003-2005-2008-2009-2010-2011-2013-2014-2016-2017-2018-2019-2022-2023-2028-2029-2031-2032-2033-2034-2036-2038-2040-2041-2042-2044-2045-2046-2049-2050-2051-2052-2058-2059-2064-2066-2067-2068-2071-2073-2074-2078-2079-2082-2083-2085-2091-2095-2096-2099-2101-2102-2103-2104-2107-2114-2115-2116-2117-2118-2119-2120-2122-2125-2126-2129-2130-2132-2135-2136-2137-2138-2139-2143-2144-2145-2146-2147-2148-2149-2154-2155-2156-2157-2158-2159-2160-2161-2162-2163-2164-2165-2166-2167-2169-2170-2171-2172-2175-2176-2177-2180-2181-2182-2183-2184-2185-2187-2191-2193-2197-2198-2199-2202-2205-2206-2207-2208-2209-2210-2211-2212-2213-2214-2215-2216-2217-2218-2220-2223-2224-2225-2226-2227-2229-2230-2231-2232-2233-2237-2239-2240-2241-2242-2243-2244-2246-2247-2252-2257-2265-2266-2267-2268",
    "2141-2151-2255-2272-2274-2280-2281-2286-2288-2290-2294-2303",
    "2262-2289-2301-2306-2307-2308-2310-2314-2318-2322-2329-2330-2331-2332-2334-2336-2337-2338-2345-2346-2348-2350",
    "2340-2343-2351-2354-2360-2361-2364-2366-2370-2371-2372",
    "2356-2493-2497-2501-2502-2506-2508-2509-2510",
    "2367-2383-2392-2393-2394-2395-2404-2405-2406-2412-2413-2414-2415-2417-2419-2420-2421-2422-2423-2426-2427-2428-2432-2433-2435-2436-2437-2441-2442-2445-2446-2447-2448-2449-2452-2455-2456-2457-2458-2460-2461",
    "2464-2467-2471-2472-2478-2479-2480-2481-2483-2484",
    "2485-2531-2532-2533-2536-2540-2542-2547-2549-2550-2551-2552-2558-2561-2562-2565-2567-2568-2569-2571-2572-2574-2575-2578-2580-2582-2583-2585-2590-2593-2594-2600-2602-2603-2611",
]
for _ni in _BUNDLE_NIS_ANKI_2485_TO_846:
    Instance.register("ankitects", _ni)(ANKI_2485_TO_846)
