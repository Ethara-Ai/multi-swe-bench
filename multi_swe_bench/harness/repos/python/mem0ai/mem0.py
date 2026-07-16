import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def parse_pytest_log(log: str) -> TestResult:
    """Parse pytest -v --no-header -rA output.

    Regex patterns match:
      PASSED:  tests/llms/test_openai.py::TestOpenAI::test_foo PASSED
      FAILED:  FAILED tests/llms/test_openai.py::TestOpenAI::test_foo
      SKIPPED: tests/llms/test_openai.py::TestOpenAI::test_foo SKIPPED
      XFAIL:   tests/llms/test_openai.py::TestOpenAI::test_foo XFAIL
    """
    passed_tests = set()
    failed_tests = set()
    skipped_tests = set()

    pattern_status_after = re.compile(
        r"^((?:tests|embedchain/tests)/.*)::(.*) (PASSED|SKIPPED|XFAIL)"
    )
    pattern_failed = re.compile(
        r"^FAILED ((?:tests|embedchain/tests)/.*)::(.*)"
    )

    for line in log.splitlines():
        line = ANSI_ESCAPE.sub("", line).strip()
        match_status_after = pattern_status_after.match(line)
        if match_status_after:
            test_path = match_status_after.group(1)
            test_name = match_status_after.group(2)
            status = match_status_after.group(3)
            full_test_name = f"{test_path}::{test_name}"
            if status == "PASSED":
                passed_tests.add(full_test_name)
            elif status in ("SKIPPED", "XFAIL"):
                skipped_tests.add(full_test_name)
            continue
        match_failed = pattern_failed.match(line)
        if match_failed:
            test_path = match_failed.group(1)
            test_name = match_failed.group(2)
            full_test_name = f"{test_path}::{test_name}"
            failed_tests.add(full_test_name)

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


# Dummy provider credentials so import-time / env-gated tests do not abort
# collection. mem0 integrates many LLM + vector-DB providers. Era-independent,
# so it is baked once into the shared base.
_DUMMY_ENV = """ENV OPENAI_API_KEY=sk-dummy0000000000000000000000000000000000000000
ENV ANTHROPIC_API_KEY=sk-ant-dummy0000000000000000000000000000000000
ENV GOOGLE_API_KEY=dummy
ENV GEMINI_API_KEY=dummy
ENV COHERE_API_KEY=dummy
ENV GROQ_API_KEY=dummy
ENV TOGETHER_API_KEY=dummy
ENV HUGGINGFACE_API_KEY=dummy
ENV HUGGINGFACEHUB_API_TOKEN=dummy
ENV MISTRAL_API_KEY=dummy
ENV DEEPSEEK_API_KEY=dummy
ENV XAI_API_KEY=dummy
ENV OPENROUTER_API_KEY=dummy
ENV AZURE_OPENAI_API_KEY=dummy
ENV AWS_ACCESS_KEY_ID=dummy
ENV AWS_SECRET_ACCESS_KEY=dummy
ENV AWS_DEFAULT_REGION=us-east-1
ENV POETRY_VIRTUALENVS_CREATE=false
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PYTHONDONTWRITEBYTECODE=1"""

_APT_PACKAGES = ["libgeos-dev"]

# Optional provider SDKs that mem0 imports lazily. Without them the affected test
# modules fail at *collection*, not at assert time -- and mem0/proxy/main.py is
# worse than a plain ImportError: it catches the ImportError and then prompts
#     input("The 'litellm' library is required. Install it now? [y/N]: ")
# which under pytest raises "OSError: reading from stdin while output is
# captured" and kills the whole module. Pre-caching them is the image's job, so
# the model never needs to reach the network for them.
#
# Appended AFTER the project install so the project's own pinned deps resolve
# first and these only fill the gaps. Deliberately NOT applied to the embedchain
# era (mem0_1005_to_189): that era imports none of them, and pulling modern
# litellm/chromadb into its old pinned dependency set risks breaking a currently
# healthy environment.
# sentence-transformers is deliberately excluded: it pulls in torch (~2-3 GB per
# image), and on a shared host that multiplied across the poetry+hatchling eras
# is what exhausted the disk. It only gates tests/embeddings/test_huggingface_*
# collection (p2p signal, never fail-to-pass), so the cost is not worth it.
_PROVIDER_DEPS = (
    "RUN pip install litellm google-generativeai vertexai ollama groq together "
    "huggingface_hub chromadb || true"
)


