# Copyright (c) 2024 Bytedance Ltd. and/or its affiliates

#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at

#      http://www.apache.org/licenses/LICENSE-2.0

#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

import functools
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Tuple, Union

from dataclasses_json import config, dataclass_json

from multi_swe_bench.harness.constant import (
    FIX_PATCH_RUN_LOG_FILE,
    REPORT_FILE,
    RUN_LOG_FILE,
    TEST_PATCH_RUN_LOG_FILE,
)
from multi_swe_bench.harness.image import Config
from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.pull_request import Base, PullRequest, PullRequestBase
from multi_swe_bench.harness.test_result import Test, TestResult, TestStatus


REPORT_SCHEMA_VERSION = 3


@dataclass_json
@dataclass
class Report(PullRequestBase):
    valid: Optional[bool] = None
    error_msg: Optional[str] = None
    fixed_tests: dict[str, Test] = field(default_factory=dict)
    p2p_tests: dict[str, Test] = field(default_factory=dict)
    f2p_tests: dict[str, Test] = field(default_factory=dict)
    s2p_tests: dict[str, Test] = field(default_factory=dict)
    n2p_tests: dict[str, Test] = field(default_factory=dict)
    run_result: TestResult = None
    test_patch_result: TestResult = None
    fix_patch_result: TestResult = None

    # Raw patch bytes; used at construction to derive *_files, then dropped from JSON.
    test_patch: str = field(
        default="", metadata=config(exclude=lambda _: True)
    )
    fix_patch: str = field(
        default="", metadata=config(exclude=lambda _: True)
    )

    test_patch_files: list[str] = field(default_factory=list)
    fix_patch_files: list[str] = field(default_factory=list)

    reclassified_from_target: dict[str, Test] = field(default_factory=dict)
    fix_patch_authored_candidates: dict[str, Test] = field(default_factory=dict)
    guard_fix_patch_touched_tests: dict[str, Test] = field(default_factory=dict)

    schema_version: int = 1

    _tests: dict[str, Test] = field(
        default_factory=dict, metadata=config(exclude=lambda _: True)
    )
    _test_patch_matcher_ok: bool = field(
        default=True, metadata=config(exclude=lambda _: True)
    )
    _fix_patch_matcher_ok: bool = field(
        default=True, metadata=config(exclude=lambda _: True)
    )
    _test_patch_added: list[str] = field(default_factory=list)
    # Fix-patch added lines kept ONLY for files that are test files (gold test
    # files or heuristic), so a production method sharing a name with a test
    # method cannot trip the cheating guard. Keyed by file path.
    _fix_patch_test_added: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self):
        if not self.run_result:
            raise ValueError("Invalid run_result: None")
        if not self.test_patch_result:
            raise ValueError("Invalid test_patch_result: None")
        if not self.fix_patch_result:
            raise ValueError("Invalid fix_patch_result: None")

        all_tests = (
            self.run_result._tests.keys()
            | self.test_patch_result._tests.keys()
            | self.fix_patch_result._tests.keys()
        )

        for test_name in all_tests:
            run = self.run_result._tests.get(test_name, TestStatus.NONE)
            test = self.test_patch_result._tests.get(test_name, TestStatus.NONE)
            fix = self.fix_patch_result._tests.get(test_name, TestStatus.NONE)
            self._tests[test_name] = Test(run, test, fix)

        # Trust persisted lists on JSON regeneration; ReportTask stubs empty patches.
        if self.test_patch and not self.test_patch_files:
            self.test_patch_files = sorted(_extract_touched_files(self.test_patch))
        if self.fix_patch and not self.fix_patch_files:
            self.fix_patch_files = sorted(_extract_touched_files(self.fix_patch))

        if self.test_patch and not self._test_patch_added:
            self._test_patch_added = _extract_added_lines(self.test_patch)
        if self.fix_patch and not self._fix_patch_test_added:
            by_file = _extract_added_lines_by_file(self.fix_patch)
            self._fix_patch_test_added = {
                f: lines
                for f, lines in by_file.items()
                if f in self.test_patch_files or _looks_like_test_file(f)
            }
        self._test_patch_matcher_ok = _file_matcher_can_hit(
            self.test_patch_files, self._tests.keys()
        )
        self._fix_patch_matcher_ok = _file_matcher_can_hit(
            self.fix_patch_files, self._tests.keys()
        )

        # Loaded artifact predates the current classifier schema. Wipe the
        # cached classification so check() re-runs under current rules.
        if self.schema_version < REPORT_SCHEMA_VERSION:
            self.valid = None
            self.error_msg = None
            self.fixed_tests = {}
            self.p2p_tests = {}
            self.f2p_tests = {}
            self.s2p_tests = {}
            self.n2p_tests = {}
            self.reclassified_from_target = {}
            self.fix_patch_authored_candidates = {}
            self.guard_fix_patch_touched_tests = {}

        self.valid, self.error_msg = self.check()
        if self.valid is True:
            self.schema_version = REPORT_SCHEMA_VERSION

    @classmethod
    def from_dict(cls, d: dict) -> "Report":
        # ``cls(**d)`` already runs __post_init__ once (dataclass); no re-call.
        return cls(**d)

    @classmethod
    def from_json(cls, json_str: str) -> "Report":
        return cls.from_dict(cls.schema().loads(json_str))

    def dict(self) -> dict:
        return asdict(self)

    def json(self) -> str:
        return self.to_json(ensure_ascii=False)

    def _touched_by_test_patch(self, test_name: str) -> bool:
        if not self.test_patch and not self.test_patch_files:
            return True
        # Precise: test's identifier appears as a definition or string in test_patch's + lines.
        if _authored_via_diff(test_name, self._test_patch_added):
            return True
        # File-based fallback (works across regen when patch bytes are lost).
        if self.test_patch_files and self._test_patch_matcher_ok:
            return _test_name_matches_files(test_name, self.test_patch_files)
        return True

    def _touched_by_fix_patch(self, test_name: str) -> bool:
        if not self.fix_patch and not self.fix_patch_files:
            return False
        # Content authorship counts ONLY when the match is inside the test's OWN
        # file. A method that merely shares a name with the credited test — a
        # production method (Java's PostCommentSubject.get() vs
        # PostCommentSubjectTest > get()) OR a same-named method in a DIFFERENT
        # test file (a shared base class / helper) — must not trip the guard.
        for path, lines in self._fix_patch_test_added.items():
            if _file_hosts_test(path, test_name) and _authored_via_diff(test_name, lines):
                return True
        # File-path branch (works for pytest/Go IDs that embed a path).
        if self.fix_patch_files and self._fix_patch_matcher_ok:
            return _test_name_matches_files(test_name, self.fix_patch_files)
        return False

    def check(self, force: bool = False) -> Tuple[bool, str]:
        if not force and self.valid is not None:
            return (self.valid, self.error_msg)

        # Recomputing (fresh construction, force=True, or a schema-version wipe):
        # clear every classification bucket first so a re-run cannot leave a test
        # double-classified (e.g. lingering in f2p after it moves to p2p) or an
        # invalid verdict sitting on top of previously-populated buckets.
        self.fixed_tests = {}
        self.p2p_tests = {}
        self.f2p_tests = {}
        self.s2p_tests = {}
        self.n2p_tests = {}
        self.reclassified_from_target = {}
        self.fix_patch_authored_candidates = {}
        self.guard_fix_patch_touched_tests = {}

        # 1. Exist valid fix patch result
        if self.fix_patch_result.all_count == 0:
            self.valid = False
            self.error_msg = f"After applying the fix patch, no test results were captured when executing the test command. A brief summary is as follows: {self.short_report()}"
            return (self.valid, self.error_msg)

        # 2. No new failures
        for name, test in self._tests.items():
            if test.test == TestStatus.PASS and test.fix == TestStatus.FAIL:
                self.valid = False
                self.error_msg = f"Before applying the fix patch, the test passed; however, after applying the fix patch, the test failed. A brief summary is as follows: {self.short_report()}. `{name}`: {test}"
                return (self.valid, self.error_msg)

        # 3. Fix something
        fix_something = False
        for name, test in self._tests.items():
            if test.test != TestStatus.PASS and test.fix == TestStatus.PASS:
                fix_something = True
                self.fixed_tests[name] = test

        if not fix_something:
            self.valid = False
            self.error_msg = f"After applying the fix patch, no test cases transitioned from failed to passed. A brief summary is as follows: {self.short_report()}"
            return (self.valid, self.error_msg)

        # 4. Anomalous Pattern
        for name, test in self._tests.items():
            if (
                (test.test == TestStatus.NONE or test.test == TestStatus.SKIP)
                and test.fix == TestStatus.FAIL
                and test.run == TestStatus.PASS
            ):
                self.valid = False
                self.error_msg = f"By comparing the test results before and after applying the fix patch, an anomalous pattern was detected. A brief summary is as follows: {self.short_report()}. `{name}`: {test}"
                return (self.valid, self.error_msg)

        # 5. Cheating guard: a fix patch that touches a credited test's file
        #    is a self-satisfying loop. Scoped to `fixed_tests` so maintenance
        #    PRs touching test files without crediting any test in them are
        #    not rejected.
        for name in self.fixed_tests:
            if self._touched_by_fix_patch(name):
                self.guard_fix_patch_touched_tests[name] = self._tests[name]
        # Instance-level tamper: the fix patch modifies a gold test file. Language-
        # agnostic — catches Gradle-style IDs that never map to a file path, and
        # doctored assertions that do not re-declare the credited test.
        tampered_files = sorted(set(self.fix_patch_files) & set(self.test_patch_files))
        if self.guard_fix_patch_touched_tests or (tampered_files and self.fixed_tests):
            self.valid = False
            detail = []
            if self.guard_fix_patch_touched_tests:
                detail.append(
                    "Touched credited tests: "
                    + ", ".join(sorted(self.guard_fix_patch_touched_tests))
                )
            if tampered_files:
                detail.append(
                    "Fix also modifies gold test file(s): " + ", ".join(tampered_files)
                )
            self.error_msg = (
                f"Fix patch modified file(s) containing credited test(s); "
                f"cannot credit the fix as unbiased. A brief summary is as "
                f"follows: {self.short_report()}. " + ". ".join(detail)
            )
            return (self.valid, self.error_msg)

        # 6. Baseline-first classifier. run_result was captured before test.patch,
        #    so test.run != NONE is temporal proof a test existed at baseline.
        #    Status alone drives CBC detection and F2P/S2P inference; matchers are
        #    only needed for the phantom-N2P case where status is genuinely ambiguous.
        for name, test in self._tests.items():
            if test.fix != TestStatus.PASS:
                continue

            if test.test == TestStatus.PASS:
                self.p2p_tests[name] = test
                continue

            if test.test == TestStatus.FAIL:
                self.f2p_tests[name] = test
                continue

            if test.test == TestStatus.SKIP:
                self.s2p_tests[name] = test
                continue

            # test.test == NONE from here. test.run disambiguates.
            if test.run == TestStatus.PASS:
                # Classic CBC: was passing at baseline, hidden by test.patch, restored.
                self.p2p_tests[name] = test
                self.reclassified_from_target[name] = test
                continue

            if test.run == TestStatus.FAIL:
                # Hidden F2P: was failing at baseline, hidden by test.patch, fixed.
                self.f2p_tests[name] = test
                continue

            if test.run == TestStatus.SKIP:
                # Hidden S2P.
                self.s2p_tests[name] = test
                continue

            # test.run == NONE, test.test == NONE, test.fix == PASS.
            # The one case where matcher is needed: genuine N2P vs phantom.
            if self._touched_by_test_patch(name):
                self.n2p_tests[name] = test
            else:
                self.fix_patch_authored_candidates[name] = test

        self.valid = True
        self.error_msg = ""
        return (self.valid, self.error_msg)

    def short_report(self) -> str:
        return (
            "Test Result Summary:\n"
            "Stage Descriptions:\n"
            "  run  : Execute the test command without any patches applied.\n"
            "  test : Execute the test command after applying the test patch.\n"
            "  fix  : Execute the test command after applying both the test patch and the fix patch.\n"
            "Each stage is reported as (pass, fail, skip), representing the number of tests that passed, failed, or were skipped, respectively.\n\n"
            f"Results:\n"
            f"  run  = ({self.run_result.passed_count}, {self.run_result.failed_count}, {self.run_result.skipped_count})\n"
            f"  test = ({self.test_patch_result.passed_count}, {self.test_patch_result.failed_count}, {self.test_patch_result.skipped_count})\n"
            f"  fix  = ({self.fix_patch_result.passed_count}, {self.fix_patch_result.failed_count}, {self.fix_patch_result.skipped_count})"
        )


