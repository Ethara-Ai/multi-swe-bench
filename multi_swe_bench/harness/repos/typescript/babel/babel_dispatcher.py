import importlib
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ROOT_IMAGE = "node:12-buster"
_NODE8_IMAGE = "node:8-slim"
_NODE_MAJOR = "8"
_SHARED_BASE_TAG = "base"

_ERAS = [
    (4892, 7357, "babel_classic_mocha"),
    (7358, 10852, "babel_classic_jest"),
]


class BabelSharedImageBase(Image):
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
        return _ROOT_IMAGE

    def image_tag(self) -> str:
        return _SHARED_BASE_TAG

    def workdir(self) -> str:
        return _SHARED_BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        sections = [f"FROM {image_name}"]
        if self.global_env:
            sections.append(self.global_env)
        sections.append("WORKDIR /home/")
        sections.append(
            "RUN sed -i 's|deb.debian.org/debian|archive.debian.org/debian|g' /etc/apt/sources.list && \\\n"
            "    sed -i 's|security.debian.org/debian-security|archive.debian.org/debian-security|g' /etc/apt/sources.list && \\\n"
            "    sed -i '/buster-updates/d' /etc/apt/sources.list && \\\n"
            "    apt-get -o Acquire::Check-Valid-Until=false update && \\\n"
            "    apt-get install -y --no-install-recommends --allow-unauthenticated \\\n"
            "    ca-certificates \\\n"
            "    curl \\\n"
            "    git \\\n"
            "    make \\\n"
            "    python \\\n"
            "    python3 \\\n"
            "    xz-utils && \\\n"
            "    rm -rf /var/lib/apt/lists/*"
        )
        sections.append(f'RUN git -C /home clone "${{REPO_URL}}" {self.pr.repo}')
        sections.append(
            f"COPY --from={_NODE8_IMAGE} /usr/local/ /usr/local/\n"
            f"COPY --from={_NODE8_IMAGE} /opt/ /opt/"
        )
        sections.append(
            "RUN set -eux; \\\n"
            f'    test "$(node -p "process.versions.node.split(\'.\')[0]")" = "{_NODE_MAJOR}"; \\\n'
            "    command -v yarn; \\\n"
            f"    test -d /home/{self.pr.repo}/.git; \\\n"
            f"    test -f /home/{self.pr.repo}/package.json"
        )
        if self.clear_env:
            sections.append(self.clear_env)
        sections.append('CMD ["/bin/bash"]')
        return "\n\n".join(sections) + "\n"


def _load_era(label: str):
    module = importlib.import_module(
        f"multi_swe_bench.harness.repos.typescript.babel.{label}"
    )
    return getattr(module, label)


def select_era(number: int):
    for low, high, label in _ERAS:
        if low <= number <= high:
            return _load_era(label)

    known = ", ".join(f"{low}-{high}" for low, high, _l in _ERAS)
    raise ValueError(
        f"babel/babel PR {number} falls outside every verified era ({known}). "
        f"Babel's era ranges overlap (era 3 is 7358-11973, era 4 is 10853-13727, "
        f"era 5 is 11554-16101), so the PR number alone cannot decide the "
        f"toolchain -- the same number can be yarn-classic+jest on one branch and "
        f"yarn-berry on another. Read package.json and .yarnrc.yml at this PR's "
        f"base commit, confirm which era it matches, then add an interval to "
        f"_ERAS."
    )


@Instance.register("babel", "babel")
class BABEL(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config
        self._delegate = select_era(pr.number)(pr, config, *args, **kwargs)

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def delegate(self) -> Instance:
        return self._delegate

    def dependency(self) -> Optional[Image]:
        return self._delegate.dependency()

    def run(self, run_cmd: str = "") -> str:
        return self._delegate.run(run_cmd)

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return self._delegate.test_patch_run(test_patch_run_cmd)

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return self._delegate.fix_patch_run(fix_patch_run_cmd)

    def parse_log(self, test_log: str) -> TestResult:
        return self._delegate.parse_log(test_log)