class ImageBase(Image):
    """Toolchain-only base image, SHARED by every mem0 PR across every era.

    One image (tag ``base``, constant) for all PRs. It carries the interpreter,
    apt packages, pip tooling and dummy provider creds -- and deliberately does
    NOT clone the repo.

    That last point is the whole safety argument for a shared base. A base that
    clones must checkout ``${BASE_COMMIT}``, and Image._HARDENING_BLOCK then
    force-detaches to that commit and deletes every other ref. Under a constant
    tag the first PR to build would pin the image to *its* commit and strip the
    history every other PR needs, so all the others would check out the wrong
    tree. Keeping the clone out of the base makes the image commit-independent,
    which is exactly what lets it be shared.

    ``dependency()`` returns a string, so the DockerfileEnhancer still adds the
    syntax directive and the ARG/ENV/LABEL infra block. It injects no hardening
    here because this Dockerfile contains no git clone/fetch/remote-add for it
    to find -- correct, since there is no repo in this layer to harden.
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

    def dependency(self) -> Union[str, "Image"]:
        return "python:3.11-slim"

    def image_tag(self) -> str:
        # Constant: no PR-specific state in this layer, so all 99 PRs share it.
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return list(_APT_PACKAGES)

    def dockerfile(self) -> str:
        # Overrides Image.dockerfile() only to omit its clone/checkout/hardening
        # sequence; everything else mirrors it, including the apt helper.
        base_img = self.dependency()

        default_packages = [
            "ca-certificates",
            "curl",
            "build-essential",
            "git",
            "gnupg",
            "make",
            "python3",
            "sudo",
            "wget",
        ]
        packages_str = " \\\n    ".join(default_packages + self.extra_packages())
        apt_command = self._get_apt_update_command(packages_str, base_img)

        sections = [f"FROM {base_img}"]

        if self.global_env:
            sections.append(self.global_env)

        sections.append(
            "WORKDIR /home/\nENV DEBIAN_FRONTEND=noninteractive\nENV LANG=C.UTF-8"
        )
        sections.append(apt_command)
        sections.append(_DUMMY_ENV)
        sections.append(
            "RUN pip install --upgrade pip setuptools wheel || true"
        )

        if self.clear_env:
            sections.append(self.clear_env)

        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


def pr_dockerfile(image: Image, install: str) -> str:
    """Render the per-PR layer: clone, checkout, era install, COPY, harden.

    dependency() is an Image, so DockerfileEnhancer.enhance() returns this file
    verbatim -- it injects nothing. Two consequences this function has to honour:

    * build_dataset only passes REPO_URL / BASE_COMMIT as build args for *string*
      dependencies, so both are declared here as ARGs with literal defaults. The
      BASE_COMMIT default is this PR's own sha, which is what the hardening block
      interpolates.
    * no hardening is injected for us, so Image._HARDENING_BLOCK is embedded
      explicitly, last, after the repo is cloned and the project installed.
    """
    base = image.dependency()
    org = image.pr.org
    repo = image.pr.repo

    sections = [
        f"FROM {base.image_full_name()}",
        f'ARG REPO_URL="https://github.com/{org}/{repo}.git"\n'
        f'ARG BASE_COMMIT="{image.pr.base.sha}"',
    ]

    if image.global_env:
        sections.append(image.global_env)

    sections.append("WORKDIR /home/")
    sections.append(f'RUN git clone "${{REPO_URL}}" /home/{repo}')
    sections.append(f"WORKDIR /home/{repo}")
    sections.append("RUN git reset --hard\nRUN git checkout ${BASE_COMMIT}")

    if install:
        sections.append(install)

    copy_commands = "".join(f"COPY {file.name} /home/\n" for file in image.files())
    if copy_commands:
        sections.append(copy_commands.rstrip("\n"))

    sections.append(Image._HARDENING_BLOCK.rstrip("\n"))

    if image.clear_env:
        sections.append(image.clear_env)

    sections.append('CMD ["/bin/bash"]')

    return "\n\n".join(sections) + "\n"


_INSTALL = (
    "RUN pip install --upgrade pip setuptools wheel poetry-core hatchling poetry || true\n"
    "RUN poetry config virtualenvs.create false 2>/dev/null || true\n"
    "RUN poetry install --all-extras 2>/dev/null "
    '|| pip install -e ".[dev,test]" 2>/dev/null '
    '|| pip install -e ".[test,graph,vector_stores,llms,extras]" 2>/dev/null '
    '|| pip install -e ".[test]" 2>/dev/null || pip install -e . 2>/dev/null || true\n'
    "RUN pip install pytest pytest-mock pytest-asyncio pytest-env || true"
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
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                """ls -la
###ACTION_DELIMITER###
pip install --upgrade pip setuptools wheel poetry-core hatchling poetry
###ACTION_DELIMITER###
poetry install --all-extras 2>/dev/null || pip install -e ".[dev,test]" 2>/dev/null || pip install -e ".[test,graph,vector_stores,llms,extras]" 2>/dev/null || pip install -e ".[test]" 2>/dev/null || pip install -e . 2>/dev/null || true
###ACTION_DELIMITER###
pip install pytest pytest-mock pytest-asyncio pytest-env || true""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
pytest tests/ -v --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors -o 'addopts='
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
pytest tests/ -v --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors -o 'addopts='
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
pytest tests/ -v --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors -o 'addopts='
""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        return pr_dockerfile(self, _INSTALL)


