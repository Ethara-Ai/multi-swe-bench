import re
from pathlib import Path

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "NodeBB"
_REPO = "NodeBB"
_ERA_FILE = Path(__file__).stem
_ERA_MATCH = re.fullmatch(r"nodebb_(\d+)_to_(\d+)", _ERA_FILE)
if _ERA_MATCH is None:
    raise ImportError(f"{__file__}: file name must look like nodebb_<hi>_to_<lo>.py")
_ERA_HI = int(_ERA_MATCH.group(1))
_ERA_LO = int(_ERA_MATCH.group(2))
_BASE_TAG = f"base-{_ERA_HI}_to_{_ERA_LO}"

NODE_IMAGE = "node:24.22.1-trixie"

_ENV_EXPORTS = "export CI=true\nexport NODE_OPTIONS=--max-old-space-size=4096\n"

_TEST_CMD = "bash /home/run_tests.sh\n"

_APPLY_EXCLUDES = (
    "--exclude=package-lock.json --exclude='*/package-lock.json' "
    "--exclude=yarn.lock --exclude='*/yarn.lock' "
    "--exclude=pnpm-lock.yaml --exclude='*/pnpm-lock.yaml'"
)

_MANIFEST = "install/package.json"

_TEST_FILE_RE = re.compile(r"^test/(?!mocks/|helpers/|files/|build/)\S+\.js$")

_SETUP_JSON = (
    '{"url":"http://127.0.0.1:4567/forum","secret":"abcdef",'
    '"admin:username":"admin","admin:email":"test@example.org",'
    '"admin:password":"hAN3Eg8W","admin:password:confirm":"hAN3Eg8W",'
    '"database":"redis","redis:host":"127.0.0.1","redis:port":6379,'
    '"redis:password":"","redis:database":0}'
)

_CI_DB_JSON = '{"host":"127.0.0.1","database":1,"port":6379}'

_REDIS_START = """mkdir -p /home/mswe-redis
redis-server --daemonize yes --bind 127.0.0.1 --port 6379 --dir /home/mswe-redis --dbfilename dump.rdb --save '' --appendonly no --logfile /home/mswe-redis/redis.log --ignore-warnings ARM64-COW-BUG
mswe_redis_ready=0
for i in $(seq 1 60); do
  if [ "$(redis-cli -h 127.0.0.1 -p 6379 ping 2>/dev/null)" = "PONG" ]; then
    mswe_redis_ready=1
    break
  fi
  sleep 1
done
"""

_REPORTER_JS = r"""'use strict';

const fs = require('fs');

const out = process.env.MSWE_RESULTS;
const spec = process.env.MSWE_SPEC;
const clean = s => String(s).replace(/\s+/g, ' ').trim();
const nameOf = r => [spec].concat(r.titlePath().map(clean)).join(' > ');
const write = line => fs.appendFileSync(out, `${line}\n`);

function collect(suite, acc) {
	suite.tests.forEach(t => acc.push(t));
	suite.suites.forEach(s => collect(s, acc));
	return acc;
}

function MsweReporter(runner) {
	const seen = new Set();
	const emit = (status, r) => {
		const name = nameOf(r);
		seen.add(name);
		write(`MSWE_RESULT ${status} ${name}`);
	};
	runner.on('start', () => write('MSWE_STARTED'));
	runner.on('pass', t => emit('PASS', t));
	runner.on('pending', t => emit('SKIP', t));
	runner.on('fail', (r, err) => {
		const msg = clean((err && err.message) || err).slice(0, 300);
		process.stdout.write(`[mswe] failure in ${nameOf(r)} :: ${msg}\n`);
		if (r.type === 'hook') {
			collect(r.parent, []).forEach((t) => {
				if (!t.isPending() && !seen.has(nameOf(t))) {
					emit('FAIL', t);
				}
			});
			return;
		}
		emit('FAIL', r);
	});
	runner.on('end', () => write('MSWE_ENDED'));
}

module.exports = MsweReporter;
"""

