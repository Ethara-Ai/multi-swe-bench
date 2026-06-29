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
        return "node:20"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        # SHARED base (tag "base", reused by every Vitest-era PR). The `# syntax`
        # directive makes DockerfileEnhancer.enhance() return this Dockerfile
        # unchanged; otherwise it rewrites the clone to `git checkout
        # ${{BASE_COMMIT}}` + hardening + gc-prune, pinning the shared base to ONE
        # commit and pruning it — which breaks every other PR's `git checkout
        # {{base.sha}}` in prepare.sh. Per-PR hardening lives in ImageDefault.
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

{self.global_env}

WORKDIR /home/
RUN apt update && apt install -y git
RUN npm install -g pnpm
RUN apt install -y jq

{code}

{self.clear_env}

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

    def dependency(self) -> Image | None:
        return ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
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
pnpm install || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
pnpm dev:prepare
pnpm test:unit -- --verbose
pnpm test:runtime  --no-watch

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch
pnpm install || true
pnpm dev:prepare 
pnpm test:unit -- --verbose
pnpm test:runtime  --no-watch

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch /home/fix.patch
pnpm install || true
pnpm dev:prepare
pnpm test:unit -- --verbose
pnpm test:runtime  --no-watch

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

        # Per-PR anti-cheat hardening. dependency() returns an Image, so
        # DockerfileEnhancer emits this Dockerfile verbatim (it only auto-injects
        # the hardening into str-dependency/base images), hence we embed
        # Image._HARDENING_BLOCK ourselves. ENV BASE_COMMIT resolves the block's
        # ${BASE_COMMIT}; WORKDIR pins the repo dir so the hardening RUN (detach
        # onto BASE_COMMIT -> drop every ref/remote -> GC unreachable objects ->
        # self-audit) operates on the checkout prepare.sh produced.
        return f"""FROM {name}:{tag}

ENV BASE_COMMIT={self.pr.base.sha}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{self.pr.repo}

{Image._HARDENING_BLOCK}

{self.clear_env}

CMD ["/bin/bash"]
"""


