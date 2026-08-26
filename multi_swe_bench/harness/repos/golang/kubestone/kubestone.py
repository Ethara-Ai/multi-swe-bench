import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image, _safe_path_component
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class KubestoneImageBase(Image):
    """Toolchain + source + envtest control-plane binaries.

    kubestone is a Kubebuilder operator: its controller suites spin up a real
    etcd + kube-apiserver via controller-runtime's envtest. Without those two
    binaries every suite panics in ProcessState.Stop (nil deref), so installing
    them is not optional -- it is what makes the tests runnable at all.
    """

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
        # go.mod declares `go 1.12` and the deps are 2019-era Kubernetes
        # (k8s.io/api v0.0.0-20190409..., controller-runtime v0.2.0), but a
        # modern toolchain compiles and runs them cleanly -- verified in Docker:
        # `go build ./...` exits 0 and the full suite is 9/9 PASS on go1.22.12.
        # Pinned rather than `golang:latest` so the image stays reproducible.
        return "golang:1.22-bookworm"

    def image_tag(self) -> str:
        # Per-PR: the injected hardening block detaches at one ${BASE_COMMIT}
        # and prunes every other ref, so a shared tag would let whichever PR
        # built first pin the commit for all the others.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def extra_setup(self) -> str:
        # Rendered after `git checkout ${BASE_COMMIT}` and before the history
        # scrub, with WORKDIR already at /home/kubestone.
        #
        # controller-runtime v0.2.0 looks for the envtest control plane at
        # /usr/local/kubebuilder/bin. Their CI gets it from the kubebuilder
        # 2.0.0-beta.0 bundle, but that release publishes linux_amd64 only --
        # no arm64 -- and the kubebuilder-tools GCS bucket is not publicly
        # reachable (403 on every version/arch probed).
        #
        # So the three binaries are assembled from their upstream projects,
        # which DO ship arm64, pinned to the exact versions that bundle
        # contains (verified by running them: etcd 3.3.11, apiserver v1.14.1).
        return """ENV GO111MODULE=on
ENV GOFLAGS=-mod=mod
# The build container's DNS cannot resolve sum.golang.org (Go's public
# checksum database), so `go mod download` below fails module verification
# with a DNS lookup error even though every module is already pinned in this
# repo's checked-in go.sum. GOSUMDB=off skips that network round-trip; go
# still verifies downloaded modules against go.sum, so this does not weaken
# integrity checking, it only removes the unreachable-host dependency.
ENV GOSUMDB=off
ENV PATH="/usr/local/kubebuilder/bin:${PATH}"
# etcd 3.3.x aborts on arm64 unless this opt-in equals GOARCH. On amd64 -- a
# supported arch -- etcd never consults it. Verified on both arches.
ENV ETCD_UNSUPPORTED_ARCH=${TARGETARCH}
# envtest defaults to a 20s control-plane start budget. Native boot is ~3-5s,
# but a slow or emulated arm64 host needs far more headroom.
ENV KUBEBUILDER_CONTROLPLANE_START_TIMEOUT=180s
ENV KUBEBUILDER_CONTROLPLANE_STOP_TIMEOUT=180s

RUN set -eux; \\
    arch="${TARGETARCH:-amd64}"; \\
    mkdir -p /usr/local/kubebuilder/bin; \\
    curl -sSL -o /tmp/etcd.tar.gz \\
        "https://github.com/etcd-io/etcd/releases/download/v3.3.11/etcd-v3.3.11-linux-${arch}.tar.gz"; \\
    tar -C /tmp -xzf /tmp/etcd.tar.gz; \\
    mv "/tmp/etcd-v3.3.11-linux-${arch}/etcd" /usr/local/kubebuilder/bin/etcd; \\
    curl -sSL -o /usr/local/kubebuilder/bin/kube-apiserver \\
        "https://dl.k8s.io/v1.14.1/bin/linux/${arch}/kube-apiserver"; \\
    curl -sSL -o /usr/local/kubebuilder/bin/kubectl \\
        "https://dl.k8s.io/v1.14.1/bin/linux/${arch}/kubectl"; \\
    chmod +x /usr/local/kubebuilder/bin/etcd \\
             /usr/local/kubebuilder/bin/kube-apiserver \\
             /usr/local/kubebuilder/bin/kubectl; \\
    rm -rf /tmp/etcd.tar.gz "/tmp/etcd-v3.3.11-linux-${arch}"; \\
    /usr/local/kubebuilder/bin/etcd --version; \\
    /usr/local/kubebuilder/bin/kube-apiserver --version

# Warm the module cache so the graded runs do not re-download ~40 modules.
# Verified in Docker: leaves go.mod/go.sum untouched and the worktree clean,
# which the prepare.sh clean-tree assertions depend on.
RUN go mod download"""

    def dockerfile(self) -> str:
        # Reimplements Image.dockerfile() rather than calling super(), for one
        # reason only: the base class hardcodes its own
        # "ENV DEBIAN_FRONTEND=noninteractive\nENV LANG=C.UTF-8" here, and
        # DockerfileEnhancer._ENV_BLOCK (injected into every rendered
        # Dockerfile, this one included) already sets both - plus TZ and the
        # proxy/CA vars - earlier in the same file. The duplicate was a
        # harmless no-op (same value, set twice), but this repo's QC flagged
        # it, and fixing the shared base class would touch every other
        # default-template repo, not just this one. So: everything below is
        # byte-for-byte identical to Image.dockerfile() except that one
        # ENV pair is dropped and WORKDIR /home/ is kept on its own line.
        base_img = self.dependency()
        if isinstance(base_img, Image):
            raise NotImplementedError(
                "Subclass must override dockerfile() or return a string from dependency()"
            )

        default_packages = [
            "ca-certificates",
            "curl",
            "build-essential",
            "git",
            "gnupg",
            "make",
            "python3",
            "sudo",
            "wget",
        ]

        all_packages = default_packages + self.extra_packages()
        packages_str = " \\\n    ".join(all_packages)
        apt_command = self._get_apt_update_command(packages_str, base_img)

        repo = _safe_path_component(self.pr.repo)
        clone_section = f'RUN git clone "${{REPO_URL}}" /home/{repo}'

        extra_setup = self.extra_setup()

        sections = [f"FROM {base_img}"]

        if self.global_env:
            sections.append(self.global_env)

        sections.append("WORKDIR /home/")

        sections.append(apt_command)
        sections.append(clone_section)
        sections.append(f"WORKDIR /home/{repo}")
        sections.append("RUN git reset --hard\nRUN git checkout ${BASE_COMMIT}")

        if extra_setup:
            sections.append(extra_setup)

        sections.append(self._HARDENING_BLOCK)

        if self.clear_env:
            sections.append(self.clear_env)

        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