_ENUM_JS = r"""'use strict';

const fs = require('fs');

const spec = process.argv[2];
const clean = s => String(s).replace(/\s+/g, ' ').trim();
const src = fs.readFileSync(spec, 'utf8');
const cwd = [process.cwd()];

function tryLoad(name) {
	try {
		return require(require.resolve(name, { paths: cwd }));
	} catch (e) {
		return null;
	}
}

function parse() {
	const opts = {
		ecmaVersion: 'latest', locations: true, loc: true, allowHashBang: true,
		allowReturnOutsideFunction: true, allowAwaitOutsideFunction: true,
	};
	const acorn = tryLoad('acorn');
	const espree = tryLoad('espree');
	const babel = tryLoad('@babel/parser');
	for (const sourceType of ['script', 'module']) {
		try {
			if (acorn) return acorn.parse(src, { ...opts, sourceType });
		} catch (e) {}
		try {
			if (espree) return espree.parse(src, { ecmaVersion: 'latest', loc: true, sourceType });
		} catch (e) {}
	}
	try {
		if (babel) {
			return babel.parse(src, {
				sourceType: 'unambiguous', errorRecovery: true,
				allowReturnOutsideFunction: true, allowAwaitOutsideFunction: true,
			});
		}
	} catch (e) {}
	return null;
}

const SUITES = new Set(['describe', 'context']);
const TESTS = new Set(['it', 'specify']);

function calleeName(c) {
	if (!c) return null;
	if (c.type === 'Identifier') return c.name;
	if (c.type === 'MemberExpression' && !c.computed && c.object && c.object.type === 'Identifier' &&
		c.property && ['only', 'skip'].includes(c.property.name)) {
		return c.object.name;
	}
	return null;
}

function titleOf(n) {
	if (!n) return null;
	if ((n.type === 'Literal' || n.type === 'StringLiteral') && typeof n.value === 'string') return n.value;
	if (n.type === 'TemplateLiteral' && n.expressions.length === 0) return n.quasis[0].value.cooked;
	return null;
}

const lines = [];

function walk(node, stack) {
	if (!node || typeof node.type !== 'string') return;
	if (node.type === 'CallExpression') {
		const name = calleeName(node.callee);
		if (name && (SUITES.has(name) || TESTS.has(name))) {
			const t = titleOf(node.arguments[0]);
			if (t === null) return;
			const path = stack.concat([clean(t)]);
			if (TESTS.has(name)) {
				lines.push(`MSWE_LOADFAIL ${node.loc.start.line}-${node.loc.end.line} ${spec} > ${path.join(' > ')}`);
				return;
			}
			node.arguments.slice(1).forEach(a => walk(a, path));
			return;
		}
	}
	Object.keys(node).forEach((key) => {
		if (key === 'loc' || key === 'parent') return;
		const v = node[key];
		if (Array.isArray(v)) {
			v.forEach(c => walk(c, stack));
		} else if (v && typeof v === 'object') {
			walk(v, stack);
		}
	});
}

const ast = parse();
if (!ast) {
	process.stdout.write('MSWE_ENUM_UNAVAILABLE\n');
	process.exit(0);
}
walk(ast, []);
process.stdout.write(lines.map(l => `${l}\n`).join(''));
"""

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


def _fix_touches_manifest(pr: PullRequest) -> bool:
    return re.search(
        rf"^diff --git a/{re.escape(_MANIFEST)} b/", pr.fix_patch, re.MULTILINE
    ) is not None


class NodebbImageBase(Image):
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
    git ca-certificates redis-server \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class NodebbImageDefault(Image):
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
        return NodebbImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _manifest_sh(self) -> str:
        if _fix_touches_manifest(self.pr):
            return (
                f"git apply --whitespace=nowarn --include={_MANIFEST} /home/fix.patch\n"
                f"cp {_MANIFEST} package.json\n"
                f"git checkout -- {_MANIFEST}\n"
            )
        return f"cp {_MANIFEST} package.json\n"

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
npm --version
{self._manifest_sh()}timeout --kill-after=30 1800 npm install --no-audit --no-fund || true

rm -rf /home/mswe-redis
{_REDIS_START}test "$mswe_redis_ready" = "1"
timeout --kill-after=30 2400 node app --setup='{_SETUP_JSON}' --ci='{_CI_DB_JSON}' || true
redis-cli -h 127.0.0.1 -p 6379 save || true
redis-cli -h 127.0.0.1 -p 6379 shutdown nosave || true
git checkout -- .

cat > /home/mswe_reporter.cjs <<'MSWE_REPORTER_EOF'
{_REPORTER_JS}MSWE_REPORTER_EOF

cat > /home/mswe_enum.cjs <<'MSWE_ENUM_EOF'
{_ENUM_JS}MSWE_ENUM_EOF

cat > /home/run_tests.sh <<'MSWE_RUN_TESTS_EOF'
{self._run_tests_sh()}MSWE_RUN_TESTS_EOF
chmod +x /home/run_tests.sh

node -e "require('./package.json'); require('nconf'); require('redis'); require('benchpressjs'); const c = require('./config.json'); if (!c.test_database || c.database !== 'redis') process.exit(1); console.log('DEPS_OK')"
node_modules/.bin/mocha --version
node -e "require('/home/mswe_reporter.cjs')"
test -d build/public/templates
test -d build/public/language
test -s /home/mswe-redis/dump.rdb
command -v redis-server
command -v redis-cli
test -z "$(git status --porcelain --untracked-files=no)"
test -s /home/run_tests.sh
"""

    def _run_tests_sh(self) -> str:
        repo = self.pr.repo
        files = " ".join(_test_files(self.pr))
        return f"""#!/bin/bash
