from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

from .mermaid import (
    MermaidVersionBase,
    mermaid_vitest_parse_log,
    _CHECK_GIT_CHANGES_SH,
    _PNPM_PREPARE_SH,
    _PNPM_RUN_SH,
    _PNPM_TEST_RUN_SH,
    _PNPM_FIX_RUN_SH,
)

# --- MITM proxy / cert scaffolding (PIPELINE §2a, §8) -- written out in full ---
# This PR image has an `Image`-typed dependency, so `DockerfileEnhancer.enhance()`
# returns its Dockerfile raw and injects nothing. Per §2a the MITM block is added
# BY HAND, spelled out literally below. The text is identical to the canonical
# `image.py` constants (`_PROXY_ARGS`, `_ENV_BLOCK`, `_CERT_SYMLINKS`) -- keep it
# that way; if image.py changes, update these literals or §8.1 will flag the drift.
# `_MITM_MOUNT` stays latent (§2a). Empty proxy ARG defaults = passthrough.

_MITM_PROXY_ARGS = '''\
ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"'''

_MITM_ENV_BLOCK = '''\
ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${http_proxy} \\
    https_proxy=${https_proxy} \\
    HTTP_PROXY=${HTTP_PROXY} \\
    HTTPS_PROXY=${HTTPS_PROXY} \\
    no_proxy=${no_proxy} \\
    NO_PROXY=${NO_PROXY} \\
    SSL_CERT_FILE=${CA_CERT_PATH} \\
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\
    CURL_CA_BUNDLE=${CA_CERT_PATH}'''

_MITM_CERT_SYMLINKS = '''\
RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt'''

_NODE_IMAGE = "node:22-bookworm"
_INTERVAL_NAME = "mermaid_7533_to_6407"


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
        return MermaidVersionBase(
            self.pr,
            self._config,
            _NODE_IMAGE,
            _INTERVAL_NAME,
            pkg_manager="pnpm",
            pnpm_version="10",
        )

    def image_tag(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def workdir(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(
                ".",
                "prepare.sh",
                _PNPM_PREPARE_SH.format(
                    repo=self.pr.repo, base_sha=self.pr.base.sha
                ),
            ),
            File(
                ".",
                "run.sh",
                _PNPM_RUN_SH.format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                _PNPM_TEST_RUN_SH.format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                _PNPM_FIX_RUN_SH.format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        """Per-PR image — PIPELINE §4 reference format.

        `prepare.sh` resets and checks out `base.sha`, then the canonical
        `Image._HARDENING_BLOCK` runs with the **literal** base sha substituted
        for `${BASE_COMMIT}` so the fix cannot be recovered from git history
        (PIPELINE §2/§9).
        """
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += "COPY {name} /home/\n".format(name=file.name)

        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return """# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

ARG TARGETARCH

{mitm_args}

{mitm_env}

{mitm_certs}

{global_env}

{copy_commands}
RUN bash /home/prepare.sh

WORKDIR /home/{repo}

{hardening}

{clear_env}

CMD ["/bin/bash"]
""".format(
            name=name,
            tag=tag,
            mitm_args=_MITM_PROXY_ARGS,
            mitm_env=_MITM_ENV_BLOCK,
            mitm_certs=_MITM_CERT_SYMLINKS,
            global_env=self.global_env,
            copy_commands=copy_commands,
            repo=self.pr.repo,
            hardening=hardening,
            clear_env=self.clear_env,
        )


@Instance.register("mermaid-js", _INTERVAL_NAME)
class MermaidPnpmVitest22(Instance):

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
        return mermaid_vitest_parse_log(test_log)


# === bundle number_interval routing (prs_in_bundle dash-joined) -- PIPELINE 11b ===
_BUNDLE_NIS_MERMAID_7533_TO_6407 = [
    "6704",
]
for _ni in _BUNDLE_NIS_MERMAID_7533_TO_6407:
    Instance.register("mermaid-js", _ni)(MermaidPnpmVitest22)