_DIFF_FILE_RE = re.compile(
    r'^diff --git "?a/(.+?)"? "?b/(.+?)"?$',
    re.MULTILINE,
)
# Fallback for patches WITHOUT a ``diff --git`` header (plain ``diff -u`` output,
# ``git diff --no-prefix``, or tool-generated unified diffs). Without it such a
# patch parses to zero touched files and the cheating guard fails OPEN — a fix
# patch could doctor a gold test file undetected. Matches the ``---``/``+++``
# header pair and strips an optional ``a/``/``b/`` prefix and trailing tab-stamp.
_DIFF_HDR_RE = re.compile(
    r'^(?:---|\+\+\+) "?(?:[ab]/)?(.+?)"?(?:\t.*)?$',
    re.MULTILINE,
)


def _extract_touched_files(patch_text: str) -> set[str]:
    if not patch_text:
        return set()
    # Normalise line endings first: under ``re.MULTILINE`` a trailing ``\r`` from
    # a CRLF patch is captured into the path (``"tests/foo.py\r"``), silently
    # defeating every downstream path comparison and the tamper overlap.
    text = patch_text.replace("\r\n", "\n").replace("\r", "\n")
    files = {m.group(2) for m in _DIFF_FILE_RE.finditer(text)}
    for m in _DIFF_HDR_RE.finditer(text):
        path = m.group(1)
        if path and path != "/dev/null":
            files.add(path)
    return files


