from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "bazelbuild"
_REPO = "bazel"
_INTERVAL_NAME = "bazel_32039_to_30948"
_INTERVAL_LO = 30948
_INTERVAL_HI = 32039
_BASE_IMAGE = "ubuntu:22.04"
_BASE_TAG = "base-32039_to_30948"
_REPO_DIR = "/home/bazel"
_BAZELISK_VERSION = "v1.25.0"
_BAZELISK_SHA256_AMD64 = "fd8fdff418a1758887520fa42da7e6ae39aefc788cf5e7f7bb8db6934d279fc4"
_BAZELISK_SHA256_ARM64 = "4c8d966e40ac2c4efcc7f1a5a5cceef2c0a2f16b957e791fa7a867cce31e8fcb"
_ANCHOR_COMMIT = "ee35cb8887999d1b39cdc97b61dd2ac43e8c93b4"
_ANCHOR_ANALYSIS_TARGETS = "//src/test/java/com/google/devtools/build/lib/..."
_ANCHOR_BUILD_TARGETS = (
    "//src/main/java/com/google/devtools/build/lib/..."
    " //src/test/java/com/google/devtools/build/lib/testutil/..."
)
_WARM_BAZEL_VERSIONS = (
    "7.6.2",
    "7.7.0",
    "8.8.0",
    "8.8.2",
    "9.1.1",
    "9.2.0",
    "9.3.0",
    "9.3.1",
)
_TEST_FLAGS = (
    "--keep_going --build_tests_only --nocache_test_results --test_output=errors"
    " --test_timeout=1800 --flaky_test_attempts=2 --local_test_jobs=2 --noshow_progress"
)

_BAZELRC = """startup --output_user_root=/home/mswb/.cache/bazel
startup --host_jvm_args=-Xmx4g
common --lockfile_mode=off
build --symlink_prefix=/
build --color=no
build --curses=no
build --verbose_failures
build --repository_cache=/home/mswb/.cache/bazel-repo
build --local_resources=memory=HOST_RAM*.6
"""

_BASE_WARM_SH = """#!/bin/bash
set -o pipefail
cd /home/bazel
git checkout --detach {anchor}
for version in {versions}; do
    USE_BAZEL_VERSION="$version" /usr/local/bin/bazelisk --version || true
done
bazel build --nobuild --keep_going --noshow_progress {analysis_targets} || true
bazel build --keep_going --noshow_progress {build_targets} || true
bazel shutdown || true
exit 0
""".format(
    anchor=_ANCHOR_COMMIT,
    versions=" ".join(_WARM_BAZEL_VERSIONS),
    analysis_targets=_ANCHOR_ANALYSIS_TARGETS,
    build_targets=_ANCHOR_BUILD_TARGETS,
)

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

git rev-parse --is-inside-work-tree > /dev/null 2>&1

test -z "$(git status --porcelain)"

exit 0
"""

_PREPARE_SH = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/bazel
git config --global --add safe.directory '*'
git reset --hard
git clean -fdq
bash /home/check_git_changes.sh
git cat-file -e "${BASE_COMMIT}^{commit}" 2>/dev/null || \\
    git fetch --no-tags origin "+${BASE_COMMIT}:refs/heads/mswb-base-commit"
git checkout --detach "${BASE_COMMIT}"
bash /home/check_git_changes.sh

chown -R mswb:mswb /home/bazel
runuser -u mswb -- env HOME=/home/mswb USER=mswb bash /home/warm.sh

git reset --hard
git clean -fdq
bash /home/check_git_changes.sh

test -x /usr/local/bin/bazel
test -r /home/mswb/.bazelrc
test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"
"""

_WARM_SH = """#!/bin/bash
set -o pipefail
export CI=true

cd /home/bazel
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || \\
    git apply --whitespace=nowarn /home/test.patch || true
python3 /home/mswb_bazel.py targets /home/test.patch /home/fix.patch /tmp/mswb_map.json \\
    > /tmp/mswb_warm.txt || true
if [ -s /tmp/mswb_warm.txt ]; then
    mapfile -t WARM < /tmp/mswb_warm.txt
    bazel build --keep_going --noshow_progress "${WARM[@]}" || true
fi
bazel shutdown || true
git checkout -- . || true
exit 0
"""

