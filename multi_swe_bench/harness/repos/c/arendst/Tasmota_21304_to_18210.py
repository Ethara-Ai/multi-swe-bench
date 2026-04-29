from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class Tasmota_21304_to_18210_ImageBase(Image):
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
        return "gcc:latest"

    def image_tag(self) -> str:
        return "base-21304-to-18210"

    def workdir(self) -> str:
        return "base-21304-to-18210"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/Tasmota"
        else:
            code = "COPY Tasmota /home/Tasmota"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends libreadline-dev && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class Tasmota_21304_to_18210_ImageDefault(Image):
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
        return Tasmota_21304_to_18210_ImageBase(self.pr, self.config)

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
                "berry_standalone_fix.sh",
                r"""#!/bin/bash
BERRYDIR="$1"
if [ -z "$BERRYDIR" ] || [ ! -f "$BERRYDIR/Makefile" ]; then exit 0; fi
cd "$BERRYDIR"

for cfile in tools/coc/hash_map.cpp tools/coc/str_build.h tools/coc/coc_string.h tools/coc/main.cpp; do
  [ -f "$cfile" ] && grep -q '<cstdint>' "$cfile" || sed -i '1i #include <cstdint>' "$cfile" 2>/dev/null
done

if [ -f default/berry_conf.h ]; then
  sed -i 's|#ifdef COMPILE_BERRY_LIB|#if 0|' default/berry_conf.h
  sed -i 's|#define BE_USE_FILE_SYSTEM.*0|#define BE_USE_FILE_SYSTEM              1|' default/berry_conf.h
  sed -i 's|#define BE_USE_TIME_MODULE.*0|#define BE_USE_TIME_MODULE              1|' default/berry_conf.h
  sed -i 's|#define BE_USE_OS_MODULE.*0|#define BE_USE_OS_MODULE                1|' default/berry_conf.h
  sed -i 's|#define BE_USE_SYS_MODULE.*0|#define BE_USE_SYS_MODULE               1|' default/berry_conf.h
fi

rm -f default/be_port.cpp default/static_block.hpp 2>/dev/null
cat > default/be_port.c << 'BEPORT'
#include "berry.h"
#include "be_mem.h"
#include "be_sys.h"
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <unistd.h>
#include <sys/stat.h>
#include <dirent.h>
BERRY_API void* be_fopen(const char *fn, const char *m) { return fopen(fn, m); }
BERRY_API int be_fclose(void *h) { return fclose((FILE*)h); }
BERRY_API size_t be_fwrite(void *h, const void *b, size_t l) { return fwrite(b, 1, l, (FILE*)h); }
BERRY_API size_t be_fread(void *h, void *b, size_t l) { return fread(b, 1, l, (FILE*)h); }
BERRY_API char* be_fgets(void *h, void *b, int s) { return fgets(b, s, (FILE*)h); }
BERRY_API int be_fseek(void *h, long o) { return fseek((FILE*)h, o, SEEK_SET); }
BERRY_API long int be_ftell(void *h) { return ftell((FILE*)h); }
BERRY_API long int be_fflush(void *h) { return fflush((FILE*)h); }
BERRY_API size_t be_fsize(void *h) {
    long p = ftell((FILE*)h); fseek((FILE*)h, 0, SEEK_END);
    long s = ftell((FILE*)h); fseek((FILE*)h, p, SEEK_SET); return (size_t)s;
}
BERRY_API void be_writebuffer(const char *b, size_t l) { fwrite(b, 1, l, stdout); fflush(stdout); }
BERRY_API char* be_readstring(char *b, size_t s) { return fgets(b, s, stdin); }
int be_isdir(const char *p) { struct stat s; return (stat(p, &s)==0 && S_ISDIR(s.st_mode)) ? 1 : 0; }
int be_isfile(const char *p) { struct stat s; return (stat(p, &s)==0 && S_ISREG(s.st_mode)) ? 1 : 0; }
int be_isexist(const char *p) { struct stat s; return stat(p, &s)==0 ? 1 : 0; }
char* be_getcwd(char *b, size_t s) { return getcwd(b, s); }
int be_chdir(const char *p) { return chdir(p); }
int be_mkdir(const char *p) { return mkdir(p, 0755); }
int be_unlink(const char *f) { return remove(f); }
int be_dirfirst(bdirinfo *i, const char *p) {
    i->dir = opendir(p); if (!i->dir) return 1; return be_dirnext(i);
}
int be_dirnext(bdirinfo *i) {
    struct dirent *e; DIR *d = (DIR*)i->dir;
    if (!d) return 1; e = readdir(d);
    if (e) { i->name = e->d_name; return 0; } return 1;
}
int be_dirclose(bdirinfo *i) { if (i->dir) closedir((DIR*)i->dir); i->dir = NULL; return 0; }
BEPORT

# Detect API version: newer Berry uses bntvmodule_t, older uses bntvmodule
if grep -q 'bntvmodule_t' src/berry.h 2>/dev/null; then
  # Newer Berry API (PR #21304 era) - needs bntvmodule_t + bclass_array + be_load_custom_libs
  cat > default/be_modtab.c << 'BEMOD'
#include "berry.h"
be_extern_native_module(string);
be_extern_native_module(json);
be_extern_native_module(math);
be_extern_native_module(time);
be_extern_native_module(os);
be_extern_native_module(global);
be_extern_native_module(sys);
be_extern_native_module(debug);
be_extern_native_module(gc);
be_extern_native_module(solidify);
be_extern_native_module(introspect);
be_extern_native_module(strict);
be_extern_native_module(undefined);
BERRY_LOCAL const bntvmodule_t* const be_module_table[] = {
    &be_native_module(string), &be_native_module(json), &be_native_module(math),
    &be_native_module(time), &be_native_module(os), &be_native_module(global),
    &be_native_module(sys), &be_native_module(debug), &be_native_module(gc),
    &be_native_module(solidify), &be_native_module(introspect),
    &be_native_module(strict), &be_native_module(undefined), NULL
};
BERRY_LOCAL bclass_array be_class_table = { NULL };
BERRY_API void be_load_custom_libs(bvm *vm) { (void)vm; }
BEMOD
else
  # Older Berry API (PR #12363 era) - uses bntvmodule (no _t)
  cat > default/be_modtab.c << 'BEMOD'
#include "berry.h"
be_extern_native_module(string);
be_extern_native_module(json);
be_extern_native_module(math);
be_extern_native_module(time);
be_extern_native_module(os);
be_extern_native_module(global);
be_extern_native_module(sys);
be_extern_native_module(debug);
be_extern_native_module(gc);
be_extern_native_module(solidify);
BERRY_LOCAL const bntvmodule* const be_module_table[] = {
    &be_native_module(string), &be_native_module(json), &be_native_module(math),
    &be_native_module(time), &be_native_module(os), &be_native_module(global),
    &be_native_module(sys), &be_native_module(debug), &be_native_module(gc),
    &be_native_module(solidify), NULL
};
BEMOD
fi

cat > default/berry_main.c << 'BEMAIN'
#include "berry.h"
#include <stdio.h>
#include <string.h>
int main(int argc, char *argv[]) {
    bvm *vm = be_vm_new(); int ret = 0;
    if (argc >= 2) {
        if (strcmp(argv[1], "-e") == 0 && argc >= 3) {
            if (be_loadstring(vm, argv[2]) == 0) ret = be_pcall(vm, 0); else ret = 1;
        } else {
            if (be_loadfile(vm, argv[1]) == 0) ret = be_pcall(vm, 0);
            else { fprintf(stderr, "error: %s\n", be_tostring(vm, -1)); ret = 1; }
        }
        if (ret != 0 && be_top(vm) > 0) fprintf(stderr, "error: %s\n", be_tostring(vm, -1));
    }
    be_vm_delete(vm); return ret;
}
BEMAIN

for rmf in be_tasmotalib.c be_driverlib.c be_energylib.c be_flash_lib.c be_gpio_lib.c be_i2c_driverlib.c be_light_lib.c be_lv_lvgl_module.c be_lvgl_color_lib.c be_lvgl_font_lib.c be_lvgl_widgets_lib.c be_md5_lib.c be_webserver_lib.c be_wirelib.c berry.c; do
  rm -f "default/$rmf" 2>/dev/null
done
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/Tasmota
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# --- IRremoteESP8266 setup ---
TESTDIR=$(find /home/Tasmota/lib -type f -name Makefile -path '*/IRremoteESP8266*/test/Makefile' -not -path '*/googletest/*' | head -1 | xargs dirname 2>/dev/null || true)
if [ -n "$TESTDIR" ]; then
  echo "Found IRremoteESP8266 test dir: $TESTDIR"
  cd "$TESTDIR"
  make install-googletest
  sed -i 's/ -c \\\\$/ -Wno-maybe-uninitialized -c \\\\/' "$TESTDIR/Makefile"
fi

# --- Berry interpreter setup ---
BERRYDIR=""
for candidate in /home/Tasmota/lib/libesp32/berry /home/Tasmota/lib/libesp32/Berry; do
  if [ -f "$candidate/Makefile" ] && [ -d "$candidate/tests" ]; then
    BERRYDIR="$candidate"
    break
  fi
done

if [ -n "$BERRYDIR" ]; then
  echo "Found Berry dir: $BERRYDIR"
  cd "$BERRYDIR"
  bash /home/berry_standalone_fix.sh "$BERRYDIR"
  make CC=gcc clean || true
  make CC=gcc -j$(nproc) 2>&1 || true
  if [ -x "$BERRYDIR/berry" ]; then
    echo "Berry interpreter built successfully"
  else
    echo "WARNING: Berry interpreter build failed"
  fi
fi

if [ -z "$TESTDIR" ] && [ -z "$BERRYDIR" ]; then
  echo "WARNING: No test framework found (neither IRremoteESP8266 nor Berry)"
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -o pipefail

cd /home/Tasmota
FOUND=0

TESTDIR=$(find /home/Tasmota/lib -type f -name Makefile -path '*/IRremoteESP8266*/test/Makefile' -not -path '*/googletest/*' | head -1 | xargs dirname 2>/dev/null || true)
if [ -n "$TESTDIR" ]; then
  FOUND=1
  echo "Running IR tests in: $TESTDIR"
  cd "$TESTDIR"
  make run || true
  cd /home/Tasmota
fi

BERRYDIR=""
for candidate in /home/Tasmota/lib/libesp32/berry /home/Tasmota/lib/libesp32/Berry; do
  if [ -x "$candidate/berry" ] && [ -d "$candidate/tests" ]; then
    BERRYDIR="$candidate"
    break
  fi
done

if [ -n "$BERRYDIR" ]; then
  FOUND=1
  echo "Running Berry tests in: $BERRYDIR"
  cd "$BERRYDIR"
  for testfile in tests/*.be; do
    testname=$(basename "$testfile" .be)
    echo "BERRY_TEST_START: $testname"
    if ./berry "$testfile" 2>&1; then
      echo "BERRY_TEST_PASS: $testname"
    else
      echo "BERRY_TEST_FAIL: $testname"
    fi
  done
fi

if [ "$FOUND" -eq 0 ]; then
  echo "ERROR: No test framework found"
  exit 1
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -o pipefail

cd /home/Tasmota
git apply --whitespace=nowarn /home/test.patch
FOUND=0

TESTDIR=$(find /home/Tasmota/lib -type f -name Makefile -path '*/IRremoteESP8266*/test/Makefile' -not -path '*/googletest/*' | head -1 | xargs dirname 2>/dev/null || true)
if [ -n "$TESTDIR" ]; then
  FOUND=1
  echo "Running IR tests in: $TESTDIR"
  cd "$TESTDIR"
  make run || true
  cd /home/Tasmota
fi

BERRYDIR=""
for candidate in /home/Tasmota/lib/libesp32/berry /home/Tasmota/lib/libesp32/Berry; do
  if [ -f "$candidate/Makefile" ] && [ -d "$candidate/tests" ]; then
    BERRYDIR="$candidate"
    break
  fi
done

if [ -n "$BERRYDIR" ]; then
  FOUND=1
  echo "Running Berry tests in: $BERRYDIR"
  cd "$BERRYDIR"
  bash /home/berry_standalone_fix.sh "$BERRYDIR"
  make CC=gcc clean || true
  make CC=gcc -j$(nproc) 2>&1 || true
  for testfile in tests/*.be; do
    testname=$(basename "$testfile" .be)
    echo "BERRY_TEST_START: $testname"
    if ./berry "$testfile" 2>&1; then
      echo "BERRY_TEST_PASS: $testname"
    else
      echo "BERRY_TEST_FAIL: $testname"
    fi
  done
fi

if [ "$FOUND" -eq 0 ]; then
  echo "ERROR: No test framework found"
  exit 1
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -o pipefail

cd /home/Tasmota
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
FOUND=0

TESTDIR=$(find /home/Tasmota/lib -type f -name Makefile -path '*/IRremoteESP8266*/test/Makefile' -not -path '*/googletest/*' | head -1 | xargs dirname 2>/dev/null || true)
if [ -n "$TESTDIR" ]; then
  FOUND=1
  echo "Running IR tests in: $TESTDIR"
  cd "$TESTDIR"
  make run || true
  cd /home/Tasmota
fi

BERRYDIR=""
for candidate in /home/Tasmota/lib/libesp32/berry /home/Tasmota/lib/libesp32/Berry; do
  if [ -f "$candidate/Makefile" ] && [ -d "$candidate/tests" ]; then
    BERRYDIR="$candidate"
    break
  fi
done

if [ -n "$BERRYDIR" ]; then
  FOUND=1
  echo "Running Berry tests in: $BERRYDIR"
  cd "$BERRYDIR"
  bash /home/berry_standalone_fix.sh "$BERRYDIR"
  make CC=gcc clean || true
  make CC=gcc -j$(nproc) 2>&1 || true
  for testfile in tests/*.be; do
    testname=$(basename "$testfile" .be)
    echo "BERRY_TEST_START: $testname"
    if ./berry "$testfile" 2>&1; then
      echo "BERRY_TEST_PASS: $testname"
    else
      echo "BERRY_TEST_FAIL: $testname"
    fi
  done
fi

if [ "$FOUND" -eq 0 ]; then
  echo "ERROR: No test framework found"
  exit 1
fi

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

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("arendst", "Tasmota_21304_to_18210")
class Tasmota_21304_to_18210(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Tasmota_21304_to_18210_ImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # --- gtest patterns (IRremoteESP8266) ---
        re_passes = [
            re.compile(r"^\[       OK \] (\S+?)( \(.+\))?$"),
            re.compile(r"^\x1b\[0;32m\[       OK \] \x1b\[0m(\S+?)( \(.+\))?$"),
        ]
        re_fails = [
            re.compile(r"^\[  FAILED  \] (\S+?)( \(.+\))?$"),
            re.compile(r"^\x1b\[0;31m\[  FAILED  \] \x1b\[0m(\S+?)( \(.+\))?$"),
        ]

        # --- Berry test patterns ---
        re_berry_pass = re.compile(r"^BERRY_TEST_PASS:\s+(\S+)$")
        re_berry_fail = re.compile(r"^BERRY_TEST_FAIL:\s+(\S+)$")

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            for re_pass in re_passes:
                pass_match = re_pass.match(line)
                if pass_match:
                    test = pass_match.group(1)
                    passed_tests.add(test)

            for re_fail in re_fails:
                fail_match = re_fail.match(line)
                if fail_match:
                    test = fail_match.group(1)
                    failed_tests.add(test)

            berry_pass = re_berry_pass.match(line)
            if berry_pass:
                passed_tests.add(f"berry.{berry_pass.group(1)}")

            berry_fail = re_berry_fail.match(line)
            if berry_fail:
                failed_tests.add(f"berry.{berry_fail.group(1)}")

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