def _file_hosts_test(path: str, test_name: str) -> bool:
    """Does ``path`` plausibly contain the *definition* of ``test_name``?

    Ties a content match to the credited test's own file, so a same-named method
    added to a DIFFERENT test file (shared base class / helper) cannot flag a
    test whose own file the fix never touched. Handles two ID shapes:

    * path-embedded IDs (pytest ``file.py::x``, JS ``file > desc``) via the
      existing path-boundary matcher;
    * JVM class IDs (``PostCommentSubjectTest > get()``, ``com.pkg.MyClass#m``)
      where the class name equals the file's basename stem
      (``PostCommentSubjectTest`` -> ``PostCommentSubjectTest.java``).
    """
    if _test_name_matches_files(test_name, [path]):
        return True
    cls = re.split(r" > |#", test_name.strip(), maxsplit=1)[0]
    cls = cls.rsplit(".", 1)[-1].strip()  # drop a dotted FQCN package prefix
    if not cls or "/" in cls or "::" in cls or " " in cls:
        return False
    stem = path.replace("\\", "/").split("/")[-1].rsplit(".", 1)[0]
    return cls == stem


def _test_name_matches_files(test_name: str, files: Iterable[str]) -> bool:
    # Path-boundary match; naive substring would false-positive on "test.py"
    # against "my_test.py::x" etc.
    file_head = test_name.split("::", 1)[0]
    for f in files:
        if file_head == f or file_head.endswith("/" + f):
            return True
        # JS/TS: "src/x.test.ts > describe > it" — file appears as head prefix.
        if test_name.startswith(f + " > "):
            return True
    return False


