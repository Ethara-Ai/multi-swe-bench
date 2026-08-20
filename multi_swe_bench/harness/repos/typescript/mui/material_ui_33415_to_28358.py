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
        return "node:18"

    def image_tag(self) -> str:
        return "base33415to28358"

    def workdir(self) -> str:
        return "base33415to28358"

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


@Instance.register("mui", "material-ui_33415_to_28358")
class MaterialUi33415to28358(Instance):
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


# --- Bundle-level number_interval routing keys (all -> MaterialUi33415to28358) ---
# Each bundle's dash-joined number_interval registered so Instance.create()
# resolves f"mui/{number_interval}" to this era class. Fixes routing: records with
# empty/era-name number_interval otherwise fall through to the mui/material-ui fallback.
_BUNDLE_NIS_MATERIALUI33415TO28358 = [
    "28449-30163-30428-30601-30766-30906-30918-30921-30961-30981-31052-31113-31165-31175-31209-31219-31237-31241-31242-31251-31267-31268-31270-31275-31281-31284-31285-31297-31302-31304-31308-31316-31317-31318-31319-31320-31321-31322-31323-31324-31325-31326-31328-31329-31330-31331-31333-31334-31335-31336-31341-31346-31352-31353",
    "28645-28674-28685-28702-28800-28815-28819-28828-29023-29042-29088-29097-29118-29150-29168-29170-29174-29182-29196-29204-29229-29234-29280-29282-29286-29297-29300-29303-29305-29314-29324-29326-29327-29329-29336-29340-29347-29350-29351-29353-29354-29357-29358-29360-29370-29383-29390-29391-29392-29393-29394-29395-29396-29397-29398-29399-29400-29401-29402-29403-29404-29405-29406-29407-29408-29409-29414-29415-29418-29419-29420-29424-29425-29427-29432-29435-29436-29438-29453-29454-29458-29459-29462-29463-29465-29467-29471-29472-29477-29478-29484-29487-29499-29505-29532-29533-29534-29535-29537-29538-29539-29540-29541-29542-29543-29544-29545-29546-29548-29555-29569-29571",
    "28964-29464-29779-29781-29782-29800-29845-29850-29852-29857-29866-29882-29893-29894-29895-29902-29905-29912-29913-29914-29915-29916-29917-29918-29919-29920-29921-29922-29929-29931-29932-29933-29934-29935-29937-29938-29939-29940-29944-29946-29947-29948",
    "28970-29208-29290-29295-29302-29322",
    "29067-29553-29619-29621-29759-29843-29846-29880-29898-29911-29936-29945-29950-29952-29956-29957-29967-29972-29974-29981-29982-29985-29989-30000-30003-30008-30021-30026-30027-30040-30041-30042-30044-30046-30048-30049-30053-30055-30056-30057-30058-30059-30062-30069-30071-30073-30076-30077-30079",
    "29213-29311-29630-29715-29839-29888-29976-30050-30051-30052-30067-30072-30078-30080-30087-30095-30098-30100-30101-30106-30108-30109-30117-30122-30134-30135-30136-30141-30155-30156-30157-30158-30159-30160-30162-30165-30166-30167-30168-30189-30199",
    "29240-29485-29732-29994-30002-30075-30107-30114-30128-30147-30149-30183-30186-30195-30204-30206-30211-30213-30216-30217-30219-30222-30226-30248-30253-30254-30281-30288-30289",
    "29474-30239-30534-30633-30636-30637-30640-30641-30642-30643-30644-30654-30682-30736-30738-30740-30743-30757-30768-30774-30790-30794-30803-30828-30834-30835-30836-30837-30838-30839-30840-30841-30846-30849-30857-30862-30863-30864-30866-30867-30872-30891-30895-30897-30899-30901-30904-30910-30912-30919-30923-30924-30931-30932-30933-30934-30935-30936-30939-30940-30942-30943-30944-30947-30950-30952-30959",
    "29672-31398-31407-31987-32128-32204-32264-32374-32376-32383-32386-32483-32492-32496-32498-32502-32519-32521-32522-32523-32525-32526-32527-32528-32529-32531-32532-32533-32534-32536-32547-32549-32552-32554-32555-32556-32561-32562-32564-32565-32566-32567-32575-32576-32577-32578-32580-32581-32582-32583-32584-32588-32590-32594-32595-32596-32598-32600-32602-32603-32605-32606-32607-32609-32610-32612-32613-32614-32615-32616-32617-32618-32619-32626-32636-32638-32646-32648-32649-32650-32652-32653-32657-32692-32698-32712",
    "29765-29829-29833-29836-29854-29858-29870-29876-29879-29884-29889",
    "29813-31933-32624-32820-32946-32972-33030-33097-33132-33145-33149-33168-33189-33203-33207-33208-33210-33211-33215-33238-33241-33243-33244-33249-33253-33256-33261-33265-33270-33277-33279-33305",
    "29896-30065-30094-30121-30172-30212-30263-30264-30283-30411-30437-30515-30524-30527-30530-30531-30532-30533-30541-30542-30543-30544-30545-30546-30547-30549-30552-30553-30554-30555-30556-30557-30561-30570-30578-30581-30583-30586-30587-30589-30593-30595-30596-30606-30622-30659-30662-30663-30667",
    "29930-30512-30592-30598-30608-30614-30616-30634-30635-30664-30677-30679-30681-30684-30690-30691-30695-30697-30698-30700-30704-30716-30721-30723-30724-30729-30733-30742-30751-30756",
    "29954-30020-30255-30282-30366-30374-30385-30388-30425-30426-30427-30442-30446-30454-30455-30459-30460-30470-30471",
    "30054-30169-30262-30265-30272-30273-30275-30362-30371-30386-30387-30395-30398-30399-30400-30401-30402-30457-30473-30482-30487-30489-30492-30493-30495-30499-30502-30503-30505-30528-30558-30563-30567-30574",
    "30088-30976-31139-31269-31416-31417-31806-31810-31845-31873-31878-31880-31881-31894-31896-31899-31903-31906-31909-31923-31935-31939-31942-31945-31950-31951-31953-31954-31956-31959-31965-31967-31969-31970-31971-31975-31980-31990-32019",
    "30118-30842-30974-30999-31227-31240-31257-31262-31273-31291-31295-31299-31332-31339-31340-31351-31354-31356-31359-31360-31378-31382-31386-31394-31395-31402-31406-31412-31418-31419-31423-31424-31425-31458-31460-31505-31588-31589-31590-31591-31592-31593-31595-31596-31597-31598-31599-31600-31620-31651-31696-31711-31767-31798",
    "30441-31830-31955-32403-32436-32512-32570-32599-32623-32628-32647-32655-32666-32670-32672-32674-32675-32676-32679-32680-32681-32683-32684-32713-32726-32757-32772-32780-32793-32798-32801-32803-32815-32816-32817-32824-32825-32828-32836-32842-32844-32865-32866-32869-32879",
    "30444-30632-30646-30713-30755-30786-30788-30878-30883-30884-30890-30927-30929-30930-30937-30938-30941-30955-30960-30963-30966-30967-30971-30977-30978-30983-30984-30993-30994-30995-30996-31000-31003-31016-31017-31026-31038-31041-31043-31045-31046-31049-31058-31061-31062-31064-31067-31070-31074-31076",
    "30458-34017-34101-34141-34152-34158-34161-34188-34222-34259-34260-34261-34262-34263-34264-34265-34266-34267-34268-34269-34270-34271-34272-34273-34274-34275-34276-34279-34281-34283-34288-34291-34295-34311-34320-34321-34330-34331-34343-34381",
    "30639-31904-32343-32855-33027-33115-33118-33205-33480-33534-33537-33541-33552-33566-33583-33587-33594-33595-33619-33622-33623-33624-33625-33627-33628-33629-33635-33636-33638-33642-33643-33648-33649-33650-33654-33659-33664-33665-33666-33667-33668-33674-33675-33679-33684-33685-33691-33692-33699-33712-33713-33715-33718-33720-33721-33722-33723-33724-33725-33726-33727-33728-33729-33739-33740-33741",
    "30680-30969-31024-31040-31042-31044-31048-31051-31053-31054-31055-31056-31086-31087-31101-31124-31137-31141-31148-31150-31151-31152-31153-31154-31155-31156-31157-31158-31159-31162-31163-31166-31167-31169-31172-31176-31178-31182-31186-31187-31189-31193-31195-31200-31213-31216-31220-31221-31222-31223-31224-31225-31226-31228-31229-31230-31231-31234-31236-31239-31243",
    "30759-34948-35486-35700-35805-35807-35846-35859-35866-35870-35873-35876-35877-35881-35898-35899-35901-35921-35925",
    "30894-30913-30987-31021-31029-31036-31037-31065-31088-31092-31095-31099-31118-31120-31131-31134-31135-31136-31160-31161-31164",
    "30920-31998-32055-32417-32706-32769-32778-32808-32838-32843-32895-32910-32913-32959-32962-32975-32996-32997-33007-33015-33021-33022-33023-33024-33025-33026-33028-33029-33032-33033-33036-33040-33047-33059-33064-33065-33068-33069-33071-33073-33077-33087-33091-33095-33099-33107-33110-33111-33112-33122-33123-33128-33129-33130-33134-33136-33142",
    "31138-31373-31604-31813-31816-31831-31850-31895-31905-31918-31940-31964-31974-31984-31999-32000-32001-32002-32003-32004-32005-32006-32007-32008-32009-32010-32011-32012-32015-32018-32021-32023-32027-32029-32034-32045-32050-32052-32060-32063-32073-32076-32079-32081-32083-32091-32092-32093-32095-32096-32097-32101-32105-32108-32109-32110-32111-32112-32113-32114-32115-32116-32117-32118-32119-32121-32122-32123-32130-32131-32133-32140",
    "31303-31790-31804-31807-31808-31811-31814-31826-31829-31833-31872-31882-31889-31891-31892-31893-31898-31901-31902-31907-31908-31910-31916-31917-31925",
    "31313-32104-32927-32974-33009-33108-33273-33315-33408-33438-33456-33466-33472-33482-33484-33485-33486-33496-33508-33520-33521-33522-33527-33533-33535-33536-33538-33539-33540-33542-33547-33557",
    "31401-31997-32134-32165-32174-32178-32179-32185-32188-32211-32235-32237-32238-32242-32244-32257-32260-32261-32262-32268-32276-32282-32283-32290-32297-32305-32309-32315-32323-32326-32327-32328-32329-32330-32332-32334-32341-32347-32352-32354",
    "31789-33156-33333-33430-33435-33506-33524-33528-33548-33549-33555-33567-33569-33570-33571-33573-33580-33585-33586-33588-33589-33591-33593-33608-33611-33612-33614-33617-33620-33630-33631-33633-33640",
    "31802-31848-31946-32157-32168-32263-32271-32291-32382-32458-32491-32506-32635-32637-32661-32682-32690-32695-32708-32709-32711-32714-32715-32717-32720-32723-32725-32728-32729-32730-32733-32744-32745-32758-32771-32781-32782-32790-32795",
    "31875-31983-32030-32080-32159-32180-32240-32267-32279-32295-32313-32314-32322-32324-32325-32331-32333-32335-32355-32360-32364-32365-32370-32384-32389-32393-32396-32401-32405-32433-32440-32441-32442-32443-32444-32445-32446-32447-32448-32456-32459",
    "31897-32735-32969-33183-33714-33836-33840-33877-33882-33896-33926-33937-33954-33974-33975-33995-34008-34022-34024-34037-34049-34053-34054-34055-34060-34064-34070-34073-34077-34086-34091-34095-34096-34097-34098-34099-34100-34102-34103-34104-34105-34106-34107-34108-34109-34110-34111-34115-34119-34120-34121",
    "32156-32254-32258-32310-32361-32380-32390-32399-32402-32410-32412-32423-32426-32429-32431-32432-32435-32450-32454-32462-32481-32487-32488-32489-32500-32503-32505-32507-32509-32514-32515-32516-32517-32541-32542-32543-32544-32553",
    "32170-32535-32573-32643-32739-32750-32766-32800-32802-32810-32819-32847-32848-32851-32852-32853-32854-32856-32857-32858-32859-32860-32861-32862-32863-32864-32868-32873-32874-32878-32883-32886-32890-32900-32912-32915-32918-32923-32931-32936-32938-32943-32949",
    "32181-33797-39869-39979-40133-40147-40148-40149-40151-40152-40153-40154-40155-40156-40158-40159-40160-40161-40168-40180-40186-40193-40197-40200-40209-40216-40222-40230-40232",
    "32230-32530-32579-32671-32694-32707-32740-32742-32747-32791-32811-32850-32887-32896-32905-32919-32925-32928-32939-32940-32945-32947-32950-32954-32957-32961-32963-32966-32971-32976-32980-32991-32993-32994-33002-33037-33038-33045-33048",
    "32308-33088-33119-33158-33254-33267-33269-33283-33285-33287-33288-33293-33294-33295-33296-33297-33298-33299-33300-33301-33302-33309-33310-33314-33324-33325-33361-33365-33366-33367-33368-33369-33370-33371-33372-33373-33374-33379-33381-33389",
    "32321-32746-32984-33154-33181-33196-33197-33201-33240-33257-33284-33292-33326-33338-33356-33376-33383-33384-33386-33390-33393-33394-33396-33397-33398-33401-33415-33432-33434-33439-33446-33447-33448-33449-33450-33451-33452-33453-33454-33455-33462-33463-33464-33474-33477-33479-33483",
    "32499-32608-32835-32901-32987-33005-33014-33034-33051-33063-33086-33094-33096-33100-33102-33103-33106-33120-33125-33131-33143-33153-33159-33160-33161-33163-33170-33171-33174-33176-33180-33193-33206-33217-33218-33226",
    "32508-33127-33530-33970-34229-34602-34771-34793-35005-35364-35373-35374-35377-35452-35497-35524-35545-35547-35548-35552-35553-35559-35560-35562-35564-35570-35571-35573-35575-35577-35579-35587-35602-35603-35604-35605-35606-35607-35608-35609-35610-35612-35617-35623-35624-35625-35626-35629-35633",
    "32511-34183-34247-34325-34337-34496-34505-34514-34757-34764-34776-34786-34846-34849-34853-34854-34855-34856-34857-34858-34859-34860-34861-34866-34869-34875-34878-34884-34890-34894-34897-34902-34908-34913-34914-34918-34919-34926-34929-34930-34934-34935-34937-34938-34939-34940-34941-34942-34944-34945-34950-34953-34955-34958-34960-34963-34964",
    "32697-33488-33503-33554-33626-33670-33687-33693-33702-33706-33707-33711-33716-33717-33734-33737-33745-33749-33750-33751-33752-33753-33756-33760-33761-33763-33764-33772-33774-33777-33786-33796-33800-33803-33813-33816-33817-33823-33824-33827-33828-33829-33830-33831-33832-33834-33835-33837-33838-33842-33849-33854-33862",
    "32822-33795-35941-36006-36008-36024-36090-36091-36103-36104-36109-36144-36156-36157-36235-36242-36243-36246-36247-36248-36249-36250-36251-36252-36253-36254-36255-36256-36257-36258-36259-36260-36261-36263-36265-36266-36271-36272-36284-36288-36291-36295-36299-36307-36312-36315-36321-36323-36333-36337-36338-36340-36341-36342-36357-36360",
    "33031-33859-33860-33952-33986-33989-33994-34050-34087-34118-34123-34124-34125-34127-34132-34134-34138-34140-34157-34160-34171-34172-34180-34197",
    "33162-33411-33582-34189-34677-34693-34702-34749-34762-34765-34766-34769-34773-34774-34775-34777-34778-34779-34780-34781-34782-34784-34785-34790-34805-34806-34809-34810-34822-34823-34825-34843-34844-34848-34850-34852-34862-34876-34879",
    "33227-33278-33340-33391-33526-33958-34243-34245-34421-34445-34481-34485-34492-34494-34511-34534-34535-34537-34541-34543-34549-34550-34551-34552-34553-34554-34555-34556-34557-34558-34559-34560-34561-34562-34563-34565-34566-34567-34568-34569-34581-34586-34589-34590",
    "33236-35741-36151-36279-36280-36348-36401-36404-36458-36472-36490-36505-36576-36586-36602-36604-36606-36607-36611-36621-36628-36629-36635-36636-36637-36638-36639-36640-36641-36642-36643-36644-36645-36654-36669",
    "33312-35739-36050-36052-36190-36225-36231-36274-36282-36298-36301-36310-36316-36331-36334-36339-36344-36350-36353-36354-36365-36366-36371-36374-36378-36380-36381-36382-36394-36397-36398-36406-36410-36411-36420-36443",
]
for _ni in _BUNDLE_NIS_MATERIALUI33415TO28358:
    Instance.register("mui", _ni)(MaterialUi33415to28358)
