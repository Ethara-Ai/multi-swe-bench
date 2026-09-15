from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.dataset import Dataset as _Dataset
from multi_swe_bench.harness.repos.typescript.jestjs.jest import (
    _ERA2_MIN_PR,
    JestImageDefault,
    _parse_jest_log,
)

_DEFAULT_NODE_IMAGE = "node:8-stretch"
_DEFAULT_BASE_TAG = "base-node8"

_NODE_IMAGE_RANGES: tuple[tuple[int, int, str], ...] = (
    (3217, 3217, "node:7"),
)


def _node_range(number: int) -> Optional[tuple[int, int, str]]:
    for lo, hi, image in _NODE_IMAGE_RANGES:
        if lo <= number <= hi:
            return lo, hi, image
    return None


def _node_image(number: int) -> str:
    entry = _node_range(number)
    return entry[2] if entry is not None else _DEFAULT_NODE_IMAGE


_OLD_GIT_NODE_IMAGES = frozenset({"node:7"})


def _base_tag(number: int) -> str:
    entry = _node_range(number)
    if entry is None:
        return _DEFAULT_BASE_TAG
    lo, hi, _ = entry
    return f"base-{hi}-to-{lo}"


_INSTALL = (
    "yarn install --frozen-lockfile --network-timeout 600000 --ignore-scripts"
    " || yarn install --network-timeout 600000 --ignore-scripts"
    " || echo 'yarn install reported failure; continuing -- the jest run is the arbiter'\n"
)

_LINK = (
    "if node -e \"const p=require('fs').readFileSync('package.json','utf8');"
    'process.exit(JSON.parse(p).workspaces?0:1)"; then\n'
    '  echo "link: yarn workspaces tree -- packages/* already linked by yarn install"\n'
    "else\n"
    '  echo "link: pre-workspaces tree -- bootstrapping with lerna"\n'
    "  rm -rf packages/*/node_modules\n"
    '  npm config set before "$(git show -s --format=%cI HEAD)" || true\n'
    "  ./node_modules/.bin/lerna bootstrap --npm-client npm --concurrency 1"
    " || { npm config delete before || true;"
    " ./node_modules/.bin/lerna bootstrap --npm-client npm --concurrency 1; }"
    " || ./node_modules/.bin/lerna bootstrap --concurrency 1"
    " || echo 'bootstrap reported failure; continuing -- the jest run is the arbiter'\n"
    "  npm config delete before || true\n"
    "fi\n"
)

_BUILD = "node ./scripts/build.js\n"

_PR_SOFT_BUILD = frozenset({3559})


def _build_step(number: int) -> str:
    if number in _PR_SOFT_BUILD:
        return _BUILD.rstrip(chr(10)) + " || true" + chr(10)
    return _BUILD


_JEST_BIN = "node ./packages/jest-cli/bin/jest.js"

_PR_TEST_FLAGS = {
    3651: " --runInBand --colors",
    4114: " --runInBand",
}


def _test_cmd(number: int) -> str:
    return f"{_JEST_BIN} --verbose{_PR_TEST_FLAGS.get(number, '')} 2>&1" + chr(10)


_RESET = "git reset --hard\ngit clean -fd\n"

_TEST_DIR_SEGMENTS = frozenset({"__tests__", "__mocks__", "__snapshots__"})
_TEST_ROOTS = ("integration_tests/",)
_TEST_BASENAMES = frozenset({"test_utils.js"})

_DIFF_HEADER = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$")


def _is_test_path(path: str) -> bool:
    segments = path.split("/")
    if _TEST_DIR_SEGMENTS.intersection(segments[:-1]):
        return True
    if path.startswith(_TEST_ROOTS):
        return True
    return segments[-1] in _TEST_BASENAMES


def _diff_sections(patch: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    path: Optional[str] = None
    lines: list[str] = []
    for line in (patch or "").splitlines(keepends=True):
        header = _DIFF_HEADER.match(line.rstrip("\n"))
        if header:
            if path is not None:
                sections.append((path, "".join(lines)))
            path = header.group("b")
            lines = [line]
        elif path is not None:
            lines.append(line)
    if path is not None:
        sections.append((path, "".join(lines)))
    return sections


def _rebalance_patches(fix_patch: str, test_patch: str) -> tuple[str, str]:
    sections = _diff_sections(test_patch)
    if not sections:
        return fix_patch, test_patch
    if "".join(text for _, text in sections) != (test_patch or ""):
        return fix_patch, test_patch

    moved = [text for path, text in sections if not _is_test_path(path)]
    if not moved:
        return fix_patch, test_patch

    kept = [text for path, text in sections if _is_test_path(path)]
    fix = fix_patch or ""
    if fix and not fix.endswith("\n"):
        fix += "\n"
    return fix + "".join(moved), "".join(kept)


if not _Dataset.__dict__.get("_jest_era2_build_patched", False):
    _jest_era2_orig_build = _Dataset.build.__func__

    def _jest_era2_build(cls, pr, report):
        ds = _jest_era2_orig_build(cls, pr, report)
        if pr.org == "jestjs" and pr.repo == "jest" and pr.number >= _ERA2_MIN_PR:
            ds.fix_patch, ds.test_patch = _rebalance_patches(
                pr.fix_patch, pr.test_patch
            )
        return ds

    _Dataset.build = classmethod(_jest_era2_build)
    _Dataset._jest_era2_build_patched = True


class JestEra2ImageBase(Image):
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
        return _node_image(self.pr.number)

    def image_tag(self) -> str:
        return _base_tag(self.pr.number)

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        repo = self.pr.repo
        org = self.pr.org
        node_image = self.dependency()
        repo_url = f"https://github.com/{org}/{repo}.git"

        attributes_fix = ""
        if node_image in _OLD_GIT_NODE_IMAGES:
            attributes_fix = (
                f"\nRUN echo '* -text' > /home/{repo}/.git/info/attributes"
                f" && git -C /home/{repo} checkout -f HEAD\n"
            )

        build_args = (
            f"{DockerfileEnhancer._TARGETARCH_ARG}\n"
            f'ARG REPO_URL="{repo_url}"\n'
            f"ARG BASE_COMMIT\n"
            f"\n{DockerfileEnhancer._PROXY_ARGS}"
        )
        labels = (
            f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
            f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}


FROM {node_image}

{build_args}

{DockerfileEnhancer._ENV_BLOCK}

{labels}

{DockerfileEnhancer._CERT_SYMLINKS}

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}
{attributes_fix}
CMD ["/bin/bash"]
"""


class JestImagePR(JestImageDefault):
    def dependency(self) -> Image:
        return JestEra2ImageBase(self.pr, self._config)

    def dockerfile(self) -> str:
        rendered = super().dockerfile()
        kept = [
            line
            for line in rendered.split("\n")
            if not line.lstrip().startswith("#")
            or line.lstrip().startswith("# syntax=")
        ]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(kept))

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        test_cmd = _test_cmd(self.pr.number)
        build = _build_step(self.pr.number)

        inherited = {f.name: f for f in super().files()}

        fix_patch, test_patch = _rebalance_patches(
            self.pr.fix_patch, self.pr.test_patch
        )

        return [
            File(".", "fix.patch", fix_patch),
            File(".", "test.patch", test_patch),
            inherited["check_git_changes.sh"],
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e
export CI=true

cd /home/{repo}
test "$(git rev-parse HEAD)" = "{sha}"
echo "prepare: HEAD pinned at {sha}"

{install}
{link}
{build_soft}

npm config delete before || true
""".format(
                    repo=repo,
                    sha=sha,
                    install=_INSTALL.rstrip("\n"),
                    link=_LINK.rstrip("\n"),
                    build_soft=_BUILD.rstrip("\n") + " || true",
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
{reset}{install}{link}{build}{test_cmd}""".format(
                    repo=repo,
                    reset=_RESET,
                    install=_INSTALL,
                    link=_LINK,
                    build=build,
                    test_cmd=test_cmd,
                ),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
{reset}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply of test.patch failed" >&2
    exit 1
fi
{install}{link}{build}{test_cmd}""".format(
                    repo=repo,
                    reset=_RESET,
                    install=_INSTALL,
                    link=_LINK,
                    build=build,
                    test_cmd=test_cmd,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
{reset}
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply of test.patch + fix.patch failed" >&2
    exit 1
fi
{install}{link}{build}{test_cmd}""".format(
                    repo=repo,
                    reset=_RESET,
                    install=_INSTALL,
                    link=_LINK,
                    build=build,
                    test_cmd=test_cmd,
                ),
            ),
        ]


@Instance.register("jestjs", "jest_4506_to_3217")
class Jest_4506_to_3217(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return JestImagePR(self.pr, self._config)

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
        return _parse_jest_log(test_log)