class ImageDefault24709(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
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
pnpm install || true
# `pnpm build:stub` runs `unbuild --stub` in every package; early Nuxt 3 often
# can't resolve unbuild from the workspace (engine warns + partial install), so
# `command not found: unbuild` aborts build:stub and no tests run. Install it
# globally so it's always on PATH, and keep the workspace add as a fallback.
npm install -g unbuild || true
pnpm add -w unbuild || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
pnpm build:stub
pnpm test:unit -- --verbose
pnpm test:runtime  --no-watch

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch
pnpm install || true
pnpm build:stub
pnpm test:unit -- --verbose
pnpm test:runtime  --no-watch

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch /home/fix.patch
pnpm install || true
pnpm build:stub
pnpm test:unit -- --verbose
pnpm test:runtime  --no-watch

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

        # Per-PR anti-cheat hardening. dependency() returns an Image, so
        # DockerfileEnhancer emits this Dockerfile verbatim (it only auto-injects
        # the hardening into str-dependency/base images), hence we embed
        # Image._HARDENING_BLOCK ourselves. ENV BASE_COMMIT resolves the block's
        # ${BASE_COMMIT}; WORKDIR pins the repo dir so the hardening RUN (detach
        # onto BASE_COMMIT -> drop every ref/remote -> GC unreachable objects ->
        # self-audit) operates on the checkout prepare.sh produced.
        return f"""FROM {name}:{tag}

ENV BASE_COMMIT={self.pr.base.sha}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{self.pr.repo}

{Image._HARDENING_BLOCK}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("nuxt", "nuxt")
class Nuxt(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        if self.pr.number <= 24709:
            return ImageDefault24709(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        current_suite = None

        re_pass_suite = re.compile(r"^✓\s+(.+?)\s+\(\d+")

        re_fail_suite = re.compile(r"^❯\s+(.+?)\s+\(\d+")

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            pass_match = re_pass_suite.match(line)
            if pass_match:
                current_suite = pass_match.group(1)
                passed_tests.add(current_suite)

            fail_match = re_fail_suite.match(line)
            if fail_match:
                current_suite = fail_match.group(1)
                failed_tests.add(current_suite)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# Delivered bundles routed to this era class. Instance.create() resolves
# nuxt/<number_interval> -> Nuxt. Bundle-level; data-derived from the
# delivered resolved set (regenerate if it changes).
_BUNDLE_NIS = [
    "24744-24853-24888-24896-24897-24898-24899-24906-24923-24924-24931-24935-24937-24946-24948-24951-24957-24959-24961-24978-24980-24985-24986-24990-24993-25003-25007-25008-25009-25010-25013-25015-25017-25020-25026-25027-25036-25046-25054-25056-25057-25067-25070-25074-25075",
    "25635-25639-25641-25644-25646-25648-25658-25660-25667-25669-25670-25673-25679-25682-25683-25692-25714-25720-25726-25728-25731-25749-25750-25752-25764-25765-25766-25770-25774-25780-25783-25786-25788",
    "26359-26360-26366-26368-26370-26376-26386-26387-26389-26392-26399-26404-26407-26408-26410-26413-26421-26422-26427-26430-26436-26447-26448-26460-26462-26466-26478-26480-26482-26486-26492-26496-26500-26503-26504-26506-26518-26519-26530-26532-26537-26541-26544-26545-26546-26547-26548-26553-26554-26556-26558-26559-26562-26563-26564-26568-26569-26573-26577-26583-26584-26589-26595-26607-26611-26613-26621-26626-26627-26631-26632-26633-26636-26641-26644",
    "27398-27428-27487-27529-27531-27539-27540-27542-27549-27551-27562-27567-27568-27569-27571-27573-27575-27582-27583-27586-27587-27590-27596-27599-27600-27601-27603-27611-27615-27621-27622-27628-27630-27632-27633-27637-27638-27640-27641-27642",
    "27518-27521-27523-27524-27525-27526",
    "27970-27974-27977-27983-27984-28017-28026-28047-28048-28055-28058-28074-28080-28104-28153-28205-28207",
    "28214-28222-28231-28235-28253-28256-28261-28302-28326-28368-28430-28441-28452-28457-28465-28466-28470-28481-28487-28492-28493-28503-28505-28519-28528-28530-28547-28568-28571-28575-28578-28585-28596-28598-28606-28614-28617-28637-28638-28646-28652-28654-28658",
    "28660-28662-28666-28679-28689-28702-28708-28714-28729-28737-28750-28791-28800-28811-28817",
    "28824-28836-28840-28853-28864-28897-28915-28926-28932-28938-28949-28952-28962-28974-28989-28990-29001-29013-29015-29016",
    "29019-29030-29035-29039-29040-29048-29071-29073-29077-29082-29083-29091-29092-29096-29097-29114-29127-29129-29130-29135-29140-29147-29160-29164-29166-29168-29169-29175-29178-29193-29200-29201-29205-29209-29214-29216-29223-29231-29235-29238-29256-29266-29277-29281-29295-29305-29306-29307-29310-29319-29325-29328-29330-29335-29337-29343-29356-29358-29374-29375-29384-29390-29400-29403-29513-29517-29521-29524-29528-29532-29584-29590-29607-29608-29614-29633-29641-29645-29648-29652-29659-29671-29680-29685-29688-29694-29700-29705-29707-29711-29715-29717-29726-29729-29753-29763-29766",
    "29826-29828-29830-29832-29834-29836-29851-29860-29864-29874-29880-29886-29888-29897-29901-29908-29926-29928-29951-29983-29984",
    "29871-29987-29990-29994-30044-30051-30055-30060-30065-30069-30072-30077-30083-30086-30110-30119-30129-30133-30144-30161-30167-30186-30218-30227-30252-30257-30276-30281-30299-30301-30308-30309-30311-30330-30337-30342-30351-30357",
    "30364-30366-30371-30373-30376-30377-30387-30389-30406-30415-30423-30441-30449",
    "30463-30470-30473-30475-30487-30495-30509-30538-30545-30551-30554-30566-30572-30596-30601",
    "30611-30613-30622-30633-30642-30662-30687-30692-30694-30702-30714-30724-30733",
    "31269-31272-31281-31286-31292-31305-31310-31324-31390-31396-31400-31402-31404-31421-31438-31441-31452",
    "31456-31463-31464-31466-31486-31491-31520-31537-31542-31548-31560-31566-31581-31590-31595-31615",
    "31621-31636-31643-31653-31656-31667-31687-31690-31697-31699-31703-31705-31708-31717-31722-31723-31731-31741-31756-31763-31765-31795-31796-31799-31815-31822-31829-31838-31841-31858-31861-31863-31866-31867-31887-31912-31918-31919",
    "31812-32397-32792-32857-32858-32861-32863-32868-32871-32874-32877-32880-32881-32887-32891-32893-32897-32899-32901-32902-32907-32913-32914-32920-32921-32922-32924-32925-32932-32935-32938-32939-32950-32979-32981-32982-32983-32987-32988-32991-32993-32995-32999-33000-33004-33016-33018-33023-33025-33026-33030-33031-33037-33051-33052-33053-33057-33058-33061-33063-33069-33072-33075-33077-33081-33093-33094-33098-33099-33100-33103-33115",
    "31920-31922-31931-31936",
    "31958-31960-31991",
    "32005-32025-32075-32076-32083",
    "32086-32106-32126-32150-32161-32172",
    "32313-32793-32798-32803-32807-32820-32827-32832-32835-32841-32843-32846-32848-32849-32850-32852-32853-32855",
    "32320-32324-32340-32341-32346-32404-32406-32409-32410-32415-32440-32457-32459-32460-32506-32507-32513-32517",
    "32386-32701-32707-32710-32711-32722-32725-32726-32744-32746-32755-32757-32758-32759-32760-32772-32776-32779-32783-32790-32791",
    "32520-32521-32545-32562",
    "32531-33131-33159-33222-33314-33328-33344-33359-33360-33361-33380-33384-33396-33405-33409-33411-33412-33419-33420-33425-33427-33429-33444-33445-33449-33451-33462-33469-33470-33471-33476-33483-33484-33492-33494-33499-33503-33505-33507-33509-33512-33515-33520-33521-33522-33523-33526-33531-33550-33552-33554-33555-33557",
    "32606-32607-32612-32702-32712-32713-32724-32767-32786",
    "32608-32926-32927-32930-32931-32933-32936-32940-32984-32985-32986-32989-32996-33054-33055-33056-33062-33095-33104-33114",
    "32844-33153-33155-33160-33161-33162-33163-33164-33165-33167-33172-33177-33192-33199-33200-33205-33207-33211-33212-33213-33214-33217",
    "33046-33229-33230-33234-33235-33239-33242-33258-33264-33265-33271-33284-33285-33286-33287-33290-33291-33297-33298-33299-33302-33308-33317-33318-33325-33330-33333-33334-33335-33336-33340-33341-33343-33345-33346-33347-33348-33349-33350-33351-33358-33363-33368-33373-33379-33389-33394-33395-33397-33402-33403",
    "33168-33169-33170-33171-33173-33178-33179",
    "33221-33511-33565-33567-33569-33572-33574-33575-33576-33586-33595-33596-33601-33603-33608-33615-33617-33625-33626-33628-33637-33638-33639-33640-33643-33650-33654-33655-33658-33662-33663-33665-33667-33670-33673",
    "33233-33269-33292-33352-33353-33354-33355-33356-33362-33398-33399",
    "33371-33388-33510-33672-33682-33683-33687-33688-33689-33691-33701-33702-33707-33709-33710-33712-33713-33715-33726-33740-33750-33751-33752-33753-33754-33763-33767-33774-33779-33793-33794-33802-33810-33813-33820-33825-33830-33831",
    "33472-33641-33645-33646-33649-33674",
    "33684-33690-33696-33716-33719-33720-33755-33756-33758-33759-33797-33832",
]
for _ni in _BUNDLE_NIS:
    Instance.register("nuxt", _ni)(Nuxt)