def _file_matcher_can_hit(
    files: Iterable[str], test_names: Iterable[str]
) -> bool:
    files_list = list(files)
    if not files_list:
        return True
    for name in test_names:
        if _test_name_matches_files(name, files_list):
            return True
    return False


_ADDED_LINE_RE = re.compile(r"^\+(?!\+\+)(.*)$", re.MULTILINE)
_PYTEST_PARAM_RE = re.compile(r"\[.*?\]$")
_GO_TEST_RE = re.compile(r"^Test[A-Z_]")


def _extract_added_lines(patch_text: str) -> list[str]:
    if not patch_text:
        return []
    # Normalise line endings for parity with _extract_added_lines_by_file and
    # _extract_touched_files: under re.MULTILINE a trailing \r from a CRLF patch
    # is captured into the added line (``"def test_x(): ...\r"``), leaving dirty
    # strings in the persisted _test_patch_added.
    text = patch_text.replace("\r\n", "\n").replace("\r", "\n")
    return _ADDED_LINE_RE.findall(text)


def _extract_added_lines_by_file(patch_text: str) -> dict[str, list[str]]:
    """Added ('+') lines grouped by the file they belong to.

    Provenance lets the cheating guard ignore matches that occur in production
    files (a production method that merely shares a name with a test method must
    not trip the guard).
    """
    if not patch_text:
        return {}
    # Normalise line endings for parity with _extract_touched_files (a trailing
    # \r would otherwise leak into the captured path and desync the keys).
    text = patch_text.replace("\r\n", "\n").replace("\r", "\n")
    result: dict[str, list[str]] = {}
    current: Optional[str] = None
    for line in text.split("\n"):
        m = _DIFF_FILE_RE.match(line)
        if m:
            current = m.group(2)
            result.setdefault(current, [])
            continue
        # Plain diff (no ``diff --git`` header): the ``+++ b/<path>`` line names
        # the target file. Without this, tool-generated / ``--no-prefix`` patches
        # parse to zero added lines and the content-based cheating guard is blind
        # to them — the same fail-open _extract_touched_files was hardened against.
        if line.startswith("+++"):
            hm = _DIFF_HDR_RE.match(line)
            if hm and hm.group(1) and hm.group(1) != "/dev/null":
                current = hm.group(1)
                result.setdefault(current, [])
            continue
        if current is not None and line.startswith("+"):
            result[current].append(line[1:])
    return result


