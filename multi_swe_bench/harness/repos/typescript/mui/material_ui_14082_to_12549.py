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
        return "node:10"

    def image_tag(self) -> str:
        return "base14082to12549"

    def workdir(self) -> str:
        return "base14082to12549"

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
    sed -i '/stretch-updates/d' /etc/apt/sources.list && \
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


@Instance.register("mui", "material-ui_14082_to_12549")
class MaterialUi14082to12549(Instance):
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


# --- Bundle-level number_interval routing keys (all -> MaterialUi14082to12549) ---
# Each bundle's dash-joined number_interval registered so Instance.create()
# resolves f"mui/{number_interval}" to this era class. Fixes routing: records with
# empty/era-name number_interval otherwise fall through to the mui/material-ui fallback.
_BUNDLE_NIS_MATERIALUI14082TO12549 = [
    "12549-12695-12758-12761-12763-12769-12775-12778-12785-12790-12799-12802-12803-12804-12806-12809-12812-12813-12814",
    "12590-12698-12703-12705-12706-12712-12713-12716-12717-12718-12719-12720-12722-12724-12730-12733-12734-12735-12736-12743-12745-12747-12750-12752",
    "12665-13229-14084-14305-14307-14308-14309-14311-14312-14313-14314-14315-14316-14317-14322-14324-14332-14333-14334-14339-14340-14350-14351-14353-14354-14355-14356-14361-14362-14367-14399",
    "12671-12675-12677-12680-12681-12684-12692-12693-12694",
    "12725-12916-12967-12993-13018-13050-13051-13055-13060-13066-13067-13071-13073-13077-13078-13079-13084-13089-13090-13091-13092-13093-13095-13096-13102-13107-13108-13110-13113-13115-13118-13119-13120-13123-13124-13125-13126-13127-13128-13129-13135-13136-13138-13139-13141",
    "12888-12895-12896-12901-12903-12904-12905-12906-12908-12910-12911-12912-12923-12926-12929-12933-12938-12939-12942-12952-12953-12954-12955-12958-12959-12963-12964-12966-12968-12969-12971-12974-12975-12976-12977",
    "12972-12978-12982-12992-13003-13007-13008-13009-13012-13016-13017-13022-13023-13026-13031-13035-13038-13040-13041-13043-13046-13047",
    "13030-13074-13112-13117-13122-13130-13140-13148-13149-13151-13153-13157-13158-13161-13166-13168-13172-13173-13187-13188-13192-13200-13204-13205-13209-13213-13214-13219-13223-13227-13228-13230-13233-13236-13237-13238-13240",
    "13044-13056-13348-13350-13351-13352-13362",
    "13069-13082-13094-13181-13217-13225-13258-13260-13266-13268-13269-13270-13271-13275-13280-13282-13284-13285-13286-13287-13295-13298-13301-13308-13309-13310-13312-13314-13316-13317-13318-13321-13323-13326-13327-13328-13330",
    "13244-13252-13254-13255",
    "13305-13324-13325-13329-13342-13375-13376-13378-13379-13380-13381-13389-13392-13393-13395-13396-13398-13400-13401-13405-13409-13410",
    "13320-13789-13905-14023-14170-14182-14186-14193-14194-14196-14197-14198-14200-14209-14210-14212-14214-14215-14223-14227-14229-14230-14235-14237-14238-14240-14242-14248-14250-14254-14256-14259-14261-14262-14266-14269-14273-14275-14277-14281-14282-14289-14290-14298-14300-14304",
    "13408-13413-13415-13418-13419-13420-13421-13423-13426-13427-13428-13429-13430-13437-13445-13451-13452-13453-13458-13460-13474-13477-13478-13483-13490-13498-13499-13500-13508-13509",
    "13479-13553-13573-13574-13579-13582-13583-13584-13588-13590-13592-13601-13604-13612-13619-13620-13621-13624-13626-13628-13634-13637-13638-13640-13642-13645-13650-13651-13654-13661-13665-13667-13668-13669-13674-13675-13678-13684-13688",
    "13487-13503-13510-13511-13517-13519-13524-13525-13528-13529-13534-13536-13537-13540-13542-13544-13547-13555-13562-13565-13567-13568-13580",
    "13494-13764-13766-13768-13769-13775-13778-13785-13786-13791-13797-13798-13804-13805-13806-13810-13813-13815-13818-13821-13822-13825-13826-13828-13830-13832-13843-13845-13848-13851-13852-13853-13856-13857-13858-13862",
    "13497-13632-13931-13973-13980-13981-13989-13992-13994-13998-13999-14000-14001-14005-14006-14007-14009-14010-14011-14012-14013-14015-14019-14022-14025-14029-14031-14032-14033-14035-14039",
    "13685-13686-13689-13690-13694-13700-13707-13709-13715-13719-13720-13721-13726-13730-13731-13737-13741-13743-13747-13749-13750-13754-13755-13758-13759",
    "13697-13698-13723-13740-13788-13816-13827-13859-13860-13873-13874-13877-13879-13892-13896-13902-13909-13910-13911-13913-13917-13919",
    "13867-13920-13928-13929-13930-13934-13935-13943-13945-13946-13947-13950-13954-13956-13959-13960-13969-13970-13975-13976-13983-13987",
    "13923-14118-14120-14121-14122-14123-14125-14127-14128-14130-14134-14139-14142-14151-14154-14156-14158-14160-14161-14162-14163-14164-14165-14168-14171-14172-14173-14180",
    "14034-14036-14043-14046-14047-14049-14050-14051-14054-14056-14059-14060-14061-14065-14067-14071-14080-14081-14083-14089-14090-14091-14092-14094-14096-14098-14100-14102-14103-14104-14106-14108",
    "14082-14093-14112-14116-14117",
]
for _ni in _BUNDLE_NIS_MATERIALUI14082TO12549:
    Instance.register("mui", _ni)(MaterialUi14082to12549)