_STAGE_SH = """#!/bin/bash
set -o pipefail
export CI=true

cd /home/bazel
git reset --hard
git clean -fdq
if [ "$#" -gt 0 ]; then
    git apply --whitespace=nowarn "$@" || exit 1
fi

python3 /home/mswb_bazel.py targets /home/test.patch /home/fix.patch /tmp/mswb_map.json \\
    > /tmp/mswb_targets.txt
rm -f /tmp/mswb_bep.json
if [ -s /tmp/mswb_targets.txt ]; then
    mapfile -t TARGETS < /tmp/mswb_targets.txt
    bazel test {flags} --build_event_json_file=/tmp/mswb_bep.json "${{TARGETS[@]}}" || true
fi
bazel shutdown || true
python3 /home/mswb_bazel.py digest /tmp/mswb_targets.txt /tmp/mswb_bep.json /tmp/mswb_map.json
exit 0
""".format(flags=_TEST_FLAGS)

_RUN_SH = """#!/bin/bash
set -eo pipefail
exec runuser -u mswb -- env HOME=/home/mswb USER=mswb bash /home/stage.sh
"""

_TEST_RUN_SH = """#!/bin/bash
set -eo pipefail
exec runuser -u mswb -- env HOME=/home/mswb USER=mswb bash /home/stage.sh /home/test.patch
"""

_FIX_RUN_SH = """#!/bin/bash
set -eo pipefail
exec runuser -u mswb -- env HOME=/home/mswb USER=mswb bash /home/stage.sh \\
    /home/test.patch /home/fix.patch
"""

