import re
from dataclasses import asdict, dataclass
from json import JSONDecoder
from typing import Generator, Optional, Union

from dataclasses_json import dataclass_json

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
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
        return "node:16"

    def image_tag(self) -> str:
        return "base28313to24870"

    def workdir(self) -> str:
        return "base28313to24870"

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

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"

{DockerfileEnhancer._PROXY_ARGS}

{self.global_env}

{DockerfileEnhancer._ENV_BLOCK}
ENV LC_ALL=C.UTF-8

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

WORKDIR /home/

{code}

RUN sed -i 's/http:\/\/deb.debian.org/http:\/\/archive.debian.org/g' /etc/apt/sources.list && \
    sed -i 's/http:\/\/security.debian.org/http:\/\/archive.debian.org/g' /etc/apt/sources.list && \
    sed -i '/updates/d' /etc/apt/sources.list && \
    apt-get update && apt-get install -y --allow-unauthenticated ca-certificates git jq

RUN git config --global --add safe.directory '*'

# Light hardening only: keep FULL history (gc off) so every PR base.sha can be
# checked out; the PR layer does the strict per-sha strip.
WORKDIR /home/{self.pr.repo}
RUN git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
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
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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

yarn install || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
yarn run test:unit --reporter json  --exit

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude='docs/*' --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.pdf' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='yarn.lock' --exclude='package-lock.json' --exclude='pnpm-lock.yaml' /home/test.patch
yarn run test:unit --reporter json  --exit

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude='docs/*' --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.pdf' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='yarn.lock' --exclude='package-lock.json' --exclude='pnpm-lock.yaml' /home/test.patch /home/fix.patch
yarn run test:unit --reporter json  --exit

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

        prepare_commands = "RUN bash /home/prepare.sh"

        # Strict anti-reward-hack hardening at the PR layer with this PR's LITERAL
        # base.sha (shared base keeps full history; each PR strips its own image).
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{self.pr.repo}
{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("mui", "material-ui_28313_to_24870")
class MaterialUi28313to24870(Instance):
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
        return _parse_mocha_log(test_log)


def _parse_mocha_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    @dataclass_json
    @dataclass
    class MaterialUiStats:
        suites: int
        tests: int
        passes: int
        pending: int
        failures: int
        start: str
        end: str
        duration: int

        @classmethod
        def from_dict(cls, d: dict) -> "TestResult":
            return cls(**d)

        @classmethod
        def from_json(cls, json_str: str) -> "TestResult":
            return cls.from_dict(cls.schema().loads(json_str))

        def dict(self) -> dict:
            return asdict(self)

        def json(self) -> str:
            return self.to_json(ensure_ascii=False)

    @dataclass_json
    @dataclass
    class MaterialUiTest:
        title: str
        fullTitle: str
        currentRetry: int
        err: dict
        file: Optional[str] = None
        duration: Optional[int] = None
        speed: Optional[str] = None

        @classmethod
        def from_dict(cls, d: dict) -> "TestResult":
            return cls(**d)

        @classmethod
        def from_json(cls, json_str: str) -> "TestResult":
            return cls.from_dict(cls.schema().loads(json_str))

        def dict(self) -> dict:
            return asdict(self)

        def json(self) -> str:
            return self.to_json(ensure_ascii=False)

    @dataclass_json
    @dataclass
    class MaterialUiInfo:
        stats: MaterialUiStats
        tests: list[MaterialUiTest]
        pending: list[MaterialUiTest]
        failures: list[MaterialUiTest]
        passes: list[MaterialUiTest]

        @classmethod
        def from_dict(cls, d: dict) -> "MaterialUiInfo":
            return cls(**d)

        @classmethod
        def from_json(cls, json_str: str) -> "MaterialUiInfo":
            return cls.from_dict(cls.schema().loads(json_str))

        def dict(self) -> dict:
            return asdict(self)

        def json(self) -> str:
            return self.to_json(ensure_ascii=False)

    def extract_json_objects(
        text: str, decoder=JSONDecoder()
    ) -> Generator[dict, None, None]:
        pos = 0
        while True:
            match = text.find("{", pos)
            if match == -1:
                break
            try:
                result, index = decoder.raw_decode(text[match:])
                yield result
                pos = match + index
            except ValueError:
                pos = match + 1

    if "Building new" in test_log:
        test_log = test_log[test_log.find("Building new", 0) :]

    re_removes = [
        re.compile(r"error Command failed with exit code \d+\.", re.DOTALL),
    ]
    for re_remove in re_removes:
        test_log = re_remove.sub("", test_log)

    original_log = test_log
    test_log = test_log.replace("\r\n", "")
    test_log = test_log.replace("\n", "")

    for obj in extract_json_objects(test_log):
        try:
            info = MaterialUiInfo.from_dict(obj)
        except (KeyError, TypeError):
            continue
        for test in info.passes:
            test_id = f"{test.file}:{test.fullTitle}" if test.file else test.fullTitle

            passed_tests.add(test_id)
        for test in info.failures:
            test_id = f"{test.file}:{test.fullTitle}" if test.file else test.fullTitle

            failed_tests.add(test_id)
        for test in info.pending:
            test_id = f"{test.file}:{test.fullTitle}" if test.file else test.fullTitle

            skipped_tests.add(test_id)

    for test in failed_tests:
        if test in passed_tests:
            passed_tests.remove(test)
        if test in skipped_tests:
            skipped_tests.remove(test)

    for test in skipped_tests:
        if test in passed_tests:
            passed_tests.remove(test)

    if not passed_tests and not failed_tests and not skipped_tests:
        clean_log = re.sub(r'\x1b\[[0-9;]*m', '', original_log)

        vitest_match = re.search(
            r"Tests\s+(\d+)\s+failed\s*\|\s*(\d+)\s+passed(?:\s*\|\s*(\d+)\s+skipped)?",
            clean_log,
        )
        if not vitest_match:
            vitest_match = re.search(
                r"Tests\s+(\d+)\s+passed(?:\s*\|\s*(\d+)\s+skipped)?",
                clean_log,
            )
            if vitest_match:
                vp = int(vitest_match.group(1) or 0)
                vs = int(vitest_match.group(2) or 0)
                for i in range(vp):
                    passed_tests.add(f"vitest_pass_{i}")
                for i in range(vs):
                    skipped_tests.add(f"vitest_skip_{i}")
        else:
            vf = int(vitest_match.group(1) or 0)
            vp = int(vitest_match.group(2) or 0)
            vs = int(vitest_match.group(3) or 0)
            if vp > 0 or vf > 0:
                for i in range(vp):
                    passed_tests.add(f"vitest_pass_{i}")
                for i in range(vf):
                    failed_tests.add(f"vitest_fail_{i}")
                for i in range(vs):
                    skipped_tests.add(f"vitest_skip_{i}")

        if not passed_tests and not failed_tests:
            dot_pass = re.search(r"(\d+)\s+passing", clean_log)
            dot_fail = re.search(r"(\d+)\s+failing", clean_log)
            dot_pend = re.search(r"(\d+)\s+pending", clean_log)
            dp = int(dot_pass.group(1)) if dot_pass else 0
            df = int(dot_fail.group(1)) if dot_fail else 0
            ds = int(dot_pend.group(1)) if dot_pend else 0
            if dp > 0 or df > 0:
                for i in range(dp):
                    passed_tests.add(f"dot_pass_{i}")
                for i in range(df):
                    failed_tests.add(f"dot_fail_{i}")
                for i in range(ds):
                    skipped_tests.add(f"dot_skip_{i}")

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


# --- Bundle-level number_interval routing keys (all -> MaterialUi28313to24870) ---
# Each bundle's dash-joined number_interval registered so Instance.create()
# resolves f"mui/{number_interval}" to this era class. Fixes routing: records with
# empty/era-name number_interval otherwise fall through to the mui/material-ui fallback.
_BUNDLE_NIS_MATERIALUI28313TO24870 = [
    "24870-24946-25111-25285-25543-25978-26003",
    "25095-26600-26772-28176-28177-28248-28252-28267-28348-28382-28384-28389-28391-28393-28396-28400-28401-28402-28410-28412-28417-28421-28426-28428-28429-28433-28434-28435-28436-28438-28439-28441-28443-28444-28445-28446-28447-28448-28450-28452-28455-28456-28457-28458-28459-28468-28469-28474-28475-28476-28478-28485-28486-28489-28495-28497-28498-28504-28505-28506-28507-28521-28530-28531-28533-28534",
    "26489-28491-29153-29298-29328-29517-29536-29556-29590-29597-29610-29669-29680-29684-29694-29695-29697-29701-29711-29714-29717-29718-29737-29738-29739-29743-29747-29748-29754-29756-29760-29761-29762-29763-29776-29777-29778-29783-29786-29787-29788-29789-29790-29791-29792-29793-29794-29795-29796-29797-29798-29799-29801-29802-29803-29804-29812-29818-29824-29826-29830-29831",
    "27237-27262-27293-27307-27309-27351-27355",
    "27299-31212-31283-31989-32044-32048-32090-32098-32120-32182-32192-32195-32199-32201-32202-32206-32207-32208-32209-32212-32215-32216-32217-32218-32219-32220-32221-32222-32223-32224-32225-32226-32227-32228-32229-32231-32232-32236-32239-32241-32248",
    "27414-27503-27573-27576-27784-28227-28343-28344-28358-28684-28896-28999-29010-29647-32041-32056-32126-32127",
    "27520-29962-30161-30173-30176-30245-30246-30259-30260-30266-30267-30268-30269-30270-30274-30286-30307-30319-30338-30345-30353-30354-30358-30363-30368-30372-30373-30376-30378-30380-30381-30382-30391-30393-30396-30397-30404-30405-30415",
    "27939-34066-34424-34574-34592-34593-34601-34610-34611-34619-34623-34632-34638-34639-34653-34658-34661-34667-34668-34669-34670-34671-34673-34675-34676-34678-34679-34681-34682-34683-34684-34685-34700-34704",
    "28038-29146-29194-29422-29423-29430-29431-29433-29452-29483-29488-29502-29503-29526-29547-29565-29566-29570-29573-29575-29579-29582-29583-29585-29586-29589-29592-29593-29594-29609-29611-29614-29616-29622-29623-29624-29633-29640-29650-29651-29652-29653-29654-29655-29656-29657-29658-29659-29660-29661-29662-29663-29664-29665-29666-29681-29683-29685-29686",
    "28053-28226-28621-28744-28788-28801-28803-28849-28865-28873-28876-28884-28887-28890-28903-28907-28929-28931-28932-28933-28934-28935-28936-28937-28938-28939-28940-28941-28942-28943-28944-28945-28946-28947-28948-28949-28952-28954-28957-28958-28974-28982-28986-28987-28995-29005-29006-29007-29025-29028-29034-29039-29040-29047",
    "28059-28652-28665-28743-28764-28908-28910-28923-28930-28959-28965-29004-29035-29048-29051-29064-29069-29070-29073-29090-29091-29092-29093-29094-29095-29096-29098-29100-29101-29102-29103-29105-29106-29107-29108-29109-29110-29113-29117-29132-29133-29139-29141-29143-29148-29154-29156-29157-29167-29172-29177-29180-29186-29187-29189-29195-29198-29214-29218-29220-29233-29241-29242-29243-29244-29245-29246-29247-29248-29249-29250-29251-29252-29253-29254-29255-29256-29257-29258-29259-29260-29261-29262-29263-29264-29283-29285-29289",
    "28178-28253-28255-28406-28413-28423-28472-28511-28527-28541-28549-28553-28564-28565-28570-28576-28581-28582-28596-28597-28598-28599-28600-28601-28602-28603-28604-28605-28607-28608-28609-28610-28611-28613-28614-28615-28616-28617-28618-28619-28620-28634-28635-28638-28640-28642-28644-28651-28656-28661-28663-28667-28676",
    "28241-30113-30550-30560-30624-30655-30656-30657-30668-30705-30706-30708-30710-30734-30737-30741-30744-30747-30749-30773-30776-30780-30785-30795-30798-30808-30809-30813-30819-30823-30829-30831-30843-30852-30853-30855",
    "28313-28488-28509-28606-28612-28641-28646-28680-28682-28692-28698-28699-28700-28707-28710-28713-28715-28720-28725-28737-28747-28748-28750-28751-28762-28765-28770-28772-28774-28775-28776-28777-28778-28779-28780-28781-28782-28783-28785-28786-28787-28789-28790-28791-28793-28794-28797-28799-28804-28805-28807-28812-28813-28814-28816-28822-28823-28827-28830-28840-28843-28862-28863-28864-28868-28869-28881-28885",
]
for _ni in _BUNDLE_NIS_MATERIALUI28313TO24870:
    Instance.register("mui", _ni)(MaterialUi28313to24870)
