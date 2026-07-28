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
        # buster (Debian 10), NOT default slim (now bookworm): anki tests import
        # aqt -> `from PyQt5.Qt import *`. PyQt5 has no pip wheel for py3.7/arm64,
        # and bookworm apt PyQt5 is cp311 (ABI-incompatible with this 3.7 runtime).
        # On buster the system python IS 3.7, so apt python3-pyqt5 + python3-sip
        # import cleanly under /usr/local's 3.7 via dist-packages on PYTHONPATH.
        return "python:3.7-slim-buster"

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

apt-get update && apt-get install -y gcc portaudio19-dev

pip install nose mock beautifulsoup4 send2trash requests decorator markdown pyaudio jsonschema

cd /home/anki
git reset --hard
git checkout {pr.base.sha}

bash tools/build_ui.sh >/dev/null 2>&1 || true
QT_QPA_PLATFORM=offscreen PYTHONPATH=/usr/lib/python3/dist-packages:/home/anki nosetests -vs tests || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/anki
bash tools/build_ui.sh >/dev/null 2>&1 || true
QT_QPA_PLATFORM=offscreen PYTHONPATH=/usr/lib/python3/dist-packages:/home/anki nosetests -vs tests

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
bash tools/build_ui.sh >/dev/null 2>&1 || true
QT_QPA_PLATFORM=offscreen PYTHONPATH=/usr/lib/python3/dist-packages:/home/anki nosetests -vs tests

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
bash tools/build_ui.sh >/dev/null 2>&1 || true
QT_QPA_PLATFORM=offscreen PYTHONPATH=/usr/lib/python3/dist-packages:/home/anki nosetests -vs tests

""",
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
FROM python:3.7-slim-buster

## Set noninteractive
ENV DEBIAN_FRONTEND=noninteractive

# buster is EOL -> repoint apt at archive.debian.org, then install the Qt stack.
# python3-pyqt5 + python3-sip land in /usr/lib/python3/dist-packages (cp37, ABI
# matched to this 3.7 runtime); the run scripts add that dir to PYTHONPATH.
RUN sed -i 's|deb.debian.org|archive.debian.org|g; s|security.debian.org|archive.debian.org|g; /buster-updates/d' /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y git gcc portaudio19-dev \
       python3-pyqt5 python3-sip python3-pyqt5.qtwebengine pyqt5-dev-tools

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

RUN pip install nose mock beautifulsoup4 send2trash requests decorator markdown pyaudio jsonschema

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/ankitects/anki.git /home/anki

WORKDIR /home/anki
RUN git reset --hard
RUN git checkout {pr.base.sha}
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("ankitects", "anki_692_to_255")
class ANKI_692_TO_255(Instance):
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
        # nosetests output format:
        #   tests.test_importing.test_anki2_mediadupes ... ok
        #   tests.test_importing.test_apkg ... ok
        #   tests.test_exporting.test_export_fail ... FAIL
        #   tests.test_exporting.test_export_error ... ERROR
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        for line in test_log.splitlines():
            line = line.strip()
            if " ... " in line:
                test_part, status_part = line.split(" ... ", 1)
                status = status_part.split()[0]
                test_name = test_part.strip()
                if status == "ok":
                    passed_tests.add(test_name)
                elif status in ("ERROR", "FAIL"):
                    failed_tests.add(test_name)

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
_BUNDLE_NIS_ANKI_692_TO_255 = [
    "255-257-260-262-263-264-265-266",
    "283-285-286-287-288-289-292",
    "304-306",
    "324-325-326-327-328-329-330-332-338-339-340-343-345-346-347-352-353-354-355-356-357-359",
    "362-363-365-366-367-369-370-372-375-377-378-381-382-383-384-385-386-387-388-390-392-394-395-396-397-399-400-401-402-403-404-405-406-408-409-410-412-413-417-418-419",
    "554-610-611-613-616-618-619-620-624-625-626-627-628-629-630-633-634-635-636-637-639-640-641-642-643-645-646-648-650-653-655-657-658-659-660-662-663-665-666-667-669-670-671-673-675-676-678-679-682-684-685-686-687-688-689-690-691",
    "578-579-581-582-583-587-592-593-595-596",
    "692-693-694-695-696-697-698-700-701-702-704-705",
]
for _ni in _BUNDLE_NIS_ANKI_692_TO_255:
    Instance.register("ankitects", _ni)(ANKI_692_TO_255)
