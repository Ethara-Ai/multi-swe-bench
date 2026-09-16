import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

GO_IMAGE = "golang:1.21"
NODE_VERSION = "16.13.1"
_BASE_APT = "ca-certificates curl git"

_GO_SKIP = "TestHandleVote/Valid_request,_with_multi_setting,_second_vote"
_GO_TEST_CMD = f"go test -v -count=1 -skip '{_GO_SKIP}' ./server/..."
_JEST_CMD = "npx jest --forceExit --detectOpenHandles --verbose"


class MatterpollImageBase(Image):
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
        return GO_IMAGE

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list:
        return []

    def dockerfile(self) -> str:
        image = self.dependency()
        org, repo = self.pr.org, self.pr.repo
        return f"""# syntax=docker/dockerfile:1.6

FROM {image}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC \\
    GO111MODULE=on \\
    GOTOOLCHAIN=local \\
    CGO_ENABLED=1 \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends \\
    {_BASE_APT} \\
    && rm -rf /var/lib/apt/lists/*

RUN ARCH=$(dpkg --print-architecture) && \\
    if [ "$ARCH" = "amd64" ]; then NODE_ARCH="x64"; else NODE_ARCH="$ARCH"; fi && \\
    curl -fsSL "https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-linux-${{NODE_ARCH}}.tar.gz" \\
    | tar -xz -C /usr/local --strip-components=1 && \\
    node --version && npm --version

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

CMD ["/bin/bash"]
"""


class MatterpollImageDefault(Image):
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
        return MatterpollImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list:
        repo = self.pr.repo
        sha = self.pr.base.sha

        check_git_changes_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            "if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then\n"
            '  echo "check_git_changes: Not inside a git repository"\n'
            "  exit 1\n"
            "fi\n"
            "if [[ -n $(git status --porcelain) ]]; then\n"
            '  echo "check_git_changes: Uncommitted changes"\n'
            "  git status --porcelain\n"
            "  exit 1\n"
            "fi\n"
            'echo "check_git_changes: No uncommitted changes"\n'
            "exit 0\n"
        )

        prepare_sh = (
            "#!/bin/bash\n"
            "set -euo pipefail\n"
            f'BASE_COMMIT="${{BASE_COMMIT:-{sha}}}"\n'
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -fdx\n"
            "bash /home/check_git_changes.sh\n"
            'git checkout --detach "${BASE_COMMIT}"\n'
            "bash /home/check_git_changes.sh\n"
            "go mod download\n"
            "go build ./server/...\n"
            f"cd /home/{repo}/webapp\n"
            "node -e \"const fs=require('fs'),p='package-lock.json',"
            "d=JSON.parse(fs.readFileSync(p,'utf8'));let n=0;"
            "for(const s of ['packages','dependencies'])"
            "for(const k of Object.keys(d[s]||{})){const e=d[s][k];"
            "if(e&&typeof e==='object'&&/^git[+:]/.test(e.resolved||'')&&e.integrity)"
            "{delete e.integrity;n++;}}"
            "fs.writeFileSync(p,JSON.stringify(d,null,2));"
            "console.log('stripped integrity from '+n+' git deps');\"\n"
            "npm ci --no-audit --no-fund || npm install --no-audit --no-fund\n"
            "git checkout -- package-lock.json\n"
            f"cd /home/{repo}\n"
            "go vet ./server/... >/dev/null\n"
            f"cd /home/{repo}/webapp\n"
            "npx jest --version\n"
            "node -e \"const fs=require('fs');"
            "for(const m of ['jest','enzyme','enzyme-adapter-react-16','@testing-library/jest-dom'])"
            "require.resolve(m);"
            "for(const p of ['node_modules/mattermost-webapp/packages/mattermost-redux/src',"
            "'node_modules/mattermost-webapp/packages/reselect/src'])"
            "if(!fs.existsSync(p))throw new Error('missing '+p);"
            "console.log('DEPS_OK')\"\n"
        )

        run_sh = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            "export CI=true\n"
            f"cd /home/{repo}\n"
            "rc=0\n"
            f"{_GO_TEST_CMD} || rc=$?\n"
            f"cd /home/{repo}/webapp\n"
            f"{_JEST_CMD} || rc=$?\n"
            "exit $rc\n"
        )
        test_run_sh = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            "export CI=true\n"
            f"cd /home/{repo}\n"
            "git apply --whitespace=nowarn /home/test.patch\n"
            "rc=0\n"
            f"{_GO_TEST_CMD} || rc=$?\n"
            f"cd /home/{repo}/webapp\n"
            f"{_JEST_CMD} || rc=$?\n"
            "exit $rc\n"
        )
        fix_run_sh = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            "export CI=true\n"
            f"cd /home/{repo}\n"
            "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            "rc=0\n"
            f"{_GO_TEST_CMD} || rc=$?\n"
            f"cd /home/{repo}/webapp\n"
            f"{_JEST_CMD} || rc=$?\n"
            "exit $rc\n"
        )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", check_git_changes_sh),
            File(".", "prepare.sh", prepare_sh),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run_sh),
            File(".", "fix-run.sh", fix_run_sh),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        name = base.image_name()
        tag = base.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{sha}"

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""


@Instance.register("matterpoll", "matterpoll")
class Matterpoll(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return MatterpollImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_go = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+)")
        re_suite = re.compile(r"^(PASS|FAIL)\s+(\S+\.[cm]?[jt]sx?)\b")
        re_jest = re.compile(
            "^( *)([✓✕✗○✎])\\s+(.+?)"
            r"(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$"
        )
        jest_status = {
            "✓": "PASS",
            "✕": "FAIL",
            "✗": "FAIL",
            "○": "SKIP",
            "✎": "SKIP",
        }

        def record(name: str, status: str) -> None:
            if status == "FAIL":
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
            elif status == "PASS":
                if name in failed_tests:
                    return
                skipped_tests.discard(name)
                passed_tests.add(name)
            else:
                if name in passed_tests or name in failed_tests:
                    return
                skipped_tests.add(name)

        suite = None
        describes: list[str] = []
        in_console = False

        for raw in test_log.splitlines():
            line = raw.rstrip()
            m = re_go.match(line)
            if m:
                record(m.group(2), m.group(1))
                continue
            m = re_suite.match(line)
            if m:
                suite = m.group(2)
                describes = []
                in_console = False
                continue
            if suite is None:
                continue
            stripped = line.strip()
            if not stripped:
                in_console = False
                continue
            if stripped.startswith("●"):
                if stripped.startswith("● Console"):
                    in_console = True
                else:
                    suite = None
                continue
            if in_console:
                continue
            m = re_jest.match(line)
            if m:
                depth = len(m.group(1)) // 2
                leaf = m.group(3).strip()
                for prefix in ("skipped ", "todo "):
                    if leaf.startswith(prefix):
                        leaf = leaf[len(prefix):]
                name = " > ".join([suite] + describes[: max(depth - 1, 0)] + [leaf])
                record(name, jest_status[m.group(2)])
                continue
            indent = len(line) - len(line.lstrip())
            if indent >= 2 and indent % 2 == 0 and not line[0].isalnum():
                level = indent // 2
                describes = describes[: level - 1] + [stripped]
            elif indent == 0:
                suite = None

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
