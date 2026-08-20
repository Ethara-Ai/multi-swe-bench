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
        return "node:20"

    def image_tag(self) -> str:
        return "base46712to42247"

    def workdir(self) -> str:
        return "base46712to42247"

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

RUN apt update && apt install -y git ca-certificates
RUN npm install -g pnpm@9
RUN apt install -y jq

RUN git config --global --add safe.directory '*'

# Light hardening only: keep FULL history (gc off) so every PR's base.sha can be
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

sed -i 's/packageManager": ".*"/packageManager": "pnpm@^9"/' package.json
jq '.packageManager = "pnpm@^9" | del(.engines)' package.json > temp.json && mv temp.json package.json
pnpm install || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
pnpm test:unit -- --reporter json

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude='docs/*' --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.pdf' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='yarn.lock' --exclude='package-lock.json' --exclude='pnpm-lock.yaml' /home/test.patch
pnpm test:unit -- --reporter json
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude='docs/*' --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.pdf' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='yarn.lock' --exclude='package-lock.json' --exclude='pnpm-lock.yaml' /home/test.patch /home/fix.patch
pnpm test:unit -- --reporter json
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


@Instance.register("mui", "material-ui_46712_to_42247")
class MaterialUi46712to42247(Instance):
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


# --- Bundle-level number_interval routing keys (all -> MaterialUi46712to42247) ---
# Each bundle's dash-joined number_interval registered so Instance.create()
# resolves f"mui/{number_interval}" to this era class. Fixes routing: records with
# empty/era-name number_interval otherwise fall through to the mui/material-ui fallback.
_BUNDLE_NIS_MATERIALUI46712TO42247 = [
    "42428-43947-44014-44019-44068-44080-44093-44095-44097-44131-44132-44150-44167-44170-44183-44184-44187-44188-44193-44199-44200-44202-44204-44207-44208-44209-44216-44217-44218-44219-44220-44221-44222-44223-44224-44225-44226-44227-44228-44229-44230-44231-44232-44233-44234-44235-44236-44248-44256-44268-44276",
    "42453-42456-42462-42463-42496-42509-42511-42513-42535-42538-42570-42571-42608-42613-42617",
    "42494-43022-43555-43709-43711-43744-43805-43809-43812-43814-43818-43820-43822-43825-43828-43829-43831-43832-43833-43836-43837-43838-43841-43842-43843-43844-43846-43847-43848-43849-43850-43851-43852-43853-43854-43860-43861-43862-43864-43865-43866-43867-43868-43870-43881-43889-43890-43895-43911-43913-43914-43915-43918-43919-43920-43921-43922-43923-43924-43926-43928-43929-43930-43931-43932-43935-43937-43946-43948-43949-43951-43956-43957",
    "42669-42677-42681-42696-42700-42708-42709-42745-42763-42766-42769-42776",
    "42713-42846-42847-42849-42850-42851-42852-42853-42854-42855-42856-42888-42897",
    "42806-42808-42813-42820-42829-42834-42838",
    "42899-42973-43079-43117-43246-43360-43569-43627-43656-43662-43664-43671-43680-43682-43694-43712-43713-43717-43723-43725-43726-43729-43734-43735-43739-43740-43742-43743-43745-43747-43748-43751-43752-43754-43756-43757-43759-43760-43761-43762-43763-43764-43765-43766-43767-43768-43769-43770-43771-43772-43773-43774-43775-43776-43777-43778-43779-43780-43781-43785-43788-43789-43793-43794-43799-43802",
    "42917-42947",
    "42984-44790-45246-45426-45478-45483-45584-45596-45622-45629-45670-45671-45722-45776-45812-45813-45825-45852-45857-45860-45863-45864-45866-45871-45872-45880-45883-45887-45888-45889-45890-45891-45892-45893-45894-45895-45896-45897-45898-45899-45900-45901-45902-45903-45905-45906-45912-45914-45915-45916-45917-45920-45923-45924-45925-45926-45927-45929-45930-45931-45935-45937-45941-45942-45944-45945-45947-45951-45952-45953-45954-45955-45956-45957-45958-45959-45960-45961-45962-45963-45964-45965-45966-45967-45968-45969-45970-45971-45980-45986-45996-45998-46000-46003-46004-46006-46007-46008-46009-46010-46011-46012-46013-46014-46015-46016-46017-46018-46019-46020-46021-46022-46023-46024-46025-46029-46033-46034-46039-46044-46046-46048-46051-46053-46054-46055-46064-46066-46067-46068-46069-46070-46071-46072-46073-46074-46075-46076-46079-46080-46082-46083-46084-46085-46086-46087",
    "42987-43783-44315-44337-44350-44377-44382-44383-44400-44410-44412-44416-44419-44421-44422-44424-44425-44430-44431-44432-44433-44434-44435-44436-44437-44438-44439-44440-44441-44442-44443-44445-44446-44447-44448-44449-44453-44454-44455-44461-44473-44474-44478-44481",
    "43059-43064-43105-43112-43114-43121",
    "43140-43143-43233-43239",
    "43272-43397-43435-43446-43447-43564-43566-43585-43753-43755-44475-44487-44494-44543-44550-44557-44561-44574-44577-44578",
    "43402-43412-43733-43791-43801-43834-43835-43856-43873-43879-43904-43916-43927-43933-43939-43945-43950-43959-43961-43963-43967-43968-43985-43987-43993-43995-43999-44000-44001-44003-44004-44005-44006-44007-44008-44010-44011-44012-44013-44015-44016-44017-44023-44024-44026-44027-44028-44029-44032-44034-44035-44036-44038-44043-44050",
    "43526-43625-44137-44191-44198-44253-44260-44275-44277-44281-44282-44288-44289-44292-44295-44296-44297-44298-44299-44300-44301-44302-44303-44304-44305-44306-44307-44308-44309-44310-44312-44313-44314-44316-44317-44330-44338-44339-44340-44345-44352-44353-44356-44357-44358-44359-44360-44361-44362-44363-44364-44365-44366-44367-44368-44369-44370-44372-44374-44375-44376-44386-44388-44390-44393-44397",
    "43530-44135-44792-44812-44870-44914-44946-44989-45009-45021-45022-45025-45026-45030-45036-45057-45058-45060-45061-45064-45070-45077",
    "43576-44538-44664-44682-44701-44703-44714-44731-44737-44743-44744-44747-44752-44753-44761-44762-44763-44764-44766-44767-44768-44769-44770-44771-44772-44773-44774-44775-44776-44784-44786-44787",
    "43708-43731-43982-43994-44074-44081-44082-44083-44084-44085-44086-44087-44088-44089-44090-44091-44092-44098-44106-44111-44115-44118-44119-44121-44122-44124-44125-44136-44139-44143-44148-44155-44156-44157-44158-44159-44160-44161-44162-44163-44164-44165-44166-44168-44171-44174-44176-44178",
    "43714-43796-44009-44018-44021-44046-44049-44051-44052-44056-44058-44059-44062-44065-44069-44070-44075-44076-44094-44099-44103-44104",
    "43903-44040-44325-46887-46969-46996-47075-47111-47113-47131-47152-47159-47160-47161-47162-47163-47165-47171-47176-47177-47178-47179-47182-47183-47185-47186-47187-47188-47189-47192-47193-47194-47199-47200-47201-47207-47208-47209-47210-47214-47217-47218-47219-47220-47221-47222-47223-47224-47225-47229-47233-47235-47242-47249-47251-47252-47257-47258-47261-47271-47272-47273-47274-47275-47276-47277-47279-47280-47281-47282-47283-47284-47314-47324-47328-47339-47342-47344-47345-47347-47348-47349-47350-47351-47352-47353-47354-47358-47359-47360-47361-47362-47363-47366-47367-47370-47373-47378-47380-47382-47383-47384-47392-47395-47396-47397-47398-47399-47400-47401-47403-47405-47406-47409",
    "43942-44267-45632-45789-46182-46258-46323-46376-46382-46405-46414-46421-46438-46440-46441-46442-46443-46444-46445-46446-46447-46448-46449-46450-46451-46452-46453-46454-46455-46456-46457-46459-46463-46466-46470-46473-46474-46475-46476-46480-46482-46483-46485-46489-46490-46491-46494-46505-46506-46508-46511-46512-46513-46514-46515-46516-46517-46518-46519-46520-46523-46524-46525-46526-46529-46530-46531-46532-46534-46535-46537-46538-46539-46540-46542-46544-46546-46551-46552-46557-46558-46561-46563-46564-46565-46566-46567-46568-46569-46570-46571-46572-46573-46574-46575-46576-46579-46584-46588-46598-46599-46600-46601-46602-46603-46604-46605-46606-46612-46617-46618-46619-46620-46621-46625-46630-46637-46638-46640-46642-46643-46644-46645-46646-46647-46648-46649-46650-46651-46652-46654-46655-46658-46659-46660-46661",
    "44020-44195-44318-44371-44420-44444-44451-44462-44466-44467-44476-44479-44480-44484-44489-44490-44498-44502-44513-44514-44515-44517-44518-44519-44520-44521-44522-44523-44524-44525-44526-44528-44529-44530-44533-44541-44551-44559-44560-44562-44565-44566-44567",
    "44290-44403-44486-44516-44531-44535-44536-44552-44581-44585-44586-44587-44588-44591-44593-44598-44599-44605-44606-44607-44608-44609-44610-44611-44612-44613-44614-44615-44616-44617-44618-44619-44629-44630-44631-44632-44634-44636-44638-44639-44645",
    "44426-44678-44728-44789-44805-44820-44827-44829-44832-44833-44846-44848-44849-44852-44856-44858-44864-44867-44868-44875-44878-44879-44880-44881-44882-44883-44884-44885-44887-44888-44889-44890-44891-44892-44893-44894-44895-44896-44916-44925",
    "44540-44873-44976-45050-45079-45081-45083-45100-45133-45138",
    "44637-44729-44795-44809-44861-44862-44876-44877-44909-44919-44927-44928-44933-44934-44935-44937-44938-44941-44942-44943-44944-44945-44948-44953-44956-44957-44959-44969-44971-44975-44979-44980-44985-44992-44994-44995-44996-44997-44998-44999-45000-45001-45002-45003-45004-45005-45006-45007-45008-45010-45013-45017-45023",
    "44720-44735-44757-44785",
    "44746-44871-44930-45387-45418-45621-45727-45734-45736-45737-45738-45739-45740-45741-45742-45743-45744-45745-45746-45747-45748-45750-45751-45752-45753-45754-45755-45756-45758-45760-45761-45762-45763-45768-45773-45778-45779-45782-45783-45784-45793-45794-45798-45799-45801-45803-45806-45808-45817-45819-45820-45821-45822-45823-45824-45826-45827-45828-45829-45830-45831-45832-45835-45840-45841-45843-45846-45848-45850",
    "44811-44815-44853-44855",
    "44857-44954-44955",
    "45045-45688-45691-45692-45693-45694-45696-45701-45704-45706-45708-45709-45711-45714-45715-45716-45718-45721-45726",
    "45129-45217-45219-45221-45223-45226-45242-45282-45286",
    "45191-45194-45200-45209",
    "45231-46416-46837-47072-47140-47215-47364-47385-47407-47410-47411-47414-47416-47417-47420-47422-47427-47428-47429-47430-47431-47432-47433-47434-47435-47436-47437-47439-47440-47441-47442-47446-47448-47449-47450-47453-47455-47457-47459-47460-47463-47464-47466-47467-47470-47471-47473-47474-47475-47476-47477-47478-47479-47480-47481-47482-47483-47484-47486-47487-47491-47493-47496-47499-47503-47504-47508-47509-47511-47512-47517-47518-47519-47520-47521-47523-47524-47525-47526-47528-47529-47530-47531-47532-47533-47534-47535-47536-47537-47538-47539-47540-47541-47542-47543-47544-47546-47547-47548-47554-47557-47558-47559-47560-47562-47565-47566-47568-47569-47570-47571-47573-47574-47578",
    "45238-45357-45359-45604-45605",
    "45240-45508-45607-45616-45620-45628-45635-45636-45668-45676",
    "45265-45839-45909-45977-45990-45992-45999-46002-46047-46057-46058-46061-46065-46090-46098-46099-46100-46103-46108-46111-46112-46113-46114-46115-46116-46117-46118-46119-46120-46121-46122-46123-46124-46125-46126-46127-46129-46135-46141-46144-46145-46149-46151-46154-46155-46159-46160-46161-46162-46163-46164-46165-46166-46167-46168-46169-46170-46171-46172-46173-46174-46175-46176-46177-46178-46185-46187-46196-46198-46199-46200-46201-46202-46203-46204-46205-46206-46207-46208-46209-46210-46211-46212-46213-46214-46215-46221-46222-46227-46228-46229-46230-46237",
    "45292-45303-45336-45339-45342-45349",
    "45337-45338-45344-45352-45355-45356-45361-45364-45365-45366-45367-45368-45369-45372-45377-45392-45393-45403-45409-45412-45416",
    "45398-45430-45437-45440-45475-45485",
    "45481-45493-45496-45498-45507-45511-45534-45536-45551-45552-45553-45560-45563-45564-45570-45571-45573-45574",
    "45524-46318-46873-46915-47248-47445-47469-47579-47581-47584-47587-47588-47590-47593-47594-47595-47596-47597-47598-47599-47600-47605-47607-47608-47609-47610-47612-47614-47615-47617-47619-47620-47621-47623-47624-47625-47626-47629-47632-47633-47634-47635-47638-47639-47640-47641-47642-47643-47644-47645-47646-47647-47648-47649-47650-47654-47655-47656-47660-47662-47663-47667-47668-47669-47673-47674-47675-47676-47677-47678-47679-47680-47681-47682-47686-47690-47692-47696-47697-47698-47702-47705-47706-47707-47709-47710-47711-47712-47718-47727-47728-47729-47732-47737-47739-47741-47742-47743-47745-47746-47753-47754",
    "45669-45723-45733-45735-45769",
    "45770-45814-45838-45842-45845-45851-45855-45858-45859",
    "45810-45865-45870-45874-45875-45876-45877-45886-45928-45938-45948-45973-46038-46153-46184-46238",
    "45972-45991-46320-46432",
    "46283-46335-46357-46431",
    "46371-46380",
    "46408-46467-46522-46595-46614-46633-46634-46656-46657-46668-46676-46677-46679-46680-46683-46685-46690-46691-46693-46695-46696-46697-46698-46699-46700-46701-46702-46703-46704-46705-46706-46710-46711-46713-46714-46715-46719-46720-46721-46722-46726-46727-46730-46731-46732-46733-46735-46737-46738-46740-46742-46745-46746-46747-46748-46749-46752-46754-46756-46757-46759-46760-46761-46762-46763-46764-46765-46766-46767-46768-46769-46770-46771-46780-46781-46783-46784-46786-46787-46793-46794-46796-46797-46800-46801-46802-46803-46804-46805-46806-46807-46808-46809-46810-46811-46812-46813-46814-46815-46816-46817-46818-46819-46823-46824-46831-46832-46833-46835-46843-46844-46845-46847-46848-46849-46852-46853",
    "46528-46653-46666-46672-46674",
    "46755-46772-46795-46834-46841-46851-46854-46855-46856-46860-46861-46865-46866-46868-46869-46870-46871-46872-46874-46875-46876-46877-46878-46879-46880-46881-46882-46883-46884-46885-46886-46889-46892-46894-46896-46898-46899-46902-46903-46905-46907-46909-46916-46920-46921-46922-46923-46924-46927-46933-46934-46935-46936-46937-46939-46940-46941-46943-46944-46948-46951-46954-46956-46957-46958-46959-46960-46961-46962-46967-46977-46983-46987-46988-46989-46990-46991-46992-46993-46994-46995-47000-47002-47003-47005-47006-47009",
]
for _ni in _BUNDLE_NIS_MATERIALUI46712TO42247:
    Instance.register("mui", _ni)(MaterialUi46712to42247)