class KubestoneImageDefault(Image):
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
        return KubestoneImageBase(self.pr, self.config)

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

""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
# `reset --hard` only reverts TRACKED files. This PR's patches add new files
# (api/v1alpha1/ycsbbench_types.go, controllers/ycsbbench/, ...), which survive
# as untracked and would trip the clean-tree assert below. Verified in Docker:
# without this clean, re-running prepare.sh on a patched tree exits 1.
git clean -fd
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Warm the build cache so the three graded runs do not each pay full
# compilation. `|| true` because a warm-up hiccup must not fail the image
# build -- the graded runs decide pass/fail, not this.
go build ./... || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
go test -v -count=1 ./api/... ./controllers/... ./pkg/...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
go test -v -count=1 ./api/... ./controllers/... ./pkg/...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
go test -v -count=1 ./api/... ./controllers/... ./pkg/...

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {image.image_name()}:{image.image_tag()}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("kubestone", "kubestone")
class Kubestone(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return KubestoneImageDefault(self.pr, self._config)

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
        # Matches `go test -v` top-level result lines, captured verbatim from
        # this repo at 0c0e4a5e0251:
        #   --- PASS: TestYcsbBenchController (0.00s)
        #   --- PASS: TestK8sController (6.97s)
        #
        # The suites are Ginkgo, so individual specs render as bullet glyphs
        # and never produce their own `--- PASS:` line -- granularity is one
        # entry per Test function (9 at base, 10 with the fix applied).
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Do NOT broaden these to a bare `FAIL\\s+(\\S+)`: go prints a
        # package-summary line (`FAIL\\tgithub.com/xridge/kubestone/...\\t0.0s`)
        # that would inject a phantom failing "test" and corrupt failed_count.
        # Verified against the test-patch-only run, whose ycsbbench package
        # fails to BUILD and emits exactly such a line.
        re_pass = re.compile(r"^--- PASS: (\S+)")
        re_fail = re.compile(r"^--- FAIL: (\S+)")
        re_skip = re.compile(r"^--- SKIP: (\S+)")

        for line in test_log.splitlines():
            line = line.strip()

            m = re_pass.match(line)
            if m:
                name = m.group(1)
                if name not in failed_tests:
                    skipped_tests.discard(name)
                    passed_tests.add(name)
                continue

            m = re_fail.match(line)
            if m:
                name = m.group(1)
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
                continue

            m = re_skip.match(line)
            if m:
                name = m.group(1)
                if name not in passed_tests and name not in failed_tests:
                    skipped_tests.add(name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )