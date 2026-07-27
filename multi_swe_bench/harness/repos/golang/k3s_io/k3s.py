import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _test_pkgs(patch: str) -> list[str]:
    """Go package directories that own the ``*_test.go`` files in a patch.

    Scoping ``go test`` to just these packages (instead of ``./...``) is what
    keeps k3s honest: a whole-repo ``go test ./...`` marks entire packages
    ``[build failed]`` when any of them (or the test patch's own new code)
    fails to compile, which sweeps hundreds of unrelated base-passing tests
    into a false ``NONE`` at the test stage (the n2p contamination). ``./...``
    also pulls in ``tests/integration/*`` and ``tests/e2e/*`` which spin up real
    servers and hang the run. Testing only the patch's packages removes both.
    """
    pkgs: set[str] = set()
    for line in (patch or "").splitlines():
        if not line.startswith("diff --git a/"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        path = parts[2][2:] if parts[2].startswith("a/") else parts[2]
        if not path.endswith("_test.go"):
            continue
        pkg = path.rsplit("/", 1)[0] if "/" in path else "."
        # Exclude end-to-end / integration suites: they spin up real k3s
        # servers, etcd, containerd, VMs, etc. — they cannot run in the eval
        # container, so they only ever hang (capped by -timeout) or fail, adding
        # noise. A PR whose ONLY tests are e2e/integration yields no unit signal
        # and is correctly left with nothing to run (-> invalid).
        if pkg.startswith(("tests/e2e", "tests/integration", "tests/perf")):
            continue
        pkgs.add(pkg)
    return sorted(pkgs)


class K3sImageBase(Image):
    """ONE shared toolchain base image (golang + k3s build deps). Tag ``:base``.

    The harness builds this once and every PR image builds ``FROM`` it. It
    deliberately does NOT clone the repo and does NOT check out any commit, so a
    single ``:base`` can serve every PR. This matters because ``dependency()``
    returns a *string*, which means (a) DockerfileEnhancer rewrites any clone
    line here into "clone + checkout ${BASE_COMMIT} + _HARDENING_BLOCK", and
    (b) build_dataset.py passes BASE_COMMIT as a build arg. A clone in this
    layer would therefore gc-prune the shared base down to whichever single PR
    happened to trigger the base build, breaking every other PR. With no clone
    line, the enhancer contributes only the infra ARGs/ENV/LABEL and never
    hardens or pins this layer. The per-PR clone + checkout + hardening live in
    K3sImageDefault.
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
        return "golang:latest"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV GOTOOLCHAIN=auto
ENV GOFLAGS=-mod=mod

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    build-essential \\
    git \\
    gnupg \\
    make \\
    python3 \\
    sudo \\
    wget \\
    libseccomp-dev \\
    && rm -rf /var/lib/apt/lists/*

{self.clear_env}

"""


class K3sImageDefault(Image):
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
        # Chains to the ONE shared :base. Because this returns an Image (not a
        # string), DockerfileEnhancer returns dockerfile() verbatim and
        # build_dataset.py supplies no build args — so this layer bakes
        # REPO_URL/BASE_COMMIT as ARG defaults and applies _HARDENING_BLOCK
        # itself. This is the per-PR work the shared base cannot do.
        return K3sImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        pkgs = _test_pkgs(self.pr.test_patch)
        pkg_list = " ".join(pkgs) if pkgs else "./..."
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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
# Warm the Go module + build caches so the eval runs don't need network. The
# repo is already cloned and checked out at ${{BASE_COMMIT}} by the Dockerfile,
# and the hardening block runs right after this script, so prepare performs no
# git checkout of its own. The download/build steps are allowed to fail
# (|| true): their only job here is to populate caches, and the real pass/fail
# signal comes from run/test-run/fix-run.
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh

go mod download 2>&1 || true
go build -mod=mod ./... 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run_tests.sh",
                # Per-package `go test`, scoped to the packages the test patch
                # touches (baked as PKGS). Each package is fenced with
                # `### K3SPKG: <pkg> ###` so parse_log can namespace test ids and
                # avoid cross-package name collisions. -timeout caps hung
                # integration tests; -vet=off stops vet-only failures from
                # masking the real outcome; GOFLAGS=-mod=mod avoids the
                # double `go test` fallback.
                """#!/bin/bash
set -uo pipefail
cd /home/{repo}
export GOFLAGS=-mod=mod GOTOOLCHAIN=auto
go mod download 2>/dev/null || true

PKGS="{pkgs}"
if [ "$PKGS" = "./..." ]; then
  go test -v -count=1 -vet=off -timeout=20m ./... 2>&1 || true
else
  for pkg in $PKGS; do
    [ -d "$pkg" ] || continue
    echo "### K3SPKG: $pkg ###"
    go test -v -count=1 -vet=off -timeout=20m "./$pkg/" 2>&1 || true
  done
fi
""".format(repo=self.pr.repo, pkgs=pkg_list),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{repo}
bash /home/run_tests.sh
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{repo}
git checkout -- . 2>/dev/null || true
git apply --whitespace=nowarn /home/test.patch \\
  || git apply --whitespace=nowarn --3way /home/test.patch || true
bash /home/run_tests.sh
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{repo}
git checkout -- . 2>/dev/null || true
git apply --whitespace=nowarn /home/test.patch \\
  || git apply --whitespace=nowarn --3way /home/test.patch || true
git apply --whitespace=nowarn /home/fix.patch \\
  || git apply --whitespace=nowarn --3way /home/fix.patch || true
bash /home/run_tests.sh
""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # Per-PR work on the shared :base: clone, check out THIS PR's
        # ${BASE_COMMIT}, stage the scripts/patches, warm caches, then harden.
        # REPO_URL/BASE_COMMIT are baked as ARG defaults because chaining to a
        # base *Image* means DockerfileEnhancer returns this verbatim and the
        # builder supplies neither. The staged files live in /home/, outside the
        # git tree, so the hardening pass leaves them untouched.
        return """FROM {name}:{tag}

ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT="{base_sha}"

{global_env}

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

{hardening}

{clear_env}

CMD ["/bin/bash"]
""".format(
            name=name,
            tag=tag,
            org=self.pr.org,
            repo=self.pr.repo,
            base_sha=self.pr.base.sha,
            global_env=self.global_env,
            copy_commands=copy_commands,
            hardening=Image._HARDENING_BLOCK,
            clear_env=self.clear_env,
        )


@Instance.register("k3s-io", "k3s")
class K3s(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return K3sImageDefault(self.pr, self._config)

    _APPLY_OPTS = "--whitespace=nowarn"

    # Warm the module cache; -mod=mod so a single `go test` resolves deps without
    # the old `|| go test -mod=mod` double-run (which recompiled the whole tree
    # and was the ~2x eval slowness on old-era instances).
    _SETUP = "export GOFLAGS=-mod=mod GOTOOLCHAIN=auto ; go mod download 2>/dev/null || true"

    def _test_loop(self) -> str:
        """Per-package `go test` bash, scoped to the test patch's packages.

        Fenced with `### K3SPKG: <pkg> ###` (parse_log namespaces ids by it);
        -timeout caps hung integration tests; -vet=off avoids vet-only failures
        masking outcomes. Falls back to `./...` only when the patch carries no
        `*_test.go` (rare). See _test_pkgs for why scoping matters.
        """
        gotest = "go test -v -count=1 -vet=off -timeout=20m"
        pkgs = _test_pkgs(self.pr.test_patch)
        if not pkgs:
            return f"{gotest} ./... 2>&1 || true"
        pkg_list = " ".join(pkgs)
        return (
            f"for pkg in {pkg_list} ; do "
            f'[ -d "$pkg" ] || continue ; '
            f'echo "### K3SPKG: $pkg ###" ; '
            f'{gotest} "./$pkg/" 2>&1 || true ; '
            f"done"
        )

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        return "bash -c 'cd /home/{repo} ; {setup} ; {loop}'".format(
            repo=self.pr.repo,
            setup=self._SETUP,
            loop=self._test_loop(),
        )

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        return (
            "bash -c '"
            "cd /home/{repo} ; "
            "git checkout -- . ; "
            "git apply {opts} /home/test.patch || "
            "git apply {opts} --3way /home/test.patch || true ; "
            "{setup} ; "
            "{loop}"
            "'".format(
                repo=self.pr.repo,
                opts=self._APPLY_OPTS,
                setup=self._SETUP,
                loop=self._test_loop(),
            )
        )

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        return (
            "bash -c '"
            "cd /home/{repo} ; "
            "git checkout -- . ; "
            "git apply {opts} /home/test.patch || "
            "git apply {opts} --3way /home/test.patch || true ; "
            "git apply {opts} /home/fix.patch || "
            "git apply {opts} --3way /home/fix.patch || true ; "
            "{setup} ; "
            "{loop}"
            "'".format(
                repo=self.pr.repo,
                opts=self._APPLY_OPTS,
                setup=self._SETUP,
                loop=self._test_loop(),
            )
        )

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Strip ANSI escapes so `--- PASS/FAIL/SKIP` lines match cleanly.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_result = re.compile(r"^\s*--- (PASS|FAIL|SKIP):\s+(\S+)")
        re_pkg = re.compile(r"^### K3SPKG:\s+(\S+)\s+###")

        def get_base_name(test_name: str) -> str:
            index = test_name.rfind("/")
            if index == -1:
                return test_name
            return test_name[:index]

        # Namespace each test id by the package fence (`### K3SPKG: <pkg> ###`
        # emitted by the per-package run loop) so identically named tests in
        # different packages do not collide. Logs without the fence (e.g. the
        # `./...` fallback) parse exactly as before.
        pkg = ""
        for line in test_log.splitlines():
            line = line.strip()

            pkg_match = re_pkg.match(line)
            if pkg_match:
                pkg = pkg_match.group(1)
                continue

            result_match = re_result.match(line)
            if not result_match:
                continue

            status, test_name = result_match.group(1), result_match.group(2)
            base_name = get_base_name(test_name)
            tid = f"{pkg}::{base_name}" if pkg and pkg != "." else base_name

            if status == "PASS":
                failed_tests.discard(tid)
                skipped_tests.discard(tid)
                passed_tests.add(tid)
            elif status == "FAIL":
                passed_tests.discard(tid)
                skipped_tests.discard(tid)
                failed_tests.add(tid)
            elif status == "SKIP":
                if tid in passed_tests or tid in failed_tests:
                    continue
                skipped_tests.add(tid)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# Route bundled instances by their number_interval (the dash-joined list of
# prs_in_bundle, e.g. "146-147-150-155-157" — the ACTUAL PRs in the bundle, not
# a 146-157 range). Instance.create() looks up f"{org}/{number_interval}" when a
# record carries a non-empty number_interval, so each interval string must be
# registered against the K3s class (same pattern as juanfont/headscale and
# bufbuild/buf). One entry per instance in k3s-io__k3s_lht_final.jsonl (141).
_NUMBER_INTERVALS = [
    "10084-10097-10105-10112",
    "10095-10098-10114",
    "10096-10103-10113",
    "10446-10460-10497-10507-10536-10596",
    "10623-10650-10661-10665-10672-10704-10720",
    "10624-10651-10662-10666-10673-10705-10719",
    "12233-12318-12327-12345-12356-12359-12361",
    "12234-12317-12328-12346-12357-12363-12371",
    "12235-12316-12333-12347-12358-12364-12372",
    "12462-12492-12512-12518-12529",
    "13241-13251-13264-13267-13288-13306-13310-13318-13331-13337-13339-13340-13350-13361-13391-13399-13404-13421-13427-13436-13447-13452-13460-13483-13496-13513-13522-13536",
    "6615-6663-6707-6726-6760-6772-6778-6791-6797-6801-6807-6812-6828-6829-6832-6850-6851-6852-6857-6860-6876-6911-6922-6932-6944-6950-6952-6974-6979-7011",
    "7043-7045-7061-7064-7075-7079-7106-7138",
    "7259-8064-8215-8250-8265-8279-8284-8292-8295-8312-8342-8344-8346-8354-8385-8392-8397-8402-8414-8423-8433-8439-8458-8460-8470-8507-8523-8524-8526-8566-8568-8579-8581-8593-8602-8604-8624-8630-8638-8653-8667-8675-8682-8683-8710-8729-8739-8753-8761-8771-8792-8795",
    "7376-7379-7407-7435-7453-7462-7467-7472-7516-7536-7549-7577",
    "8324-8356",
    "8325-8357",
    "8326-8350-8384",
    "8759-8821-8828-8878-8887-8902-8907-8921-8937-8999",
    "8964-9014-9019-9028-9042-9077",
    "10089-10143-10183-10214-10222-10259-10290-10297-10314-10324-10332-10346-10356-10378-10429",
    "10090-10144-10182-10213-10221-10258-10289-10299-10315-10323-10331-10347-10355-10377-10428",
    "10142-10181-10212-10220-10249-10288-10298-10316-10322-10329-10334-10348-10354-10376-10427",
    "10498-10508-10539-10597",
    "10499-10509-10541-10598",
    "10500-10510-10542-10599",
    "10649-10660-10664-10671-10703-10721",
    "10801-10818-10843-10872-10888-10909",
    "10802-10817-10842-10871-10895-10910",
    "10803-10819-10844-10873-10885-10908",
    "10804-10820-10845-10874-10884-10907",
    "10903-10975-11003-11022-11044-11047-11061-11073-11083-11092-11113-11126-11162",
    "10904-10974-11002-11023-11041-11048-11054-11072-11079-11093-11118-11125-11155",
    "10905-10976-11004-11021-11043-11046-11062-11074-11084-11094-11114-11127-11160",
    "10906-10977-11005-11020-11042-11045-11063-11075-11085-11095-11115-11128-11161",
    "11227-11248-11262-11299-11308-11325-11371-11404",
    "11229-11249-11263-11300-11309-11326-11370-11405",
    "11230-11247-11261-11302-11307-11324-11372-11403",
    "11393-11562-11566-11588-11596-11611-11621-11649",
    "11394-11561-11567-11589-11597-11612-11618-11650",
    "11395-11560-11568-11590-11598-11613-11615-11651",
    "11440-11446-11456-11459-11460-11466",
    "11441-11445-11455-11458-11461-11465",
    "11442-11444-11454-11457-11462-11464",
    "11563-11565-11583-11610-11620-11648",
    "11683-11726-11738-11765-11772-11785-11822",
    "11684-11725-11737-11764-11786-11821",
    "11685-11724-11732-11763-11787-11820-11834",
    "11686-11723-11730-11788-11819-11833",
    "11867-11888-11919-11930-11953-11960-11968-11991-12000-12003",
    "11868-11887-11920-11927-11954-11958-11990-11999-12004",
    "11869-11886-11921-11928-11955-11959-11989-11998",
    "11929-11931-11956-11957-11988-11997",
    "12030-12037-12044-12066-12076-12097-12105-12142-12150-12168-12179-12190-12207",
    "12031-12038-12042-12050-12067-12079-12098-12104-12141-12151-12167-12178-12189-12209",
    "12035-12043-12065-12077-12099-12106-12143-12149-12169-12180-12191-12208",
    "12319-12325-12344-12355-12360-12370",
    "12461-12498-12515-12520-12531",
    "12463-12497-12513-12519-12530",
    "12499-12516-12521-12532-12571-12576-12611-12633-12643-12662",
    "12572-12604-12610-12642-12650",
    "12573-12605-12608-12631-12652",
    "12574-12603-12609-12641-12651",
    "12694-12718-12728-12741-12746-12758-12760-12764",
    "12695-12719-12730-12742-12747-12759-12761-12765",
    "12696-12721-12732-12743-12748-12762-12763-12766",
    "12885-12895",
    "12886-12894",
    "12887-12893",
    "12957-13032-13040-13057-13078-13091-13119-13125-13132-13144-13159-13177-13193-13199-13208",
    "12958-13033-13041-13058-13077-13092-13116-13126-13133-13145-13158-13178-13194-13200-13207",
    "12959-13034-13042-13059-13076-13093-13117-13127-13134-13148-13157-13179-13195-13202-13206",
    "13035-13043-13060-13075-13094-13118-13128-13135-13146-13156-13180-13196-13203-13205",
    "13239-13266-13269-13290-13299-13313-13320-13333-13342-13360-13363-13365-13393-13401-13406-13423-13429-13434-13450-13454-13462-13481-13494-13511-13520-13538",
    "13240-13252-13265-13268-13289-13307-13312-13319-13332-13341-13359-13362-13392-13400-13405-13422-13428-13435-13448-13453-13461-13482-13493-13512-13521-13537",
    "13390-13398-13403-13420-13426-13437-13446-13451-13484-13497-13523-13535-13560-13564-13570-13576-13580-13602-13608-13619-13630-13637",
    "13561-13565-13571-13577-13581-13601-13609-13620-13631-13636",
    "13562-13566-13572-13578-13582-13600-13610-13621-13632-13635",
    "13563-13567-13573-13579-13583-13599-13611-13622-13633-13634",
    "13682-13691-13702-13706",
    "13683-13694-13700-13704",
    "13688-13692-13701-13705",
    "13689-13690-13703-13707",
    "13757-13771-13789-13797-13815-13822-13827-13835-13851-13868",
    "13758-13772-13790-13798-13812-13823-13828-13834-13850-13869",
    "13759-13773-13791-13799-13813-13824-13829-13833-13849-13870",
    "6129-6130-6135-6140-6168",
    "6131-6151-6161-6180-6181-6183-6193-6203-6216-6220-6223-6224-6230-6232-6245-6253-6269-6284-6306-6321",
    "6147-6247-6267-6292-6294-6295-6296-6298-6300-6315-6316-6317-6320-6334-6337-6338-6345-6353-6354-6359-6371-6386-6388-6395-6396-6397-6399-6403-6405-6408-6409-6410-6413-6417-6468-6475-6477-6492-6494-6506-6508",
    "6237-6400-6420-6453-6464-6497-6498-6512-6517-6519-6522-6531-6534-6545-6549-6552-6559-6567-6568-6572-6582-6588-6593-6622-6631-6646-6694",
    "6445-7097-7217-7300-7303-7308-7323-7324-7331-7339-7351-7364-7371-7373-7380-7383-7387-7388-7389-7414-7415-7416-7418-7422-7423-7425-7442-7443-7454-7455-7524-7525-7533-7539-7550-7551-7567-7575-7591-7597-7608",
    "6560-6583-6614-6618-6635-6653-6682-6683-6686-6687-6688-6693-6696-6700-6701-6706-6715-6718-6722-6725-6737-6742-6744-6746-6753-6764-6769-6774",
    "6616-6695-6731-6736-6748-6762-6767-6788",
    "6730-6735-6747-6761-6768-6775",
    "6782-6798-6837-6853-6858-6864-6867-6887-6904-6907-6915-6916-6919-6929-6936-6941-6954-6975-6987-7010",
    "6783-6799-6838-6854-6859-6865-6868-6888-6905-6908-6918-6920-6925-6930-6937-6942-6955-6976-6988-7009",
    "6874-8383-8703-8812-8815-8910-8916-8917-8944-8973-8977-8984-9025-9036-9039-9049-9054-9062-9070-9076-9084-9086-9090-9104-9110-9118-9153-9159-9195-9208-9210-9213-9219-9259-9278-9323-9332-9345",
    "6885-6890-6946-6970-6973-6996-7002-7032-7039-7041-7044-7057-7066-7088-7101-7104-7108-7109-7113-7136",
    "6945-7089-7091-7111-7142-7146-7147-7154-7159-7161-7167-7168-7169-7170-7171-7181-7187-7194-7209-7210-7215-7218-7256-7257-7264-7274-7282-7292",
    "6998-7682-7791-7803-7805-7807-7827-7833-7834-7836-7838-7839-7845-7848-7858-7862-7864-7879-7887-7939-7950-7977-7978-8014-8028",
    "7042-7046-7063-7065-7076-7080-7105",
    "7121-7164-7221-7228-7240-7276-7283",
    "7122-7165-7222-7229-7241-7277-7284",
    "7352-7437-7526-7564-7583-7605-7616-7617-7619-7626-7628-7634-7635-7653-7655-7661-7664-7667-7672-7681-7683-7686-7695-7696-7716-7740-7745-7776-7777-7790",
    "7360-7374-7377-7399-7403-7432-7444-7460-7465-7474-7514-7534-7547-7573-7576-7598",
    "7361-7375-7378-7404-7433-7452-7461-7466-7473-7515-7535-7548-7574-7582",
    "7648-7658-7693-7717-7721-7727-7751-7757-7762-7782-7789",
    "7649-7659-7705-7718-7722-7728-7752-7758-7763-7784-7788-7818",
    "7719-7726-7729-7742-7753-7759-7764-7785",
    "7855-7859-7874-7882-7885-7893-7901-7908-7914-7944-7956-7968-7983-8022",
    "7856-7860-7873-7883-7886-7894-7900-7909-7915-7945-7954-7969-7984-8021",
    "7857-7861-7872-7899-7910-7916-7946-7955-7970-7985-8023",
    "7938-7972-7991-8013-8018-8047-8056-8057-8067-8077-8079-8080-8083-8085-8090-8092-8099-8109-8110-8125-8136-8138-8147-8150-8154-8155-8156-8177-8178-8184-8193-8204-8219-8236-8257-8273",
    "8075-8097-8122-8126-8129-8144-8170-8189-8212-8222-8235-8258-8274",
    "8076-8098-8123-8127-8132-8145-8169-8190-8213-8223-8241-8246-8259-8275",
    "8087-8124-8128-8135-8146-8168-8191-8214-8240-8243-8260-8276",
    "8244-8355-8521-8702-8711-8717-8724-8726-8751-8758-8764-8778-8786-8798-8799-8800-8835-8861-8863-8871-8885-8886-8894-8906-8920-8926-8983-8998",
    "8300-8412-8420-8436-8444-8453-8456-8465-8505-8510-8517-8552-8559-8570-8577-8583-8590-8598-8616-8635-8643-8647-8655-8663-8680-8691-8734-8766-8776-8790",
    "8301-8413-8421-8437-8445-8454-8457-8466-8506-8511-8518-8553-8560-8578-8584-8589-8599-8617-8636-8644-8646-8654-8664-8679-8692-8735-8767-8777-8791",
    "8305-8323-8364",
    "8411-8419-8435-8443-8451-8455-8464-8504-8509-8516-8551-8558-8569-8576-8582-8587-8597-8615-8634-8642-8650-8656-8662-8669-8681-8690-8733-8765-8775-8789",
    "8760-8820-8829-8879-8888-8903-8922-8938-9000",
    "8819-8867-8880-8889-8904-8923-8939-8993-8994",
    "8913-8936-8954-8962-9022-9027-9040-9081-9083",
    "8945-8953-8959-9184-9185-9237-9247-9249-9263-9290-9308-9309-9311-9312-9317-9318-9340-9344-9349-9353-9354-9388-9395-9396-9422-9493-9503-9517-9539-9571",
    "8963-9013-9018-9041-9078-9082",
    "9116-9123-9177-9183-9212-9218-9221-9230-9262-9271-9338-9348",
    "9117-9124-9176-9182-9211-9217-9220-9229-9261-9270-9337-9347",
    "9125-9175-9181-9203-9206-9216-9228-9260-9269-9336-9346",
    "9233-9364-9593-9948-9963-9964-9975-9977-9992-10019-10039-10040-10045-10047-10048-10049-10073-10074-10081-10082-10100-10118-10119-10122-10123-10131-10136-10137-10145-10146-10147-10177-10187-10192-10210-10211-10241-10257-10268-10271-10280-10293-10296-10302-10307-10318-10349-10352-10372-10417-10422",
    "9252-9292-9406-9409-9421-9423-9428-9429-9442-9446-9464-9471-9490-9510-9514-9547-9580",
    "9253-9291-9405-9407-9420-9425-9427-9430-9441-9445-9463-9470-9491-9509-9515-9546-9579",
    "9254-9293-9401-9404-9419-9424-9426-9431-9440-9444-9462-9469-9492-9508-9516-9545-9578",
    "9357-9472-9479-9480-9488-9495-9502-9512-9513-9519-9520-9522-9528-9555-9556-9562-9581-9582-9584-9586-9595-9599-9601-9615-9634-9635-9648-9649-9660-9666-9671-9673-9697-9698-9729-9747",
    "9572-9711-9718-9721-9722-9755-9757-9766-9770-9772-9780-9784-9801-9802-9806-9808-9809-9816-9832-9835-9838-9840-9844-9853-9863-9877-9883-9886-9890-9902-9909-9920-9926-9941-9960-9984-10001",
    "9605-9608-9631-9641-9647-9653-9669-9707-9733-9746",
    "9606-9609-9632-9642-9654-9670-9708-9734-9745",
    "9607-9610-9633-9645-9655-9692-9735-9740",
    "9803-9822-9825-9828-9850-9881-9912-9939-9943-9958-9995-10003",
    "9804-9821-9824-9827-9849-9880-9911-9938-9942-9959-9994-10002",
    "9940-10031-10057-10091-10106-10108-10115",
]
for _interval in _NUMBER_INTERVALS:
    Instance.register("k3s-io", _interval)(K3s)
