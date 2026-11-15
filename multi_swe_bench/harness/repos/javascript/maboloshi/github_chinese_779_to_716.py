import re
from pathlib import Path

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "maboloshi"
_REPO = "github-chinese"
_ERA_FILE = Path(__file__).stem
_ERA_MATCH = re.fullmatch(r"github_chinese_(\d+)_to_(\d+)", _ERA_FILE)
if _ERA_MATCH is None:
    raise ImportError(
        f"{__file__}: file name must look like github_chinese_<hi>_to_<lo>.py"
    )
_ERA_HI = int(_ERA_MATCH.group(1))
_ERA_LO = int(_ERA_MATCH.group(2))
_BASE_TAG = f"base-{_ERA_HI}_to_{_ERA_LO}"

NODE_IMAGE = "node:24.19.0-bookworm"

_ENV_EXPORTS = "export CI=true\nexport NODE_OPTIONS=--max-old-space-size=4096\n"

_TEST_CMD = "bash /home/run_tests.sh\n"

_APPLY_EXCLUDES = "--exclude='package-lock.json' --exclude='*/package-lock.json'"

_TEST_FILE_RE = re.compile(r"^test/.+\.test\.cjs$")

_CHECK_GIT_CHANGES = """#!/bin/bash
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
"""


def _test_files(pr: PullRequest) -> list[str]:
    files = re.findall(r"^diff --git a/(\S+) b/", pr.test_patch, re.MULTILINE)
    return sorted({f for f in files if _TEST_FILE_RE.match(f)})


