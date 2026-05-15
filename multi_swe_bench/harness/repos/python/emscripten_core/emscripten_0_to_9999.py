import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

class ImageBase(Image):
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
        return "ubuntu:18.04"

    def image_tag(self) -> str:
        return "base-0_to_9999"

    def workdir(self) -> str:
        return "base-0-to-9999"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f'RUN git clone "https://github.com/{self.pr.org}/{self.pr.repo}.git" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}


WORKDIR /home/

RUN apt-get update && \\
    apt-get install -y --no-install-recommends \\
    ca-certificates curl gnupg \\
    build-essential git make cmake \\
    python python3 python-pip \\
    xz-utils \\
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://nodejs.org/dist/v14.21.3/node-v14.21.3-linux-arm64.tar.xz | tar -xJ -C /usr/local --strip-components=1

RUN git clone https://github.com/emscripten-core/emsdk.git /emsdk && \\
    cd /emsdk && \\
    ./emsdk install latest && \\
    ./emsdk activate latest && \\
    rm -rf /emsdk/emscripten

ENV PATH="/emsdk:/emsdk/upstream/emscripten:/emsdk/upstream/bin:/emsdk/node/current/bin:${{PATH}}"
ENV EMSDK="/emsdk"
ENV EM_CONFIG="/emsdk/.emscripten"

{code}


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

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", """#!/bin/bash
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
"""),
            File(".", "prepare.sh", f"""#!/bin/bash
set -e
cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh
NODE_BIN=$(find /emsdk/node -name node -type f 2>/dev/null | head -1)
# Create llc symlink (old emscripten checks for it)
ln -sf /emsdk/upstream/bin/clang /emsdk/upstream/bin/llc 2>/dev/null || true
cat > /home/{repo}/.emscripten << 'EMCONFIG'
import os
EMSCRIPTEN_ROOT = '/home/{repo}'
LLVM_ROOT = '/emsdk/upstream/bin'
BINARYEN_ROOT = '/emsdk/upstream'
NODE_JS = '/usr/local/bin/node'
COMPILER_ENGINE = NODE_JS
JS_ENGINES = [NODE_JS]
EMCONFIG
npm install || true
""".format(repo=repo, sha=sha)),
            File(".", "run.sh", f"""#!/bin/bash
set -eo pipefail
export EM_CONFIG=/home/{repo}/.emscripten
cd /home/{repo}
python tests/runner.py other
"""),
            File(".", "test-run.sh", f"""#!/bin/bash
set -eo pipefail
export EM_CONFIG=/home/{repo}/.emscripten
cd /home/{repo}
git apply --whitespace=nowarn --reject /home/test.patch || true
python tests/runner.py other
"""),
            File(".", "fix-run.sh", f"""#!/bin/bash
set -eo pipefail
export EM_CONFIG=/home/{repo}/.emscripten
cd /home/{repo}
git apply --whitespace=nowarn --reject /home/test.patch || true
git apply --whitespace=nowarn --reject /home/fix.patch || true
python tests/runner.py other
"""),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())
        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""

@Instance.register("emscripten-core", "emscripten_0_to_9999")
class EMSCRIPTEN_0_TO_9999(Instance):
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
        re_unittest = re.compile(
            r"^(\S+.*?)[ \t]+\.\.\.[ \t]+(ok|FAIL|ERROR|skipped\b)", re.MULTILINE
        )
        # Resolve same-name collisions by precedence FAIL > PASS > SKIP.
        priority = {"FAIL": 3, "PASS": 2, "SKIP": 1}
        seen: dict[str, str] = {}
        for match in re_unittest.finditer(test_log):
            test_name = match.group(1).strip()
            raw = match.group(2)
            if raw == "ok":
                outcome = "PASS"
            elif raw in ("FAIL", "ERROR"):
                outcome = "FAIL"
            else:
                outcome = "SKIP"
            prev = seen.get(test_name)
            if prev is None or priority[outcome] > priority[prev]:
                seen[test_name] = outcome
        passed_tests = {n for n, s in seen.items() if s == "PASS"}
        failed_tests = {n for n, s in seen.items() if s == "FAIL"}
        skipped_tests = {n for n, s in seen.items() if s == "SKIP"}
        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
