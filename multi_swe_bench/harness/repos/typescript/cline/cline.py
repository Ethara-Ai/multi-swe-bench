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
        return "node:lts-bookworm"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # Shared base for every cline PR (built once, tag "base"). The `# syntax`
        # directive opts out of DockerfileEnhancer so this hand-written §3 reference
        # layout is used verbatim: clone FULL history + light harden only. The strict
        # anti-reward-hack strip runs in the PR layer at each PR's base.sha.
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()
        org = self.pr.org
        repo = self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        ca-certificates git jq xvfb unzip curl \\
    && rm -rf /var/lib/apt/lists/*

# Install bun
RUN curl -fsSL https://bun.sh/install | bash
ENV BUN_INSTALL="/root/.bun"
ENV PATH="$BUN_INSTALL/bin:$PATH"

RUN git config --global --add safe.directory '*'
RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

CMD ["/bin/bash"]
"""


_PREPARE_SCRIPT = """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

bun install || true

# ensure the mocha/ts-node unit runner deps exist (older revisions omit them)
[ -f node_modules/tsconfig-paths/register.js ] || bun add -d tsconfig-paths >/dev/null 2>&1 || npm i -D tsconfig-paths --no-save >/dev/null 2>&1 || true
[ -x node_modules/.bin/ts-node ] || bun add -d ts-node >/dev/null 2>&1 || npm i -D ts-node --no-save >/dev/null 2>&1 || true

# webview-ui: prefer npm ci so lockfile-pinned RTL peer (@testing-library/dom) installs
if [ -d webview-ui ] && [ -f webview-ui/package.json ]; then
  (cd webview-ui && (npm ci || npm install --legacy-peer-deps) || true)
fi

if [ -d cli ] && [ -f cli/package.json ]; then
  (cd cli && (npm ci || npm install || bun install) || true)
fi
"""


_GIT_APPLY_EXCLUDES = (
    "--exclude=package-lock.json "
    "--exclude=webview-ui/package-lock.json "
    "--exclude=cli/package-lock.json "
)


_TEST_SCRIPT = """set +e
cd /home/{repo}

# generate grpc/proto stubs -- many unit tests import generated code
if grep -q '"protos"' package.json 2>/dev/null; then
  bun run protos > /dev/null 2>&1 || npm run protos > /dev/null 2>&1 || true
fi

# unit tests: mocha + ts-node in transpile-only mode so a fix-dependent test file FAILS at
# runtime (genuine f2p) instead of the type-checker aborting the whole suite (compile-break n2p inflation)
REQ_ARGS="--require ts-node/register --require source-map-support/register"
[ -f node_modules/tsconfig-paths/register.js ] && REQ_ARGS="--require tsconfig-paths/register $REQ_ARGS"
if [ -f src/test/requires.ts ]; then
  REQ_ARGS="$REQ_ARGS --require ./src/test/requires.ts"
elif [ -f /home/vscode_mock_hook.js ]; then
  REQ_ARGS="$REQ_ARGS --require /home/vscode_mock_hook.js"
fi
TSP=""
for c in tsconfig.unit-test.json tsconfig.test.json; do [ -f "$c" ] && TSP="$c" && break; done
export TS_NODE_TRANSPILE_ONLY=1
export TS_NODE_COMPILER_OPTIONS='{{"module":"commonjs","moduleResolution":"node"}}'
[ -n "$TSP" ] && export TS_NODE_PROJECT="$TSP"

if [ -f .mocharc.json ] || [ -f .mocharc.js ] || [ -f .mocharc.cjs ]; then
  node_modules/.bin/mocha --reporter spec $REQ_ARGS 2>&1
else
  UNIT=$( (find src -path '*/__tests__/*.ts' ! -name '*.d.ts' 2>/dev/null; find src -name '*.test.ts' ! -path '*/e2e/*' ! -path '*/integration/*' 2>/dev/null) | sort -u )
  [ -n "$UNIT" ] && node_modules/.bin/mocha --reporter spec $REQ_ARGS $UNIT 2>&1
fi

# webview-ui unit tests (vitest isolates per-file)
if [ -d webview-ui ] && [ -f webview-ui/package.json ]; then
  (cd webview-ui && (node_modules/.bin/vitest run --reporter=verbose 2>&1 || npx --no-install vitest run --reporter=verbose 2>&1)) || true
fi

# cli unit tests (vitest)
if [ -d cli ] && [ -f cli/package.json ]; then
  (cd cli && (node_modules/.bin/vitest run --reporter=verbose 2>&1 || npx --no-install vitest run --reporter=verbose 2>&1)) || true
fi
"""


_VSCODE_MOCK_HOOK = """const Module = require("module")
const orig = Module.prototype.require
function stub() {
  return new Proxy(function () {}, {
    get(t, p) {
      if (p === "then") return undefined
      if (p === Symbol.toPrimitive) return () => ""
      return stub()
    },
    apply() { return stub() },
    construct() { return stub() },
  })
}
const vscodeMock = new Proxy({
  EventEmitter: class { constructor() { this.event = () => ({ dispose() {} }) } fire() {} dispose() {} },
  Uri: { file: (f) => ({ fsPath: f, path: f, scheme: "file", toString: () => f }), parse: (s) => ({ fsPath: s, path: s, toString: () => s }), joinPath: (...a) => ({ fsPath: a.join("/"), path: a.join("/") }) },
  Disposable: class { dispose() {} },
  workspace: { getConfiguration: () => ({ get: () => undefined, has: () => false, update: () => Promise.resolve() }), workspaceFolders: [], onDidChangeConfiguration: () => ({ dispose() {} }), fs: {} },
  window: { showErrorMessage: () => Promise.resolve(), showInformationMessage: () => Promise.resolve(), createOutputChannel: () => ({ appendLine() {}, append() {}, dispose() {} }), activeTextEditor: undefined },
  commands: { registerCommand: () => ({ dispose() {} }), executeCommand: () => Promise.resolve() },
  env: {}, ExtensionMode: { Test: 3, Development: 2, Production: 1 },
}, { get(t, p) { return p in t ? t[p] : stub() } })
Module.prototype.require = function (path) {
  if (path === "vscode") return vscodeMock
  return orig.apply(this, arguments)
}
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
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "vscode_mock_hook.js", _VSCODE_MOCK_HOOK),
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
""",
            ),
            File(
                ".",
                "prepare.sh",
                _PREPARE_SCRIPT.format(org=self.pr.org, repo=self.pr.repo, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                "#!/bin/bash\n" + _TEST_SCRIPT.format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                "#!/bin/bash\ncd /home/{repo}\n".format(repo=self.pr.repo)
                + "git apply --reject --exclude=package-lock.json --exclude=webview-ui/package-lock.json --exclude=cli/package-lock.json --exclude=bun.lockb --whitespace=nowarn /home/test.patch || true\n"
                + _TEST_SCRIPT.format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                "#!/bin/bash\ncd /home/{repo}\n".format(repo=self.pr.repo)
                + "git apply --reject --exclude=package-lock.json --exclude=webview-ui/package-lock.json --exclude=cli/package-lock.json --exclude=bun.lockb --whitespace=nowarn /home/test.patch /home/fix.patch || true\n"
                + _TEST_SCRIPT.format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "\n".join(f"COPY {f.name} /home/" for f in self.files())

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{self._HARDENING_BLOCK}

{self.clear_env}
"""


@Instance.register("cline", "cline")
class Cline(Instance):
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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi_re = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

        mocha_pass = re.compile(r"^\s*[✓✔]\s+(.+?)(?:\s+\(\d+\w+\))?\s*$")
        mocha_fail_num = re.compile(r"^\s*\d+\)\s+(.+?)\s*$")
        mocha_pending = re.compile(r"^\s*-\s+(.+?)\s*$")

        # trailing timing: both "(1ms)" and bare "1ms" formats
        vitest_pass = re.compile(r"^\s*[✓✔]\s+(?:\S+\s+)?(\S+\.(?:test|spec)\.[jt]sx?)(?:\s+>\s+(.+?))?(?:\s+\d+ms|\s+\(\d+ms\))?\s*$")
        vitest_fail = re.compile(r"^\s*[×✗✘]\s+(?:\S+\s+)?(\S+\.(?:test|spec)\.[jt]sx?)(?:\s+>\s+(.+?))?(?:\s+\d+ms|\s+\(\d+ms\))?\s*$")
        vitest_skip = re.compile(r"^\s*[↓⊝]\s+(?:\S+\s+)?(\S+\.(?:test|spec)\.[jt]sx?)(?:\s+>\s+(.+?))?(?:\s+\d+ms|\s+\(\d+ms\))?\s*$")

        in_failure_listing = False

        for raw in test_log.splitlines():
            line = ansi_re.sub("", raw).rstrip()
            if not line:
                continue

            if re.match(r"^\s*\d+\s+failing", line):
                in_failure_listing = True
                continue
            if re.match(r"^\s*\d+\s+passing", line):
                in_failure_listing = False
                continue
            if re.match(r"^\s*\d+\s+pending", line):
                continue

            m = vitest_pass.match(line)
            if m:
                name = f"{m.group(1)} > {m.group(2)}" if m.group(2) else m.group(1)
                passed_tests.add(name)
                continue

            m = vitest_fail.match(line)
            if m:
                name = f"{m.group(1)} > {m.group(2)}" if m.group(2) else m.group(1)
                failed_tests.add(name)
                continue

            m = vitest_skip.match(line)
            if m:
                name = f"{m.group(1)} > {m.group(2)}" if m.group(2) else m.group(1)
                skipped_tests.add(name)
                continue

            if in_failure_listing:
                m = mocha_fail_num.match(line)
                if m:
                    failed_tests.add(m.group(1))
                    continue

            m = mocha_pass.match(line)
            if m:
                passed_tests.add(m.group(1))
                continue

            m = mocha_pending.match(line)
            if m:
                name = m.group(1)
                if not name.startswith("--") and len(name) < 200:
                    skipped_tests.add(name)
                continue

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


_BUNDLE_NIS_CLINE = [
    "10090-10099-10117-10127-10153-10155-10161-10173-10207-10227",
    "10195-10196-10271-10283-10290-10295-10323-10338-10344-10353-10354-10355-10356",
    "10343-10363-10365-10374-10383-10390-10395",
    "10382-10394-10409-10435-10436-10443-10477-10485-10490-10503",
    "1777-2347-2963-2964-2976-2980-2981-2982-2985-2999-3004-3005-3010-3014-3021-3024-3028-3041-3043-3044-3045-3046-3047-3048-3049-3052-3053-3063-3066-3070-3071-3072-3082",
    "2402-2699-2741-2745-2747-2748-2749-2750-2754-2755",
    "2423-2627-2676-2677-2703-2707-2716-2720-2723-2727-2729-2731-2733-2734-2735-2746",
    "2599-2648-2651-2658-2660-2661-2669-2672-2673-2674-2675-2679-2683-2685-2686-2688-2694-2696",
    "2600-2684-2758-2795-2836-2864-2874-2878-2887-2892",
    "2752-2756-2765-2766-2768-2770-2773-2775-2776-2777-2778-2780-2785-2786",
    "2850-2851-2852-2853-2854-2855-2856-2857",
    "3454-3469-3530-3534-3535-3539-3540-3568-3569-3571-3575-3586",
    "3487-3488-3489-3490-3491-3492-3494-3496-3498-3502-3503-3504-3520-3522",
    "3596-3692-3746-3747-3780-3854-3929-3970-3987-3989-3991-3999-4008-4009-4019-4020-4038-4040-4041",
    "3976-4023-4033-4042-4053",
    "3986-4072-4101-4121-4125-4127-4129-4130-4132-4137-4139-4140-4141-4143-4144-4152-4153-4158-4170-4171-4175-4185",
    "5047-5368-5379-5392-5396-5400-5407-5413",
    "5223-5298-5308-5325-5326",
    "5275-5361-5371-5375-5376-5377-5381-5382-5383",
    "5294-5317-5328",
    "5295-5296-5297",
    "5316-5423-5499-5539-5545-5591-5617-5628-5630-5631-5634-5635-5642-5650-5656-5665-5668-5669-5673-5675-5686-5688-5689-5690",
    "5323-5324-5327-5329-5330",
    "5399-5463-5465-5466-5467-5469-5470",
    "5409-5505-5520-5542-5543-5547-5548-5549-5551-5561-5563-5569-5574-5575-5577-5578-5579-5581",
    "5835-6452-6576-6582-6595-6597-6599-6614-6615-6616-6618-6619-6624-6627-6628-6629-6630-6631-6644-6655-6657-6661-6663-6666-6668-6669-6670-6673-6674-6675-6680-6682-6690-6691-6695-6700-6701-6703-6711-6712",
    "6222-6263-6276-6277-6279-6280-6282-6287-6290",
    "6374-6377-6550",
    "6418-6507-6511-6513-6522-6523-6525-6527-6529-6530-6544",
    "6423-6458-6466-6468-6470-6485-6487",
    "6512-6537-6539-6542-6551-6556-6558",
    "6872-6921-6953-6980-7001-7086-7087-7090-7120-7121-7122-7123-7127-7129-7137-7139-7143-7144-7145-7147-7148-7149-7150-7157-7158-7159-7160-7163-7168-7170-7174-7175-7176-7177-7178-7179-7180-7186-7191-7192-7193",
    "6977-7004-7018-7027-7041-7045-7047-7054-7061-7066-7083-7089-7094-7106-7118-7119",
    "6988-7063-7126-7162-7171-7197-7198-7202-7205-7207-7214-7215-7217-7225-7246-7247-7248-7250",
    "7828-7832-7966-8022-8024-8052-8064-8065",
    "8038-8284-8335-8344-8349-8351-8388-8395-8396-8397-8398-8407-8408-8409-8410-8411-8425-8426-8431-8432-8437-8438-8439-8440-8441-8442-8449-8451-8453-8459",
    "8063-8107-8110-8135-8143-8144-8145-8148",
    "8645-8785-8787-8790-8820-8837-8839",
    "8969-9205-9272-9284",
    "9104-9145-9146-9242-9262-9263-9264-9266-9267-9270",
    "9234-9306-9307-9309-9311-9312-9313",
    "9346-9350-9356-9360-9361",
    "9424-9606-9635-9671-9679-9680-9681-9686-9690-9705-9707-9721-9725-9726-9731-9735-9736-9739-9741-9742-9745-9749-9752-9763-9764-9772-9773-9775",
    "9562-9642-9658-9688-9692-9698-9699-9706",
    "10024-10056-10164-10200-10226-10230-10236-10264-10266-10269-10274-10286-10291-10292",
    "10350-10461-10478-10539-10567-10568-10574-10575-10578-10592-10593-10641-10647-10648-10652-10659-10660-10661-10662-10663-10668-10669-10670-10672-10673-10683-10689-10696-10708-10710-10711-10717-10718-10719-10722-10725-10727",
    "1136-1808-2334-2379-2759-2830-2843-2890-2893-2900-2910-2912-2913-2914-2922-2927-2932-2934-2935-2936-2939-2953-2954-2956-2958-2959-2960-2962-2973-2974-2977-2983",
    "1464-2969-3400-3474-3672-3814-3817-3860-3867-3868-3876-3880-3882-3890-3894-3897-3917-3920-3922-3925-3936-3938-3939-3940-3942-3943-3944-3945-3946-3947-3948-3949-3956-3957-3967-3968-3971-3973-3975-3977-3978-3981-3993-3994-3995",
    "2056-3422-3425-3443-3444-3447-3448-3449-3450-3453-3459-3470-3471-3473-3477-3485-3486",
    "2078-2918-2930-3003-3011-3029-3030-3060-3079-3106-3109-3115-3116-3121-3122-3124-3126-3141-3147-3154-3170-3171-3173-3174-3175-3176-3179-3180-3181-3184-3190-3196-3197-3213-3214-3217-3218-3220-3221-3222-3224-3225-3229-3230-3237-3239-3240-3241-3242-3243-3245-3255",
    "2190-2680-2708-2722-2728-2736-2743-2779-2781-2793-2797-2798-2800-2802-2803-2805-2806-2807-2808-2810-2811-2812-2814-2815-2816-2817-2821-2827-2829-2833-2837-2839-2841",
    "2789-3423-3554-3630-3661-3686-3699-3708-3712-3713-3715-3716-3720-3721-3730-3733-3734",
    "3246-3275-3588-3606-3607-3612-3614-3615-3628-3638-3640-3647-3648-3650-3652-3659-3667-3668-3669-3670-3675-3676-3677-3680-3681-3682-3683",
    "3419-3608-3609-3691-3695-3719-3754-3765-3778-3783-3785-3786-3787-3810-3816-3820-3824-3834-3836-3846-3857-3859",
    "3456-3935-3980-4029-4079-4111-4114-4150-4155-4162-4166-4176-4179-4190-4199-4211-4217-4218-4221-4225-4226-4229-4232-4235-4236-4240-4242-4259-4264",
    "3528-3621-3649-3723-3800-4010-4012-4014-4045-4048-4058-4061-4063-4065-4070-4073-4077-4086-4088-4097-4098-4100-4104-4107-4112-4115-4123",
    "3579-3779-3840-3996-4083-4154-4164-4193-4209-4222-4237-4243-4246-4251-4260-4263-4266-4267-4269-4270-4278-4282-4288-4293-4296-4298-4299-4304-4305-4306-4309-4318-4320-4325-4329",
    "3635-4233-4443-4448-4474-4476-4490-4492-4493-4494-4496-4497-4499-4500-4504-4506-4507-4508-4509-4510-4511-4512-4513-4514-4515-4517-4518-4528-4529-4533-4536-4539-4542-4556",
    "3674-3711-4314-4398-4405-4421-4422-4427-4428-4429-4432-4435-4437-4438-4440-4441-4446-4461-4463-4464-4466-4467-4468-4470-4472-4473-4475",
    "3696-3801-3835-4056-4147-4287-4307-4323-4324-4326-4336-4337-4338-4339-4340-4343-4344-4401-4402-4404-4407-4408-4414",
    "3709-3741-3750",
    "4118-4990-5239-5406-5408-5414-5424-5425-5428-5434-5439-5443-5446-5464",
    "4159-4728-4841-4849-4852-4862-4865-4866-4867-4875-4877-4889-4890-4891-4894-4904-4905-4906",
    "4186-4411-4624-4641-4681-4748-4882-5013-5034-5051-5071-5095-5158-5159-5173-5174-5178-5181-5182-5183-5184-5185-5189-5193-5194-5195-5196-5207-5219-5220-5221-5224-5227-5234-5236-5241-5242-5245-5261-5262-5263",
    "4412-4598-4673-4709-4901-4922-4954-4955-4960-4966-4969-4972-4974-4975-4976",
    "4418-4457-4477-4537-4553-4554-4569-4581-4583-4586-4587-4588-4589-4591-4592-4593-4595-4596-4597-4600-4601-4602-4604-4605-4618-4620-4621-4622-4627-4637",
    "4501-4827-4997-4999-5002-5008-5009-5010-5011-5014-5015-5020-5021-5022-5023-5024-5027-5029-5030-5031-5037-5041-5044-5045-5049-5057-5059-5063-5064-5076-5077-5078-5080-5081-5082-5088-5091",
    "4502-4696-4702-4708-4712-4714-4715-4731-4732",
    "4505-5210-5260-5266-5270-5276-5278-5279-5281-5282",
    "4619-4630-4635-4636-4644",
    "4645-4647-4648-4649-4651-4655-4661-4664-4677-4685-4698-4699",
    "4721-4845-4879-4903-4920-4926-4935-4936-4937-4938-4941-4942-4943-4944-4945-4946-4947-4948-4949-4953",
    "4871-5346-5367-5404-5422-5426-5444-5448-5462-5472-5473-5476-5478-5479-5481-5482-5483-5490-5493-5494-5496-5498-5500-5501-5504-5507-5508-5510-5515-5516-5517-5521-5522-5524-5525-5526-5527-5528-5534-5535-5536-5537-5546",
    "4952-4961-4968-4977-4978-4980-4981-4983-4984-4987-4993-5003",
    "5110-5132-5142-5150-5151-5154-5155-5161-5162-5170-5171-5177-5179",
    "5222-5238-5280-5299-5309-5321-5322-5332-5333-5334-5343-5347-5351-5352-5353-5354-5356-5357-5359-5362-5363-5364-5369",
    "5315-5355-5435-5615-5651-5655-5676-5682-5692-5693-5696-5697-5698-5700-5701-5702-5714-5715-5716",
    "5531-5584-5592-5596-5600-5602-5604-5607-5609-5612-5614-5626-5632-5633",
    "5640-5711-5717-5718-5736-5737-5738-5741-5742-5743-5744",
    "5661-5662-5691-5729-5735-5751-5767-5768-5769-5770-5776-5781-5782",
    "5732-6128-6136-6139-6150-6153-6157-6168",
    "6014-6078-6342-6444-6462-6463-6499-6553-6559-6573-6575-6579-6580-6583-6585-6589-6590",
    "6049-6096-6131-6208-6273-6288-6292-6294-6311-6320-6332-6336-6359-6363-6379-6384-6385-6390-6391-6393-6399-6402-6403-6404-6405-6407-6408-6409-6410-6412-6413-6414-6415-6419-6422-6424-6437-6441-6442-6443-6445-6446-6448",
    "6244-6262-6272-6289-6299-6300-6315-6316-6323",
    "6420-6453-6471-6474-6488-6491-6492-6493-6495-6498-6509",
    "6547-7350-7362-7391-7439-7448-7449-7454-7455-7473-7475-7477-7478-7479-7481-7482-7502-7508-7515-7516-7520-7521-7522-7523-7525-7530-7532-7533-7536-7537-7540-7542",
    "7040-7173-7196-7200-7206-7209-7230-7255-7259-7263-7270-7271-7273-7277-7278-7279-7280-7283-7284-7285-7291-7300-7302-7303-7304-7306-7309-7311-7313-7314-7316",
    "7059-7088-7141-7142-7256-7268-7299-7307-7319-7328-7336-7337-7340-7345-7348-7352-7357-7358-7359-7363-7364-7368-7369-7376-7385-7390-7394-7396-7397-7399-7404-7410-7411-7412-7416-7417-7419-7420-7427-7428-7429-7434-7437-7440-7441-7443-7444-7445",
    "7099-7881-8169-8263-8265-8269-8271-8278-8280-8283-8286-8291-8292-8311-8317-8319-8321-8322-8324-8327-8328-8329-8330-8331-8340-8341-8345-8346-8355-8376-8385-8386-8387-8390-8402-8404-8405-8406",
    "7189-7282-7317-7324-7331-7338-7341-7342-7347",
    "7329-7589-7630-7642-7656-7702-7713-7715-7717-7729-7739-7745-7749-7760-7761-7762-7763-7764-7765-7769-7770-7773-7774-7776-7781-7783-7786-7787-7796-7798-7799-7804-7806-7808-7812-7813",
    "7712-7719-7754-7777-7795-7797-7809-7823-7831-7840-7841-7842-7850-7851-7865-7869-7874-7875-7880",
    "7888-8066-8735-8818-8835-8873-8874-8892-8895-8897-8899-8900",
    "7901-7977-7993-8029-8049-8055-8079-8083-8089-8098-8106-8112-8114-8122-8124-8128",
    "8053-8308-8360-8428-8435-8445-8478-8554-8555-8563-8564-8621-8628-8633-8643-8648-8650-8651-8653-8654-8664-8665-8666-8668-8669-8670-8671-8672-8673-8677-8690-8693-8694-8695-8696-8710-8715-8716-8722-8727-8732-8734-8738-8740-8742-8747-8748-8749-8751-8753-8754-8757-8759-8763-8764-8765-8772-8773-8774-8777-8791-8796",
    "8057-8115-8421-8611-8614-8627-8631-8632-8634-8641-8642",
    "8117-8571-8573-8574-8575-8576-8602-8618-8619-8623",
    "8175-8264-8304-8359-8413-8415-8434-8447-8470-8473-8489-8490-8496-8500-8521-8522-8527-8528-8529-8546-8547-8548-8549-8550-8558-8569",
    "8298-8544-8717-8741-8783-8784-8800-8804-8813-8814-8815-8830-8831-8832-8833",
    "8592-8843-8872-8898-8903-8913-8917-8922-8925-8931-8932-8937-8941-8952-8956-8965-8966",
    "8788-8840-8841-8842-8863-8867-8871-8875-8889",
    "8789-8926-8989-9051-9100-9113-9121-9124-9131-9133-9135-9144-9149-9150-9152-9154-9156-9158-9159-9160-9161-9168-9173-9177-9178-9180-9184-9185-9188-9192-9194-9195-9202-9204-9208-9210-9211-9212-9216-9219-9220-9229-9230-9231-9235-9237-9238-9251-9252-9254-9256-9258",
    "8803-9343-9730-9732-9738-9743-9747-9753-9762-9782-9783-9800-9812-9825-9833",
    "9089-9276-9345-9348-9364-9365-9372-9377-9379-9381-9387-9389-9393",
    "9102-9392-9449-9487-9500-9529-9544-9545-9547-9568-9569-9577",
    "9103-9253-9280-9314-9316-9318-9320-9330-9334-9335-9338-9341",
    "9227-9271-9315-9396-9401-9402-9408-9409-9413-9414-9417",
    "9245-9370-9376-9390-9400-9405-9411-9420-9421-9423-9429-9432-9438-9444-9447-9458-9459-9479-9491-9495-9496-9501-9502-9503-9505-9506-9509",
    "9259-9394-9499-9508-9511-9519-9523-9526-9528-9533-9534",
    "9434-9532-9552-9583-9584-9597-9612-9620-9633-9640",
    "9494-9711-9909-9958-9988-9997-10005-10012-10019-10057-10060-10062-10081-10085",
    "9576-9581-9626-9634-9637-9643-9646-9657-9664",
    "9632-9799-9810-9834-9836-9837-9839-9852-9867-9870-9878-9879",
    "9710-9874-9886-9914-9917-9933-9956-9959-9963-9973-9974-9980-9986",
    "9856-9858-9868-9871-9896-9898-9905-9908-9910",
]

for _ni in _BUNDLE_NIS_CLINE:
    Instance.register("cline", _ni)(Cline)