_PATCH_FILE_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$")
_TITLE_RE = re.compile(r"^\s*test\(\s*(['\"`])((?:(?!\1)[^\\]|\\.)+)\1\s*,")
_STR_RE = re.compile(r"'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"")
_ARRAY_RE = re.compile(r"const\s+(\w+)\s*=\s*\[")
_FOR_RE = re.compile(r"for\s*\(\s*const\s+(\[[^\]]*\]|\w+)\s+of\s+(\w+|\[)")
_TPL_VAR_RE = re.compile(r"\$\{(\w+)\}")


def _span(text: str, start: int, open_ch: str, close_ch: str) -> int:
    depth = 0
    for k in range(start, len(text)):
        if text[k] == open_ch:
            depth += 1
        elif text[k] == close_ch:
            depth -= 1
            if depth == 0:
                return k
    return len(text) - 1


def _literals(chunk: str) -> list[str]:
    return [a or b for a, b in _STR_RE.findall(chunk)]


def _rows(chunk: str) -> list[list[str]]:
    inner = chunk[1:-1]
    nested = re.findall(r"\[([^\[\]]*)\]", inner)
    if nested:
        return [_literals(r) for r in nested]
    return [[v] for v in _literals(inner)]


def _expand_titles(text: str, added: list[tuple[int, int]]) -> set[str]:
    arrays: dict[str, list[list[str]]] = {}
    for m in _ARRAY_RE.finditer(text):
        end = _span(text, m.end() - 1, "[", "]")
        arrays[m.group(1)] = _rows(text[m.end() - 1 : end + 1])

    loops: list[tuple[int, int, dict[str, list[str]]]] = []
    for m in _FOR_RE.finditer(text):
        names = [n.strip() for n in m.group(1).strip("[]").split(",")]
        if m.group(2) == "[":
            end = _span(text, m.end() - 1, "[", "]")
            rows = _rows(text[m.end() - 1 : end + 1])
            search_from = end
        else:
            rows = arrays.get(m.group(2), [])
            search_from = m.end()
        brace = text.find("{", search_from)
        if brace < 0 or not rows:
            continue
        body_end = _span(text, brace, "{", "}")
        bound = {
            name: [row[idx] for row in rows if idx < len(row)]
            for idx, name in enumerate(names)
        }
        loops.append((brace, body_end, bound))

    titles: set[str] = set()
    for start, end in added:
        m = _TITLE_RE.match(text[start:end])
        if not m:
            continue
        raw = re.sub(r"\\(.)", r"\1", m.group(2))
        variables = _TPL_VAR_RE.findall(raw) if m.group(1) == "`" else []
        if not variables:
            titles.add(raw)
            continue
        for brace, body_end, bound in loops:
            if not brace < start < body_end:
                continue
            if all(v in bound for v in variables):
                count = min(len(bound[v]) for v in variables)
                for k in range(count):
                    titles.add(
                        _TPL_VAR_RE.sub(lambda mm: bound[mm.group(1)][k], raw)
                    )
                break
    return titles


def _patch_added_titles(test_patch: str) -> dict[str, set[str]]:
    blocks: dict[str, list[tuple[str, bool]]] = {}
    current: str | None = None
    for line in test_patch.split("\n"):
        m = _PATCH_FILE_RE.match(line)
        if m:
            current = m.group(2)
            blocks.setdefault(current, [])
            continue
        if current is None or line.startswith(("+++", "---", "@@", "\\")):
            continue
        if line.startswith("+"):
            blocks[current].append((line[1:], True))
        elif line.startswith(" "):
            blocks[current].append((line[1:], False))

    titles: dict[str, set[str]] = {}
    for path, lines in blocks.items():
        text = ""
        added: list[tuple[int, int]] = []
        for content, is_added in lines:
            if is_added:
                added.append((len(text), len(text) + len(content)))
            text += content + "\n"
        found = _expand_titles(text, added)
        if found:
            titles[path] = found
    return titles


class GithubChineseImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return NODE_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo
        enh = DockerfileEnhancer

        label_block = (
            f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
            f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        return f"""{enh.SYNTAX_DIRECTIVE}

FROM {image_name}

{enh._TARGETARCH_ARG}
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{enh._PROXY_ARGS}

{enh._ENV_BLOCK}

{label_block}

{enh._CERT_SYMLINKS}

{self.global_env}

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class GithubChineseImageDefault(Image):
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
        return GithubChineseImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_sh(self) -> str:
        repo = self.pr.repo
        sha = self.pr.base.sha
        return f"""#!/bin/bash
set -e

{_ENV_EXPORTS}
cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
test "$(git rev-parse HEAD)" = "{sha}"
bash /home/check_git_changes.sh

node --version
if [ -f package.json ]; then
  timeout --kill-after=30 900 npm ci --no-audit --no-fund || true
  git diff --name-only -- 'package-lock.json' | xargs -r git checkout --
fi

cat > /home/run_tests.sh <<'MSWE_RUN_TESTS_EOF'
{self._run_tests_sh()}MSWE_RUN_TESTS_EOF
chmod +x /home/run_tests.sh

node -e "if (Number(process.versions.node.split('.')[0]) < 22) throw new Error('node>=22 required'); require('node:test'); require('node:assert/strict'); require('node:vm'); console.log('DEPS_OK')"
test -s /home/run_tests.sh
test -d test
test -f main.user.js
test -f locals.js
"""

    def _run_tests_sh(self) -> str:
        repo = self.pr.repo
        files = " ".join(f"'{f}'" for f in _test_files(self.pr))
        return f"""#!/bin/bash
set -uo pipefail

cd /home/{repo}

status=0
for f in {files}; do
  if [ ! -f "$f" ]; then
    echo "━━━ Skipping $f (file not found) ━━━"
    continue
  fi
  echo "━━━ Running $f ━━━"
  timeout --kill-after=30 600 node --test-reporter=tap "$f" 2>&1
  rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "MSWE_FILE PASS $f"
  else
    echo "MSWE_FILE FAIL $f"
    status=1
  fi
done
exit $status
"""

    def _run_sh(self) -> str:
        return f"""#!/bin/bash
set -eo pipefail

{_ENV_EXPORTS}
cd /home/{self.pr.repo}
{_TEST_CMD}"""

    def _test_run_sh(self) -> str:
        return f"""#!/bin/bash
set -eo pipefail

{_ENV_EXPORTS}
cd /home/{self.pr.repo}
git apply --whitespace=nowarn {_APPLY_EXCLUDES} /home/test.patch
{_TEST_CMD}"""

    def _fix_run_sh(self) -> str:
        return f"""#!/bin/bash
set -eo pipefail

{_ENV_EXPORTS}
cd /home/{self.pr.repo}
git apply --whitespace=nowarn {_APPLY_EXCLUDES} /home/test.patch /home/fix.patch
{_TEST_CMD}"""

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", self._prepare_sh()),
            File(".", "run.sh", self._run_sh()),
            File(".", "test-run.sh", self._test_run_sh()),
            File(".", "fix-run.sh", self._fix_run_sh()),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

{copy_commands}
WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}

{self.clear_env}
"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_HEADER_RE = re.compile(r"^━━━ Running (\S+) ━━━$")
_SKIP_HEADER_RE = re.compile(r"^━━━ Skipping (\S+) \(file not found\) ━━━$")
_FILE_RESULT_RE = re.compile(r"^MSWE_FILE (PASS|FAIL) (\S+)$")
_TAP_RE = re.compile(r"^\s*(not ok|ok)\s+\d+\s+-\s+(.*?)\s*$")
_TAP_DIRECTIVE_RE = re.compile(r"(?<!\\)\s#\s*(SKIP|TODO)\b.*$", re.IGNORECASE)
_SPEC_RE = re.compile(r"^\s*(✔|✓|✖|✗|❌|﹣)️?\s+(.+?)\s*$")
_SPEC_DURATION_RE = re.compile(r"\s*\(\d+(?:\.\d+)?\s*m?s\)\s*$")
_SPEC_DIRECTIVE_RE = re.compile(r"\s+#\s*(SKIP|TODO)\b.*$", re.IGNORECASE)
_CANCELLED_RE = re.compile(r"^\s*failureType:\s*'cancelledByParent'\s*$")
_SPEC_SUMMARY_TITLES = {"failing tests:"}


@Instance.register(_ORG, _REPO)
@Instance.register(_ORG, _ERA_FILE)
@Instance.register(_ORG, f"{_ERA_HI}-{_ERA_LO}")
class GITHUB_CHINESE_779_TO_716(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        if not _ERA_LO <= pr.number <= _ERA_HI:
            raise ValueError(
                f"{_ORG}/{_REPO}#{pr.number} is outside {_ERA_FILE} (#{_ERA_LO}-#{_ERA_HI})"
            )
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return GithubChineseImageDefault(self.pr, self._config)

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
        cancelled_tests: set[str] = set()

        clean = _ANSI_RE.sub("", test_log).replace("\r", "")
        targets = _patch_added_titles(self.pr.test_patch or "")

        current: str | None = None
        seen_in_file = 0
        last_failed: str | None = None
        last_result = False
        in_yaml = False
        for raw in clean.split("\n"):
            line = raw.rstrip()

            m = _HEADER_RE.match(line)
            if m:
                current = m.group(1)
                seen_in_file = 0
                last_failed = None
                last_result = False
                in_yaml = False
                continue

            if _SKIP_HEADER_RE.match(line):
                current = None
                last_failed = None
                last_result = False
                in_yaml = False
                continue

            m = _FILE_RESULT_RE.match(line)
            if m:
                path = m.group(2)
                ok = m.group(1) == "PASS"
                (passed_tests if ok else failed_tests).add(f"{path} > (file exits 0)")
                if not ok and seen_in_file == 0 and current == path:
                    for title in targets.get(path, set()):
                        failed_tests.add(f"{path} > {title}")
                current = None
                last_failed = None
                last_result = False
                in_yaml = False
                continue

            if current is None:
                continue

            if in_yaml:
                if line.strip() == "...":
                    in_yaml = False
                    last_failed = None
                elif last_failed is not None and _CANCELLED_RE.match(line):
                    cancelled_tests.add(last_failed)
                    last_failed = None
                continue

            if line.strip() == "---" and last_result:
                in_yaml = True
                continue

            status = None
            title = None
            m = _TAP_RE.match(line)
            if m:
                status = "fail" if m.group(1) == "not ok" else "pass"
                title = m.group(2)
                d = _TAP_DIRECTIVE_RE.search(title)
                if d:
                    title = title[: d.start()]
                    status = "skip"
                title = title.replace("\\#", "#").replace("\\\\", "\\").strip()
            else:
                m = _SPEC_RE.match(line)
                if m:
                    glyph = m.group(1)
                    title = m.group(2)
                    d = _SPEC_DIRECTIVE_RE.search(title)
                    if d or glyph == "﹣":
                        status = "skip"
                        if d:
                            title = title[: d.start()]
                    elif glyph in ("✔", "✓"):
                        status = "pass"
                    else:
                        status = "fail"
                    title = _SPEC_DURATION_RE.sub("", title).strip()
                    if title in _SPEC_SUMMARY_TITLES:
                        continue

            last_result = title is not None
            if not title:
                continue
            if title == current or title.endswith("/" + current):
                continue

            name = f"{current} > {title}"
            seen_in_file += 1
            last_failed = None
            if status == "pass":
                passed_tests.add(name)
            elif status == "fail":
                failed_tests.add(name)
                last_failed = name
            else:
                skipped_tests.add(name)

        failed_tests -= cancelled_tests
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