_MSWB_BAZEL_PY = r'''import json
import os
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET

RULE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\(", re.M)
NAME_RE = re.compile(r"\bname\s*=\s*\"([^\"]+)\"")
STRING_RE = re.compile(r"\"([^\"]*)\"")
JAVA_METHOD_RE = re.compile(r"@Test\b[^;{]*?\bvoid\s+([A-Za-z0-9_]+)\s*\(", re.S)
PY_CLASS_RE = re.compile(r"^class\s+([A-Za-z0-9_]+)\s*[(:]", re.M)
PY_METHOD_RE = re.compile(r"^[ \t]+def\s+(test[A-Za-z0-9_]*)\s*\(", re.M)
JAVA_CLASS_RE = re.compile(
    r"^[a-z][A-Za-z0-9_]*(\.[a-z][A-Za-z0-9_]*)*\.[A-Z][A-Za-z0-9_]*([$][A-Za-z0-9_]+)*$"
)
BUILD_NAMES = ("BUILD.bazel", "BUILD")
SKIP_KINDS = frozenset([
    "filegroup", "genrule", "package", "licenses", "exports_files", "load",
    "alias", "config_setting", "test_suite", "package_group", "toolchain",
    "platform", "constraint_setting", "constraint_value",
])
SKIP_ROOTS = ("src/test/shell/", "src/test/py/", "src/test/tools/")
JAVA_ROOTS = ("java", "javatests")
PASSED = "PASSED"
FAILED = "FAILED"
SKIPPED = "SKIPPED"
RANK = {FAILED: 3, PASSED: 2, SKIPPED: 1}


def read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def is_file(path):
    return os.path.isfile(path)


def list_files(root):
    out = []
    base = root if root else "."
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            full = os.path.join(dirpath, name)
            out.append(os.path.relpath(full, base).replace("\\", "/"))
    return out


def patch_paths(patch_file):
    text = read_text(patch_file)
    if not text:
        return []
    out = []
    for line in text.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
            if path and path != "/dev/null" and path not in out:
                out.append(path)
    return out


def is_test_source(path):
    if path.startswith(SKIP_ROOTS):
        return False
    base = path.rsplit("/", 1)[-1]
    if "." not in base:
        return False
    stem, ext = base.rsplit(".", 1)
    segments = path.split("/")
    if ext == "java":
        if not any(seg in ("test", "tests", "javatests") for seg in segments):
            return False
        return stem.endswith("Test") or stem.endswith("Tests")
    if ext in ("py", "sh", "cc", "cpp"):
        return stem.endswith("_test")
    return False


def find_package(path):
    directory = path.rsplit("/", 1)[0] if "/" in path else ""
    while True:
        for name in BUILD_NAMES:
            candidate = (directory + "/" + name) if directory else name
            if is_file(candidate):
                return directory, candidate
        if not directory:
            return None, None
        directory = directory.rsplit("/", 1)[0] if "/" in directory else ""


def parse_rules(text):
    starts = [(m.start(), m.group(1)) for m in RULE_RE.finditer(text)]
    rules = []
    for index, (position, kind) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(text)
        block = text[position:end]
        match = NAME_RE.search(block)
        if match:
            rules.append((kind, match.group(1), block))
    return rules


def glob_calls(block):
    calls = []
    for match in re.finditer(r"\bglob\s*\(", block):
        depth = 0
        index = match.end() - 1
        while index < len(block):
            if block[index] == "(":
                depth += 1
            elif block[index] == ")":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        calls.append(block[match.end():index])
    return calls


def glob_patterns(call_text):
    cut = call_text.find("exclude")
    if cut == -1:
        return STRING_RE.findall(call_text), []
    return STRING_RE.findall(call_text[:cut]), STRING_RE.findall(call_text[cut:])


def glob_regex(pattern):
    parts = pattern.split("/")
    expression = ""
    for index, segment in enumerate(parts):
        last = index == len(parts) - 1
        if segment == "**":
            expression += ".*" if last else "(?:[^/]*/)*"
            continue
        piece = ""
        for character in segment:
            if character == "*":
                piece += "[^/]*"
            elif character == "?":
                piece += "[^/]"
            else:
                piece += re.escape(character)
        expression += piece
        if not last:
            expression += "/"
    return re.compile("^" + expression + "$")


def glob_matches(call_text, relative):
    includes, excludes = glob_patterns(call_text)
    if not any(glob_regex(pattern).match(relative) for pattern in includes):
        return False
    return not any(glob_regex(pattern).match(relative) for pattern in excludes)


def block_covers(block, relative):
    if '"' + relative + '"' in block:
        return True
    for call in glob_calls(block):
        if glob_matches(call, relative):
            return True
    return False


def block_references(block, name):
    return ('":' + name + '"') in block


def test_targets_for(rules, relative, stem):
    hits = []
    for kind, name, block in rules:
        if kind.endswith("_test") and name == stem and name not in hits:
            hits.append(name)
    if hits:
        return hits
    for kind, name, block in rules:
        if kind.endswith("_test") and block_covers(block, relative) and name not in hits:
            hits.append(name)
    if hits:
        return hits
    frontier = []
    seen = set()
    for kind, name, block in rules:
        if kind in SKIP_KINDS or kind.endswith("_test"):
            continue
        if block_covers(block, relative):
            frontier.append(name)
            seen.add(name)
    while frontier:
        nxt = []
        for kind, name, block in rules:
            if name in seen or kind in SKIP_KINDS:
                continue
            if any(block_references(block, parent) for parent in frontier):
                seen.add(name)
                if kind.endswith("_test"):
                    if name not in hits:
                        hits.append(name)
                else:
                    nxt.append(name)
        frontier = nxt
    return hits


def rule_sources(rules, name, package):
    blocks = {}
    kinds = {}
    for kind, rule_name, block in rules:
        blocks[rule_name] = block
        kinds[rule_name] = kind
    seen = set([name])
    stack = [name]
    sources = set()
    listing = None
    while stack:
        current = stack.pop()
        block = blocks.get(current)
        if block is None:
            continue
        for value in STRING_RE.findall(block):
            if value.startswith("//") or value.startswith("@") or value.startswith(":"):
                continue
            if not value.endswith((".java", ".py")):
                continue
            full = (package + "/" + value) if package else value
            if is_file(full):
                sources.add(full)
        calls = glob_calls(block)
        if calls:
            if listing is None:
                listing = list_files(package)
            for call in calls:
                for relative in listing:
                    if not relative.endswith((".java", ".py")):
                        continue
                    if glob_matches(call, relative):
                        full = (package + "/" + relative) if package else relative
                        sources.add(full)
        for other_name in blocks:
            if other_name in seen or kinds.get(other_name) in SKIP_KINDS:
                continue
            if block_references(block, other_name):
                seen.add(other_name)
                stack.append(other_name)
    return sorted(sources)


def collect_targets(paths, mapping):
    for path in paths:
        if not is_file(path):
            continue
        package, build_file = find_package(path)
        if build_file is None:
            continue
        text = read_text(build_file)
        if not text:
            continue
        rules = parse_rules(text)
        relative = path[len(package) + 1:] if package else path
        stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        for name in test_targets_for(rules, relative, stem):
            label = ("//" + package + ":" + name) if package else ("//:" + name)
            entry = mapping.setdefault(
                label, {"package": package, "rule": name, "files": []}
            )
            if path not in entry["files"]:
                entry["files"].append(path)
    return mapping


def resolve(test_files, fix_files):
    mapping = collect_targets([p for p in test_files if is_test_source(p)], {})
    if mapping:
        return mapping
    rest = [
        p for p in (test_files + fix_files)
        if not p.startswith(SKIP_ROOTS) and not is_test_source(p)
    ]
    return collect_targets(rest, {})


def enrich(mapping):
    for entry in mapping.values():
        package = entry["package"]
        build_file = None
        for name in BUILD_NAMES:
            candidate = (package + "/" + name) if package else name
            if is_file(candidate):
                build_file = candidate
                break
        if build_file is None:
            entry["sources"] = list(entry["files"])
            continue
        rules = parse_rules(read_text(build_file) or "")
        entry["sources"] = rule_sources(rules, entry["rule"], package)
        if not entry["sources"]:
            entry["sources"] = list(entry["files"])
    return mapping


def java_source_for(label, classname):
    package = label[2:].split(":", 1)[0]
    segments = package.split("/")
    root_index = -1
    for index, segment in enumerate(segments):
        if segment in JAVA_ROOTS:
            root_index = index
    if root_index == -1:
        return None
    root = "/".join(segments[:root_index + 1])
    outer = classname.split("$", 1)[0]
    return root + "/" + outer.replace(".", "/") + ".java"


def python_source_for(entry):
    rule = entry.get("rule", "")
    package = entry.get("package", "")
    for path in entry.get("sources") or []:
        if path.endswith(".py") and path.rsplit("/", 1)[-1][:-3] == rule:
            return path
    for path in entry.get("files") or []:
        if path.endswith(".py"):
            return path
    candidate = (package + "/" + rule + ".py") if package else (rule + ".py")
    if is_file(candidate):
        return candidate
    return None


def test_identifier(label, entry, classname, name):
    name = " ".join(name.split())
    classname = classname.strip()
    if classname and JAVA_CLASS_RE.match(classname):
        source = java_source_for(label, classname)
        if source:
            inner = ""
            if "$" in classname:
                inner = classname.split("$", 1)[1].replace("$", ".") + "."
            return source + "::" + inner + name
    short = classname.rsplit(".", 1)[-1] if classname else ""
    suffix = (short + "." + name) if short else name
    source = python_source_for(entry)
    if source:
        return source + "::" + suffix
    return label + "::" + suffix


def enumerate_methods(entry):
    identifiers = []
    for path in entry.get("sources") or entry.get("files") or []:
        text = read_text(path)
        if not text:
            continue
        if path.endswith(".java"):
            for method in JAVA_METHOD_RE.findall(text):
                identifier = path + "::" + method
                if identifier not in identifiers:
                    identifiers.append(identifier)
        elif path.endswith(".py"):
            classes = PY_CLASS_RE.findall(text)
            prefix = (classes[0] + ".") if classes else ""
            for method in PY_METHOD_RE.findall(text):
                identifier = path + "::" + prefix + method
                if identifier not in identifiers:
                    identifiers.append(identifier)
    return identifiers


def read_bep(bep_file):
    summaries = {}
    documents = {}
    text = read_text(bep_file)
    if not text:
        return summaries, documents
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        identifier = event.get("id") or {}
        summary = identifier.get("testSummary")
        if summary and summary.get("label"):
            summaries[summary["label"]] = (event.get("testSummary") or {}).get(
                "overallStatus", "NO_STATUS"
            )
        result = identifier.get("testResult")
        if result and result.get("label"):
            key = (result["label"], result.get("run", 1), result.get("shard", 1))
            try:
                attempt = int(result.get("attempt", 1))
            except (TypeError, ValueError):
                attempt = 1
            outputs = (event.get("testResult") or {}).get("testActionOutput") or []
            for output in outputs:
                if output.get("name") != "test.xml":
                    continue
                uri = output.get("uri", "")
                if not uri.startswith("file://"):
                    continue
                path = urllib.parse.unquote(uri[len("file://"):])
                if key not in documents or documents[key][0] <= attempt:
                    documents[key] = (attempt, path)
    return summaries, documents


def collect(targets_file, bep_file, map_file):
    raw = read_text(targets_file) or ""
    labels = [line.strip() for line in raw.splitlines() if line.strip()]
    mapping = {}
    state = read_text(map_file)
    if state:
        try:
            mapping = json.loads(state)
        except ValueError:
            mapping = {}
    summaries, documents = read_bep(bep_file)
    results = {}

    def record(identifier, status):
        if RANK[status] > RANK.get(results.get(identifier), 0):
            results[identifier] = status

    covered = set()
    for key in sorted(documents):
        label = key[0]
        path = documents[key][1]
        entry = mapping.get(label) or {
            "package": label[2:].split(":", 1)[0],
            "rule": label.split(":")[-1],
            "files": [],
            "sources": [],
        }
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError):
            continue
        for case in root.iter("testcase"):
            name = case.get("name") or ""
            if not name:
                continue
            identifier = test_identifier(label, entry, case.get("classname") or "", name)
            if case.find("failure") is not None or case.find("error") is not None:
                status = FAILED
            elif case.find("skipped") is not None:
                status = SKIPPED
            else:
                status = PASSED
            record(identifier, status)
            covered.add(label)
    for label in labels:
        overall = summaries.get(label, "NO_STATUS")
        green = overall in ("PASSED", "FLAKY")
        record("bazel::" + label.lstrip("/"), PASSED if green else FAILED)
        if green or label in covered:
            continue
        entry = mapping.get(label)
        if not entry:
            continue
        for identifier in enumerate_methods(entry):
            record(identifier, FAILED)
    return results


def main():
    command = sys.argv[1]
    if command == "targets":
        test_files = patch_paths(sys.argv[2])
        fix_files = patch_paths(sys.argv[3]) if len(sys.argv) > 3 else []
        mapping = enrich(resolve(test_files, fix_files))
        with open(sys.argv[4], "w", encoding="utf-8") as handle:
            json.dump(mapping, handle)
        for label in sorted(mapping):
            print(label)
        return
    results = collect(sys.argv[2], sys.argv[3], sys.argv[4])
    print("===MSWB TESTS BEGIN===")
    for identifier in sorted(results):
        print(results[identifier] + "\t" + identifier)
    print("===MSWB TESTS END===")


main()
'''

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_BLOCK_RE = re.compile(
    r"^===MSWB TESTS BEGIN===$(.*?)^===MSWB TESTS END===$", re.MULTILINE | re.DOTALL
)
_LINE_RE = re.compile(r"^(PASSED|FAILED|SKIPPED)\t(\S.*?)\s*$")


