import re
from typing import Optional

from multi_swe_bench.harness.image import File, Image
from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.repos.rust.vercel_labs.agent_browser_564_to_99999 import (
    AGENT_BROWSER_564_TO_99999,
)
from multi_swe_bench.harness.repos.rust.vercel_labs.agent_browser_564_to_99999 import (
    ImageDefault as RustEraImageDefault,
)

_TARGET_RE = re.compile(r"^diff --git a/cli/tests/([A-Za-z0-9_]+)\.rs ", re.M)


def _test_targets(test_patch: str) -> list[str]:
    return sorted(set(_TARGET_RE.findall(test_patch or "")))


def _script(repo: str, targets: list[str], apply: str) -> str:
    names = " ".join(targets)
    return f"""#!/bin/bash
set -o pipefail
export CI=true

cd /home/{repo}
{apply}
PROFILE=""
grep -qE '^\\[profile\\.ci\\]' cli/Cargo.toml && PROFILE="--profile ci"
ARGS=""
for t in {names}; do
    if [ -f "cli/tests/$t.rs" ]; then
        ARGS="$ARGS --test $t"
    fi
done
if [ -z "$ARGS" ]; then
    echo "no test.patch integration targets present at this commit"
    exit 0
fi
cargo test $PROFILE --manifest-path cli/Cargo.toml --no-fail-fast $ARGS

"""


class ImageDefaultTestPatchTargets(RustEraImageDefault):
    def files(self) -> list[File]:
        targets = _test_targets(self.pr.test_patch)
        repo = self.pr.repo
        scripts = {
            "run.sh": _script(repo, targets, ""),
            "test-run.sh": _script(
                repo, targets, "git apply --whitespace=nowarn /home/test.patch || exit 1"
            ),
            "fix-run.sh": _script(
                repo,
                targets,
                "git apply --whitespace=nowarn /home/test.patch /home/fix.patch || exit 1",
            ),
        }
        out = []
        for f in super().files():
            if f.name in scripts:
                out.append(File(".", f.name, scripts[f.name]))
            else:
                out.append(f)
        return out


@Instance.register("vercel-labs", "agent_browser_2429_to_2006")
class AGENT_BROWSER_2429_TO_2006(AGENT_BROWSER_564_TO_99999):
    def dependency(self) -> Optional[Image]:
        return ImageDefaultTestPatchTargets(self.pr, self._config)
