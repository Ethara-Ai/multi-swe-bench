import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Mapping from PR number to the space-separated list of all PRs in its bundle.
# The fix_patch is a combined diff of ALL bundle PRs, so dependencies must be
# built at the date of the LAST bundle PR's merge commit (not the first).
# Generated from: dataset/awslabs__aws-c-http_lht_final.jsonl prs_in_bundle field.
BUNDLE_PRS = {
    23: "23 24 25 26 27 28",
    29: "29 30 31",
    33: "33 35 36 37",
    41: "41 44 45 46",
    42: "42 43",
    48: "48 49 51 52 53",
    50: "50 54",
    60: "60 64 65 67",
    62: "62 63 68 69 70 72 73 75",
    77: "77 78 79 80 81 82 83 84 85 86 87 88 89 90",
    93: "93 94 97 99",
    95: "95 107 110 118 120 121",
    108: "108 109 111 112 113 114 115 116 119 122 123 124 125",
    126: "126 127",
    128: "128 129 130 131",
    132: "132 133 134 135 136",
    137: "137 138 143",
    145: "145 146",
    149: "149 150 151 153",
    154: "154 174 175 178",
    162: "162 163",
    168: "168 169",
    181: "181 183 184 185",
    186: "186 187 188 189 190 191 192 194 195",
    200: "200 201 202 203 204 205",
    206: "206 207 208",
    209: "209 211 212 213 215 216 217 218 219 220 221 223",
    222: "222 225 226 227 228 229 230 231 232 233 234 235 236",
    240: "240 241",
    242: "242 244 245 246 248 249 250 251 254",
    253: "253 255",
    258: "258 259 260 262 263 264 267 268 269 271 273",
    275: "275 276 277 278 279",
    281: "281 287 288 290 291 292 293 294 297",
    286: "286 306 308 310 311 313 314 315",
    298: "298 300 301",
    316: "316 317 319",
    322: "322 326",
    325: "325 328 329 331 332 333 334",
    327: "327 339 340 341 343",
    330: "330 335 347",
    336: "336 337",
    338: "338 376 377",
    342: "342 374",
    344: "344 348",
    350: "350 351 352 353 355 356 357 359 360 361 363",
    364: "364 370 373 375",
    365: "365 368 371 372",
    367: "367 385 387",
    378: "378 380 381 382",
    388: "388 513",
    389: "389 390",
    396: "396 398",
    397: "397 399",
    402: "402 405 406 407 408",
    410: "410 411 412 414 415 416 417 418",
    429: "429 430 431 432",
    433: "433 436 437 438 440",
    441: "441 442 443 444",
    449: "449 452 453",
    454: "454 456",
    457: "457 460",
    458: "458 459",
    461: "461 462 463",
    469: "469 471",
    474: "474 476 478",
    479: "479 481",
    502: "502 507 509",
    503: "503 504 505",
    510: "510 511 512",
    518: "518 520",
    521: "521 522",
    529: "529 530 533",
    534: "534 535",
    537: "537 538 539",
    540: "540 542",
}


class AwsCHttpImageBase(Image):
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
        return "gcc:12"

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
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC
RUN apt update && apt install -y cmake git libssl-dev build-essential pkg-config

RUN git clone https://github.com/awslabs/aws-c-common.git /home/aws-c-common
RUN git clone https://github.com/aws/s2n-tls.git /home/s2n-tls
RUN git clone https://github.com/awslabs/aws-c-cal.git /home/aws-c-cal
RUN git clone https://github.com/awslabs/aws-c-io.git /home/aws-c-io
RUN git clone https://github.com/awslabs/aws-c-compression.git /home/aws-c-compression

{code}

{self.clear_env}

"""


class AwsCHttpImageDefault(Image):
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
        return AwsCHttpImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        bundle_prs = BUNDLE_PRS.get(self.pr.number, str(self.pr.number))

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
                "build_deps.sh",
                """#!/bin/bash