def _java_home(ref: str) -> str:
    match = re.match(r"release-(\d+)\.", ref or "")
    major = int(match.group(1)) if match else 99
    if major <= 7:
        return "/usr/lib/jvm/java-17-openjdk"
    return "/usr/lib/jvm/java-21-openjdk"


class Bazel32039To30948ImageBase(Image):
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
        return _BASE_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return [
            File(".", "bazelrc", _BAZELRC),
            File(".", "base_warm.sh", _BASE_WARM_SH),
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{_ORG}/{_REPO}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{_ORG}/{_REPO}" \\
      org.opencontainers.image.description="{_ORG}/{_REPO} Docker image" \\
      org.opencontainers.image.source="https://github.com/{_ORG}/{_REPO}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

ENV LC_ALL=C.UTF-8 \\
    CI=true \\
    JAVA_HOME=/usr/lib/jvm/java-21-openjdk \\
    BAZELISK_HOME=/opt/bazelisk

RUN printf 'Acquire::Retries "5";\\n' > /etc/apt/apt.conf.d/80-retries

RUN apt-get update && apt-get install -y --no-install-recommends \\
    build-essential \\
    ca-certificates \\
    curl \\
    file \\
    git \\
    openjdk-17-jdk-headless \\
    openjdk-21-jdk-headless \\
    patch \\
    python3 \\
    python3-dev \\
    unzip \\
    xz-utils \\
    zip \\
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \\
    arch="${{TARGETARCH:-$(dpkg --print-architecture)}}"; \\
    case "$arch" in \\
        amd64) sum={_BAZELISK_SHA256_AMD64} ;; \\
        arm64) sum={_BAZELISK_SHA256_ARM64} ;; \\
        *) exit 1 ;; \\
    esac; \\
    ln -sfn "/usr/lib/jvm/java-21-openjdk-$arch" /usr/lib/jvm/java-21-openjdk; \\
    ln -sfn "/usr/lib/jvm/java-17-openjdk-$arch" /usr/lib/jvm/java-17-openjdk; \\
    "/usr/lib/jvm/java-21-openjdk/bin/java" -version; \\
    curl -fsSL --retry 5 -o /usr/local/bin/bazelisk \\
        "https://github.com/bazelbuild/bazelisk/releases/download/{_BAZELISK_VERSION}/bazelisk-linux-$arch"; \\
    printf '%s  /usr/local/bin/bazelisk\\n' "$sum" | sha256sum -c -; \\
    chmod 0755 /usr/local/bin/bazelisk; \\
    ln -sfn /usr/local/bin/bazelisk /usr/local/bin/bazel

