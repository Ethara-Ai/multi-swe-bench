import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_MERGE_COMMIT_SHAS = {
    311: "8686ae634909fd4e89056d4ead25f181d5606cd4",
    326: "a9a11230a86296887112baabca08dffd4e0c694f",
    327: "5740b68eb57f9044b24cdf226aaf958c76ac9965",
    378: "f2ebb8d4f96225e625d60596e3e80ebd549c3eb3",
    385: "3effaeed691131a00d00fca6fc6e695dac393d62",
    572: "47aa22171050042c24617d66aaa038a7bc240020",
    573: "ec3ba33d01541c853194fbef9984ff828ff480cf",
    702: "e99257d1e25a2d24f5c625ed879aaa4957e82799",
    706: "74acf94f507dfef3e781820f4057c8ca860dc9ab",
    762: "574d0d6e76d73cef4999b37ce71c0c815b540233",
    767: "f8d62bedcd92edba5597a8e041debb782e78332c",
}


class FileTypeImageBase(Image):
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
        return "node:20-bookworm"

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

        return f"""\
FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

{code}

WORKDIR /home/{self.pr.repo}
RUN npm install || true

{self.clear_env}

"""


class FileTypeImageDefault(Image):
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
        return FileTypeImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        merge_sha = _MERGE_COMMIT_SHAS.get(self.pr.number, "")

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
                "apply_patch.sh",
                """\
#!/bin/bash
set -e

PATCH_FILES="$@"
REPO_DIR="/home/{pr.repo}"
ORG="{pr.org}"
REPO="{pr.repo}"
MERGE_SHA="{merge_sha}"

cd "$REPO_DIR"

for PATCH in $PATCH_FILES; do
    if [ ! -s "$PATCH" ]; then
        echo "$(basename $PATCH): EMPTY - skipping"
        continue
    fi

    NEW_BINARIES=""
    EXCLUDE_ARGS=""

    while IFS= read -r bline; do
        if echo "$bline" | grep -q '^Binary files /dev/null and b/'; then
            FPATH=$(echo "$bline" | sed 's|Binary files /dev/null and b/\\(.*\\) differ|\\1|')
            NEW_BINARIES="$NEW_BINARIES $FPATH"
            EXCLUDE_ARGS="$EXCLUDE_ARGS --exclude=$FPATH"
        fi
    done < <(grep '^Binary files' "$PATCH" || true)

    git apply --whitespace=nowarn -C0 $EXCLUDE_ARGS "$PATCH" || git apply --whitespace=nowarn --3way $EXCLUDE_ARGS "$PATCH" || true

    if [ -n "$MERGE_SHA" ] && [ -n "$NEW_BINARIES" ]; then
        for BP in $NEW_BINARIES; do
            mkdir -p "$(dirname "$BP")"
            curl -sL "https://raw.githubusercontent.com/$ORG/$REPO/$MERGE_SHA/$BP" -o "$BP"
        done
    fi
done
""".format(pr=self.pr, merge_sha=merge_sha),
            ),
            File(
                ".",
                "prepare.sh",
                """\
#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}

rm -rf node_modules
npm install || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
npx ava --tap --concurrency=1 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
bash /home/apply_patch.sh /home/test.patch
npm install || true
npx ava --tap --concurrency=1 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
bash /home/apply_patch.sh /home/test.patch /home/fix.patch
npm install || true
npx ava --tap --concurrency=1 2>&1
""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        return f"""\
FROM {dep.image_name()}:{dep.image_tag()}

{self.global_env}

COPY fix.patch /home/fix.patch
COPY test.patch /home/test.patch
COPY apply_patch.sh /home/apply_patch.sh
COPY prepare.sh /home/prepare.sh
COPY run.sh /home/run.sh
COPY test-run.sh /home/test-run.sh
COPY fix-run.sh /home/fix-run.sh
RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("sindresorhus", "file-type")
class FileType(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FileTypeImageDefault(self.pr, self._config)

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

        test_pattern = re.compile(r"^(ok|not ok) (\d+) - (.*?)$")
        lines = test_log.split("\n")
        tap_started = False

        for line in lines:
            line = line.strip()

            if line.startswith("TAP version 13"):
                tap_started = True
                continue

            if not tap_started:
                continue

            if line.startswith("1.."):
                break

            if not line.startswith(("ok", "not ok")):
                continue

            if "# SKIP" in line:
                skip_match = re.match(
                    r"^(ok|not ok) (\d+) - (.*?)(?:\s+# SKIP.*)$", line
                )
                if skip_match:
                    _, _, test_name = skip_match.groups()
                    skipped_tests.add(test_name.strip())
                continue

            match = test_pattern.match(line)
            if match:
                status, _, test_name = match.groups()
                test_name = test_name.strip()

                if status == "ok":
                    passed_tests.add(test_name)
                elif status == "not ok":
                    failed_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