# Shared dependency building script.
# Usage: source /home/build_deps.sh <TARGET_DATE> <REPO_NAME>
# Builds all required deps at the latest commit before TARGET_DATE,
# then builds aws-c-http with BUILD_TESTING=ON.

set -e

BUILD_DATE=$1
REPO_NAME=$2
PREFIX=/usr/local
CFLAGS_SUPPRESS="-w -fcommon -include assert.h -D_GNU_SOURCE"

echo "=== Building deps at date: $BUILD_DATE ==="

# Function to build a dependency from the latest commit before the target date
build_dep() {{
    local dep_name=$1
    local dep_path=/home/$dep_name
    local extra_cmake_args=""

    cd "$dep_path"
    git checkout main 2>/dev/null || git checkout master 2>/dev/null || true

    local dep_sha=$(git log --before="$BUILD_DATE" --format=%H -1)
    if [ -z "$dep_sha" ]; then
        dep_sha=$(git rev-list --max-parents=0 HEAD | head -1)
    fi
    echo "Building $dep_name at $dep_sha ($(git log -1 --format=%cI $dep_sha))"
    git checkout -f "$dep_sha"

    # s2n-tls compatibility patches for gcc:12 / aarch64 / glibc 2.36+
    if [ "$dep_name" = "s2n-tls" ]; then
        # Disable PQ crypto (BIKE/SIKE) - not needed by aws-c-http and has
        # ARM asm issues + static_assert failures on aarch64
        extra_cmake_args="-DS2N_NO_PQ=ON"

        # Fix __tm_gmtoff -> tm_gmtoff (glibc 2.36+ renamed the field)
        if grep -q "__tm_gmtoff" utils/s2n_asn1_time.c 2>/dev/null; then
            echo "Patching s2n-tls: __tm_gmtoff -> tm_gmtoff"
            sed -i "s/__tm_gmtoff/tm_gmtoff/g" utils/s2n_asn1_time.c
        fi
    fi

    # aws-c-cal compatibility patches for OpenSSL 3.x
    if [ "$dep_name" = "aws-c-cal" ]; then
        # Old aws-c-cal uses EVP_MD_CTX_destroy (removed in OpenSSL 3) and has
        # conflicting weak-ref declarations for HMAC_CTX_reset/HMAC_Init_ex
        if grep -q "EVP_MD_CTX_destroy" source/unix/openssl_platform_init.c 2>/dev/null; then
            echo "Patching aws-c-cal: OpenSSL 3 compatibility"
            sed -i "s/EVP_MD_CTX_destroy/EVP_MD_CTX_free/g" source/unix/openssl_platform_init.c
        fi
        # Fix HMAC_CTX_reset return type (void -> int in OpenSSL 3)
        if grep -q "extern void HMAC_CTX_reset" source/unix/openssl_platform_init.c 2>/dev/null; then
            sed -i "s/extern void HMAC_CTX_reset/extern int HMAC_CTX_reset/g" source/unix/openssl_platform_init.c
        fi
        # Fix HMAC_Init_ex size_t -> int for key length
        if grep -q "const void \\*, size_t, const EVP_MD" source/unix/openssl_platform_init.c 2>/dev/null; then
            sed -i "s/const void \\*, size_t, const EVP_MD/const void *, int, const EVP_MD/g" source/unix/openssl_platform_init.c
        fi
    fi

    rm -rf build
    mkdir -p build
    cd build
    cmake .. -DCMAKE_INSTALL_PREFIX=$PREFIX -DCMAKE_PREFIX_PATH=$PREFIX \\
        -DCMAKE_MODULE_PATH="$PREFIX/lib/cmake" \\
        -DCMAKE_C_FLAGS="$CFLAGS_SUPPRESS" -DBUILD_TESTING=OFF $extra_cmake_args
    make -j$(nproc)
    make install
    cd /home
}}

