import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _parse_tsdb_version(base_label: str) -> tuple[int, int, int]:
    """Parse (major, minor, patch) from base.label like '2.26.1..2.26.2'."""
    first_tag = base_label.split("..")[0]
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", first_tag)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.match(r"(\d+)\.(\d+)", first_tag)
    if m:
        return (int(m.group(1)), int(m.group(2)), 0)
    raise ValueError(f"Cannot parse timescaledb version from base.label: {base_label}")


def _get_pg_version(version: tuple[int, int, int]) -> int:
    """Return the PostgreSQL major version to use for a given TimescaleDB version."""
    major, minor, _ = version
    if major == 0:
        return 10
    if major == 1:
        return 12
    if major == 2:
        if minor <= 1:
            return 12
        if minor <= 3:
            return 13
        if minor <= 5:
            return 14
        if minor <= 12:
            return 15
        if minor <= 17:
            return 16
        if minor <= 22:
            return 16
        return 17
    return 17


_VERSION_TO_IMAGE = [
    (2, 18, "ubuntu:24.04"),
    (2, 0, "ubuntu:22.04"),
    (1, 0, "ubuntu:22.04"),
    (0, 0, "ubuntu:22.04"),
]
_DEFAULT_IMAGE = "ubuntu:22.04"


def _get_base_image(version: tuple[int, int, int]) -> str:
    major, minor, _ = version
    for min_major, min_minor, image in _VERSION_TO_IMAGE:
        if major > min_major or (major == min_major and minor >= min_minor):
            return image
    return _DEFAULT_IMAGE