_TEST_DIR_SEGMENTS = frozenset({"test", "tests", "__tests__", "testing"})


def _looks_like_test_file(path: str) -> bool:
    """Fallback heuristic for files the gold test_patch did not touch.

    Segment-aware (so a top-level ``tests/foo.py`` is recognised, which a naive
    ``"/tests/" in path`` check misses) and case-insensitive. The authoritative
    signal is membership in ``test_patch_files``; this only covers test files a
    fix patch might add that are not in the gold test patch.
    """
    p = path.replace("\\", "/")
    segments = p.split("/")
    if any(seg.lower() in _TEST_DIR_SEGMENTS for seg in segments[:-1]):
        return True
    base = segments[-1]
    low = base.lower()
    return (
        base.endswith(
            (
                "Test.java",
                "Tests.java",
                "IT.java",
                "TestCase.java",
                "Test.kt",
                "Tests.kt",
                "Test.cs",
                "Tests.cs",
            )
        )
        or low.endswith("_test.go")
        or (low.startswith("test_") and low.endswith(".py"))
        or low.endswith("_test.py")
        or low.endswith(
            (
                ".test.js",
                ".test.jsx",
                ".test.ts",
                ".test.tsx",
                ".spec.js",
                ".spec.jsx",
                ".spec.ts",
                ".spec.tsx",
            )
        )
        or low.endswith("_spec.rb")
    )