RUN useradd -m -d /home/mswb -s /bin/bash mswb && \\
    mkdir -p /opt/bazelisk /home/mswb/.cache && \\
    chown -R mswb:mswb /opt/bazelisk /home/mswb

COPY bazelrc /home/mswb/.bazelrc
COPY base_warm.sh /home/base_warm.sh

RUN chown mswb:mswb /home/mswb/.bazelrc && chmod 0644 /home/mswb/.bazelrc

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" {_REPO_DIR} && \\
    cd {_REPO_DIR} && git rev-parse HEAD > /dev/null

RUN chown -R mswb:mswb {_REPO_DIR} && \\
    runuser -u mswb -- env HOME=/home/mswb USER=mswb git config --global --add safe.directory '*' && \\
    runuser -u mswb -- env HOME=/home/mswb USER=mswb bash /home/base_warm.sh

WORKDIR {_REPO_DIR}

{self.clear_env}

CMD ["/bin/bash"]
"""


class Bazel32039To30948ImageDefault(Image):
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
        return Bazel32039To30948ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "mswb_bazel.py", _MSWB_BAZEL_PY),
            File(".", "warm.sh", _WARM_SH),
            File(".", "stage.sh", _STAGE_SH),
            File(".", "prepare.sh", _PREPARE_SH),
            File(".", "run.sh", _RUN_SH),
            File(".", "test-run.sh", _TEST_RUN_SH),
            File(".", "fix-run.sh", _FIX_RUN_SH),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("Bazel32039To30948ImageDefault dependency must be an Image")

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        return f"""FROM {image.image_name()}:{image.image_tag()}