# Clean any previously installed dependency files
rm -rf $PREFIX/lib/aws-c-common $PREFIX/lib/cmake/aws-c-common \\
    $PREFIX/lib/cmake/s2n $PREFIX/lib/s2n \\
    $PREFIX/lib/cmake/aws-c-cal $PREFIX/lib/aws-c-cal \\
    $PREFIX/lib/cmake/aws-c-io $PREFIX/lib/aws-c-io \\
    $PREFIX/lib/cmake/aws-c-compression $PREFIX/lib/aws-c-compression \\
    $PREFIX/include/aws \\
    $PREFIX/lib/libaws-c-common.* $PREFIX/lib/libs2n.* \\
    $PREFIX/lib/libaws-c-cal.* $PREFIX/lib/libaws-c-io.* \\
    $PREFIX/lib/libaws-c-compression.* $PREFIX/lib/libaws-c-http.* \\
    $PREFIX/lib/cmake/FindLibCrypto.cmake \\
    $PREFIX/lib/cmake/AwsCFlags.cmake $PREFIX/lib/cmake/AwsSanitizers.cmake \\
    $PREFIX/lib/cmake/AwsSharedLibSetup.cmake $PREFIX/lib/cmake/AwsSIMD.cmake \\
    $PREFIX/lib/cmake/AwsTestHarness.cmake $PREFIX/lib/cmake/AwsFindPackage.cmake \\
    $PREFIX/lib/cmake/AwsFeatureTests.cmake $PREFIX/lib/cmake/AwsCheckHeaders.cmake

build_dep aws-c-common
build_dep s2n-tls

# aws-c-cal: only needed if aws-c-io from this era requires it
cd /home/aws-c-io
git checkout main 2>/dev/null || git checkout master 2>/dev/null || true
IO_SHA=$(git log --before="$BUILD_DATE" --format=%H -1)
if [ -z "$IO_SHA" ]; then
    IO_SHA=$(git rev-list --max-parents=0 HEAD | head -1)
fi
git checkout "$IO_SHA" 2>/dev/null
if grep -qi "aws-c-cal" CMakeLists.txt 2>/dev/null; then
    build_dep aws-c-cal
fi

build_dep aws-c-io

# aws-c-compression: only needed if aws-c-http from this era requires it
if grep -qi "aws-c-compression" /home/$REPO_NAME/CMakeLists.txt 2>/dev/null; then
    build_dep aws-c-compression
fi

# Build aws-c-http
cd /home/$REPO_NAME
rm -rf build
mkdir -p build
cd build
cmake .. -DCMAKE_PREFIX_PATH=$PREFIX -DCMAKE_MODULE_PATH="$PREFIX/lib/cmake" \\
    -DCMAKE_C_FLAGS="$CFLAGS_SUPPRESS" -DBUILD_TESTING=ON
make -j$(nproc)

echo "=== Dep build complete ==="