def _candidate_identifiers(test_name: str) -> list[str]:
    # Extract likely test function / description identifiers across languages.
    n = test_name.strip()
    if not n:
        return []
    n_noparam = _PYTEST_PARAM_RE.sub("", n)
    out: list[str] = []
    if "::" in n_noparam:
        candidate = n_noparam.rsplit("::", 1)[1].strip()
        if candidate and not candidate.lstrip("#").isdigit():
            out.append(candidate)
    if "#" in n_noparam:
        prefix, suffix = n_noparam.rsplit("#", 1)
        suffix = suffix.strip()
        # Java-only separator: com.pkg.Class#method has a dotted FQCN prefix
        # with no path separator. Go/pytest subtest disambiguation
        # (Test.../#04, Test.../foo#bar) has "/" in the prefix; the suffix is
        # an index or subtest hint, not an identifier, and would false-match
        # on common words / CVE IDs / version numbers in large diffs.
        if "/" not in prefix and suffix and not suffix.lstrip("#").isdigit():
            out.append(suffix)
    if " > " in n:
        last = n.split(" > ")[-1].split("(")[0].strip()
        if last:
            out.append(last)
        first = n.split(" > ")[0].strip()
        if first and first != last:
            out.append(first)
    if "/" in n_noparam and _GO_TEST_RE.match(n_noparam):
        out.append(n_noparam.split("/", 1)[0])
    if "." in n_noparam and not any(s in n_noparam for s in ["/", "::", " > ", "#", " "]):
        out.append(n_noparam.rsplit(".", 1)[1].strip())
    out.append(n)
    out.append(n_noparam)
    seen: set[str] = set()
    result: list[str] = []
    for c in out:
        c = c.strip()
        if len(c) >= 3 and c not in seen:
            seen.add(c)
            result.append(c)
    return result


@functools.lru_cache(maxsize=4096)
def _identifier_patterns(name: str) -> Tuple[re.Pattern, re.Pattern]:
    esc = re.escape(name)
    return (
        re.compile(rf"\b{esc}\s*\("),
        re.compile(rf"""["'`]{esc}["'`]"""),
    )


def _appears_in_added(name: str, added: Iterable[str]) -> bool:
    if not name:
        return False
    hits = [line for line in added if name in line]
    if not hits:
        return False
    ident_pat, str_pat = _identifier_patterns(name)
    for line in hits:
        if ident_pat.search(line) or str_pat.search(line):
            return True
    return False


def _authored_via_diff(test_name: str, added: list[str]) -> bool:
    if not added:
        return False
    for cand in _candidate_identifiers(test_name):
        if _appears_in_added(cand, added):
            return True
    return False


def generate_report(
    instance: Instance,
    run_result: Union[str, TestResult],
    test_patch_result: Union[str, TestResult],
    fix_patch_result: Union[str, TestResult],
) -> Report:
    if isinstance(run_result, str):
        run_result = instance.parse_log(run_result)
    if isinstance(test_patch_result, str):
        test_patch_result = instance.parse_log(test_patch_result)
    if isinstance(fix_patch_result, str):
        fix_patch_result = instance.parse_log(fix_patch_result)

    report = Report(
        org=instance.pr.org,
        repo=instance.pr.repo,
        number=instance.pr.number,
        run_result=run_result,
        test_patch_result=test_patch_result,
        fix_patch_result=fix_patch_result,
        test_patch=instance.pr.test_patch,
        fix_patch=instance.pr.fix_patch,
    )

    return report


