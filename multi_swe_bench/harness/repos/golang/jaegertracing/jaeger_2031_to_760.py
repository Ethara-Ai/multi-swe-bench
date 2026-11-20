import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL_NAME = "jaeger_2031_to_760"
_BASE_IMAGE = "golang:1.12"
_GOPATH_MODE = True
_REPO_DIR = "/go/src/github.com/jaegertracing/jaeger"

_FLAKY_TEST_PREFIXES = (
    "TestServerGRPCTLS",
    "TestServerHTTPTLS",
    "TestSpanCollectorHTTPS",
    "TestSpanCollectorZipkinTLS",
    "TestGetTraceSuccessGRPC",
    "TestProxyClientTLS",
    "TestAdminServerTLS",
    "TestCreateTLSHttpServerError",
    "TestGRPCGateway_TLS_with_base_path",
    "TestGetRoundTripperTLSConfig",
    "TestReloadKeyPair",
    "TestExecuteAction",
    "TestGRPCResolverRoundRobin",
)

if _GOPATH_MODE:
    _GO_ENV = "export GOPATH=/go\nexport GO111MODULE=off\nexport CGO_ENABLED=1\nexport CI=true\n"
    _DOCKER_GO_ENV = "ENV GOPATH=/go \\\n    GO111MODULE=off \\\n    CGO_ENABLED=1 \\\n    CI=true \\\n    PATH=/go/bin:/usr/local/go/bin:$PATH"
else:
    _GO_ENV = "export GOTOOLCHAIN=local\nexport GOFLAGS=-mod=mod\nexport CGO_ENABLED=1\nexport CI=true\n"
    _DOCKER_GO_ENV = "ENV GOTOOLCHAIN=local \\\n    GOFLAGS=-mod=mod \\\n    GOPROXY=https://proxy.golang.org,direct \\\n    CGO_ENABLED=1 \\\n    CI=true"

_TOOL_INSTALL = 'RUN ARCH="${TARGETARCH:-$(dpkg --print-architecture)}" && \\\n    curl -fsSL -o /usr/local/bin/dep "https://github.com/golang/dep/releases/download/v0.5.4/dep-linux-${ARCH}" && \\\n    chmod +x /usr/local/bin/dep && dep version'
_DEPS_CMD = "dep ensure -vendor-only\n"
_RESTORE_CMD = ""


class Jaeger2031To760ImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return _BASE_IMAGE

    def image_tag(self) -> str:
        return f"base-{_INTERVAL_NAME}"

    def workdir(self) -> str:
        return f"base-{_INTERVAL_NAME}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        if self.config.need_clone:
            code = f'RUN mkdir -p "$(dirname {_REPO_DIR})" && git clone "${{REPO_URL}}" {_REPO_DIR}'
        else:
            code = f'RUN mkdir -p "$(dirname {_REPO_DIR})"\nCOPY {self.pr.repo} {_REPO_DIR}'

        label = (
            f'LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}"'
        )

        sections = [
            DockerfileEnhancer.SYNTAX_DIRECTIVE,
            f"FROM {_BASE_IMAGE}",
            (
                f"{DockerfileEnhancer._TARGETARCH_ARG}\n"
                f'ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"\n'
                "ARG BASE_COMMIT"
            ),
            DockerfileEnhancer._PROXY_ARGS,
            DockerfileEnhancer._ENV_BLOCK,
            label,
            DockerfileEnhancer._CERT_SYMLINKS,
            self.global_env,
            _DOCKER_GO_ENV,
            (
                "RUN sed -i '/debian-security/d' /etc/apt/sources.list 2>/dev/null; (apt-get update || (sed -i -e 's|deb.debian.org|archive.debian.org|g' -e 's|security.debian.org|archive.debian.org|g' -e '/-updates/d' /etc/apt/sources.list && apt-get -o Acquire::Check-Valid-Until=false update)) \\\n"
                "    && apt-get install -y --no-install-recommends git ca-certificates curl build-essential \\\n"
                "    && rm -rf /var/lib/apt/lists/*"
            ),
            _TOOL_INSTALL,
            "WORKDIR /home/",
            code,
            f"WORKDIR {_REPO_DIR}",
            self.clear_env,
            'CMD ["/bin/bash"]',
        ]
        return "\n\n".join(s for s in sections if s) + "\n"


class Jaeger2031To760ImageDefault(Image):
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
        return Jaeger2031To760ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        head = "#!/bin/bash\nset -eo pipefail\n" + _GO_ENV + f"cd {_REPO_DIR}\n"
        test_cmd = "go test -v ./... 2>&1\n"
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                head
                + "git reset --hard\n"
                + "git clean -fdx\n"
                + 'test -z "$(git status --porcelain)"\n'
                + f"git checkout --detach {self.pr.base.sha}\n"
                + 'test -z "$(git status --porcelain)"\n'
                + _DEPS_CMD
                + "go list -deps -test ./... > /dev/null\n"
                + _RESTORE_CMD,
            ),
            File(".", "run.sh", head + test_cmd),
            File(
                ".",
                "test-run.sh",
                head + "git apply --whitespace=nowarn /home/test.patch\n" + test_cmd,
            ),
            File(
                ".",
                "fix-run.sh",
                head
                + "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
                + test_cmd,
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

WORKDIR {_REPO_DIR}

{copy_commands}

RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}

{self.clear_env}

CMD ["/bin/bash"]
"""


def _parse_jaeger_log(log: str) -> TestResult:
    log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

    run_tests = set(re.findall(r"=== RUN\s+(\S+)", log))
    passed_tests = set(re.findall(r"--- PASS: (\S+)", log))
    skipped_individual = set(re.findall(r"--- SKIP: (\S+)", log))
    skipped_packages = set(re.findall(r"\?   \t([^\t]+?)\[no test files\]", log))

    def _exclude_flaky(tests: set) -> set:
        return {t for t in tests if not any(t.startswith(p) for p in _FLAKY_TEST_PREFIXES)}

    run_tests = _exclude_flaky(run_tests)
    passed_tests = _exclude_flaky(passed_tests)
    skipped_individual = _exclude_flaky(skipped_individual)

    failed_tests = run_tests - passed_tests - skipped_individual
    skipped_tests = skipped_packages | skipped_individual
    passed_tests -= failed_tests
    passed_tests -= skipped_tests
    skipped_tests -= failed_tests

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("jaegertracing", _INTERVAL_NAME)
class JAEGER_2031_TO_760(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Jaeger2031To760ImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        return _parse_jaeger_log(log)