set -uo pipefail

cd /home/{repo}

{_REDIS_START}if [ "$mswe_redis_ready" != "1" ]; then
  echo "MSWE_REDIS_NOT_READY"
  exit 1
fi

node -e "const fs = require('fs'); const c = JSON.parse(fs.readFileSync('config.json', 'utf8')); c.test_database = {{ host: '127.0.0.1', database: 1, port: 6379 }}; fs.writeFileSync('config.json', JSON.stringify(c, null, 4));"

echo "━━━ Building templates and languages ━━━"
timeout --kill-after=30 1200 node ./nodebb build --series templates languages 2>&1
echo "MSWE_BUILD rc=$?"
rm -rf /home/mswe-build-public
if [ -d build/public ]; then
  cp -a build/public /home/mswe-build-public
fi

status=0
for f in {files}; do
  if [ ! -f "$f" ]; then
    echo "━━━ Skipping $f (file not found) ━━━"
    continue
  fi
  if [ -d /home/mswe-build-public ]; then
    rm -rf build/public
    cp -a /home/mswe-build-public build/public
  fi
  res=/home/mswe-results.txt
  rm -f "$res"
  touch "$res"
  echo "━━━ Running $f ━━━"
  MSWE_SPEC="$f" MSWE_RESULTS="$res" timeout --kill-after=30 1800 node_modules/.bin/mocha --no-config --no-package --timeout 25000 --exit --reporter /home/mswe_reporter.cjs "$f" 2>&1
  rc=$?
  if ! grep -q '^MSWE_STARTED$' "$res"; then
    echo "━━━ Load failure $f ━━━"
    node /home/mswe_enum.cjs "$f" >> "$res" 2>&1
  fi
  echo "━━━ Results $f ━━━"
  cat "$res"
  echo "━━━ Finished $f ━━━"
  if [ "$rc" -ne 0 ]; then
    status=1
  fi
done

redis-cli -h 127.0.0.1 -p 6379 shutdown nosave >/dev/null 2>&1 || true
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


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_RESULT_RE = re.compile(r"MSWE_RESULT (PASS|FAIL|SKIP) (test/\S+ > .+?)\s*$")
_LOADFAIL_RE = re.compile(r"MSWE_LOADFAIL (\d+)-(\d+) (test/\S+ > .+?)\s*$")
_DIFF_RE = re.compile(r"^diff --git a/\S+ b/(\S+)")
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _added_lines(patch: str) -> dict[str, set[int]]:
    added: dict[str, set[int]] = {}
    current: str | None = None
    line_no = 0
    for line in patch.split("\n"):
        m = _DIFF_RE.match(line)
        if m:
            current = m.group(1)
            added.setdefault(current, set())
            line_no = 0
            continue
        m = _HUNK_RE.match(line)
        if m and current is not None:
            line_no = int(m.group(1))
            continue
        if current is None or line_no == 0:
            continue
        if line.startswith("+"):
            added[current].add(line_no)
            line_no += 1
        elif line.startswith("-") or line.startswith("\\"):
            continue
        else:
            line_no += 1
    return added


@Instance.register(_ORG, _REPO)
@Instance.register(_ORG, _ERA_FILE)
@Instance.register(_ORG, f"{_ERA_HI}-{_ERA_LO}")
class NODEBB_14934_TO_14341(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        if not _ERA_LO <= pr.number <= _ERA_HI:
            raise ValueError(
                f"{_ORG}/{_REPO}#{pr.number} is outside {_ERA_FILE} (#{_ERA_LO}-#{_ERA_HI})"
            )
        super().__init__()
        self._pr = pr
        self._config = config
        self._added = _added_lines(pr.test_patch or "")

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return NodebbImageDefault(self.pr, self._config)

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

        clean = _ANSI_RE.sub("", test_log).replace("\r", "")

        for raw in clean.split("\n"):
            line = raw.rstrip()

            m = _RESULT_RE.search(line)
            if m:
                status, name = m.group(1), m.group(2)
                if status == "PASS":
                    passed_tests.add(name)
                elif status == "FAIL":
                    failed_tests.add(name)
                else:
                    skipped_tests.add(name)
                continue

            m = _LOADFAIL_RE.search(line)
            if m:
                start, end, name = int(m.group(1)), int(m.group(2)), m.group(3)
                spec = name.split(" > ", 1)[0]
                touched = self._added.get(spec, set())
                if any(start <= n <= end for n in touched):
                    failed_tests.add(name)

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