@dataclass_json
@dataclass
class ReportTask(PullRequestBase):
    instance_dir: Path
    number_interval: str = ""
    tag: str = ""

    @property
    def instance(self) -> Instance:
        pr = PullRequest(
            org=self.org,
            repo=self.repo,
            number=self.number,
            state="",
            title="",
            body="",
            base=Base(label="", ref="", sha=""),
            resolved_issues=[],
            fix_patch="",
            test_patch="",
            number_interval=self.number_interval,
            tag=self.tag,
        )

        config = Config(
            need_clone=False,
            global_env=None,
            clear_env=False,
        )

        return Instance.create(pr, config)

    @property
    def run_log(self) -> str:
        run_log_path = self.instance_dir / RUN_LOG_FILE
        if not run_log_path.exists():
            raise FileNotFoundError(f"Run log file not found: {run_log_path}")
        with open(run_log_path, "r", encoding="utf-8") as f:
            run_log = f.read()
        return run_log

    @property
    def test_patch_run_log(self) -> str:
        test_patch_run_log_path = self.instance_dir / TEST_PATCH_RUN_LOG_FILE
        if not test_patch_run_log_path.exists():
            raise FileNotFoundError(
                f"Test patch run log file not found: {test_patch_run_log_path}"
            )
        with open(test_patch_run_log_path, "r", encoding="utf-8") as f:
            test_patch_run_log = f.read()
        return test_patch_run_log

    @property
    def fix_patch_run_log(self) -> str:
        fix_patch_run_log_path = self.instance_dir / FIX_PATCH_RUN_LOG_FILE
        if not fix_patch_run_log_path.exists():
            raise FileNotFoundError(
                f"Fix patch run log file not found: {fix_patch_run_log_path}"
            )
        with open(fix_patch_run_log_path, "r", encoding="utf-8") as f:
            fix_patch_run_log = f.read()
        return fix_patch_run_log

    def generate_report(
        self,
        run_log: Optional[Union[str, TestResult]] = None,
        test_patch_run_log: Optional[Union[str, TestResult]] = None,
        fix_patch_run_log: Optional[Union[str, TestResult]] = None,
        regen: bool = True,
    ) -> Report:
        if not regen:
            report_path = self.instance_dir / REPORT_FILE
            if report_path.exists():
                with open(report_path, "r", encoding="utf-8") as f:
                    report = Report.from_json(f.read())
                return report

        report = generate_report(
            self.instance,
            run_log or self.run_log,
            test_patch_run_log or self.test_patch_run_log,
            fix_patch_run_log or self.fix_patch_run_log,
        )

        with open(self.instance_dir / REPORT_FILE, "w", encoding="utf-8") as f:
            f.write(report.json())

        return report


