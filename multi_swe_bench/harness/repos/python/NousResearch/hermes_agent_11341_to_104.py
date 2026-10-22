import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class HermesAgent_11341_to_104_ImageBase(Image):

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
        return "python:3.11"

    def image_tag(self) -> str:
        return "base-11341-to-104"

    def workdir(self) -> str:
        return "base-11341-to-104"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        text = f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
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
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PIP_NO_INPUT=1 \\
    PYTHONDONTWRITEBYTECODE=1

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    ca-certificates \\
    curl \\
    build-essential \\
    ffmpeg \\
    libportaudio2 \\
 && apt-get clean \\
 && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel

{self.global_env}

WORKDIR /home/

{code}

WORKDIR /home/{self.pr.repo}

{self.clear_env}

CMD ["/bin/bash"]
"""
        return "\n".join(l for l in text.split("\n") if l.strip()) + "\n"


class HermesAgent_11341_to_104_ImageDefault(Image):

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return HermesAgent_11341_to_104_ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha

        test_cmd = """DES=()
if [ -s /home/baseline_deselect.txt ]; then
    while IFS= read -r nid; do
        [ -n "$nid" ] && DES+=(--deselect "$nid")
    done < /home/baseline_deselect.txt
fi
export PYTHONPATH="/home/pyplug${PYTHONPATH:+:$PYTHONPATH}"
export PYTEST_PLUGINS=hermes_env_guard
python -m pytest tests -n 0 --timeout=600 "${DES[@]}" -v --no-header -rA --tb=no --continue-on-collection-errors 2>&1
"""

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
set -e

git rev-parse --is-inside-work-tree > /dev/null 2>&1
test -z "$(git status --porcelain)"
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e
export CI=true

cd /home/[[REPO]]
git reset --hard
git clean -fdq
bash /home/check_git_changes.sh
git checkout -f --detach [[SHA]]
test "$(git rev-parse HEAD)" = "$(git rev-parse [[SHA]])"
bash /home/check_git_changes.sh

python -m pip install --no-cache-dir -e ".[all]" \\
    || python -m pip install --no-cache-dir -e ".[dev]"
python -m pip install --no-cache-dir "pytest==9.1.1" "pytest-asyncio==1.4.0" "pytest-xdist==3.8.0" "pytest-timeout==2.4.0" "execnet==2.1.2"

mkdir -p /home/pyplug
cp /home/hermes_env_guard.py /home/pyplug/hermes_env_guard.py
export PYTHONPATH="/home/pyplug${PYTHONPATH:+:$PYTHONPATH}"
export PYTEST_PLUGINS=hermes_env_guard

python -m pytest tests --collect-only -q -n 0 > /home/collect.txt 2>&1 || true

python -m pytest tests -n 0 --timeout=600 -v --no-header -rA --tb=no --continue-on-collection-errors > /home/baseline.txt 2>&1 || true

grep -E '^\\+\\+\\+ b/' /home/test.patch | sed -E 's#^\\+\\+\\+ b/##' | sort -u > /home/target_files.txt || true
grep -E '^(FAILED|ERROR) [^ ]+::' /home/baseline.txt | awk '{print $2}' | sort -u > /home/baseline_failed.txt || true
: > /home/baseline_deselect.txt
while IFS= read -r nid; do
    f="${nid%%::*}"
    grep -qxF "$f" /home/target_files.txt || printf '%s\\n' "$nid" >> /home/baseline_deselect.txt
    done < /home/baseline_failed.txt
for nid in tests/gateway/test_approve_deny_commands.py::TestBlockingApprovalE2E::test_parallel_mixed_approve_deny; do
    f="${nid%%::*}"
    grep -qxF "$f" /home/target_files.txt || printf '%s\\n' "$nid" >> /home/baseline_deselect.txt
done

mkdir -p /home/blobs
for b in $(cat /home/test.patch /home/fix.patch | grep -E '^index [0-9a-f]+\\.\\.' | sed -E 's/^index ([0-9a-f]+)\\.\\..*/\\1/' | sort -u); do
    git cat-file -e "$b^{blob}" 2>/dev/null && git cat-file blob "$b" > "/home/blobs/$b"
done

git reset --hard
git clean -fdq

python -c "import run_agent, model_tools, hermes_cli, pytest, pytest_asyncio, xdist, pytest_timeout; print('DEPS_OK')"
""".replace("[[REPO]]", repo).replace("[[SHA]]", sha),
            ),
            File(
                ".",
                "apply_patch.sh",
                """apply_patch() {
    local p="$1"
    git apply --whitespace=nowarn "$p" 2>/dev/null && return 0
    local ex=()
    while IFS= read -r f; do
        [ -n "$f" ] && ex+=(--exclude="$f")
    done < <(awk '/^diff --git /{f=$4; sub("^b/","",f)} /^(GIT binary patch|Binary files )/{print f}' "$p" | sort -u)
    git apply --whitespace=nowarn "${ex[@]}" "$p" 2>/dev/null && return 0
    local snap
    for b in /home/blobs/*; do
        [ -f "$b" ] && git hash-object -w "$b" > /dev/null
    done
    snap=$(git stash create)
    local fx=() rj=() o l f n
    for i in 1 2 3 4 5 6 7 8; do
        o=$(git apply --whitespace=nowarn --3way "${ex[@]}" "${fx[@]}" "$p" 2>&1 > /dev/null)
        [ -n "$(git diff --name-only --diff-filter=U)$(git diff --cached --name-only)" ] && break
        n=0
        while IFS= read -r l; do
            f=""
            case "$l" in
                "error: patch failed: "*) f="${l#error: patch failed: }"; f="${f%:*}" ;;
                "error: "*": patch does not apply") f="${l#error: }"; f="${f%: patch does not apply}" ;;
                "error: "*": already exists in working directory") f="${l#error: }"; f="${f%: already exists in working directory}" ;;
                "error: "*": does not exist in index") f="${l#error: }"; f="${f%: does not exist in index}" ;;
            esac
            if [ -n "$f" ]; then
                fx+=(--exclude="$f")
                rj+=("$f")
                n=1
            fi
        done <<< "$o"
        [ "$n" = 1 ] || break
    done
    local u
    u=$(git diff --name-only --diff-filter=U)
    if [ -n "$u" ] || [ -n "$(git diff --cached --name-only)" ]; then
        while IFS= read -r f; do
            [ -n "$f" ] || continue
            git show ":2:$f" > /tmp/m_ours 2>/dev/null || : > /tmp/m_ours
            git show ":1:$f" > /tmp/m_base 2>/dev/null || : > /tmp/m_base
            git show ":3:$f" > /tmp/m_theirs 2>/dev/null || : > /tmp/m_theirs
            python /home/merge_pick.py /tmp/m_ours /tmp/m_base /tmp/m_theirs "$f"
        done <<< "$u"
        rm -f /tmp/m_ours /tmp/m_base /tmp/m_theirs
        git reset -q
        if [ "${#rj[@]}" -gt 0 ]; then
            local inc=()
            for f in "${rj[@]}"; do
                inc+=(--include="$f")
            done
            git apply --whitespace=nowarn --reject "${inc[@]}" "$p" > /dev/null 2>&1
            find . -name '*.rej' -not -path './.git/*' -delete
        fi
        return 0
    fi
    git reset -q --hard
    [ -n "$snap" ] && git stash apply -q "$snap"
    git apply --whitespace=nowarn --reject "${ex[@]}" "$p" > /dev/null 2>&1
    find . -name '*.rej' -not -path './.git/*' -delete
    return 0
}
""",
            ),
            File(
                ".",
                "merge_pick.py",
                """import ast
import textwrap
import builtins
import difflib
import subprocess
import sys


def names(src):
    try:
        tree = ast.parse(src)
        compile(tree, "m", "exec")
    except (SyntaxError, ValueError):
        return None
    out = set()

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = prefix + child.name
                out.add(q)
                walk(child, q + ".")
            elif isinstance(child, (ast.Assign, ast.AnnAssign)):
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                for t in targets:
                    if isinstance(t, ast.Name) and not prefix:
                        out.add(t.id)
                    elif isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self":
                        out.add(prefix.split(".")[0] + ".self." + t.attr)
            elif isinstance(child, (ast.Import, ast.ImportFrom)) and not prefix:
                for a in child.names:
                    out.add("import:" + (a.asname or a.name))
            elif not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                walk(child, prefix)

    walk(tree, "")
    return out


def merged(mode, ours, base, theirs):
    r = subprocess.run(
        ["git", "merge-file", "-p", "-q", mode, ours, base, theirs],
        capture_output=True,
    )
    return r.stdout.decode("utf-8", "replace")


def split(ours, base, theirs, extra):
    r = subprocess.run(["git", "merge-file", "-p"] + extra + [ours, base, theirs], capture_output=True)
    text = r.stdout.decode("utf-8", "replace")
    parts, cur, state, a, b, m = [], [], 0, [], [], []
    for line in text.splitlines(keepends=True):
        if state == 0 and line.startswith("<<<<<<< "):
            parts.append("".join(cur))
            cur, a, b, m, state = [], [], [], [], 1
        elif state == 1 and line.startswith("||||||| "):
            state = 3
        elif state == 3 and line.startswith("======="):
            state = 2
        elif state == 3:
            m.append(line)
        elif state == 1 and line.startswith("======="):
            state = 2
        elif state == 2 and line.startswith(">>>>>>> "):
            parts.append(("".join(a), "".join(b), "".join(m)))
            state = 0
        elif state == 1:
            a.append(line)
        elif state == 2:
            b.append(line)
        else:
            cur.append(line)
    parts.append("".join(cur))
    return parts


def hunks(ours, base, theirs):
    parts = split(ours, base, theirs, [])
    mids = [p[2] for p in split(ours, base, theirs, ["--diff3"]) if isinstance(p, tuple)]
    k = sum(1 for p in parts if isinstance(p, tuple))
    out, i = [], 0
    for p in parts:
        if isinstance(p, tuple):
            m = mids[i].splitlines(keepends=True) if len(mids) == k else []
            o, t = p[0], p[1]
            out.append((o, t, novel(m, o.splitlines(keepends=True)), novel(m, t.splitlines(keepends=True))))
            i += 1
        else:
            out.append(p)
    return out


def novel(base, side):
    sm = difflib.SequenceMatcher(None, base, side, autojunk=False)
    keep = [True] * len(side)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for j in range(j1, j2):
                keep[j] = False
    return "".join(x for x, k in zip(side, keep) if k)


def render(parts, choice):
    out, i = [], 0
    for p in parts:
        if isinstance(p, tuple):
            o, t, on, tn = p
            c = choice[i]
            out.append([t, o, o + t, t + o, o + tn, tn + o, t + on, on + t][c])
            i += 1
        else:
            out.append(p)
    return "".join(out)


def norm(x):
    return "".join(x.split()).replace("'", '"').rstrip(",")


def lines(src):
    return set(norm(x) for x in src.splitlines() if len(norm(x)) > 3)


def counts(src):
    out = {}
    for x in src.splitlines():
        x = norm(x)
        if len(x) > 3:
            out[x] = out.get(x, 0) + 1
    return out


LIMIT = {}


def shadow(src):
    tree = ast.parse(src)
    top = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                top.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            top.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    top.add(t.id)
    bad = 0
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        loads, stores, todo = {}, {}, list(fn.body)
        while todo:
            node = todo.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(node, (ast.Global, ast.Nonlocal)):
                for x in node.names:
                    stores[x] = -1
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    k = (a.asname or a.name).split(".")[0]
                    stores[k] = min(stores.get(k, node.lineno), node.lineno)
            if isinstance(node, ast.Name):
                d = loads if isinstance(node.ctx, ast.Load) else stores
                d[node.id] = min(d.get(node.id, node.lineno), node.lineno)
            todo.extend(ast.iter_child_nodes(node))
        args = fn.args
        params = set(a.arg for a in args.args + args.kwonlyargs + args.posonlyargs)
        params |= set(a.arg for a in (args.vararg, args.kwarg) if a)
        for k, line in loads.items():
            if k in top and k not in params and 0 <= stores.get(k, 10 ** 9) < 10 ** 9 and line < stores[k]:
                bad += 1
    return bad


def rate(src, want, delta):
    n = names(src)
    if n is None:
        return None
    c = counts(src)
    dup = sum(v - LIMIT.get(k, 1) for k, v in c.items() if v > LIMIT.get(k, 1))
    return (-shadow(src), len(want[0] & n), len(want[1] & n), len(delta & set(c)), -dup)


def whole(chunk):
    try:
        tree = ast.parse(textwrap.dedent(chunk))
    except SyntaxError:
        return False
    return all(not isinstance(getattr(n, "value", None), (ast.Tuple, ast.Name)) for n in tree.body)


def kept(p, c, out, prev):
    if not whole(p[2] if c in (6, 7) else p[3]):
        return False
    have, before, mine = counts(out), counts(prev), counts(p[0] + p[1])
    if not any(have.get(x, 0) < v and v > mine.get(x, 0) for x, v in before.items()):
        return False
    for x in set(have) | set(before):
        h, b = have.get(x, 0), before.get(x, 0)
        if h > b and x not in mine:
            return False
        if h < b and (h < LIMIT.get(x, 1) or b <= LIMIT.get(x, 1)):
            return False
    return True


def greedy(parts, want, delta):
    k = sum(1 for p in parts if isinstance(p, tuple))
    idx = [j for j, p in enumerate(parts) if isinstance(p, tuple)]
    if k == 0 or k > 60:
        return None
    choice = [0] * k
    score = rate(render(parts, choice), want, delta) or (-10 ** 9, -1, -1, -1, 0)
    for _ in range(2):
        changed = False
        for i in range(k):
            for c in (1, 2, 3):
                trial = list(choice)
                trial[i] = c
                s = rate(render(parts, trial), want, delta)
                if s is None:
                    continue
                if s > score:
                    choice, score, changed = trial, s, True
        if not changed:
            break
    for i in range(k):
        for c in range(4, 8):
            trial = list(choice)
            trial[i] = c
            s = rate(render(parts, trial), want, delta)
            if s is None:
                continue
            if all(a >= b for a, b in zip(s, score)) and s[4] > score[4] and kept(parts[idx[i]], c, render(parts, trial), render(parts, choice)):
                choice, score = trial, s
    if score[1] < 0:
        return None
    return render(parts, choice), score


def add_imports(src, others):
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src
    bound, loaded = set(dir(builtins)), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            (loaded if isinstance(node.ctx, ast.Load) else bound).add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
    need = loaded - bound
    if not need:
        return src
    add = []
    for other in others:
        try:
            ot = ast.parse(other)
        except SyntaxError:
            continue
        for node in ot.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)) and node.col_offset == 0:
                keys = set((a.asname or a.name).split(".")[0] for a in node.names)
                if keys & need and len(node.names) == 1:
                    seg = ast.get_source_segment(other, node)
                    if seg and seg not in add:
                        add.append(seg)
                        need -= keys
    if not add:
        return src
    last = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last = node.end_lineno
    nl = chr(10)
    rows = src.split(nl)
    out = nl.join(rows[:last] + add + rows[last:])
    if names(out) is None:
        return src
    return out


def main():
    ours, base, theirs, dest = sys.argv[1:5]
    read = lambda p: open(p, encoding="utf-8", errors="replace").read()
    so, sb, st = read(ours), read(base), read(theirs)
    cands = [
        merged("--theirs", ours, base, theirs),
        merged("--union", ours, base, theirs),
        merged("--ours", ours, base, theirs),
    ]
    if st:
        cands.append(st)
    if not dest.endswith(".py"):
        open(dest, "w", encoding="utf-8").write(cands[0])
        return
    no, nb, nt = names(so) or set(), names(sb) or set(), names(st) or set()
    want = (no | (nt - nb), no | nt)
    co, ct = counts(so), counts(st)
    for k in set(co) | set(ct):
        LIMIT[k] = max(co.get(k, 0), ct.get(k, 0), 1)
    lb = lines(sb)
    delta = (lines(so) - lb) | (lines(st) - lb)
    best, best_score = None, (-10 ** 9, -1, -1, -1, 0)
    g = greedy(hunks(ours, base, theirs), want, delta)
    if g is not None:
        best, best_score = g
    for c in cands:
        s = rate(c, want, delta)
        if s is None:
            continue
        if s > best_score:
            best, best_score = c, s
    best = add_imports(best, [so, st]) if best is not None else cands[0]
    open(dest, "w", encoding="utf-8").write(best)


main()
""",
            ),
            File(
                ".",
                "hermes_env_guard.py",
                """import os
import shutil

import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    snap = dict(os.environ)
    cwd = os.getcwd()
    gone = not os.path.exists("/nonexistent")
    yield
    if gone and os.path.exists("/nonexistent"):
        shutil.rmtree("/nonexistent", ignore_errors=True)
    if dict(os.environ) != snap:
        os.environ.clear()
        os.environ.update(snap)
    try:
        if os.getcwd() != cwd:
            os.chdir(cwd)
    except OSError:
        os.chdir(cwd)
""",
            ),
            File(
                ".",
                "repair_markers.py",
                """import os
import sys

added = {}
cur = None
with open(sys.argv[1], encoding="utf-8", errors="replace") as fh:
    for raw in fh:
        line = raw.rstrip("\\n")
        if line.startswith("+++ "):
            cur = line[6:] if line.startswith("+++ b/") else None
            continue
        if cur and cur.endswith(".py") and line.startswith("+"):
            added.setdefault(cur, set()).add(line[1:].strip())


def is_marker(s):
    if not s:
        return False
    if len(s) >= 7 and s[:7] in ("=======", "<<<<<<<", ">>>>>>>"):
        return True
    if " " in s or not all(c.isalnum() or c in "-_./" for c in s):
        return False
    return s in ("main", "master") or "-" in s or "/" in s


for path, lines in added.items():
    if not os.path.isfile(path):
        continue
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    try:
        compile(src, path, "exec")
        continue
    except SyntaxError:
        pass
    src_lines = src.splitlines(True)
    marks = [i for i, l in enumerate(src_lines) if l.strip() in lines and is_marker(l.strip())]
    regions = []
    for a, b in zip(marks[0::2], marks[1::2]):
        body = list(range(a + 1, b))
        seps = [i for i in body if not src_lines[i].strip() or src_lines[i].strip().startswith("=======")]
        if seps:
            regions.append((set(range(a + 1, seps[0] + 1)), set(range(seps[0], b))))
        else:
            regions.append((set(), set()))
    choices = [[]]
    for r in regions:
        choices = [c + [k] for c in choices for k in (0, 1, 2)]
    fixed = None
    for c in choices:
        drop = set(marks)
        for r, k in zip(regions, c):
            if k == 1:
                drop |= r[1]
            elif k == 2:
                drop |= r[0]
        new = "".join(l for i, l in enumerate(src_lines) if i not in drop)
        try:
            compile(new, path, "exec")
        except SyntaxError:
            continue
        fixed = new
        break
    if fixed is None:
        continue
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(fixed)


def merge_tables(path):
    import tomllib

    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    try:
        tomllib.loads(text)
        return
    except tomllib.TOMLDecodeError:
        pass
    blocks = [["", []]]
    for l in text.splitlines(True):
        s = l.strip()
        if s.startswith("[") and not s.startswith("[[") and s.endswith("]"):
            blocks.append([s, [l]])
        else:
            blocks[-1][1].append(l)
    seen = {}
    out = []
    for name, body in blocks:
        if name and name in seen:
            tgt = out[seen[name]][1]
            while tgt and not tgt[-1].strip():
                tgt.pop()
            tgt.extend(body[1:])
            if not tgt[-1].strip() == "":
                tgt.append("\\n")
            continue
        if name:
            seen[name] = len(out)
        out.append([name, list(body)])
    new = "".join("".join(b) for _, b in out)
    try:
        tomllib.loads(new)
    except tomllib.TOMLDecodeError:
        return
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new)


merge_tables("pyproject.toml")
""",
            ),
            File(
                ".",
                "run.sh",
                ("""#!/bin/bash
set -uo pipefail
export CI=true

cd /home/[[REPO]]
""" + test_cmd).replace("[[REPO]]", repo),
            ),
            File(
                ".",
                "test-run.sh",
                ("""#!/bin/bash
set -uo pipefail
export CI=true

cd /home/[[REPO]]
source /home/apply_patch.sh
apply_patch /home/test.patch
""" + test_cmd).replace("[[REPO]]", repo),
            ),
            File(
                ".",
                "fix-run.sh",
                ("""#!/bin/bash
set -uo pipefail
export CI=true

cd /home/[[REPO]]
source /home/apply_patch.sh
apply_patch /home/test.patch
apply_patch /home/fix.patch
python /home/repair_markers.py /home/fix.patch
""" + test_cmd).replace("[[REPO]]", repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        text = f"""FROM {name}:{tag}
ARG BASE_COMMIT="{sha}"
{self.global_env}
WORKDIR /home/{self.pr.repo}
RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}
{copy_commands}
RUN bash /home/prepare.sh
{Image._HARDENING_BLOCK}
{self.clear_env}
"""
        return "\n".join(l for l in text.split("\n") if l.strip()) + "\n"


@Instance.register("NousResearch", "hermes_agent_11341_to_104")
class HermesAgent_11341_to_104(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return HermesAgent_11341_to_104_ImageDefault(self.pr, self._config)

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

        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        inline_re = re.compile(
            r"^(?:\[gw\d+\]\s+\[\s*\d+%\]\s+)?(?P<id>\S+\.py::\S+?)\s+"
            r"(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b",
            re.MULTILINE,
        )
        xdist_re = re.compile(
            r"^\[gw\d+\]\s+(?:\[\s*\d+%\]\s+)?"
            r"(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+"
            r"(?P<id>\S+\.py::\S+)",
            re.MULTILINE,
        )
        summary_re = re.compile(
            r"^(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+"
            r"(?:\[\d+\]\s+)?(?P<id>\S+\.py::\S+?)(?:\s+-\s.*)?\s*$",
            re.MULTILINE,
        )
        matches = (
            list(inline_re.finditer(log))
            + list(xdist_re.finditer(log))
            + list(summary_re.finditer(log))
        )
        for m in matches:
            status, tid = m.group("status"), m.group("id")
            if status in ("PASSED", "XPASS"):
                passed_tests.add(tid)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(tid)
            else:
                skipped_tests.add(tid)

        for m in re.finditer(r"^ERROR\s+(\S+\.py)\s*(?:-.*)?$", log, re.MULTILINE):
            failed_tests.add(m.group(1))

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
