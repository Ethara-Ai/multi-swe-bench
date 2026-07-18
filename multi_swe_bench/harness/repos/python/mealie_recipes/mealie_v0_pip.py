import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Binary fixtures in these v0 patches are emitted as plain "Binary files differ"
# markers (no applicable git binary payload); git apply is atomic so one such
# file rejects the whole patch. Exclude common binary types so the text portion
# (the actual code + tests) still applies.
_BIN_EXCLUDES = (
    "--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' "
    "--exclude='*.ico' --exclude='*.webp' --exclude='*.bmp' --exclude='*.svg' "
    "--exclude='*.zip' --exclude='*.gz' --exclude='*.tar' --exclude='*.tgz' "
    "--exclude='*.pdf' --exclude='*.db' --exclude='*.sqlite' --exclude='*.sqlite3' "
    "--exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' "
    "--exclude='*.mo' --exclude='*.jar' --exclude='*.so' --exclude='*.pyc'"
)

# Test layout shifts across v0 (mealie/test -> mealie/tests -> tests). Pick the
# directory that actually exists at the current (possibly patched) tree.
_TP_DISCOVER = (
    "if [ -d mealie/tests ]; then TP=mealie/tests; "
    "elif [ -d mealie/test ]; then TP=mealie/test; "
    "elif [ -d tests ]; then TP=tests; else TP=.; fi"
)
_APPLY_FN = (
    "apply_patch() {{\n"
    "  git -C /home/{repo} apply --whitespace=nowarn " + _BIN_EXCLUDES + " \"$1\" && return 0\n"
    "  git -C /home/{repo} apply --whitespace=nowarn --3way " + _BIN_EXCLUDES + " \"$1\" && return 0\n"
    "  ( cd /home/{repo} && patch -p1 --forward --fuzz=3 < \"$1\" ) ; return 0\n"
    "}}"
)


class ImageBase(Image):
    """Shared per-era base: OS + toolchain + a FULL clone of the repo (all
    history, NO checkout, NO hardening). Built ONCE and reused by every PR in
    this era. The leading `# syntax=` directive makes DockerfileEnhancer return
    this Dockerfile verbatim (image.py: `if SYNTAX_DIRECTIVE in raw: return raw`)
    so the enhancer does NOT inject the ${BASE_COMMIT} hardening pass here — the
    base has no BASE_COMMIT and must keep full history so any PR's base.sha stays
    reachable. Per-PR checkout + hardening live in ImageDefault.
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
        return "python:3.8-bullseye"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return "base-py38-pip"

    def workdir(self) -> str:
        return "base-py38-pip"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return """# syntax=docker/dockerfile:1.6
FROM python:3.8-bullseye

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends git build-essential libxml2-dev libxslt1-dev patch

RUN printf 'setuptools<58\\nlxml<5\\n' > /cons.txt
RUN PIP_CONSTRAINT=/cons.txt pip install --upgrade "pip<24" "setuptools<58" wheel
RUN pip install pytest

WORKDIR /home/
RUN git clone https://github.com/mealie-recipes/mealie.git /home/mealie
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
        return ImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        apply_fn = _APPLY_FN.format(repo=self.pr.repo)
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
                """ls -la
###ACTION_DELIMITER###
apt-get update && apt-get install -y libxml2-dev libxslt1-dev
###ACTION_DELIMITER###
printf 'setuptools<58\\nlxml<5\\n' > /cons.txt
###ACTION_DELIMITER###
PIP_CONSTRAINT=/cons.txt pip install "pip<24" "setuptools<58" wheel
###ACTION_DELIMITER###
PIP_CONSTRAINT=/cons.txt pip install -r requirements.txt
###ACTION_DELIMITER###
pip install pytest
###ACTION_DELIMITER###
"""
                + _TP_DISCOVER
                + """; python -m pytest "$TP" --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{repo}
{tp}
python -m pytest "$TP" --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors

""".format(repo=self.pr.repo, tp=_TP_DISCOVER),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{repo}
{apply_fn}
apply_patch /home/test.patch
{tp}
python -m pytest "$TP" --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors

