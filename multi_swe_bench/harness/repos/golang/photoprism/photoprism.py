from __future__ import annotations
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class PhotoprismImageBase(Image):
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
        return "golang:latest"

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

        # This shared base deliberately does NOT clone the repo. The base image is
        # built once per repo, so cloning + hardening here would pin every instance
        # to a single ${BASE_COMMIT}: the hardening block deletes all refs and runs
        # `gc --prune=now`, so any instance whose base commit is not an ancestor of
        # that one loses its objects and fails with "fatal: unable to read tree".
        # The clone/checkout/harden therefore happens per-PR in
        # PhotoprismImageDefault.dockerfile(), matching the airbnb/javascript layout.

        # Single-FROM layout: the TensorFlow artifacts are copied straight from
        # the external photoprism/develop image via `COPY --from=<image-ref>`,
        # rather than a leading `FROM ... AS tf-source` builder stage. This keeps
        # `FROM {image_name}` as the first (and only) FROM, so
        # DockerfileEnhancer._find_from() targets the real runtime stage and its
        # injected ARGs/ENV/certs/labels land in the image that is actually run.
        return f"""FROM {image_name}

{self.global_env}

# Runtime media tooling that photoprism's Go tests shell out to. Without these,
# metadata/media tests (Exif, Vips/HEIC, FFmpeg, imaging) fail with "exiftool ...
# could not find" / "no such file" -- an environment gap, not a code defect. The
# same tools ship in photoprism/develop:bookworm (the image we COPY TensorFlow
# from), so this matches photoprism's own intended test environment.
RUN apt-get update && apt-get install -y --no-install-recommends \\
    gcc pkg-config libvips-dev \\
    ffmpeg libimage-exiftool-perl imagemagick \\
    libheif1 libheif-examples \\
    darktable rawtherapee \\
    && rm -rf /var/lib/apt/lists/*

COPY --from=photoprism/develop:bookworm /usr/lib/libtensorflow.so /usr/lib/libtensorflow.so.1 /usr/lib/
COPY --from=photoprism/develop:bookworm /usr/lib/libtensorflow_framework.so /usr/lib/libtensorflow_framework.so.1 /usr/lib/
COPY --from=photoprism/develop:bookworm /usr/include/tensorflow/ /usr/include/tensorflow/

RUN ln -sf /usr/lib/libtensorflow.so /usr/local/lib/libtensorflow.so && \\
    ln -sf /usr/lib/libtensorflow_framework.so /usr/local/lib/libtensorflow_framework.so && \\
    ldconfig

WORKDIR /home/

{self.clear_env}

"""


class PhotoprismImageDefault(Image):
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
        return PhotoprismImageBase(self.pr, self.config)

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
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "strip_binary.sh",
                r"""#!/bin/bash
# Drop binary file sections from a unified diff.
#
# The dataset's patches carry binary files (compiled .mo catalogs, images,
# sqlite fixtures) as markers WITHOUT the binary payload, so `git apply`
# rejects them with "cannot apply binary patch ... without full index line".
# Because `git apply` is all-or-nothing, those few unappliable assets veto
# every text hunk in the same patch -- including the actual code fix.
# Stripping them lets the real source changes apply.
#
# usage: strip_binary.sh <in.patch> <out.patch>
set -e

awk '
  function flush() { if (have && !bin) printf "%s", buf; buf = ""; bin = 0 }
  BEGIN            { have = 1; bin = 0; buf = "" }
  /^diff --git /   { flush(); have = 1 }
  /^GIT binary patch/ { bin = 1 }
  /^Binary files /    { bin = 1 }
                   { buf = buf $0 "\n" }
  END              { flush() }
' "$1" > "$2"

""",  # noqa: no .format() -- the awk body's braces are not format fields
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
bash /home/strip_binary.sh /home/test.patch /home/test.nobin.patch
git apply --whitespace=nowarn /home/test.nobin.patch || echo "Warning: test.patch had errors, continuing..."
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
bash /home/strip_binary.sh /home/test.patch /home/test.nobin.patch
bash /home/strip_binary.sh /home/fix.patch /home/fix.nobin.patch
# Applied as two invocations, not one: `git apply a b` is atomic across both,
# so a problem in fix.patch would also roll back test.patch and leave the fix
# stage silently running unpatched code.
git apply --whitespace=nowarn /home/test.nobin.patch || echo "Warning: test.patch had errors, continuing..."
git apply --whitespace=nowarn /home/fix.nobin.patch || echo "Warning: fix.patch had errors, continuing..."
go test -v -count=1 ./...

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

        # The shared toolchain base does NOT clone, so this per-PR image clones full
        # history and checks out ${BASE_COMMIT} inline. Because dependency() is an
        # Image, the DockerfileEnhancer returns this Dockerfile verbatim -- the clone
        # and hardening below are kept as written (and pinning here is correct: it is
        # per-PR, not the shared base).
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

        # Anti-reward-hacking hardening -- the canonical Image._HARDENING_BLOCK
        # (detach at ${BASE_COMMIT}, remove origin, delete all refs, reflog expire,
        # gc/repack, drop alternates, + asserts, then submodule strip). Concatenated
        # raw (not via f-string) so its ${BASE_COMMIT} / %(refname) tokens stay
        # literal. Runs AFTER prepare.sh, which needs full history.
        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail


@Instance.register("photoprism", "photoprism")
class Photoprism(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PhotoprismImageDefault(self.pr, self._config)

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

        re_pass_tests = [re.compile(r"--- PASS: (\S+)")]
        re_fail_tests = [
            re.compile(r"--- FAIL: (\S+)"),
            re.compile(r"FAIL:?\s?(.+?)\s"),
        ]
        re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

        def get_base_name(test_name: str) -> str:
            index = test_name.rfind("/")
            if index == -1:
                return test_name
            return test_name[:index]

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    test_name = pass_match.group(1)
                    base_name = get_base_name(test_name)
                    if base_name in failed_tests:
                        continue
                    if base_name in skipped_tests:
                        skipped_tests.remove(base_name)
                    passed_tests.add(base_name)

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    test_name = fail_match.group(1)
                    base_name = get_base_name(test_name)
                    if base_name in passed_tests:
                        passed_tests.remove(base_name)
                    if base_name in skipped_tests:
                        skipped_tests.remove(base_name)
                    failed_tests.add(base_name)

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    test_name = skip_match.group(1)
                    base_name = get_base_name(test_name)
                    if base_name in passed_tests:
                        continue
                    if base_name not in failed_tests:
                        continue
                    skipped_tests.add(base_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === number_interval routing =================================================
# Each LHT record is a BUNDLE of several PRs over a tag range, not a single PR:
# `prs_in_bundle` lists them and the generated dataset carries them dash-joined
# in `number_interval` (e.g. "854-863-867-868-875-880-899-904-905-908-922").
#
# Instance.create() routes on f"{org}/{number_interval}" whenever that field is
# non-empty, falling back to f"{org}/{repo}" only when it is "" (instance.py:41-51).
# The raw *_lht_final.jsonl inputs omit the field, so they resolve via the
# "photoprism/photoprism" registration above -- but any dataset the harness
# GENERATES has it populated. Feeding such a file back in would raise
# "Instance 'photoprism/854-863-...' is not registered" before a single image is
# built. Registering every interval to the same Photoprism class keeps both
# directions working; the decorator above stays for the no-interval path.
#
# Values are "-".join(prs_in_bundle) for all 50 records of
# dataset/photoprism__photoprism_lht_final.jsonl, verified to reproduce the
# number_interval emitted in data1/dataset/*_dataset_final.jsonl exactly.
_NUMBER_INTERVALS = [
    "1053-1079-1084",
    "1108-1133-1141-1142-1144-1147-1163-1193-1201-1202",
    "1161-1221",
    "1220-1222-1243-1244-1247",
    "1489-1579",
    "1585-1600",
    "1620-1622",
    "1804-1843-1870",
    "1947-1956",
    "1964-2099-2112-2118-2122-2130-2134-2136-2137-2138-2139-2140-2145-2147-2152-2154-2157-2160-2164-2165-2172-2173-2177-2180-2190-2191-2192-2193-2198-2205-2219-2220-2263-2264-2272-2276-2279-2284-2288-2289-2294-2295-2297-2300-2301-2306-2308-2310-2321-2322-2326",
    "2007-2086-2087-2088-2092",
    "2329-2338-2342-2346-2348-2351",
    "2383-2384-2388-2406-2413-2417",
    "2392-2419-2421-2424",
    "2481-2487-2488-2489-2500-2513-2527-2528-2529",
    "2574-2575-2576-2577-2578-2579-2580-2581-2582-2583-2603-2616-2636-2637-2641-2649-2664",
    "2849-2850",
    "2863-2869-2877-2879-2880-2881-2884-2885-2886-2887-2890-2893-2901",
    "2902-2903-2904-2906",
    "3365-3405-3407-3408-3428",
    "3383-3388-3389-3399-3400-3401",
    "3448-3457",
    "3778-3786-3787-3791-3792-3794",
    "3800-3820-3824-3825-3826-3828-3830",
    "3838-3842-3870-3893",
    "4295-4301",
    "4311-4312",
    "4317-4318-4348-4364-4370-4373-4374-4379",
    "4409-4422-4423-4431-4458-4489-4494-4499-4500-4511-4521-4522-4527",
    "4826-4832-4834-4835-4836-4840-4842-4847-4850-4851-4852-4853-4854-4856-4857-4858-4863-4864-4865-4866-4867-4868-4869-4870-4874-4875-4876-4878-4879",
    "918-969-974-983",
    "938-940",
    "947-951-958-965",
    "1328-1349-1393-1398-1410-1443-1470-1541",
    "1674-1690-1706-1726-1729-1751",
    "1873-2596-3532-3555-3566-3568-3577-3588-3595-3605-3606-3636-3648-3657-3662-3700-3710-3714-3749-3750-3751-3752-3760",
    "2292-2379-2430-2433-2434-2435-2436-2438-2439-2441-2445-2448-2449-2454-2455-2456-2458-2459-2471-2473-2474-2475",
    "2414-2508-2721-3498-3510-3538-3545",
    "2623-2648-2670-2671-2683-2686-2693-2701-2709-2712-2716-2720-2724-2725-2730-2732-2737-2762-2764-2766-2767-2770-2782-2787-2789-2792-2804-2822-2823-2824-2825-2826-2827-2829-2830-2834-2835-2836-2837-2838-2841",
    "2920-2925-2934-2935-2943-2944-2945-2947-2948-2949-2950-2951-2952-2958-2960-2975-2980-2985-2989-2990-2992-2993-2996-3002-3006-3007-3010-3019-3022-3024-3025-3028-3030-3031-3036-3037-3043-3047-3054-3059-3060-3061-3065-3067-3093-3104-3107-3111-3112-3115-3116-3117-3119-3122-3147-3153-3154-3161-3162-3166-3167-3176-3178-3180-3184-3188-3193-3202-3208-3209-3210-3211-3212-3213-3217-3219-3225-3233-3234-3235-3240-3241-3242-3244-3251-3252-3253-3254-3255-3256-3257-3258-3259-3261-3262-3264-3265-3266-3267-3268-3270-3271-3272-3273-3276-3277-3281-3283-3288-3299-3300-3303-3304-3307-3308-3312-3313-3314-3315-3316-3322-3323-3326-3327-3329-3330-3331-3336-3343-3354-3355",
    "3928-4128-4137-4138-4143-4144-4145-4146-4152-4158-4161-4164-4165-4172-4177-4178-4189-4190-4191-4196-4197-4198-4199",
    "4204-4218-4223-4228-4252-4256-4265-4267-4270-4271-4272-4274-4275-4276-4277-4278-4279-4280-4281-4282-4283-4287-4288-4289-4292-4293",
    "4506-4608-4691-4890-4893-4894-4896-4897-4900-4903-4909-4910-4911-4914-4915-4918-4922-4924-4925-4926-4928-4930-4931-4934-4939-4941-4943-4944-4945-4948-4958",
    "4530-4556-4706-4708-4709-4722-4726-4759-4764-4766-4768-4773-4774-4776-4791-4794-4795-4802-4803-4805-4806",
    "4971-4972-4974-4975-4978-4981-4990-4991-4993-4999-5003-5004-5005-5014-5015-5026-5035-5042-5043-5061-5065-5067-5068-5069-5071-5074-5078-5080-5082-5083-5086-5092",
    "5011-5087-5150-5158-5160-5161-5163-5164-5172-5177-5178-5188-5191-5192-5196-5226-5237-5239-5241-5243-5249-5252-5256-5257-5275-5280-5281-5282-5283-5287-5288-5290-5291-5293-5297-5298-5302-5305-5307-5315-5318-5319-5320-5323-5324-5326-5327-5329-5332-5336-5338-5339-5340-5341-5342-5343-5344-5345-5346-5351",
    "5309-5378-5387-5403-5409-5410-5420-5421-5422-5423-5424-5434-5456-5458-5460",
    "801-804-816",
    "824-833-836-837-839-849-850-853-871-872",
    "854-863-867-868-875-880-899-904-905-908-922",
]

for _interval in _NUMBER_INTERVALS:
    Instance.register("photoprism", _interval)(Photoprism)