_ORG = "mem0ai"
_REPO = "mem0"

_SEMVER = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_BUNDLE = re.compile(r"^\d+(?:-\d+)*$")

# Era coverage, declared by each era module at import time via register_era().
# Entries are (min_version, min_anchor, era_key), sorted ascending.
_ERAS = []
# Exact number_interval -> era key, for eras that cover one specific bundle.
_ERA_BUNDLES = {}


def register_era(era_key, min_version=None, min_anchor=None, bundles=()):
    """Declare the releases an era module packages.

    The dataset ships ``number_interval`` as the dash-joined bundle of PRs a
    release-bundled instance covers, e.g. "2146-2147-2148-2151". Those strings
    are per-instance, so they cannot be registered as literal keys -- there
    would be one key per row. Instead each era module declares the release range
    it packages here, and the Instance.create hook below maps a bundle onto its
    era.

    ``min_version`` is the inclusive lower bound of the era, as a semver tuple;
    the era with the highest ``min_version`` that a release satisfies wins.
    ``min_anchor`` is the same bound expressed as a PR number, used only for
    rows whose base.label carries no parseable semver. ``bundles`` pins exact
    number_interval strings to an era, for eras covering a single bundle.
    """
    for bundle in bundles:
        _ERA_BUNDLES[bundle] = era_key
    if min_version is not None:
        _ERAS.append((min_version, min_anchor or 0, era_key))
        _ERAS.sort()


def _lower_version(label: str) -> Optional[tuple]:
    """(0, 1, 44) from a "v0.1.44..v0.1.45" tag range; None if not semver."""
    if not label:
        return None
    match = _SEMVER.search(label.split("..")[0])
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _anchor(interval: str, number: int) -> int:
    """Lowest PR in a dash-joined bundle; the instance's own number otherwise."""
    if interval and _BUNDLE.match(interval):
        return min(int(part) for part in interval.split("-"))
    return number


def resolve_era(pr: PullRequest) -> str:
    """Era key for a mem0 PR whose number_interval is a raw PR bundle.

    The eras are *packaging* eras -- embedchain+poetry, poetry, hatchling -- so
    the discriminator is the release the bundle landed in, taken from the lower
    tag of base.label ("v0.1.44..v0.1.45" -> (0, 1, 44)). PR number cannot be
    used: mem0's tags do not advance monotonically with PR number (PR 2367 ships
    in v0.1.107 while the higher PR 2371 ships in v0.1.69), so the eras
    interleave by number and separate only by version. The anchor PR is a
    fallback for labels that carry no parseable semver.
    """
    interval = getattr(pr, "number_interval", "") or ""
    if interval in _ERA_BUNDLES:
        return _ERA_BUNDLES[interval]
    if not _ERAS:
        return ""

    version = _lower_version(getattr(getattr(pr, "base", None), "label", "") or "")
    if version is not None:
        selected = _ERAS[0][2]
        for min_version, _, era_key in _ERAS:
            if version >= min_version:
                selected = era_key
        return selected

    anchor = _anchor(interval, getattr(pr, "number", 0) or 0)
    selected = _ERAS[0][2]
    for _, min_anchor, era_key in sorted(_ERAS, key=lambda era: era[1]):
        if anchor >= min_anchor:
            selected = era_key
    return selected


# Route a bundle onto its era only when the stock exact-match lookup would fail,
# so a number_interval that is already a registered key still wins. number_interval
# itself is left untouched: report.py and dataset.py pass it straight through, and
# they should record the real bundle rather than an era key.
if not getattr(Instance, "_mem0_route_hook", False):
    _stock_create = Instance.create.__func__

    def _mem0_create(cls, pr, config, *args, **kwargs):
        interval = getattr(pr, "number_interval", "") or ""
        if (
            interval
            and getattr(pr, "org", "") == _ORG
            and getattr(pr, "repo", "") == _REPO
            and f"{pr.org}/{interval}" not in cls._registry
        ):
            era_key = resolve_era(pr)
            if era_key and f"{pr.org}/{era_key}" in cls._registry:
                return cls._registry[f"{pr.org}/{era_key}"](pr, config, *args, **kwargs)
        return _stock_create(cls, pr, config, *args, **kwargs)

    Instance.create = classmethod(_mem0_create)
    Instance._mem0_route_hook = True


@Instance.register("mem0ai", "mem0")
class MEM0(Instance):
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
        return parse_pytest_log(log)