class ImageBase(Image):
    """PostgreSQL toolchain image, SHARED by every timescaledb PR on that pg major.

    The tag is ``base-pg{N}``, so across the 57 delivered PRs there are only 7
    distinct base images -- ``base-pg10`` alone is shared by 15 PRs. That sharing
    is the whole point (the pgdg apt layer is expensive, doubly so under QEMU on
    a multi-arch build), and it is also why this image MUST NOT clone the
    repository and MUST NOT reference ``BASE_COMMIT``.

    ``dependency()`` returns a *string* (``ubuntu:XX.XX``) and this Dockerfile
    carries no ``# syntax`` directive, so ``DockerfileEnhancer.enhance()``
    engages. Its ``_standardize_repo_fetch`` rewrites any
    ``RUN git clone ... /home/<repo>`` (or ``COPY <repo> /home/<repo>``) into
    ``git clone "${REPO_URL}"`` + ``git checkout ${BASE_COMMIT}`` + the
    history-stripping ``Image._HARDENING_BLOCK``, and ``build_dataset`` supplies
    a ``BASE_COMMIT`` buildarg for string dependencies. Because the tag is shared,
    that pinned this ONE image to whichever PR happened to build first and then
    ``gc --prune``d every other commit out of the clone -- so the remaining 14
    ``base-pg10`` PRs could no longer check out their own base commit.

    The clone, the per-PR ``${BASE_COMMIT}`` checkout and the hardening block
    therefore live in ``ImageDefault``, whose ``dependency()`` is an ``Image`` and
    whose Dockerfile the enhancer consequently emits verbatim. What this image
    keeps from the enhancer is exactly what is safe to share: the OCI labels.
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
        version = _parse_tsdb_version(self.pr.base.label)
        return _get_base_image(version)

    def image_tag(self) -> str:
        version = _parse_tsdb_version(self.pr.base.label)
        pg_ver = _get_pg_version(version)
        return f"base-pg{pg_ver}"

    def workdir(self) -> str:
        version = _parse_tsdb_version(self.pr.base.label)
        pg_ver = _get_pg_version(version)
        return f"base-pg{pg_ver}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        version = _parse_tsdb_version(self.pr.base.label)
        pg_ver = _get_pg_version(version)
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # Deliberately no `git clone` / `COPY <repo>` here -- see the class
        # docstring. Emitting either token would make DockerfileEnhancer rewrite
        # this shared image into a BASE_COMMIT-pinned, history-stripped clone.
        #
        # The leading `# syntax=` directive is load-bearing, not decoration:
        # DockerfileEnhancer.enhance() returns the Dockerfile VERBATIM as soon as
        # it sees that string. Without it the enhancer stamps its proxy ARGs
        # (http_proxy/https_proxy/CA_CERT_PATH), the proxy + SSL_CERT_FILE /
        # REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE ENV lines, the CA-cert symlink RUN
        # block and the mitm_ca secret mount into every image built from this
        # registry. Declaring it here removes all of that from the generated
        # Dockerfile without touching image.py, so no other repo is affected.
        #
        # Because the enhancer no longer contributes them, the ARG header, the
        # combined ENV block and the OCI labels are written out by hand below, in
        # the same order and shape as the reference base Dockerfile
        # (data_2/workdir/go-playground/validator/images/base/Dockerfile).
        # ARG BASE_COMMIT is declared but never referenced here -- exactly as in
        # the reference. Declaring it is inert; it was *using* it in a checkout
        # that pinned this shared image to a single PR (see the class docstring).
        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT


ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
WORKDIR /home/

RUN apt-get update && \\
    apt-get install -y gnupg lsb-release wget curl ca-certificates && \\
    install -d /usr/share/postgresql-common/pgdg && \\
    curl -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc --fail https://www.postgresql.org/media/keys/ACCC4CF8.asc && \\
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list && \\
    apt-get update && \\
    apt-get install -y \\
    build-essential cmake git libssl-dev libkrb5-dev \\
    postgresql-{pg_ver} postgresql-server-dev-{pg_ver} libpq-dev \\
    flex bison pkg-config sudo \\
    && (apt-get install -y tzdata-legacy 2>/dev/null || true) \
    && apt-get clean

RUN mv /usr/lib/postgresql/{pg_ver}/bin/pg_config /usr/lib/postgresql/{pg_ver}/bin/pg_config.real && \\
    echo '#!/bin/bash' > /usr/lib/postgresql/{pg_ver}/bin/pg_config && \\
    echo 'if [ "$1" = "--version" ]; then' >> /usr/lib/postgresql/{pg_ver}/bin/pg_config && \\
    echo '  /usr/lib/postgresql/{pg_ver}/bin/pg_config.real --version | sed "s/ (.*//"' >> /usr/lib/postgresql/{pg_ver}/bin/pg_config && \\
    echo 'else' >> /usr/lib/postgresql/{pg_ver}/bin/pg_config && \\
    echo '  /usr/lib/postgresql/{pg_ver}/bin/pg_config.real "$@"' >> /usr/lib/postgresql/{pg_ver}/bin/pg_config && \\
    echo 'fi' >> /usr/lib/postgresql/{pg_ver}/bin/pg_config && \\
    chmod +x /usr/lib/postgresql/{pg_ver}/bin/pg_config

{self.clear_env}

CMD ["/bin/bash"]
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
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _pg_version(self) -> int:
        version = _parse_tsdb_version(self.pr.base.label)
        return _get_pg_version(version)

    def _is_early_era(self) -> bool:
        """v0.x (minor < 8) uses plain Makefile + PGXS, no bootstrap script."""
        version = _parse_tsdb_version(self.pr.base.label)
        major, minor, _ = version
        return major == 0 and minor < 8
    def _is_mid_era(self) -> bool:
        """v0.8-0.12 uses bootstrap+CMake but has installcheck, not regresscheck."""
        version = _parse_tsdb_version(self.pr.base.label)
        major, minor, _ = version
        return major == 0 and minor >= 8

    def _build_commands(self) -> str:
        pg_ver = self._pg_version()
        if self._is_early_era():
            return (
                f"export PG_CONFIG=/usr/lib/postgresql/{pg_ver}/bin/pg_config\n"
                f"# Patch version check to accept PG10\n"
                f"sed -i 's/MIN_SUPPORTED_VERSION_STR \"9.6\"/MIN_SUPPORTED_VERSION_STR \"9.6\"/' Makefile 2>/dev/null || true\n"
                f"sed -i 's/MIN_SUPPORTED_VERSION 90600/MIN_SUPPORTED_VERSION 90600/' Makefile 2>/dev/null || true\n"
                f"make -j$(nproc) PG_CONFIG=/usr/lib/postgresql/{pg_ver}/bin/pg_config\n"
                f"make install PG_CONFIG=/usr/lib/postgresql/{pg_ver}/bin/pg_config"
            )
        # pg10-13 lack pg_isolation_regress; without -DREGRESS_CHECKS=OFF cmake
        # will FATAL_ERROR.  pg14+ ship the binary so we leave checks ON so that
        # the regresscheck make-target is generated (needed by _test_commands).
        regress_off = " -DREGRESS_CHECKS=OFF" if pg_ver <= 13 else ""
        return (
            f"export BUILD_FORCE_REMOVE=true\n"
            f"./bootstrap -DCMAKE_BUILD_TYPE=Release "
            f"-DPG_CONFIG=/usr/lib/postgresql/{pg_ver}/bin/pg_config{regress_off}\n"
            f"cd build && make -j$(nproc)\n"
            f"make install"
        )

    def _test_commands(self) -> str:
        pg_ver = self._pg_version()
        if self._is_early_era():
            return f'su - postgres -c "cd /home/{self.pr.repo} && make installcheck PG_CONFIG=/usr/lib/postgresql/{pg_ver}/bin/pg_config 2>&1 || true"'
        if self._is_mid_era():
            return f'su - postgres -c "cd /home/{self.pr.repo}/build && make installcheck 2>&1 || true"'
        return f'su - postgres -c "cd /home/{self.pr.repo}/build && make regresscheck 2>&1 || true && make regresscheck-t 2>&1 || true"'

    def files(self) -> list[File]:
        build_cmds = self._build_commands()
        test_cmds = self._test_commands()

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

""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
# The repo is already cloned and checked out at ${{BASE_COMMIT}} by dockerfile(),
# so this script performs no git checkout of its own -- it only asserts the tree
# is the expected commit and clean, then hands ownership to postgres. Runs BEFORE
# the hardening block, and `safe.directory` is set here so root's `git gc` in the
# (now postgres-owned) tree does not trip git's dubious-ownership check.
set -e

cd /home/{repo}
git config --global --add safe.directory /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "{sha}"
chown -R postgres:postgres /home/{repo}

""".format(repo=self.pr.repo, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
{build}
chown -R postgres:postgres /home/{repo}
{test}
""".format(repo=self.pr.repo, build=build_cmds, test=test_cmds),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --exclude="*.gz" /home/test.patch
{build}
chown -R postgres:postgres /home/{repo}
{test}

""".format(repo=self.pr.repo, build=build_cmds, test=test_cmds),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || git apply --whitespace=nowarn --exclude="*.gz" /home/test.patch /home/fix.patch
{build}
chown -R postgres:postgres /home/{repo}
{test}

""".format(repo=self.pr.repo, build=build_cmds, test=test_cmds),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # ${BASE_COMMIT} has to be defined before the checkout and the hardening
        # block reference it. build_dataset only injects REPO_URL/BASE_COMMIT
        # buildargs for *string* dependencies, and this image's dependency() is
        # an Image (the shared toolchain base), so both are baked in as ARG
        # defaults here -- still overridable at build time.
        header = f"""FROM {name}:{tag}

ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/
RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh
"""

        # Anti-reward-hacking hardening -- the canonical Image._HARDENING_BLOCK
        # (detach at ${BASE_COMMIT}, drop origin, delete every ref, expire the
        # reflog, gc/repack, drop alternates, then the same for submodules, with
        # asserts). Because dependency() is an Image, DockerfileEnhancer emits
        # this Dockerfile verbatim and does NOT append the block itself, so it is
        # written out here. Concatenated raw rather than interpolated into an
        # f-string so its ${BASE_COMMIT} and %(refname) tokens stay literal.
        # Placed last, after prepare.sh and before CMD, so nothing downstream can
        # reintroduce a ref and so it is never dead code.
        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + "\n" + Image._HARDENING_BLOCK + tail


@Instance.register("timescale", "timescaledb")
class TimescaleDB(Instance):
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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # pg_regress output format:
        # test <name>-<pgver>  ... ok   <ms>
        # test <name>-<pgver>  ... FAILED   <ms>
        # test <name>          ... ok   <ms>
        re_ok = re.compile(
            r"^(?:test\s+)?(\S+)\s+\.\.\.\s+ok\s+\d+\s*ms", re.MULTILINE
        )
        re_fail = re.compile(
            r"^(?:test\s+)?(\S+)\s+\.\.\.\s+FAILED\s+\d+\s*ms", re.MULTILINE
        )
        # TAP output format (v2.14+):
        # ok 1         + <name>   <ms>
        # not ok 102   + <name>   <ms>
        re_tap_ok = re.compile(
            r"^ok\s+\d+\s+[+-]\s+(\S+)\s+\d+\s*ms", re.MULTILINE
        )
        re_tap_fail = re.compile(
            r"^not ok\s+\d+\s+[+-]\s+(\S+)\s+\d+\s*ms", re.MULTILINE
        )

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            ok_match = re_ok.match(line)
            if ok_match:
                test_name = ok_match.group(1)
                passed_tests.add(test_name)
                continue

            fail_match = re_fail.match(line)
            if fail_match:
                test_name = fail_match.group(1)
                failed_tests.add(test_name)
                continue

            tap_ok_match = re_tap_ok.match(line)
            if tap_ok_match:
                test_name = tap_ok_match.group(1)
                passed_tests.add(test_name)
                continue

            tap_fail_match = re_tap_fail.match(line)
            if tap_fail_match:
                test_name = tap_fail_match.group(1)
                failed_tests.add(test_name)
                continue

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )

# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# Instance.create() routes on f"{org}/{number_interval}" whenever a delivered PR
# carries one, and falls back to f"{org}/{repo}" otherwise. The delivered
# timescale__timescaledb_lht_final.jsonl leaves number_interval null (PullRequest
# normalises that to ""), so the "timescale/timescaledb" registration above is
# what routes all 57 PRs today -- but a re-delivery that populates the field from
# prs_in_bundle would otherwise raise "Instance ... is not registered" before any
# image is built. Registering both shapes keeps the registry robust either way.
#
# The value is the bundle's PR numbers dash-joined VERBATIM -- never a start-end
# range. "146-157" would wrongly imply every PR from 146 to 157; the real bundle
# [146, 147, 150, 155, 157] is written "146-147-150-155-157".
_NUMBER_INTERVALS = [
    # lead PR 106 (0.1.0..0.2.0)
    "106-107-110-115-117-120-121-123-124-125",
    # lead PR 126 (0.2.0..0.3.0)
    "126-127-131-133-135-136-137-139-141",
    # lead PR 143 (0.3.0..0.4.0)
    "143-145-148-149-151-152-160-162-165-166-167-168-170",
    # lead PR 171 (0.4.0..0.4.1)
    "171-172-173-175-176-179-182-188-191",
    # lead PR 192 (0.4.1..0.4.2)
    "192-196",
    # lead PR 216 (0.5.0..0.6.0)
    "216-219-221-223-227-228-229-230-231-232-233-234-236-237-238-240-243-246-247-248-251-254",
    # lead PR 282 (0.8.0..0.9.0)
    "282-357-368-370-371-372-375-378-379-380-383-385-386-387-388-391-396-397-398-399-402-403-404-405-407-409-411-412-413-416-417-418-420-427-428-430-431-433-435-436-437-438-441-443-444-446-449-450-454-455",
    # lead PR 332 (0.7.0..0.7.1)
    "332-335",
    # lead PR 426 (0.9.0..0.9.1)
    "426-445-456-458-460-461-465-467-469-472-474-477-478-484",
    # lead PR 459 (0.10.1..0.11.0)
    "459-514-587-594-597-599-600-603-604-607-608-609-610-611-614-615-617-618-619-621-622-623-624-626-630-633-634-636-637-640-646-647-649-650-651-653-654",
    # lead PR 486 (0.9.1..0.9.2)
    "486-516-519-520-521",
    # lead PR 491 (0.9.2..0.10.0)
    "491-502-523-524-539-541-556-560-564-565-566-567-568-569-570-571",
    # lead PR 575 (0.10.0..0.10.1)
    "575-576-577-578-580-582-584-588-589-590-591",
    # lead PR 652 (0.11.0..0.12.0)
    "652-655-657-660-662-665-666-670-671-676-687-689-692-693-694",
    # lead PR 675 (0.12.0..0.12.1)
    "675-690-695-696-697-703-704-705-706-708-731",
    # lead PR 828 (1.0.0..1.0.1)
    "828-832-878",
    # lead PR 2035 (1.7.1..1.7.2)
    "2035-2047-2055",
    # lead PR 2255 (1.7.2..1.7.3)
    "2255-2261",
    # lead PR 2502 (1.7.4..1.7.5)
    "2502-2552-2631-2632-2668-2800-2920-2924-2933-2934-2939-2946-2950",
    # lead PR 2960 (2.0.1..2.0.2)
    "2960-2965",
    # lead PR 3061 (2.1.0..2.1.1)
    "3061-3062-3071",
    # lead PR 3283 (2.3.0..2.3.1)
    "3283-3373",
    # lead PR 3755 (2.5.0..2.5.1)
    "3755-3861",
    # lead PR 5236 (2.9.2..2.9.3)
    "5236-5258-5269-5273-5274-5283",
    # lead PR 5370 (2.10.0..2.10.1)
    "5370-5371-5372-5373-5374-5391-5392-5402",
    # lead PR 5408 (2.10.1..2.10.2)
    "5408-5423-5430-5457-5469-5493-5494-5495-5496-5503-5508-5559-5561-5563-5565-5566-5568-5571-5572-5576-5581-5588-5589",
    # lead PR 5597 (2.10.2..2.10.3)
    "5597-5598-5616-5618-5622",
    # lead PR 5714 (2.11.0..2.11.1)
    "5714-5751-5757-5781-5796-5800-5814-5816-5819-5820-5821-5823",
    # lead PR 5841 (2.11.1..2.11.2)
    "5841-5886-5929",
    # lead PR 6120 (2.12.0..2.12.1)
    "6120-6149-6150-6151-6154-6159-6162-6167-6173-6182",
    # lead PR 6192 (2.12.1..2.12.2)
    "6192-6201-6215-6217-6219-6220",
    # lead PR 6366 (2.13.0..2.13.1)
    "6366-6378-6418-6436-6441-6455-6457-6462-6467-6472-6478-6479-6481-6483-6485-6489-6490-6493",
    # lead PR 6647 (2.14.0..2.14.1)
    "6647-6649-6652",
    # lead PR 6914 (2.15.0..2.15.1)
    "6914-6924-6926-6930-6933-6942-6944-6945-6946-6950-6952-6954-6955-6961-6962-6963-6964-6970",
    # lead PR 6980 (2.15.1..2.15.2)
    "6980-6994-6995-7000-7001-7002-7003-7007-7009-7011-7014",
    # lead PR 7027 (2.15.2..2.15.3)
    "7027-7036-7037-7057-7067-7082-7083-7090-7092-7093",
    # lead PR 7346 (2.17.0..2.17.1)
    "7346-7350-7361-7366-7371-7379",
    # lead PR 7385 (2.17.1..2.17.2)
    "7385-7392-7393-7394-7395-7396-7397-7400-7402-7407-7420-7422-7423-7425",
    # lead PR 7641 (2.18.0..2.18.1)
    "7641-7660-7666-7670-7671-7678-7680",
    # lead PR 7688 (2.18.1..2.18.2)
    "7688-7698-7715-7717-7728-7730-7731-7733-7734-7737-7738",
    # lead PR 7851 (2.19.0..2.19.1)
    "7851-7860-7875-7891-7894",
    # lead PR 7916 (2.19.1..2.19.2)
    "7916-7924-7925-7927",
    # lead PR 7931 (2.19.2..2.19.3)
    "7931-7934-7937-7943-7944-7947-7950-7956-7957",
    # lead PR 8137 (2.20.0..2.20.1)
    "8137-8154-8155-8156-8161-8168-8169-8170-8174-8176-8184-8185",
    # lead PR 8202 (2.20.1..2.20.2)
    "8202-8203",
    # lead PR 8215 (2.20.2..2.20.3)
    "8215-8219-8239-8246-8248",
    # lead PR 8363 (2.21.0..2.21.1)
    "8363-8365-8404-8405",
    # lead PR 8429 (2.21.1..2.21.2)
    "8429-8431-8448-8450-8460",
    # lead PR 8473 (2.21.2..2.21.3)
    "8473-8475-8477-8480",
    # lead PR 8486 (2.21.3..2.21.4)
    "8486-8667-8668-8669-8670-8672-8678",
    # lead PR 8572 (2.22.0..2.22.1)
    "8572-8579-8597-8603-8604-8614-8633-8634-8639-8646-8652-8653-8658-8666-8680-8683-8689-8695-8709-8711-8714-8717-8718",
    # lead PR 8860 (2.23.0..2.23.1)
    "8860-8864-8865-8874-8886-8887-8891-8906-8911-8927-8928",
    # lead PR 9208 (2.25.0..2.25.1)
    "9208-9224-9233-9235-9242-9251-9259-9261-9269",
    # lead PR 9272 (2.25.1..2.25.2)
    "9272-9280-9286-9287-9288-9299-9302-9304-9311-9317-9330-9333-9335-9338-9340-9341-9343-9345-9346-9347-9348",
    # lead PR 9489 (2.26.1..2.26.2)
    "9489-9492-9496-9497-9498-9499-9523-9525-9526-9528-9529",
    # lead PR 9545 (2.26.2..2.26.3)
    "9545-9556-9562-9577-9585-9586-9587-9589-9591",
    # lead PR 9593 (2.26.3..2.26.4)
    "9593-9594-9601-9611-9617-9618-9620-9621-9647-9649-9651-9661-9662-9663-9664-9667-9669-9671-9672-9676-9677",
]

for _interval in _NUMBER_INTERVALS:
    Instance.register("timescale", _interval)(TimescaleDB)
