import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_TEST_CMD = 'npx nyc mocha "test/**/*-test.js" --reporter spec --exit --timeout 20000'

_INSTALL_CMD = "npm install --no-audit --no-fund --ignore-scripts"

_DROP_NODE_SHIM = "rm -f node_modules/.bin/node node_modules/.bin/node.cmd"


def _runner(repo: str, apply_block: str) -> str:
    return """#!/bin/bash
set -eo pipefail

export CI=true
export NODE_ENV=test

cd /home/{repo}

{apply_block}{install_cmd}
{drop_node_shim}

{test_cmd}
""".format(
        repo=repo,
        apply_block=apply_block,
        install_cmd=_INSTALL_CMD,
        drop_node_shim=_DROP_NODE_SHIM,
        test_cmd=_TEST_CMD,
    )


def _apply(patch_path: str) -> str:
    return """if ! git apply --whitespace=nowarn {patch}; then
    if ! git apply --3way --whitespace=nowarn {patch}; then
        echo "PATCH_APPLY_FAILED: {patch}"
        find . -name '*.rej' -print -exec cat {{}} \\;
        exit 1
    fi
fi
""".format(patch=patch_path)


class ReactJsonViewImageBase(Image):
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
        return "node:14-bullseye"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        label = (
            f'LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        base_hardening = Image._HARDENING_BLOCK.rstrip("\n")

        runtime_env = (
            'ENV npm_config_loglevel="warn" \\\n'
            '    npm_config_audit="false" \\\n'
            '    npm_config_fund="false" \\\n'
            '    CI="true" \\\n'
            '    NODE_ENV="test" \\\n'
            '    PUPPETEER_SKIP_CHROMIUM_DOWNLOAD="1" \\\n'
            '    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD="1"'
        )

        sections = [
            DockerfileEnhancer.SYNTAX_DIRECTIVE,
            f"FROM {image_name}",
            (
                "ARG TARGETARCH\n"
                f'ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"\n'
                "ARG BASE_COMMIT\n"
                "\n"
                f"{DockerfileEnhancer._PROXY_ARGS}"
            ),
            DockerfileEnhancer._ENV_BLOCK,
            label,
            runtime_env,
            DockerfileEnhancer._CERT_SYMLINKS,
            "WORKDIR /home/",
            code,
            f"WORKDIR /home/{self.pr.repo}",
            "RUN git reset --hard",
            "RUN git checkout ${BASE_COMMIT}",
            base_hardening,
            'CMD ["/bin/bash"]',
        ]
        return "\n\n".join(sections) + "\n"


class ReactJsonViewImageDefault(Image):
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
        return ReactJsonViewImageBase(self.pr, self._config)

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
                "check_git_changes.sh",
                """#!/bin/bash
set -e
cd /home/{repo}
if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "check_git_changes: /home/{repo} is not a git repository" >&2
    exit 1
fi
if [ -n "$(git status --porcelain)" ]; then
    echo "check_git_changes: working tree is dirty:" >&2
    git status --porcelain >&2
    exit 1
fi
echo "check_git_changes: clean at $(git rev-parse HEAD)"
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}

step() {{
    label="$1"; shift
    echo "===== prepare: ${{label}} ====="
    if ! "$@"; then
        echo "prepare: FAILED at '${{label}}' -- aborting the image build." >&2
        exit 1
    fi
}}

reset_to_base() {{
    git reset --hard
    git clean -fdq
    bash /home/check_git_changes.sh
    test "$(git rev-parse HEAD)" = "{sha}"
}}

reset_to_base

step "apply fix.patch (cache warm)" git apply --whitespace=nowarn /home/fix.patch
step "npm install (fix era, cache warm)" {install_cmd}

reset_to_base
step "npm install (base era)" {install_cmd}
{drop_node_shim}

reset_to_base

test -d node_modules
test -x node_modules/.bin/mocha
test -x node_modules/.bin/nyc
test ! -e node_modules/.bin/node
echo "===== prepare: done ====="
""".format(
                    repo=self.pr.repo,
                    sha=self.pr.base.sha,
                    install_cmd=_INSTALL_CMD,
                    drop_node_shim=_DROP_NODE_SHIM,
                ),
            ),
            File(".", "run.sh", _runner(self.pr.repo, "")),
            File(
                ".",
                "test-run.sh",
                _runner(self.pr.repo, _apply("/home/test.patch")),
            ),
            File(
                ".",
                "fix-run.sh",
                _runner(
                    self.pr.repo,
                    _apply("/home/test.patch") + _apply("/home/fix.patch"),
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        file_names = " ".join(file.name for file in self.files())
        copy_command = f"COPY {file_names} /home/"
        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

WORKDIR /home/{self.pr.repo}

{copy_command}

RUN bash /home/prepare.sh

CMD ["/bin/bash"]
"""


@Instance.register("mac-s-g", "react-json-view")
class ReactJsonView(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ReactJsonViewImageDefault(self.pr, self._config)

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

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        epilogue = re.search(
            r"^\s*\d+\s+(?:passing|failing|pending)\b", clean_log, re.MULTILINE
        )
        tree = clean_log[: epilogue.start()] if epilogue else clean_log

        path: list[str] = []
        indent_to_level: dict[int, int] = {}

        for raw_line in tree.splitlines():
            line = raw_line.rstrip()
            if not line.strip():
                continue
            if re.match(r"^\s+at\s", line):
                continue

            match = re.match(
                r"^(\s+)(✓|✔|√|-|\d+\))?\s*(\S.*?)(?:\s+\(\d+m?s\))?$", line
            )
            if not match:
                continue

            spaces, marker, name = match.groups()
            name = name.strip()
            if not name:
                continue

            indent = len(spaces)
            if indent not in indent_to_level:
                if not indent_to_level:
                    indent_to_level[indent] = 0
                else:
                    shallower = sorted(i for i in indent_to_level if i < indent)
                    indent_to_level[indent] = (
                        indent_to_level[shallower[-1]] + 1 if shallower else 0
                    )
            level = indent_to_level[indent]

            if marker is None:
                path = path[:level]
                path.append(name)
                continue

            full_name = ":".join(path[:level] + [name])
            if marker in ("✓", "✔", "√"):
                passed_tests.add(full_name)
            elif marker == "-":
                skipped_tests.add(full_name)
            else:
                failed_tests.add(full_name)

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
