import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ARDUINO_H_STUB = """\
#pragma once

#ifndef ARDUINO
#define ARDUINO 10813
#endif

#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

typedef uint8_t byte;

// Arduino String is modelled as a thin subclass of std::string so that
// const String& and const std::string& are distinct types – this lets the
// test harness detect whether patches correctly migrate from String to
// std::string (a plain typedef would collapse the two).
class String : public std::string {
public:
    using std::string::string;   // inherit all constructors
    using std::string::operator=;
};

inline long random(long max) {
    if (max <= 0) return 0;
    return std::rand() % max;
}

inline long random(long min, long max) {
    if (max <= min) return min;
    return min + std::rand() % (max - min);
}

class SerialClass {
public:
    void begin(unsigned long baud) {}
    size_t write(uint8_t b) { return 1; }
    size_t write(const uint8_t* buf, size_t len) { return len; }
    size_t write(const char* str) { return str ? std::strlen(str) : 0; }
    void println(const char* s = "") {}
    void print(const char* s) {}
};

inline SerialClass Serial;
"""

EEPROM_H_STUB = """\
#pragma once
// Stub - not used with CDPCFG_WIFI_NONE
"""

ARDUINO_TIMER_H_STUB = """\
#pragma once

template <unsigned max_tasks = 16, unsigned long resolution = 1>
class Timer {
public:
    bool tick() { return false; }
};

inline Timer<> timer_create_default() { return Timer<>(); }
"""

CDP_EXTERNAL_BOARD_H_STUB = """\
#pragma once
#ifndef CDPCFG_WIFI_NONE
#define CDPCFG_WIFI_NONE
#endif
#define CDPCFG_OLED_NONE
#define CDPCFG_RADIO_SX1262
"""

MAIN_WRAPPER_CPP = """\
// Unity test framework requires setUp/tearDown (capital U/D).
// The Arduino test file only defines setup() (lowercase), so we
// provide the stubs Unity's UnityDefaultTestRun expects.
extern "C" {
    void setUp(void) {}
    void tearDown(void) {}
}

extern void setup();

int main() {
    setup();
    return 0;
}
"""

CXX_FLAGS = "-std=c++20 -DUNIT_TEST -DCDPCFG_WIFI_NONE -DCDP_EXTERNAL_BOARD -DARDUINO"
CXX_INCLUDES = "-I /home/stubs -I src/include -I src -I /home/unity/src"

COMPILE_AND_RUN = """\
# Clean previous build artifacts
rm -f /tmp/unity.o /tmp/DuckUtils.o /tmp/test_DuckUtils.o /tmp/main_wrapper.o /tmp/test_runner

# Compile Unity (C)
gcc -c -o /tmp/unity.o /home/unity/src/unity.c -I /home/unity/src

# Compile source (C++)
g++ {cxx_flags} -c -o /tmp/DuckUtils.o src/DuckUtils.cpp {cxx_includes}

# Compile test (C++)
g++ {cxx_flags} -c -o /tmp/test_DuckUtils.o test/test_DuckUtils/test_DuckUtils.cpp {cxx_includes}

# Compile main wrapper
g++ -std=c++20 -c -o /tmp/main_wrapper.o /home/main_wrapper.cpp

# Link
g++ -o /tmp/test_runner /tmp/unity.o /tmp/DuckUtils.o /tmp/test_DuckUtils.o /tmp/main_wrapper.o

# Run tests
/tmp/test_runner""".format(cxx_flags=CXX_FLAGS, cxx_includes=CXX_INCLUDES)


class ClusterDuckProtocolImageBase(Image):
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
        return "gcc:14"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return [
            File(".", "Arduino.h", ARDUINO_H_STUB),
            File(".", "EEPROM.h", EEPROM_H_STUB),
            File(".", "arduino-timer.h", ARDUINO_TIMER_H_STUB),
            File(".", "cdp_external_board.h", CDP_EXTERNAL_BOARD_H_STUB),
            File(".", "main_wrapper.cpp", MAIN_WRAPPER_CPP),
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

RUN git clone --depth 1 https://github.com/ThrowTheSwitch/Unity.git /home/unity

RUN mkdir -p /home/stubs
COPY Arduino.h /home/stubs/Arduino.h
COPY EEPROM.h /home/stubs/EEPROM.h
COPY arduino-timer.h /home/stubs/arduino-timer.h
COPY cdp_external_board.h /home/stubs/cdp_external_board.h
COPY main_wrapper.cpp /home/main_wrapper.cpp

{self.clear_env}

"""


class ClusterDuckProtocolImageDefault(Image):
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
        return ClusterDuckProtocolImageBase(self.pr, self.config)

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

cd /home/{repo}
git reset --hard
git checkout {base_sha}
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}

{compile_and_run}
""".format(repo=self.pr.repo, compile_and_run=COMPILE_AND_RUN),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch

{compile_and_run}
""".format(repo=self.pr.repo, compile_and_run=COMPILE_AND_RUN),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

# fix_patch omits the header — align it with the .cpp change
sed -i 's/const String& str/const std::string\\& str/g' src/include/DuckUtils.h

{compile_and_run}
""".format(repo=self.pr.repo, compile_and_run=COMPILE_AND_RUN),
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()

        return f"""FROM {dep.image_name()}:{dep.image_tag()}

{self.global_env}

COPY fix.patch /home/fix.patch
COPY test.patch /home/test.patch
COPY prepare.sh /home/prepare.sh
COPY run.sh /home/run.sh
COPY test-run.sh /home/test-run.sh
COPY fix-run.sh /home/fix-run.sh

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("ClusterDuck-Protocol", "ClusterDuck-Protocol")
class ClusterDuckProtocol(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return ClusterDuckProtocolImageDefault(self.pr, self._config)

    def run(self) -> str:
        return "bash /home/run.sh"

    def test_patch_run(self) -> str:
        return "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        return "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Unity (ThrowTheSwitch) output format:
        #   <source_file>:<line>:<test_name>:PASS
        #   <source_file>:<line>:<test_name>:FAIL[: <message>]
        #   <source_file>:<line>:<test_name>:IGNORE
        unity_re = re.compile(r"^.+?:\d+:(.+?):(PASS|FAIL|IGNORE)", re.MULTILINE)

        for match in unity_re.finditer(clean_log):
            test_name = match.group(1)
            status = match.group(2)
            if status == "PASS":
                passed_tests.add(test_name)
            elif status == "FAIL":
                failed_tests.add(test_name)
            elif status == "IGNORE":
                skipped_tests.add(test_name)

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
