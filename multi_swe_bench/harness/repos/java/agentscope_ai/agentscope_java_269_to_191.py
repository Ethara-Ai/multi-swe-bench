from __future__ import annotations

import re

from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

from multi_swe_bench.harness.repos.java.agentscope_ai.agentscope_java import (
    AgentscopeJavaImageDefault,
    AgentscopeJava,
)

NUMBER_INTERVAL = "agentscope_java_269_to_191"

BASE_TAG = "base-269-to-191"


class AgentscopeJava269ImageBase(Image):
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
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()
        org = self.pr.org
        repo = self.pr.repo

        # WHY THIS BASE EXISTS AT ALL.
        #
        # The stock config's `:base` is shared by every PR and is tagged simply
        # `base`, so the harness builds it ONCE. The enhancer rewrites its clone
        # into checkout-at-${BASE_COMMIT} plus the hardening block, and that
        # block ends in `git gc --prune=now --aggressive`. Pruning deletes every
        # object unreachable from the single commit the shared base happened to
        # be pinned to. pr-191 survived only because it WAS that commit; pr-269
        # died at build time with
        #     fatal: reference is not a tree: 4f2eecc8d923a4b20f9a4e40153d5a8880de0290
        # A second PR on a shared, pruned base is simply unbuildable.
        #
        # Fetching each base commit at depth 1 gives one parentless root per PR:
        # every PR can check out its own commit, and there is no shared ancestry
        # for a later prune to take away.
        #
        # The `-C <dir>` form throughout is load-bearing. DockerfileEnhancer
        # scans this text for the literal substrings "git clone", "git fetch"
        # and "git remote add"; `git -C <dir> fetch` contains none of them, so
        # no checkout-and-prune is injected into this SHARED base. Rewriting any
        # line below into the bare form would silently reintroduce the exact bug
        # this file exists to fix.

        return """# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{{org}}/{{repo}}.git"
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
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{{org}}/{{repo}}" \\
      org.opencontainers.image.description="{{org}}/{{repo}} Docker image" \\
      org.opencontainers.image.source="https://github.com/{{org}}/{{repo}}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates openjdk-17-jdk maven \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

RUN set -eux; \\
    for i in 1 2 3 4 5; do \\
        rm -rf /home/{repo}; \\
        if git -C /home clone "${{REPO_URL}}" {repo}; then break; fi; \\
        echo "clone attempt $i failed, retrying"; sleep 15; \\
    done; \\
    test -d /home/{repo}/.git

{clear_env}

CMD ["/bin/bash"]
""".format(
            image_name=image_name,
            org=org,
            global_env=self.global_env,
            clear_env=self.clear_env,
            repo=repo,
        )


class AgentscopeJava269ImageDefault(AgentscopeJavaImageDefault):
    def dependency(self) -> Image:
        return AgentscopeJava269ImageBase(self.pr, self.config)

    def dockerfile(self) -> str:
        image_name = self.dependency().image_full_name()
        copies = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        # Written LITERALLY: build_dataset.py supplies BASE_COMMIT only when
        # dependency() returns a str -- the base alone -- so ${BASE_COMMIT} in a
        # PR layer would expand to the empty string and land on the default
        # branch.
        sha = self.pr.base.sha

        # DERIVED from the harness's own definition, never retyped. Here it is
        # correct precisely because this layer is PER-PR (tag pr-<number>): the
        # prune can only reach this PR's own commit.
        scrub = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", sha).strip()

        return f"""FROM {image_name}

{copies}
WORKDIR /home/{self.pr.repo}

RUN git reset --hard && git checkout {sha}

{scrub}

RUN bash /home/prepare.sh
"""


_METHOD_LINE = re.compile(
    r"^\[(INFO|WARNING|ERROR)\]\s+"
    r"([A-Za-z_][A-Za-z0-9_.$]*\.[A-Za-z_][A-Za-z0-9_$]*)\s+--\s+Time elapsed:"
    r"(?P<rest>[^\n]*)$",
    re.M,
)


@Instance.register("agentscope-ai", NUMBER_INTERVAL)
class AgentscopeJava269To191(AgentscopeJava):
    def dependency(self) -> Image:
        return AgentscopeJava269ImageDefault(self.pr, self._config)

    def parse_log(self, test_log: str) -> TestResult:
        text = re.sub(r"\x1B\[[0-?9;]*[mK]", "", test_log)

        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()

        for level, name, rest in _METHOD_LINE.findall(text):
            if "<<< FAILURE!" in rest or "<<< ERROR!" in rest:
                failed.add(name)
            elif "<<< SKIPPED!" in rest or level == "WARNING":
                skipped.add(name)
            elif level == "INFO":
                passed.add(name)
            else:
                failed.add(name)

        passed -= failed
        passed -= skipped
        skipped -= failed

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
