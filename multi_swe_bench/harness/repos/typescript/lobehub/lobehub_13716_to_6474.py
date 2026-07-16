"""lobehub/lobehub config for PRs 6474-13716 (monorepo era, pnpm+vitest)."""

import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _clean_test_name(name: str) -> str:
    """Strip variable timing and metadata from test names for stable eval matching."""
    # Strip vitest file-level metadata: (2 tests) 75ms, (1 test | 1 failed) 120ms
    name = re.sub(
        r"\s+\(\d+\s+tests?(?:\s*\|\s*\d+\s+\w+)*\)\s*(?:\d+(?:\.\d+)?\s*m?s)?\s*$",
        "",
        name,
    )
    # Strip parenthesized timing: (75ms), (150 ms), (8.954 s)
    name = re.sub(r"\s+\(\d+(?:\.\d+)?\s*m?s\)\s*$", "", name)
    return name.strip()


class LobeHubImageBaseLate(Image):
    """Base image for lobehub late era (PRs 6474-13716, pnpm monorepo)."""

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
        return "node:20-bookworm"

    def image_tag(self) -> str:
        return "base-late"

    def workdir(self) -> str:
        return "base-late"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # Toolchain-only base: deliberately does NOT clone the repo. A single
        # ``base-late`` tag is shared by every PR in this era, but each PR has a
        # different ``base.sha``. Cloning here would let DockerfileEnhancer
        # (which processes string-dependency images like this one) rewrite the
        # hardcoded clone into a ``git checkout ${BASE_COMMIT}`` + hardening
        # sequence pinned to whichever PR triggered the shared base build,
        # pruning every other PR's commit out of git history and breaking them.
        # The clone + checkout + hardening happen per-PR in
        # ``LobeHubImageDefaultLate`` instead. With no clone/COPY line here, the
        # enhancer leaves this base untouched apart from its infra block.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends git libvips-dev && rm -rf /var/lib/apt/lists/*

{self.clear_env}