""".format(repo=self.pr.repo, apply_fn=apply_fn, tp=_TP_DISCOVER),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{repo}
{apply_fn}
apply_patch /home/test.patch
apply_patch /home/fix.patch
{tp}
python -m pytest "$TP" --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors

""".format(repo=self.pr.repo, apply_fn=apply_fn, tp=_TP_DISCOVER),
            ),
        ]

    def dockerfile(self) -> str:
        # Two-stage: chain to the shared ImageBase *Image*. Because dependency()
        # returns an Image (not a str), DockerfileEnhancer returns this verbatim
        # and supplies neither ARG BASE_COMMIT nor the hardening pass — so we set
        # BASE_COMMIT and embed Image._HARDENING_BLOCK ourselves. The base holds
        # a full clone + /cons.txt + pytest; here we check out THIS PR's base.sha,
        # install requirements against it, then the hardening block prunes every
        # other ref/commit (reward-hack defense). `hardening` is inserted as a
        # plain value so its ${...}/$(...) tokens stay byte-identical.
        base = self.dependency()
        name = base.image_name()
        tag = base.image_tag()
        base_sha = self.pr.base.sha
        repo = self.pr.repo
        hardening = Image._HARDENING_BLOCK

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{base_sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

WORKDIR /home/{repo}
RUN git checkout {base_sha}
RUN PIP_CONSTRAINT=/cons.txt pip install -r requirements.txt

{copy_commands}
{hardening}
CMD ["/bin/bash"]
"""


@Instance.register("mealie-recipes", "mealie_v0_pip")
class MEALIE_V0_PIP(Instance):
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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # pytest `-rA` short test summary lines:
        #   PASSED mealie/tests/test_recipes/test_scraper.py::test_name[a b]
        #   FAILED mealie/test/test_scraper.py::test_name - AssertionError: ...
        #   ERROR  mealie/test/test_scraper.py            (collection error)
        summary_pattern = re.compile(
            r"^(PASSED|FAILED|ERROR|XFAIL|XPASS)\s+(.+?)\s*$", re.MULTILINE
        )
        for status, name in summary_pattern.findall(log):
            if status in ("FAILED", "ERROR"):
                name = re.sub(r"\s+-\s.*$", "", name).strip()
                failed_tests.add(name)
            elif status == "PASSED":
                passed_tests.add(name.strip())
            # XFAIL / XPASS: expected-fail bookkeeping, not real pass/fail

        # Grouped skip summary: SKIPPED [6] mealie/test/test_x.py:18: reason
        for m in re.finditer(
            r"^SKIPPED\s+\[\d+\]\s+(\S+?):(\d+):", log, re.MULTILINE
        ):
            skipped_tests.add(f"{m.group(1)}:{m.group(2)}")

        # Defensive fallback: verbose per-test lines `nodeid STATUS [ 12%]`
        verbose_pattern = re.compile(
            r"^(.+?::.+?)\s+(PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)"
            r"(?:\s+\[\s*\d+%\])?\s*$",
            re.MULTILINE,
        )
        for name, status in verbose_pattern.findall(log):
            name = name.strip()
            if status == "PASSED":
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            elif status == "SKIPPED":
                skipped_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )

# Route bundled PRs by their dash-joined `prs_in_bundle` interval to this era.
# Instance.create() looks up f"{org}/{number_interval}", so every bundle whose
# base.sha matches this era (requirements.txt era (no pyproject.toml) — python:3.8-bullseye)
# must be registered here. Era was derived from the repo state at each base.sha
# (packaging files), not from PR-number ranges — routing is NOT monotonic in PR
# number (e.g. bundle 5883 is uv-era while the higher 6128/6268 are poetry-era).
# 2 bundle(s); intervals come from the lht dataset's prs_in_bundle.
_NUMBER_INTERVALS = [
    "30-31-33-35-39-40-42-49-52-53-59",
    "60-74-85",
]
for _interval in _NUMBER_INTERVALS:
    Instance.register("mealie-recipes", _interval)(MEALIE_V0_PIP)
