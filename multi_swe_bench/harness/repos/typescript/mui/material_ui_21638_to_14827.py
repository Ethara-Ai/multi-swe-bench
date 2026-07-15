import re
from dataclasses import asdict, dataclass
from json import JSONDecoder
from typing import Generator, Optional, Union

from dataclasses_json import dataclass_json

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
        return "node:12"

    def image_tag(self) -> str:
        return "base21638to14827"

    def workdir(self) -> str:
        return "base21638to14827"

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

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/

{code}

RUN sed -i 's/http:\/\/deb.debian.org/http:\/\/archive.debian.org/g' /etc/apt/sources.list && \
    sed -i 's/http:\/\/security.debian.org/http:\/\/archive.debian.org/g' /etc/apt/sources.list && \
    sed -i '/stretch-updates/d' /etc/apt/sources.list && \
    apt-get update && apt-get install -y --allow-unauthenticated git jq

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


@Instance.register("mui", "material-ui_21638_to_14827")
class MaterialUi21638to14827(Instance):
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


# --- Bundle-level number_interval routing keys (all -> MaterialUi21638to14827) ---
# Each bundle's dash-joined number_interval registered so Instance.create()
# resolves f"mui/{number_interval}" to this era class. Fixes routing: records with
# empty/era-name number_interval otherwise fall through to the mui/material-ui fallback.
_BUNDLE_NIS_MATERIALUI21638TO14827 = [
    "14827-16046-16192-16384-16521-16546-16582-16585-16597-16608-16611-16620-16621-16623-16624-16625-16628-16632-16635-16636-16639-16640-16654-16658-16659-16660-16662-16668-16671-16672-16677-16678-16679-16680-16684-16687-16688-16690-16691-16694-16699-16701-16702-16705-16708-16715-16719-16721-16724-16725-16727-16729-16731-16735-16737-16739-16743-16744-16748-16750-16751-16755-16765-16766-16767-16769-16770-16771-16778-16779-16781",
    "14992-15732-15839-16137-16169-16170-16182-16197-16199-16200-16207-16208-16211-16214-16217-16220-16225-16226-16229-16230-16231-16235-16236-16246-16250-16252-16254-16256-16257-16258-16261-16262-16266-16267-16268-16274-16275-16279-16281-16283-16284-16286-16288-16289-16290-16291-16294-16296-16297-16298-16304-16311-16322-16323-16326-16329-16335-16337",
    "15546-15548-15678-15720-15744-15947-15983-16002-16003-16004-16009-16031-16032-16036-16038-16039-16045-16048-16054-16056-16057-16059-16061-16063-16064-16066-16067-16069-16070-16071-16074-16077-16082-16083-16085-16086-16087-16098-16099-16106-16108-16109-16115-16117-16119-16121-16124-16125-16127-16130-16131",
    "15623-15751-15780-15863-15869-15874-15875-15884-15886-15890-15891-15894-15895-15896-15901-15908-15913-15915-15916-15919-15921-15934-15939-15943-15944-15949-15950-15952-15958-15965-15966-15967-15970-15972-15975-15977-15978-15981-15982-15987-15989-15990-15991-15995-15996-16000-16005-16010",
    "15703-16120-16129-16133-16134-16139-16141-16142-16148-16154-16157-16160-16164-16165-16166-16167-16171-16174-16175-16177-16187-16195-16196",
    "15758-15762-15764-15782-15804-15807-15809-15810-15811-15813-15814-15815-15816-15817-15818-15822-15826-15830-15831-15836-15838-15841-15843-15844-15845-15847-15849-15851-15853-15856-15857-15859-15861-15862-15864-15865-15866-15871-15880-15885",
    "15872-18663-18688-18699-18707-18717-18720-18721-18722-18723-18724-18725-18726-18727-18728-18729-18730-18731-18732-18734-18735-18736-18740-18741-18744-18745-18752-18753-18754-18758-18762-18763-18765-18767-18768-18772-18778-18780-18786-18787-18792-18796-18804-18805-18806-18808-18813-18814-18817-18823-18825",
    "16321-16324-16334-16342-16345-16348-16350",
    "16333-16356-16358-16361-16362-16365-16368-16370-16375-16376-16378-16380-16385-16387-16388-16392-16395-16396-16397-16398-16401-16404-16405-16406-16411-16412-16413-16416-16417-16420-16423-16425-16427-16428-16429-16432-16433-16438-16440-16446-16450-16453-16461-16467-16472-16497",
    "16343-16386-16399-16410-16455-16478-16490-16503-16510-16516-16519-16520-16522-16523-16525-16526-16529-16530-16531-16532-16533-16535-16538-16539-16540-16542-16553-16555-16561-16564-16567-16568-16576-16579-16583-16587-16589-16590-16592-16593-16603-16607-16613",
    "16473-17332-17388-17394-17419-17506-17525-17527-17528-17529-17531-17533-17534-17536-17537-17538-17552-17557-17558-17561-17571-17573-17577-17584-17587-17591-17594-17597-17598-17599-17600-17601-17603-17606-17607-17608-17609-17610-17619-17620-17621-17622-17623-17624-17625-17626-17627-17628-17629-17631-17632-17640-17648-17649-17651-17652-17657-17659",
    "16487-16777-17213-17240-17247-17249-17257-17259-17260-17261-17262-17263-17264-17266-17268-17270-17272-17274-17278-17280-17284-17285-17286-17288-17290-17292-17296-17300-17303-17304-17307-17310-17311-17312-17315-17316-17317-17331-17336-17337-17345-17347-17351-17354-17356-17357-17360-17362",
    "16642-16786-16842-16861-16863-16864-16869-16870-16871-16872-16874-16875-16876-16877-16880-16882-16883-16886-16888-16889-16891-16892-16893-16896-16898-16899-16900-16903-16916-16917-16923-16936-16937-16941-16946-16948-16951-16957-16958",
    "16693-16780-16783-16785-16789-16790-16792-16799-16804-16806-16807-16809-16814-16815-16816-16820-16821-16822-16823-16824-16825-16836-16838-16839-16850-16856",
    "16956-16959-16960-16961-16971-16974-16980-16982-16986-16988-16990-16991-16992-16993-17003-17005-17009-17013-17019-17020-17021-17024-17045-17046-17051-17053-17054-17055-17056-17058-17059-17060-17061-17062-17063-17074",
    "17037-17695-17715-17825-17827-17831-17835-17839-17841-17852-17855-17856-17857-17858-17862-17863-17865-17867-17870-17873-17874-17878-17880-17885-17889-17892-17893-17894-17896-17897-17902-17910-17911-17912-17913-17914-17924-17926-17929-17930-17933-17938-17939-17941-17942-17943-17944-17945-17946-17947-17948-17949-17950-17951-17952-17953-17954-17955-17956-17957-17958-17959-17961-17962-17963-17964-17967-17968-17969-17972-17976-17979-17982-17983-17984-17985-17994-18008-18011-18015-18020-18024-18025-18026-18027-18030-18032-18033-18034-18037-18038-18040-18042-18048-18049-18050-18051-18052-18053-18055-18056-18057-18058-18059-18060-18061-18062-18063-18064-18065-18066-18073",
    "17040-17078-17080-17081-17085-17091-17093-17095-17097-17103-17104-17109-17115-17118-17120-17122-17128-17132-17133-17134-17135-17139-17141-17148-17149-17150-17151-17152-17153-17154-17155-17156-17157-17159-17160-17162-17163-17164-17165-17166-17167-17169-17174-17176-17177-17178-17181-17182-17183-17184-17185-17187-17188-17189-17192-17193-17194-17195-17198-17200-17204-17205-17206-17214-17217-17218-17219-17221-17230-17232",
    "17057-20342-20389-20401-20403-20405-20406-20407-20408-20409-20410-20411-20412-20413-20414-20415-20416-20418-20419-20421-20422-20423-20424-20425-20426-20427-20428-20429-20432-20433-20434-20435-20443-20444-20450-20451-20454-20458-20463-20464-20465-20469-20472-20474-20475-20481-20486-20489-20490-20491-20493-20496",
    "17211-18702-18964-18981-19003-19041-19043-19044-19046-19050-19056-19058-19060-19062-19065-19071-19072-19073-19075-19079-19083-19085-19086-19088-19090-19093-19095",
    "17299-17447-17633-17642-17653-17661-17673-17675-17676-17677-17678-17683-17684-17690-17691-17694-17696-17698-17704-17714-17716-17722-17723-17724-17725-17726-17728-17729-17730-17731-17732-17733-17734-17736-17737-17738-17739-17740-17741-17742-17743-17744-17745-17746-17747-17748-17749-17750-17751-17756-17757-17758-17759-17760-17763-17765-17766-17768-17770-17771-17773-17778-17781-17782-17783-17785-17788-17790-17793-17798-17800-17802-17804-17805-17807-17817-17819-17821-17822-17830-17837-17851",
    "17301-17343-17390-17400-17401-17404-17406-17411-17420-17421-17422-17428-17429-17430-17431-17432-17433-17434-17435-17436-17437-17438-17439-17450-17451-17453-17455-17457-17458-17460-17466-17467-17468-17469-17482-17487-17488-17489-17490-17500-17501-17502-17508-17509-17512-17513-17514-17516-17517-17518-17520-17522-17523-17526",
    "17326-18503-19105-19223-19319-19324-19350-19366-19369-19375-19377-19380-19381-19383-19384-19385-19389-19390-19392-19394-19395-19396-19398-19399-19402-19403-19404-19405-19406-19409-19410-19412-19414-19415-19416-19417-19425-19428-19430-19431-19434-19439-19440-19451-19457-19471-19474-19478-19483-19485-19491-19492-19494-19495-19497-19499-19502-19503-19504-19505-19514-19516-19517-19519-19523-19529-19530-19532-19534",
    "17363-17371-17372-17374-17378-17380-17382-17389",
    "17483-17643-17829-17891-17993-18041-18045-18069-18071-18072-18074-18076-18084-18085-18088-18090-18093-18096-18100-18116-18117-18118-18125-18127-18128-18129-18131-18137-18141-18142-18144-18146-18148-18154-18155-18156-18160-18161-18162-18163-18174-18178-18179-18181-18182-18184-18185-18187-18188-18189-18190-18192-18195",
    "17662-17797-18153-18165-18186-18204-18214-18215-18216-18217-18220-18222-18224-18229-18231-18233-18235-18238-18239-18241-18242-18247-18250-18257-18259-18260-18261-18264-18266-18268-18274-18275-18281-18283-18284-18285-18286-18289-18290-18291-18292-18293-18294-18295-18296-18297-18298-18299-18300-18301-18316-18318-18319-18320-18321",
    "17680-18512-18854-19001-19070-19078-19097-19102-19103-19107-19111-19119-19121-19122-19123-19126-19129-19138-19143-19146-19158-19170-19172-19175-19177-19178-19179-19180-19181-19182-19183-19184-19185-19186-19188-19189-19190-19192-19193-19198-19200-19201-19215-19216-19219-19226-19228-19232-19234-19236-19237-19243-19256-19257-19259-19260-19263-19266-19269-19277-19278-19281-19282-19283-19286-19287-19291-19292-19293-19294-19295-19297-19298-19299-19300-19301-19304-19305-19307-19320-19321-19330-19332-19333-19334-19337-19339-19342",
    "17978-18306-18498-18500-18501-18506-18507-18509-18513-18516-18520-18521-18522-18523-18524-18525-18526-18527-18528-18529-18530-18531-18532-18534-18535-18536-18539-18543-18548-18551-18552-18553-18560-18562-18565-18566-18570-18571-18578-18584-18589-18591-18598-18603-18609-18611-18612-18614-18617-18630-18631-18632-18633-18634-18635-18636-18638-18639",
    "18035-18219-18271-18323-18325-18338-18339-18340-18341-18343-18346-18354-18355-18356-18361-18362-18364-18366-18370-18376-18379-18380-18381-18382-18383-18384-18385-18395-18396-18399-18400-18401-18403-18404-18405-18406-18407-18408-18409-18410-18411-18412-18413-18414-18415-18419-18422-18428-18429-18433-18437-18438-18440-18445-18451-18455-18458-18461-18468-18480-18481",
    "18357-19242-19667-19802-19806-19807-19808-19810-19811-19812-19813-19814-19815-19816-19817-19818-19819-19820-19822-19823-19825-19837-19841-19844-19849-19850-19851-19857-19858-19862-19867-19873-19890",
    "18441-19599-19608-19722-19853-19874-19896-19898-19899-19900-19901-19902-19903-19904-19905-19906-19907-19908-19909-19910-19911-19912-19913-19914-19915-19916-19917-19918-19919-19921-19923-19926-19928-19933-19934-19937-19939-19944-19949-19950-19951-19954-19956-19959-19962-19964-19966-19969-19971-19972-19974-19978-19979-19987-19992-19993-19995-19996-19998-20006-20007-20015-20016-20017-20019-20021-20024-20025-20026-20027-20028-20029-20030-20031-20032-20033-20034-20035-20037-20038-20039-20040-20046-20047-20048-20051-20052-20055-20065-20066-20073-20075-20076-20079-20086-20091-20100-20101-20103-20105-20108-20110-20111-20112-20113-20114-20116-20117-20118-20119-20120-20121-20122-20123-20124-20127-20128-20142-20164",
    "18624-18627-18629-18641-18643-18644-18645-18654-18661-18662-18668-18680-18683-18685-18687-18692-18695-18701-18706-18708-18711",
    "18794-18837-18838-18920-18936-18942-18947-18970-18982-18984-18987-18988-18989-18993-18994-18997-18998-18999-19000-19002-19004-19008-19010-19012-19013-19014-19015-19016-19017-19021-19022-19023-19031-19034-19039-19042",
    "18820-18824-18827-18832-18834-18835-18836-18839-18840-18841-18842-18843-18844-18845-18846-18847-18848-18851-18855-18856-18857-18859-18865-18866-18867-18868-18876-18886-18887-18889-18894-18896-18897-18910-18913-18916-18917-18922-18941-18943-18944-18945-18946-18948-18949-18951-18952-18961",
    "19049-19280-19341-19393-19501-19511-19524-19533-19538-19539-19544-19547-19548-19555-19558-19560-19562-19566-19570-19582-19588-19590-19592-19593-19594-19604-19605-19614-19618-19620-19621-19622-19624-19625-19626-19627-19628-19629-19630-19633",
    "19155-19515-19967-20005-20078-20082-20102-20146-20158-20169-20171-20177-20187-20190-20194-20195-20199-20200-20202-20207-20209-20210-20211-20212-20213-20214-20215-20217-20218-20219-20220-20221-20222-20223-20225-20226-20227-20228-20230-20232-20233-20235-20237-20247-20252-20253-20255-20260-20262-20264-20265-20269-20270-20271-20272-20274-20278-20287-20290-20293-20298-20304-20305-20306-20307",
    "19598-19611-19612-19615-19617-19631-19636-19638-19639-19643-19644-19648-19661-19663-19669-19676-19678-19684-19693-19694-19695-19707-19717-19725-19726-19727-19729-19732-19735-19736-19737",
    "19699-19720-19724-19728-19731-19733-19734-19741-19743-19747-19758-19761-19762-19766-19768-19770-19771-19775-19782-19784-19789-19790-19794-19800-19803-19805",
    "19795-20657-20743-20753-20781-20783-20786-20791-20792-20793-20794-20798-20799-20803-20807-20810-20817-20822-20823-20826-20830-20841-20843-20847-20848-20850-20851-20853-20857-20860-20866-20869-20870-20873-20874-20877-20879-20880-20881-20882-20884-20885-20887-20888-20890-20891-20892-20893-20894-20898-20899",
    "20085-20601-20623-20644-20646-20647-20648-20649-20651-20654-20658-20659-20663-20664-20668-20670-20672-20673-20674-20677-20678-20679-20680-20681-20682-20684-20685-20686-20687-20688-20691-20693-20694-20697-20699-20702-20710-20715-20720-20721-20724-20728-20729-20732-20734-20736-20737-20739-20742-20745-20747-20749-20751-20754-20756-20757-20758-20759-20760-20761-20762-20763-20764-20766-20767-20770-20771-20773-20775-20777-20779-20780-20784",
    "20157-20179-20238-20276-20295-20308-20309-20312-20314-20315-20316-20317-20318-20319-20320-20321-20322-20323-20324-20325-20326-20327-20329-20330-20331-20332-20333-20334-20336-20337-20338-20339-20341-20344-20348-20349-20350-20354-20356-20361-20363-20368-20376-20377-20381-20382-20383-20390-20396-20397",
    "20294-20498-20499-20500-20502-20503-20504-20505-20506-20507-20508-20509-20510-20511-20512-20513-20514-20515-20516-20517-20518-20522-20523-20524-20535-20536-20538-20541-20542-20543-20545-20547-20549-20550-20563-20566-20567-20571-20572-20575-20577-20586-20587-20589-20592-20595-20596-20597-20603-20606-20612-20614-20617-20618-20620-20624-20626-20627-20628-20629-20630-20631-20632-20633-20634-20635-20636-20637-20638",
    "20656-20949-20965-20980-21003-21005-21006-21007-21014-21017-21027-21032-21033-21034-21035-21039-21041-21043-21047-21051-21052-21054-21056-21057-21058-21059-21060-21061-21062-21063-21064-21065-21066-21067-21068-21069-21070-21071-21072-21073-21074-21075-21076-21077-21078-21079-21080-21081-21083-21084-21087-21090-21094-21095-21097-21099-21100-21101-21106-21107-21109-21115-21116-21120-21121-21131-21138-21141-21149-21150-21151-21155",
    "20789-20806-20833-20854-20875-20876-20900-20901-20902-20903-20908-20910-20914-20916-20922-20923-20931-20934-20936-20937-20942-20943-20945-20952-20958-20961-20964-20966-20967-20970-20971-20972-20973-20974-20975-20976-20977-20978-20979-20982-20983-20985-20986",
    "21002-21192-21197-21214-21241-21246-21261-21262-21274-21279-21280-21285-21298-21300-21303-21308-21309-21319-21322-21331-21335-21336-21340-21342-21343-21344-21345-21346-21347-21348-21349-21350-21351-21352-21353-21355-21356-21357-21358-21359-21360-21362-21365-21368-21369-21370-21373-21374-21375-21384-21386-21390",
    "21122-21134-21153-21156-21158-21159-21160-21161-21167-21168-21169-21170-21172-21173-21175-21176-21177-21178-21179-21180-21181-21183-21184-21186-21187-21190-21194-21195-21196-21201-21203-21207-21209-21219-21223-21226-21234-21237-21239-21240-21243-21244-21248-21249-21252-21255-21256-21257-21258-21259-21260-21263-21264-21265-21266-21267-21268-21269-21270-21271-21275",
    "21416-21442-21445-21457-21479-21482-21500-21535-21545-21555-21560",
    "21638-21710-21714-21751-21752-21822-21925-22022-22076-22090-22094-22114-22178-22202-22206-22213-22245-22263-22363-22393-22400-22521-22618-22627-22633-22684-22686-22697-22751-22776-22837-22850-22887-23212-23326-23357-23367-23480-23513-23517-23570-23643-23692",
]
for _ni in _BUNDLE_NIS_MATERIALUI21638TO14827:
    Instance.register("mui", _ni)(MaterialUi21638to14827)