"""


class LobeHubImageDefaultLate(Image):
    """PR-specific image for lobehub late era."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, Image]:
        return LobeHubImageBaseLate(self.pr, self.config)

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
                "prepare.sh",
                """\
#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
git checkout {base_sha}

# Install the exact pnpm version declared in packageManager field
PNPM_VERSION=$(node -e "try {{ const pm = require('./package.json').packageManager; if (pm && pm.startsWith('pnpm@')) console.log(pm.split('@')[1]); else console.log('latest'); }} catch(e) {{ console.log('latest'); }}")
npm install -g "pnpm@${{PNPM_VERSION}}"

pnpm install --no-frozen-lockfile || true

# Install deps for apps/ workspaces (nested, not in root workspace)
for app_dir in apps/*/; do
    if [ -d "${{app_dir}}" ] && [ -f "${{app_dir}}package.json" ]; then
        (cd "/home/{repo}/${{app_dir}}" && pnpm install --no-frozen-lockfile || true)
    fi
done
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash

export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{repo}

pnpm vitest run --reporter=verbose 2>&1 || true

for pkg_dir in packages/*/; do
    if [ -f "${{pkg_dir}}vitest.config.mts" ] || [ -f "${{pkg_dir}}vitest.config.ts" ]; then
        (cd "/home/{repo}/${{pkg_dir}}" && pnpm vitest run --reporter=verbose 2>&1) || true
    fi
done

for app_dir in apps/*/; do
    if [ -f "${{app_dir}}vitest.config.mts" ] || [ -f "${{app_dir}}vitest.config.ts" ]; then
        (cd "/home/{repo}/${{app_dir}}" && pnpm vitest run --reporter=verbose 2>&1) || true
    fi
done
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -e

export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch

set +e

pnpm vitest run --reporter=verbose 2>&1 || true

for pkg_dir in packages/*/; do
    if [ -f "${{pkg_dir}}vitest.config.mts" ] || [ -f "${{pkg_dir}}vitest.config.ts" ]; then
        (cd "/home/{repo}/${{pkg_dir}}" && pnpm vitest run --reporter=verbose 2>&1) || true
    fi
done

for app_dir in apps/*/; do
    if [ -f "${{app_dir}}vitest.config.mts" ] || [ -f "${{app_dir}}vitest.config.ts" ]; then
        (cd "/home/{repo}/${{app_dir}}" && pnpm vitest run --reporter=verbose 2>&1) || true
    fi
done
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -e

export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

set +e

pnpm vitest run --reporter=verbose 2>&1 || true

for pkg_dir in packages/*/; do
    if [ -f "${{pkg_dir}}vitest.config.mts" ] || [ -f "${{pkg_dir}}vitest.config.ts" ]; then
        (cd "/home/{repo}/${{pkg_dir}}" && pnpm vitest run --reporter=verbose 2>&1) || true
    fi
done

for app_dir in apps/*/; do
    if [ -f "${{app_dir}}vitest.config.mts" ] || [ -f "${{app_dir}}vitest.config.ts" ]; then
        (cd "/home/{repo}/${{app_dir}}" && pnpm vitest run --reporter=verbose 2>&1) || true
    fi
done
""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("ImageDefault dependency must be an Image")
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # This per-PR image chains to a base *Image* (not a string), so
        # DockerfileEnhancer returns this dockerfile verbatim and does NOT
        # auto-inject git-history hardening. We therefore clone, check out
        # ``${BASE_COMMIT}``, and apply ``Image._HARDENING_BLOCK`` manually so
        # the fix / future commits cannot be read out of git history (reward
        # hacking). ``BASE_COMMIT`` is pinned to *this* PR's ``base.sha``.
        return f"""FROM {name}:{tag}

ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("lobehub", "lobehub_13716_to_6474")
class LOBEHUB_13716_TO_6474(Instance):
    """Instance for lobehub PRs 6474-13716 (monorepo era, pnpm+vitest)."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return LobeHubImageDefaultLate(self.pr, self._config)

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

        for line in clean_log.splitlines():
            stripped = line.strip()
            if not stripped:
                continue

            # Vitest test-level pass: ✓ or ✔
            m = re.match(r"[✓✔]\s+(.+?)(?:\s+\(?\d+(?:\.\d+)?\s*m?s\)?)?$", stripped)
            if m:
                name = _clean_test_name(m.group(1))
                passed_tests.add(name)
                continue

            # Vitest test-level fail: × or ✕ or ✗
            m = re.match(r"[×✕✗]\s+(.+?)(?:\s+\(?\d+(?:\.\d+)?\s*m?s\)?)?$", stripped)
            if m:
                name = _clean_test_name(m.group(1))
                failed_tests.add(name)
                continue

            # Vitest file-level FAIL
            m = re.match(r"FAIL\s+(.+?)$", stripped)
            if m:
                name = _clean_test_name(m.group(1))
                failed_tests.add(name)
                continue

            # Vitest skipped: ↓ or ○
            m = re.match(r"[↓○]\s+(.+?)(?:\s+\[skipped\])?$", stripped)
            if m:
                name = _clean_test_name(m.group(1))
                skipped_tests.add(name)
                continue

        # Dedup: worst wins
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# ---------------------------------------------------------------------------
# Bundle routing by number_interval
#
# The raw dataset groups each release_line's PRs into a bundle. Instance.create()
# builds the lookup name as f"{org}/{pr.number_interval}" when number_interval is
# set, so each bundle's interval string must be registered against the era class.
#
# The interval is the dash-joined list of prs_in_bundle EXACTLY as stored (NOT a
# numeric range): [146, 147, 150, 155, 157] -> "146-147-150-155-157". A range like
# "146-157" would wrongly imply every PR in between; the explicit dash-join lists
# only the PRs actually in the bundle.
#
# All 31 lobehub bundles fall in the late era (every PR >= 6474), so they all map
# to LOBEHUB_13716_TO_6474. One entry per instance in
# lobehub__lobehub_lht_final.jsonl; each string is the anchor-first prs_in_bundle.
# ---------------------------------------------------------------------------
_NUMBER_INTERVALS = [
    "9988-12199-12377-12533-12603-12654-12704-12712-12729-12743-12745-12749-12752-12758-12761-12763-12764-12770-12771-12772-12774-12781-12784-12785-12787-12788-12795-12798-12799-12807-12808-12809-12827-12828-12834-12836-12838-12839-12842-12843-12844-12846-12856-12858-12863-12865-12866-12873-12876-12881-12885-12886-12887-12890-12892-12895-12896-12897-12901-12902-12903-12904-12905-12906-12908-12911-12912-12915-12918-12920-12922-12926-12929-12930-12931-12936-12938-12941-12949-12950-12956",
    "10011-10015-10628-11715-12002-12017-12061-12215-12272-12332-12371-12393-12404-12418-12422-12424-12430-12432-12433-12457-12458-12459-12465-12471-12474-12475-12480-12482-12485-12486-12487-12489-12493-12496-12506-12509-12511-12512-12513-12514-12515-12517-12518-12520-12525-12526-12528-12534-12538-12539-12542-12547-12548-12549-12550-12551-12553-12555-12561-12562-12563-12564-12567-12568-12572-12573-12574-12581-12582-12587-12588-12595-12597-12598-12601-12604-12606-12607-12610-12611-12612-12614-12615-12624-12627-12628-12629-12630-12631",
    "10622-12678-12760-13003-13134-13139-13222-13226-13257-13262-13277-13279-13296-13321-13329-13338-13340-13342-13343-13344-13345-13349-13358-13359-13364-13365-13368-13369-13370-13373-13378-13381-13382-13383-13384-13392-13393-13395-13397-13398-13399-13401-13405-13406-13407-13408-13409-13410-13412-13414-13415-13416-13417-13418-13419-13420-13421-13422-13427-13428-13429-13430-13432-13436-13437-13440-13442-13444-13446-13447-13450-13451-13452-13453-13454-13458-13464-13465-13466-13468-13469-13470-13472-13473-13477-13478-13479-13480-13481-13482-13483-13487-13489-13491-13492-13495-13496-13500-13502-13506-13507-13508-13511-13512-13514-13517-13519-13520-13521-13524-13525-13527-13528-13529-13533-13534-13535-13536-13537-13540-13545-13546-13550-13551-13552-13555-13556-13557-13561-13566-13568-13569-13570-13571-13584-13587-13603-13604-13605-13606-13607-13608-13613-13619-13626",
    "11235-11920-11925-12013-12016",
    "11708-12289-12337-12338-12350",
    "11893-11894",
    "11901-11905",
    "11908-11911",
    "11919-11927",
    "11926-12025-12059-12205-12206-12207-12208",
    "11929-11933",
    "12004-12058-12229-12234-12236-12237-12244-12248-12249-12252-12255",
    "12028-12032",
    "12088-12121-12124-12127-12132-12135-12139-12141-12145-12154-12155",
    "12094-12134-12157-12188-12197-12201-12202",
    "12165-12174-12176-12180-12181-12191-12194",
    "12190-12203",
    "12204-12219-12250-12258-12259-12265-12267-12268-12275-12277-12279-12280-12284-12285-12286-12297-12302-12304-12306-12308-12311-12312-12316-12318-12324-12325-12327-12331-12334-12336-12339-12341-12345-12347-12348-12353-12355-12356-12364-12365-12367-12370-12373-12374-12375-12376-12382-12383-12391-12392-12399-12400-12403-12419-12420-12421",
    "12209-12210-12214-12216-12217",
    "12254-13254-13518-13523-13548-13580-13597-13614-13620-13621-13624-13625-13628-13629-13630-13631-13633-13634-13635-13636-13640-13641-13643-13644-13645-13647-13648-13649-13651-13652-13654-13655-13656-13659-13661-13662-13663-13664-13665-13666-13667-13669-13671-13672-13676-13678-13680-13681-13682-13683-13684-13685-13688-13689-13690-13696-13698-13699-13700-13701-13704-13707-13711-13712-13715-13716",
    "12260-12264",
    "12293-12296-12313",
    "12310-12323-12537-12613-12634-12636-12638-12640-12643-12644-12645-12647-12652-12663-12669-12671-12674-12675-12686-12693-12697-12708-12721-12722-12727-12731-12735-12737-12744-12750-12757-12762-12765",
    "12333-12402-12406",
    "12410-12532",
    "12477-13051-13055-13092-13096-13113-13119-13124-13129-13146-13157-13159-13160-13161-13162-13163-13164-13165-13166-13169-13170-13171-13173-13184-13189-13190-13191-13194-13196-13197-13198-13200-13203-13204-13205-13206-13207-13208-13210-13211-13213-13216-13218-13219-13220-13221-13223-13224-13228-13229-13232-13234-13235-13236-13238-13239-13240-13241-13243-13246-13247-13249-13250-13252-13255-13260-13261-13265-13270-13286-13289-13294-13300-13301-13302-13303-13304-13305-13309-13312-13314-13315-13317-13318-13319-13320-13326-13330",
    "12720-13653-13725-13754-13818-13823-13850-13853-13874-13875-13880-13884-13887-13889-13893-13894-13896-13897-13899-13900-13902-13903-13904-13905-13906-13909-13911-13914-13916-13918-13919-13920-13923-13924-13929-13930-13931-13933-13934-13937-13938-13940-13942-13943-13945-13948-13950-13951-13952-13955-13956-13957-13959-13960-13961-13962-13963-13964-13968-13970-13972-13973-13978-13979-13980-13981-13982-13983-13986-13988-13989-13990-13992-13993-13995-13996-13997-13998-14000-14001-14004-14005-14006-14010-14012-14013-14014-14015-14017-14018-14019-14020-14024-14026-14029-14030-14032-14033-14034-14035-14036-14037-14038-14039-14040-14041-14042-14048-14052-14053-14054-14056-14057-14058-14064-14065-14067-14072-14076-14079-14080-14081-14083-14086-14088-14089-14090-14091-14092-14096-14097-14098-14099-14100-14102-14103-14105-14109-14113-14114-14118-14120-14121-14123-14131-14132-14134-14135-14136-14137-14138-14139-14140-14143-14144-14147-14148-14150-14151-14154-14155-14157-14158-14159-14161-14162-14163-14164-14165-14166-14167-14168-14169-14170-14171-14172-14174-14178-14179-14180-14181-14182-14186-14187-14191-14193-14194-14195-14196-14199-14201-14203-14204-14209-14217",
    "12860-12914-12951-12958-12960-12961-12962-12963-12964-12967-12968-12970-12972-12975-12976-12982-12984-12985-12993-12994-12996-13006-13008-13011-13012-13013-13016-13018-13020-13021-13022-13025-13035-13037-13038-13040-13041-13042-13048-13049-13054-13059-13060-13061-13062-13065-13066-13067-13070-13073-13077-13078-13081-13091-13093-13094-13100-13101-13103-13104-13108-13109-13110-13112-13114-13120-13121-13123-13125-13126-13130-13131-13132-13133-13136-13137-13140-13141-13142-13145-13148-13150-13151-13152-13153-13155",
    "12874-12939",
    "13313-13324-13585-13615-13925-14051-14055-14070-14094-14108-14115-14142-14160-14192-14198-14206-14207-14208-14211-14212-14214-14216-14219-14220-14222-14223-14225-14226-14228-14229-14230-14235-14237-14239-14242-14243-14244-14246-14247-14248-14249-14253-14257-14258-14261-14264-14266-14269-14270-14271-14272-14274-14275-14277-14278-14280-14281-14282-14285-14286-14288-14289-14290-14291-14294-14297-14301-14302-14303-14304-14306-14308-14309-14311-14312-14314-14315-14316-14317-14321-14322-14323-14324-14326-14327-14329-14330-14332-14333-14334-14336-14337-14338-14339-14340-14342-14343-14345-14346-14347-14348-14349-14350-14351-14352-14353-14355-14357-14358-14364-14366-14367-14371-14372-14374-14375-14377-14378-14379-14382-14383-14391-14392-14397-14398-14399-14402-14403-14404-14406-14407-14408-14409-14410-14411-14414-14415-14418-14419-14420-14422-14423-14424-14425-14428-14429-14431-14432-14433-14434-14435-14436-14437-14438-14439-14440-14442-14443-14444-14445-14446-14448-14451-14452-14453-14454-14456-14457-14459-14460-14461-14462-14463-14468-14469-14470-14471-14472-14473-14474-14475-14476-14478-14480-14483-14484-14486-14487-14488-14489-14491-14493-14494-14495-14496-14497-14499-14500-14503-14504-14505-14506-14508-14510-14512-14513-14514-14515-14516-14517-14518-14519-14520-14521-14524-14525-14526-14531-14533-14534-14535-14536-14537-14538-14539-14540-14541-14542-14543-14546-14549-14550-14552-14553-14555-14558-14560-14563-14576",
    "13400-13413-13457-13703-13714-13717-13718-13719-13722-13723-13724-13728-13730-13731-13732-13733-13734-13736-13738-13739-13740-13741-13742-13744-13748-13749-13751-13752-13753-13756-13757-13759-13760-13761-13762-13763-13764-13765-13766-13767-13768-13769-13771-13772-13774-13778-13780-13781-13787-13788-13790-13792-13793-13794-13795-13799-13804-13805-13806-13807-13808-13809-13812-13814-13815-13816-13817-13819-13820-13821-13822-13824-13825-13828-13829-13830-13838-13840-13841-13842-13843-13847-13852-13854-13855-13856-13857-13860-13863-13865-13866-13867-13868-13871-13872-13873-13876-13877-13878-13883-13888-13895-13898",
]
for _interval in _NUMBER_INTERVALS:
    Instance.register("lobehub", _interval)(LOBEHUB_13716_TO_6474)