@dataclass_json
@dataclass
class FinalReport:
    total_instances: int
    submitted_instances: int
    completed_instances: int
    incomplete_instances: int
    resolved_instances: int
    unresolved_instances: int
    empty_patch_instances: int
    error_instances: int

    submitted_ids: list[str]
    completed_ids: list[str]
    incomplete_ids: list[str]
    resolved_ids: list[str]
    unresolved_ids: list[str]
    empty_patch_ids: list[str]
    error_ids: list[str]

    @classmethod
    def from_dict(cls, d: dict) -> "FinalReport":
        data = cls(**d)
        data.__post_init__()
        return data

    @classmethod
    def from_json(cls, json_str: str) -> "FinalReport":
        data = cls.from_dict(cls.schema().loads(json_str))
        data.__post_init__()
        return data

    def dict(self) -> dict:
        return asdict(self)

    def json(self) -> str:
        return self.to_json(ensure_ascii=False)

    @classmethod
    def from_reports(
        cls,
        reports: list[Report],
        invalid_reports: list[Report],
        failed_tasks: list[ReportTask] = [],
        expected_ids: Optional[Iterable[str]] = None,
    ) -> "FinalReport":
        """Reconcile per-instance reports into a final report (MSB-ROLLOUT-004).

        Counts are recomputed fail-closed from the raw buckets rather than
        trusted as list lengths, so a dropped, duplicated, or miscounted
        instance cannot silently move the pass rate:

        * No instance may appear in more than one terminal bucket
          (resolved / unresolved / error), nor twice within one bucket — a
          collision means upstream report generation is inconsistent, so we
          refuse to emit a report (``ValueError``).
        * When ``expected_ids`` is given (the set of instances that were
          supposed to be graded, e.g. ``set(self.dataset)``), every expected
          instance that produced no terminal result was *silently dropped* and
          is folded into ``error`` (never ``resolved``), keeping the denominator
          honest. A graded instance outside ``expected_ids`` is also an
          inconsistency and raises.
        * The reconciled buckets must satisfy
          ``resolved + unresolved + error == submitted`` (and, when expected is
          given, ``submitted == len(expected_ids)``) or the report is rejected.
        """
        resolved_ids = [report.id for report in reports]
        unresolved_ids = [report.id for report in invalid_reports]
        error_ids = [task.id for task in failed_tasks]

        resolved_set = set(resolved_ids)
        unresolved_set = set(unresolved_ids)
        error_set = set(error_ids)

        # Duplicate within a single bucket?
        if (
            len(resolved_ids) != len(resolved_set)
            or len(unresolved_ids) != len(unresolved_set)
            or len(error_ids) != len(error_set)
        ):
            raise ValueError(
                "Inconsistent reports: duplicate instance id within a single "
                "bucket (resolved/unresolved/error); refusing to emit a final "
                "report."
            )

        # Same instance in more than one terminal bucket?
        cross = (
            (resolved_set & unresolved_set)
            | (resolved_set & error_set)
            | (unresolved_set & error_set)
        )
        if cross:
            raise ValueError(
                f"Inconsistent reports: instance(s) {sorted(cross)} appear in "
                "more than one of resolved/unresolved/error; refusing to emit a "
                "final report."
            )

        # Fail-closed reconciliation against the expected instance set.
        if expected_ids is not None:
            expected_set = set(expected_ids)
            graded = resolved_set | unresolved_set | error_set
            extra = graded - expected_set
            if extra:
                raise ValueError(
                    f"Inconsistent reports: scored instance(s) {sorted(extra)} "
                    "are not in the expected set; refusing to emit a final "
                    "report."
                )
            missing = sorted(expected_set - graded)
            if missing:
                # Dropped instances count as error (never resolved).
                error_ids = error_ids + missing
                error_set = set(error_ids)

        completed_ids = resolved_ids + unresolved_ids
        incomplete_ids = list(error_ids)
        empty_patch_ids: list[str] = []
        submitted_ids = resolved_ids + unresolved_ids + list(error_ids)

        # Invariant gate: the buckets must reconcile or we refuse to score.
        n_submitted = len(submitted_ids)
        if len(resolved_ids) + len(unresolved_ids) + len(error_ids) != n_submitted:
            raise ValueError(
                "Inconsistent reports: resolved+unresolved+error != submitted; "
                "refusing to emit a final report."
            )
        if len(completed_ids) + len(incomplete_ids) != n_submitted:
            raise ValueError(
                "Inconsistent reports: completed+incomplete != submitted; "
                "refusing to emit a final report."
            )
        if expected_ids is not None and n_submitted != len(expected_set):
            raise ValueError(
                "Inconsistent reports: submitted "
                f"({n_submitted}) != expected ({len(expected_set)}); refusing to "
                "emit a final report."
            )

        final_report = FinalReport(
            total_instances=n_submitted,
            submitted_instances=n_submitted,
            completed_instances=len(completed_ids),
            incomplete_instances=len(incomplete_ids),
            resolved_instances=len(resolved_ids),
            unresolved_instances=len(unresolved_ids),
            empty_patch_instances=len(empty_patch_ids),
            error_instances=len(error_ids),
            submitted_ids=submitted_ids,
            completed_ids=completed_ids,
            incomplete_ids=incomplete_ids,
            resolved_ids=resolved_ids,
            unresolved_ids=unresolved_ids,
            empty_patch_ids=empty_patch_ids,
            error_ids=list(error_ids),
        )

        return final_report