{self.global_env}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}
ENV JAVA_HOME={_java_home(self.pr.base.ref)}

{copy_commands}
WORKDIR {_REPO_DIR}

RUN bash /home/prepare.sh

{hardening}

RUN chown -R mswb:mswb {_REPO_DIR}

{self.clear_env}
"""


@Instance.register(_ORG, _INTERVAL_NAME)
class BAZEL_32039_TO_30948(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Bazel32039To30948ImageDefault(self.pr, self._config)

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

        blocks = _BLOCK_RE.findall(_ANSI_RE.sub("", test_log))
        for block in blocks:
            for raw in block.splitlines():
                match = _LINE_RE.match(raw.rstrip("\r"))
                if not match:
                    continue
                status, name = match.group(1), match.group(2)
                if status == "FAILED":
                    failed_tests.add(name)
                elif status == "PASSED":
                    passed_tests.add(name)
                else:
                    skipped_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests | passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


def bazel_32039_to_30948_number_interval(number) -> str:
    try:
        value = int(number)
    except (TypeError, ValueError):
        return ""
    if _INTERVAL_LO <= value <= _INTERVAL_HI:
        return _INTERVAL_NAME
    return ""


def _fill_number_interval(pr) -> None:
    if getattr(pr, "org", "") != _ORG or getattr(pr, "repo", "") != _REPO:
        return
    if getattr(pr, "number_interval", ""):
        return
    interval = bazel_32039_to_30948_number_interval(getattr(pr, "number", None))
    if interval:
        pr.number_interval = interval


if not PullRequest.__dict__.get("_bazel_32039_to_30948_ni_shim", False):
    _bazel_orig_from_json = PullRequest.from_json.__func__
    _bazel_orig_from_dict = PullRequest.from_dict.__func__

    def _bazel_from_json(cls, *args, **kwargs):
        pr = _bazel_orig_from_json(cls, *args, **kwargs)
        try:
            _fill_number_interval(pr)
        except Exception:
            pass
        return pr

    def _bazel_from_dict(cls, *args, **kwargs):
        pr = _bazel_orig_from_dict(cls, *args, **kwargs)
        try:
            _fill_number_interval(pr)
        except Exception:
            pass
        return pr

    PullRequest.from_json = classmethod(_bazel_from_json)
    PullRequest.from_dict = classmethod(_bazel_from_dict)
    PullRequest._bazel_32039_to_30948_ni_shim = True


if not PullRequest.__dict__.get("_bazel_32039_to_30948_pi_shim", False):
    _bazel_orig_post_init = PullRequest.__post_init__

    def _bazel_post_init(self, *args, **kwargs):
        _bazel_orig_post_init(self, *args, **kwargs)
        try:
            _fill_number_interval(self)
        except Exception:
            pass

    PullRequest.__post_init__ = _bazel_post_init
    PullRequest._bazel_32039_to_30948_pi_shim = True