""".format(),
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

# Determine the base commit date for building deps.
# The base code must compile with deps from the base commit's era.
BASE_DATE=$(git log -1 --format=%cI {pr.base.sha})
echo "Base commit date: $BASE_DATE"

# Also compute the bundle merge date for use by test-run.sh and fix-run.sh.
# The fix/test patches may use newer dep APIs from the bundle merge era.
BUNDLE_PRS="{bundle_prs}"
MAIN_BRANCH=$(git branch -r | grep -o 'origin/main' | head -1 | sed 's|origin/||')
if [ -z "$MAIN_BRANCH" ]; then
    MAIN_BRANCH=$(git branch -r | grep -o 'origin/master' | head -1 | sed 's|origin/||')
fi
if [ -z "$MAIN_BRANCH" ]; then
    MAIN_BRANCH="main"
fi

BUNDLE_DATE=""
echo "Searching for bundle PR merge dates (bundle: $BUNDLE_PRS)..."
for pr_num in $BUNDLE_PRS; do
    COMMIT=$(git log --format="%H" --grep="(#$pr_num)" --fixed-strings "$MAIN_BRANCH" 2>/dev/null | head -1)
    if [ -n "$COMMIT" ]; then
        DATE=$(git log -1 --format=%cI "$COMMIT")
        echo "  PR #$pr_num: merged $DATE"
        if [ -z "$BUNDLE_DATE" ] || [[ "$DATE" > "$BUNDLE_DATE" ]]; then
            BUNDLE_DATE=$DATE
        fi
    else
        echo "  PR #$pr_num: not found in git log"
    fi
done

if [ -z "$BUNDLE_DATE" ]; then
    BASE_DATE_EPOCH=$(git log -1 --format=%ct {pr.base.sha})
    BUFFER_EPOCH=$((BASE_DATE_EPOCH + 2592000))
    BUNDLE_DATE=$(date -u -d "@$BUFFER_EPOCH" --iso-8601=seconds 2>/dev/null || date -u -r "$BUFFER_EPOCH" +%Y-%m-%dT%H:%M:%S%z)
    echo "Fallback: using base date + 30 days: $BUNDLE_DATE"
fi

# Save bundle date for test-run.sh and fix-run.sh to use
echo "$BUNDLE_DATE" > /home/.bundle_date
echo "Bundle merge date (saved): $BUNDLE_DATE"

echo "Trying bundle merge date for dep build: $BUNDLE_DATE"
if ! bash /home/build_deps.sh "$BUNDLE_DATE" "{pr.repo}"; then
    echo "Bundle date build failed. Trying base date + 1 day buffer..."
    BASE_DATE_EPOCH=$(git log -1 --format=%ct {pr.base.sha})
    BUFFER_EPOCH=$((BASE_DATE_EPOCH + 86400))
    BASE_DATE_BUFFERED=$(date -u -d "@$BUFFER_EPOCH" --iso-8601=seconds 2>/dev/null || date -u -r "$BUFFER_EPOCH" +%Y-%m-%dT%H:%M:%S%z)
    echo "Base date + 1 day: $BASE_DATE_BUFFERED"
    if ! bash /home/build_deps.sh "$BASE_DATE_BUFFERED" "{pr.repo}"; then
        echo "Base date + 1 day also failed. Last resort: base commit date: $BASE_DATE"
        bash /home/build_deps.sh "$BASE_DATE" "{pr.repo}"
    fi
fi

""".format(pr=self.pr, bundle_prs=bundle_prs),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
cd build
ctest -V
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --reject /home/test.patch || true

BUNDLE_DATE=$(cat /home/.bundle_date 2>/dev/null || echo "")
if [ -n "$BUNDLE_DATE" ]; then
    bash /home/build_deps.sh "$BUNDLE_DATE" "{pr.repo}"
else
    cd build
    cmake .. -DCMAKE_PREFIX_PATH=/usr/local -DCMAKE_MODULE_PATH="/usr/local/lib/cmake" -DCMAKE_C_FLAGS="-w -fcommon -include assert.h -D_GNU_SOURCE" -DBUILD_TESTING=ON
    make -j$(nproc)
fi

cd /home/{pr.repo}/build
ctest -V

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{
    git apply --whitespace=nowarn --reject /home/test.patch || true
    git apply --whitespace=nowarn --reject /home/fix.patch || true
}}

BUNDLE_DATE=$(cat /home/.bundle_date 2>/dev/null || echo "")
if [ -n "$BUNDLE_DATE" ]; then
    bash /home/build_deps.sh "$BUNDLE_DATE" "{pr.repo}"
else
    cd build
    cmake .. -DCMAKE_PREFIX_PATH=/usr/local -DCMAKE_MODULE_PATH="/usr/local/lib/cmake" -DCMAKE_C_FLAGS="-w -fcommon -include assert.h -D_GNU_SOURCE" -DBUILD_TESTING=ON
    make -j$(nproc)
fi

cd /home/{pr.repo}/build
ctest -V

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


@Instance.register("awslabs", "aws-c-http")
class AwsCHttp(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return AwsCHttpImageDefault(self.pr, self._config)

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

        re_pass_tests = [re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*Passed")]
        re_fail_tests = [
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*\*+Failed")
        ]

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    test = pass_match.group(1)
                    passed_tests.add(test)

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    test = fail_match.group(1)
                    failed_tests.add(test)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
