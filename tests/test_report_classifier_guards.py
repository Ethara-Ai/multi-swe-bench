"""Unit tests for the classifier guards added to Report.check().

Covers the four pollution axes (baseline / provenance / cheating) plus the
broken-language-matcher fallback and JSON round-trip persistence.
"""

from __future__ import annotations

import json

from multi_swe_bench.harness.report import Report
from multi_swe_bench.harness.test_result import TestStatus, mapping_to_testresult

P = TestStatus.PASSED.value
F = TestStatus.FAILED.value
S = TestStatus.SKIPPED.value


def _diff(*files: str) -> str:
    # Minimal git-diff header block; _DIFF_FILE_RE only reads the header line.
    return "\n".join(
        f"diff --git a/{f} b/{f}\n--- a/{f}\n+++ b/{f}\n@@ -1 +1 @@\n-old\n+new"
        for f in files
    )


def _report(
    run: dict,
    test: dict,
    fix: dict,
    test_patch: str = "",
    fix_patch: str = "",
) -> Report:
    return Report(
        org="o",
        repo="r",
        number=1,
        run_result=mapping_to_testresult(run),
        test_patch_result=mapping_to_testresult(test),
        fix_patch_result=mapping_to_testresult(fix),
        test_patch=test_patch,
        fix_patch=fix_patch,
    )


# --- Guard #1: baseline guard fires => CBC reclassified from n2p to p2p ----
def test_cbc_reclassified_to_p2p():
    r = _report(
        run={"tests/foo_test.py::TestFoo::test_a": P, "src/bug_test.py::test_b": F},
        test={"src/bug_test.py::test_b": F},
        fix={"tests/foo_test.py::TestFoo::test_a": P, "src/bug_test.py::test_b": P},
        test_patch=_diff("src/bug_test.py"),
    )
    assert r.valid is True
    assert "tests/foo_test.py::TestFoo::test_a" in r.p2p_tests
    assert "tests/foo_test.py::TestFoo::test_a" in r.reclassified_from_target
    assert "tests/foo_test.py::TestFoo::test_a" not in r.n2p_tests


# --- Guard #1 non-firing: test_patch touched the file => stays F2P ---------
def test_legitimate_f2p_when_test_patch_touched_file():
    r = _report(
        run={"tests/foo_test.py::TestFoo::test_a": P, "src/other_test.py::test_b": F},
        test={"tests/foo_test.py::TestFoo::test_a": F, "src/other_test.py::test_b": F},
        fix={"tests/foo_test.py::TestFoo::test_a": P, "src/other_test.py::test_b": P},
        test_patch=_diff("tests/foo_test.py", "src/other_test.py"),
    )
    assert r.valid is True
    assert "tests/foo_test.py::TestFoo::test_a" in r.f2p_tests
    assert "tests/foo_test.py::TestFoo::test_a" not in r.reclassified_from_target


# --- Guard #2: phantom N2P dropped to fix_patch_authored_candidates -------
def test_phantom_n2p_not_credited():
    r = _report(
        run={"src/bug_test.py::test_b": F},
        test={"src/bug_test.py::test_b": F},
        fix={"src/bug_test.py::test_b": P, "tests/agent_added.py::test_x": P},
        test_patch=_diff("src/bug_test.py"),
    )
    assert r.valid is True
    assert "tests/agent_added.py::test_x" in r.fix_patch_authored_candidates
    assert "tests/agent_added.py::test_x" not in r.n2p_tests


# --- Guard #3 (cheating): fix_patch modifies a test's file => invalid ----
def test_fix_patch_modifies_test_file_is_invalid():
    r = _report(
        run={"tests/bug_test.py::test_b": F},
        test={"tests/bug_test.py::test_b": F},
        fix={"tests/bug_test.py::test_b": P},
        test_patch=_diff("tests/bug_test.py"),
        fix_patch=_diff("tests/bug_test.py"),
    )
    assert r.valid is False
    assert "cannot credit" in r.error_msg


# --- Baseline-first: Go/Rust CBC caught via status-only, no matcher needed --
def test_go_cbc_caught_via_status_only():
    # Under baseline-first classifier: run=PASS + test=NONE alone is decisive CBC.
    # Works on Go-style IDs where file-based matcher structurally cannot hit
    # (test_patch touches an unrelated file; identifier isn't in added lines).
    go_cbc = "github.com/foo/bar/pkg.TestFoo"
    go_f2p = "github.com/foo/bar/pkg.TestBar"
    r = _report(
        run={go_cbc: P, go_f2p: F},
        test={go_f2p: F},
        fix={go_cbc: P, go_f2p: P},
        test_patch=_diff("pkg/unrelated.go"),
    )
    assert r.valid is True
    assert go_cbc in r.p2p_tests
    assert go_cbc in r.reclassified_from_target
    assert go_cbc not in r.n2p_tests
    assert go_f2p in r.f2p_tests


# --- Baseline-first: hidden F2P inferred from run=FAIL ----------------------
def test_hidden_f2p_inferred_from_baseline():
    # (run=FAIL, test=NONE, fix=PASS): test was failing at baseline, hidden by
    # test.patch, passing under fix. Baseline state proves the real transition
    # is FAIL -> PASS. Old classifier would have routed to n2p or phantom.
    r = _report(
        run={"pkg/foo::TestHidden": F},
        test={},
        fix={"pkg/foo::TestHidden": P},
        test_patch=_diff("pkg/foo/foo.go"),
    )
    assert r.valid is True
    assert "pkg/foo::TestHidden" in r.f2p_tests
    assert "pkg/foo::TestHidden" not in r.n2p_tests
    assert "pkg/foo::TestHidden" not in r.fix_patch_authored_candidates


# --- Baseline-first: hidden S2P inferred from run=SKIP ----------------------
def test_hidden_s2p_inferred_from_baseline():
    # (run=SKIP, test=NONE, fix=PASS): was skipping at baseline, hidden by
    # test.patch, passing under fix. Layer 2 infers S2P from baseline state.
    r = _report(
        run={"pkg/foo::TestHidden": S, "pkg/foo::TestSanity": F},
        test={"pkg/foo::TestSanity": F},
        fix={"pkg/foo::TestHidden": P, "pkg/foo::TestSanity": P},
        test_patch=_diff("pkg/foo/foo.go"),
    )
    assert r.valid is True
    assert "pkg/foo::TestHidden" in r.s2p_tests
    assert "pkg/foo::TestHidden" not in r.n2p_tests


# --- Regression: matcher_can_hit consults diff-content on non-pytest IDs -
def test_matcher_ok_via_diff_content_reclassifies_go_cbc():
    # Pre-fix: _matcher_can_hit was file-only; bare-Go IDs (no path in ID) always
    # returned False, causing fail-open on CBC. Post-fix: diff-content authorship
    # on any test in the PR proves the sanity check works, allowing per-test
    # file-based fallback to correctly return False and the baseline guard to fire.
    go_diff = "\n".join([
        "diff --git a/pkg/foo/thing_test.go b/pkg/foo/thing_test.go",
        "--- a/pkg/foo/thing_test.go",
        "+++ b/pkg/foo/thing_test.go",
        "@@ -1,2 +1,4 @@",
        " package foo",
        "+",
        "+func TestNewThing(t *testing.T) {}",
    ])
    r = _report(
        run={"TestPreExisting": P, "TestNewThing": F},
        test={"TestNewThing": F},
        fix={"TestPreExisting": P, "TestNewThing": P},
        test_patch=go_diff,
    )
    assert r.valid is True
    assert "TestPreExisting" in r.p2p_tests
    assert "TestPreExisting" in r.reclassified_from_target
    assert "TestPreExisting" not in r.n2p_tests
    assert "TestNewThing" in r.f2p_tests


# --- Regression: same-file CBC (test.patch adds a new test to the file
#                 that also contains a pre-existing CBC test) --------------
def test_same_file_cbc_reclassifies_despite_file_touched():
    # Prior bug: file-based matcher marked every test in a touched file as
    # authored, hiding CBC that lived in the same file as an F2P addition.
    # Fix: baseline guard uses strict diff-content authorship.
    py_diff = "\n".join([
        "diff --git a/tests/foo.py b/tests/foo.py",
        "--- a/tests/foo.py",
        "+++ b/tests/foo.py",
        "@@ -1,3 +1,6 @@",
        " class TestFoo:",
        "     def test_old(self): pass",
        "+",
        "+    def test_new(self):",
        "+        assert True",
    ])
    r = _report(
        run={"tests/foo.py::TestFoo::test_old": P, "tests/foo.py::TestFoo::test_new": F},
        test={"tests/foo.py::TestFoo::test_new": F},
        fix={
            "tests/foo.py::TestFoo::test_old": P,
            "tests/foo.py::TestFoo::test_new": P,
        },
        test_patch=py_diff,
    )
    assert r.valid is True
    # test_old was passing at baseline and NOT specifically added by test_patch.
    # Even though the file is touched, baseline guard should reclassify.
    assert "tests/foo.py::TestFoo::test_old" in r.p2p_tests
    assert "tests/foo.py::TestFoo::test_old" in r.reclassified_from_target
    assert "tests/foo.py::TestFoo::test_old" not in r.n2p_tests
    # test_new is genuinely new (diff-content finds it) and stays F2P.
    assert "tests/foo.py::TestFoo::test_new" in r.f2p_tests


# --- Regression: file matcher uses path-boundary, not substring ----------
def test_file_matcher_avoids_substring_false_positive():
    # Naive `f in test_name` matched "test.py" inside "tests/my_test.py::x",
    # falsely authoring unrelated tests and hiding their CBC. Path-boundary
    # match must reject this.
    from multi_swe_bench.harness.report import _test_name_matches_files

    assert _test_name_matches_files("tests/my_test.py::x", ["test.py"]) is False
    assert _test_name_matches_files("tests/foo.py::TestX::t", ["tests/foo.py"]) is True
    assert _test_name_matches_files(
        "src/x.test.ts > Group > renders", ["src/x.test.ts"]
    ) is True


# --- Regression: `#` split only fires when prefix is Java-shaped (no "/") --
def test_candidate_identifiers_hash_split_scoped_to_java_shaped_prefix():
    # Bufbuild PR#372: test ID "internal/buf/buffetch::Test.../foo#bar"
    # extracted "bar" via the `#` split, which then substring-matched common
    # occurrences of `bar(` / `"bar"` in large multi-file fix.patches → false
    # Gate 5 rejection. `#` is a Java-only separator (com.pkg.Class#method);
    # any prefix containing `/` is Go/pytest subtest disambiguation and must
    # not produce a suffix candidate.
    from multi_swe_bench.harness.report import _candidate_identifiers

    bufbuild_id = "internal/buf/buffetch::TestGetParsedRefError/path/to/foo#bar"
    cands = _candidate_identifiers(bufbuild_id)
    assert "bar" not in cands
    assert "TestGetParsedRefError/path/to/foo#bar" in cands  # full suffix kept

    # Java Class#method — prefix has no "/" → suffix MUST be extracted.
    java_cands = _candidate_identifiers("com.pkg.MyClass#testFoo")
    assert "testFoo" in java_cands

    # Numeric subtest (trivy PR#5923 case) — still rejected by the numeric filter.
    trivy_id = "pkg/iac/scanners/terraform::Test_Foo/#04"
    trivy_cands = _candidate_identifiers(trivy_id)
    assert "04" not in trivy_cands
    assert "#04" not in trivy_cands


# --- Regression: _candidate_identifiers rejects pure-digit subtest indices -
def test_candidate_identifiers_rejects_numeric_subtest_index():
    # Trivy PR#5923: test ID has subtest index "#04". Extracting "04" as an
    # identifier and searching for it in a large fix.patch would false-match on
    # CVE IDs, version numbers, dates. Bug caused ~2/67 spurious Gate 5 rejections.
    from multi_swe_bench.harness.report import _candidate_identifiers

    trivy_id = "pkg/iac/scanners/terraform::Test_IgnoreInlineByAVDID/#04"
    candidates = _candidate_identifiers(trivy_id)
    assert "04" not in candidates
    assert "#04" not in candidates
    # Legitimate candidate (test-shaped name with slash) still extracted:
    assert "Test_IgnoreInlineByAVDID/#04" in candidates

    # Alphanumeric candidates unchanged: filter is precise (isdigit-only).
    assert _candidate_identifiers("com.pkg.MyClass#testFoo") == \
        _candidate_identifiers("com.pkg.MyClass#testFoo")
    assert "testFoo" in _candidate_identifiers("com.pkg.MyClass#testFoo")
    assert "test5" in _candidate_identifiers("MyClass#test5")


# --- Persistence: derived state and diagnostics round-trip through JSON --
def test_derived_state_round_trips_through_json():
    r = _report(
        run={"tests/foo_test.py::TestFoo::test_a": P, "src/bug_test.py::test_b": F},
        test={"src/bug_test.py::test_b": F},
        fix={"tests/foo_test.py::TestFoo::test_a": P, "src/bug_test.py::test_b": P},
        test_patch=_diff("src/bug_test.py"),
    )
    assert r.valid is True
    assert "src/bug_test.py" in r.test_patch_files
    assert r.reclassified_from_target

    r2 = Report.from_json(r.json())
    assert r2.test_patch_files == r.test_patch_files
    assert set(r2.reclassified_from_target) == set(r.reclassified_from_target)
    assert set(r2.p2p_tests) == set(r.p2p_tests)
    assert r2.schema_version == r.schema_version


def test_fix_patch_touches_only_src_stays_valid():
    r = _report(
        run={"tests/a.py::t": F},
        test={"tests/a.py::t": F},
        fix={"tests/a.py::t": P},
        test_patch=_diff("tests/a.py"),
        fix_patch=_diff("src/impl.py"),
    )
    assert r.valid is True
    assert not r.guard_fix_patch_touched_tests
    assert "tests/a.py::t" in r.f2p_tests


def test_guard_fix_patch_touched_tests_collects_all_offenders():
    r = _report(
        run={
            "tests/foo.py::test_one": F,
            "tests/foo.py::test_two": F,
            "tests/bar.py::test_three": F,
        },
        test={
            "tests/foo.py::test_one": F,
            "tests/foo.py::test_two": F,
            "tests/bar.py::test_three": F,
        },
        fix={
            "tests/foo.py::test_one": P,
            "tests/foo.py::test_two": P,
            "tests/bar.py::test_three": P,
        },
        test_patch=_diff("tests/foo.py", "tests/bar.py"),
        fix_patch=_diff("tests/foo.py", "tests/bar.py"),
    )
    assert r.valid is False
    assert set(r.guard_fix_patch_touched_tests) == {
        "tests/foo.py::test_one",
        "tests/foo.py::test_two",
        "tests/bar.py::test_three",
    }
    assert not r.fix_patch_authored_candidates


def test_old_schema_reload_forces_recheck():
    stale_json = json.dumps({
        "org": "o",
        "repo": "r",
        "number": 1,
        "valid": True,
        "error_msg": None,
        "fixed_tests": {},
        "p2p_tests": {},
        "f2p_tests": {},
        "s2p_tests": {},
        "n2p_tests": {},
        "run_result": {
            "name": "run",
            "all_count": 1,
            "passed_count": 0,
            "failed_count": 1,
            "skipped_count": 0,
            "passed_tests": [],
            "failed_tests": ["tests/bug_test.py::test_b"],
            "skipped_tests": [],
        },
        "test_patch_result": {
            "name": "test",
            "all_count": 1,
            "passed_count": 0,
            "failed_count": 1,
            "skipped_count": 0,
            "passed_tests": [],
            "failed_tests": ["tests/bug_test.py::test_b"],
            "skipped_tests": [],
        },
        "fix_patch_result": {
            "name": "fix",
            "all_count": 1,
            "passed_count": 1,
            "failed_count": 0,
            "skipped_count": 0,
            "passed_tests": ["tests/bug_test.py::test_b"],
            "failed_tests": [],
            "skipped_tests": [],
        },
        "test_patch_files": ["tests/bug_test.py"],
        "fix_patch_files": ["tests/bug_test.py"],
    })
    r = Report.from_json(stale_json)
    assert r.valid is False
    assert "cannot credit" in r.error_msg


def test_added_lines_survive_json_round_trip():
    r = _report(
        run={"tests/foo.py::test_a": P},
        test={},
        fix={"tests/foo.py::test_a": P},
        test_patch="\n".join([
            "diff --git a/tests/foo.py b/tests/foo.py",
            "--- a/tests/foo.py",
            "+++ b/tests/foo.py",
            "@@ -1 +1,2 @@",
            " existing",
            "+def test_a(): assert True",
        ]),
    )
    assert r._test_patch_added
    r2 = Report.from_json(r.json())
    assert r2._test_patch_added == r._test_patch_added


def test_extract_added_lines_normalizes_crlf():
    # A CRLF test patch must not leak a trailing '\r' into the added-line scan
    # (parity with _extract_added_lines_by_file / _extract_touched_files).
    from multi_swe_bench.harness.report import _extract_added_lines

    crlf = (
        "diff --git a/tests/foo.py b/tests/foo.py\r\n"
        "--- a/tests/foo.py\r\n"
        "+++ b/tests/foo.py\r\n"
        "@@ -1 +1,2 @@\r\n"
        " existing\r\n"
        "+def test_new(): assert True\r\n"
    )
    added = _extract_added_lines(crlf)
    assert added == ["def test_new(): assert True"]
    assert all("\r" not in line for line in added)

    # LF input is unchanged by the normalization.
    lf = crlf.replace("\r\n", "\n")
    assert _extract_added_lines(lf) == ["def test_new(): assert True"]


def test_crlf_test_patch_added_round_trips_clean():
    # End-to-end: a CRLF test_patch flows through Report and the persisted
    # _test_patch_added stays clean across a JSON round-trip.
    r = _report(
        run={"tests/foo.py::test_a": P},
        test={},
        fix={"tests/foo.py::test_a": P},
        test_patch=(
            "diff --git a/tests/foo.py b/tests/foo.py\r\n"
            "--- a/tests/foo.py\r\n"
            "+++ b/tests/foo.py\r\n"
            "@@ -1 +1,2 @@\r\n"
            " existing\r\n"
            "+def test_a(): assert True\r\n"
        ),
    )
    assert r._test_patch_added == ["def test_a(): assert True"]
    r2 = Report.from_json(r.json())
    assert r2._test_patch_added == r._test_patch_added
    assert all("\r" not in line for line in r2._test_patch_added)


def test_candidate_identifiers_rejects_two_letter_descriptions():
    from multi_swe_bench.harness.report import _candidate_identifiers

    for name in ("suite > it > do", "suite > it > go", "suite > it > it"):
        assert "do" not in _candidate_identifiers(name)
        assert "go" not in _candidate_identifiers(name)
        assert "it" not in _candidate_identifiers(name)


def test_extract_touched_files_handles_quoted_paths():
    from multi_swe_bench.harness.report import _extract_touched_files

    quoted = (
        'diff --git "a/tests/dir with space/foo.py" '
        '"b/tests/dir with space/foo.py"\n'
        "--- a/tests/dir with space/foo.py\n"
        "+++ b/tests/dir with space/foo.py\n"
    )
    assert _extract_touched_files(quoted) == {"tests/dir with space/foo.py"}


# --- Cheating guard is file-context aware (Java naming false positive) -------
def _diff_body(path: str, added_line: str) -> str:
    return "\n".join([
        f"diff --git a/{path} b/{path}",
        f"--- a/{path}",
        f"+++ b/{path}",
        "@@ -1 +1,2 @@",
        " ctx",
        f"+{added_line}",
    ])


_HALO_TEST_FILE = "src/test/java/run/halo/PostCommentSubjectTest.java"
_HALO_PROD_FILE = "src/main/java/run/halo/PostCommentSubject.java"
_HALO_TEST_PATCH = _diff_body(_HALO_TEST_FILE, "@Test void get() {}")
_HALO_ID = "PostCommentSubjectTest > get()"


def test_java_production_only_fix_not_flagged():
    # halo false positive: fix touches only production code, but the credited
    # test method `get()` shares its name with a production method. The guard
    # must NOT fire — no test file is modified.
    r = _report(
        run={_HALO_ID: F},
        test={_HALO_ID: F},
        fix={_HALO_ID: P},
        test_patch=_HALO_TEST_PATCH,
        fix_patch=_diff_body(_HALO_PROD_FILE, "return svc.get();"),
    )
    assert r.valid is True
    assert not r.guard_fix_patch_touched_tests
    assert _HALO_ID in r.f2p_tests


def test_java_multi_verb_pr_not_flagged():
    # Dataset-scale halo reproduction (PR#3087 fingerprint): several Java tests
    # whose method names are common English verbs (get/list/create/add) that
    # inevitably recur in production code. The fix touches ONLY production files
    # (src/main/java/...); gold test_patch owns the test files. None of the verb
    # collisions may trip the cheating guard. Regression pin for the reported
    # 55% Java false-positive rate.
    cases = {
        "PostCommentSubjectTest > get()": (
            "src/main/java/run/halo/PostCommentSubject.java",
            "public String get() { return subject.get(); }",
        ),
        "CategoryFinderImplTest > list()": (
            "src/main/java/run/halo/CategoryFinderImpl.java",
            "public List<Category> list() { return repo.list(); }",
        ),
        "CommentServiceImplTest > create()": (
            "src/main/java/run/halo/CommentServiceImpl.java",
            "public Comment create(Comment c) { return repo.create(c); }",
        ),
        "ReplyServiceImplTest > add()": (
            "src/main/java/run/halo/ReplyServiceImpl.java",
            "list.add(reply); return service.add(reply);",
        ),
    }
    gold_test_files = {
        tid: "src/test/java/run/halo/%s.java" % tid.split(" > ")[0]
        for tid in cases
    }
    test_patch = "\n".join(
        _diff_body(gold_test_files[tid], "@Test void %s() {}" % tid.split(" > ")[1].rstrip("()"))
        for tid in cases
    )
    fix_patch = "\n".join(_diff_body(prod, added) for prod, added in cases.values())

    r = _report(
        run={tid: F for tid in cases},
        test={tid: F for tid in cases},
        fix={tid: P for tid in cases},
        test_patch=test_patch,
        fix_patch=fix_patch,
    )
    assert r.valid is True
    assert not r.guard_fix_patch_touched_tests
    assert set(r.f2p_tests) == set(cases)
    # No production file leaked into the test-file content bucket.
    assert r._fix_patch_test_added == {}


def test_java_multi_verb_pr_still_catches_real_cheat():
    # Same multi-verb PR, but the fix ALSO re-declares a credited test method
    # inside the gold test file. The file-context guard must still fire — the
    # false-positive fix did not weaken real cheat detection.
    fix_patch = "\n".join([
        _diff_body(_HALO_PROD_FILE, "public String get() { return subject.get(); }"),
        _diff_body(_HALO_TEST_FILE, "void get() { assertEquals(1, 1); }"),
    ])
    r = _report(
        run={_HALO_ID: F},
        test={_HALO_ID: F},
        fix={_HALO_ID: P},
        test_patch=_HALO_TEST_PATCH,
        fix_patch=fix_patch,
    )
    assert r.valid is False
    assert _HALO_ID in r.guard_fix_patch_touched_tests


def test_java_fix_editing_test_file_content_is_invalid():
    # Real cheat: fix re-declares the credited test method inside the test file.
    r = _report(
        run={_HALO_ID: F},
        test={_HALO_ID: F},
        fix={_HALO_ID: P},
        test_patch=_HALO_TEST_PATCH,
        fix_patch=_diff_body(_HALO_TEST_FILE, "void get() { assertEquals(1, 1); }"),
    )
    assert r.valid is False
    assert "cannot credit" in r.error_msg


def test_java_fix_doctored_assertion_in_test_file_is_invalid():
    # Real cheat the content matcher CANNOT see (no re-declaration of `get`);
    # the file-overlap rule (fix modifies a gold test file) still rejects it.
    r = _report(
        run={_HALO_ID: F},
        test={_HALO_ID: F},
        fix={_HALO_ID: P},
        test_patch=_HALO_TEST_PATCH,
        fix_patch=_diff_body(_HALO_TEST_FILE, "    expected = 42;  // doctored"),
    )
    assert r.valid is False
    assert "gold test file" in r.error_msg


def test_looks_like_test_file_recognizes_conventions():
    from multi_swe_bench.harness.report import _looks_like_test_file

    # target case + the leading-slash bug (top-level tests/) + major conventions
    for p in (
        "src/test/java/run/halo/PostCommentSubjectTest.java",
        "tests/test_report_classifier_guards.py",   # top-level tests/ dir
        "test_foo.py",                                # pytest prefix
        "pkg/foo_test.py",                            # pytest suffix
        "src/components/Button.spec.ts",              # jest/vitest
        "src/components/Button.test.jsx",
        "internal/foo_test.go",                       # go
    ):
        assert _looks_like_test_file(p) is True, p

    for p in (
        "src/main/java/run/halo/PostCommentSubject.java",
        "src/impl.py",
        "pkg/service.go",
    ):
        assert _looks_like_test_file(p) is False, p


def test_fix_test_added_round_trips_through_json():
    # Derived fix-patch test-file content must survive regen (raw patch dropped).
    r = _report(
        run={_HALO_ID: F},
        test={_HALO_ID: F},
        fix={_HALO_ID: P},
        test_patch=_HALO_TEST_PATCH,
        fix_patch=_diff_body(_HALO_TEST_FILE, "void get() { assertEquals(1, 1); }"),
    )
    assert r._fix_patch_test_added
    r2 = Report.from_json(r.json())
    assert r2._fix_patch_test_added == r._fix_patch_test_added
    assert r2.valid is False  # verdict is stable across regen


def test_java_fix_editing_a_DIFFERENT_test_file_not_flagged():
    # Residual Java false positive (containing-file scoping): the fix modifies a
    # DIFFERENT test file (a shared base class / helper) that merely *calls*
    # `subject.get()`. The credited test `PostCommentSubjectTest > get()` lives in
    # its own gold file, which the fix never touches. A common-verb collision in
    # an unrelated test file must NOT flag the credited test.
    helper = "src/test/java/run/halo/AbstractServiceTestSupport.java"
    r = _report(
        run={_HALO_ID: F},
        test={_HALO_ID: F},
        fix={_HALO_ID: P},
        test_patch=_HALO_TEST_PATCH,
        fix_patch=_diff_body(helper, "Object v = subject.get();"),
    )
    assert r.valid is True
    assert not r.guard_fix_patch_touched_tests
    assert _HALO_ID in r.f2p_tests
    # The helper is a test file (so its lines are retained), but it does not host
    # the credited test — the content match is scoped to the test's own file.
    assert helper in r._fix_patch_test_added


# --- Robustness: cheating guard must not fail open on non-`diff --git` patches -
def _plain_diff(path: str) -> str:
    # Unified diff with NO `diff --git` header (plain `diff -u` / tool output).
    return "\n".join([
        f"--- a/{path}",
        f"+++ b/{path}",
        "@@ -1 +1 @@",
        "-real_assertion()",
        "+assert True  # doctored",
    ])


def test_plain_unified_diff_fix_is_caught():
    # A fix patch lacking the `diff --git` header used to parse to zero touched
    # files, so a doctored gold test file sailed through as valid. It must now be
    # recognised and rejected.
    r = _report(
        run={"tests/bug_test.py::test_b": F},
        test={"tests/bug_test.py::test_b": F},
        fix={"tests/bug_test.py::test_b": P},
        test_patch=_diff("tests/bug_test.py"),
        fix_patch=_plain_diff("tests/bug_test.py"),
    )
    assert r.fix_patch_files == ["tests/bug_test.py"]
    assert r.valid is False
    assert "cannot credit" in r.error_msg


def test_plain_diff_added_lines_have_file_provenance():
    # Content-based cheat detection was blind to plain diffs: _extract_added_lines
    # _by_file only recognised `diff --git` headers, so a `--no-prefix` / tool
    # patch yielded zero added lines by file. The `+++ b/<path>` header must now
    # give the added lines their file provenance, in parity with the file-list
    # extractor that already handles headerless diffs.
    from multi_swe_bench.harness.report import _extract_added_lines_by_file

    by_file = _extract_added_lines_by_file(_plain_diff("tests/bug_test.py"))
    assert by_file == {"tests/bug_test.py": ["assert True  # doctored"]}


def test_no_prefix_git_diff_is_parsed():
    from multi_swe_bench.harness.report import _extract_touched_files

    no_prefix = "\n".join([
        "diff --git tests/foo.py tests/foo.py",
        "--- tests/foo.py",
        "+++ tests/foo.py",
        "@@ -1 +1 @@",
        "-a",
        "+b",
    ])
    assert _extract_touched_files(no_prefix) == {"tests/foo.py"}


def test_crlf_patch_paths_are_normalized():
    from multi_swe_bench.harness.report import _extract_touched_files

    crlf = (
        "diff --git a/tests/foo.py b/tests/foo.py\r\n"
        "--- a/tests/foo.py\r\n"
        "+++ b/tests/foo.py\r\n"
        "@@ -1 +1 @@\r\n"
        "-o\r\n"
        "+n"
    )
    # No trailing '\r' leaks into the path.
    assert _extract_touched_files(crlf) == {"tests/foo.py"}


def test_added_file_uses_target_path_not_devnull():
    from multi_swe_bench.harness.report import _extract_touched_files

    added = "\n".join([
        "diff --git a/tests/new_test.py b/tests/new_test.py",
        "new file mode 100644",
        "--- /dev/null",
        "+++ b/tests/new_test.py",
        "@@ -0,0 +1 @@",
        "+def test_new(): pass",
    ])
    assert _extract_touched_files(added) == {"tests/new_test.py"}


# --- Regression: force re-check must not leave stale / doubled buckets ---------
def test_force_recheck_resets_buckets():
    r = _report(
        run={"tests/a.py::t": F, "tests/a.py::keep": F},
        test={"tests/a.py::t": F, "tests/a.py::keep": F},
        fix={"tests/a.py::t": P, "tests/a.py::keep": P},
        test_patch=_diff("tests/a.py"),
    )
    assert set(r.f2p_tests) == {"tests/a.py::t", "tests/a.py::keep"}
    # `t` now looks like a p2p (test-stage PASS); a forced recompute must move it
    # cleanly, never leave it double-classified in both f2p and p2p.
    r._tests["tests/a.py::t"].test = TestStatus.PASS
    r.check(force=True)
    assert set(r.f2p_tests) & set(r.p2p_tests) == set()
    assert "tests/a.py::t" in r.p2p_tests
    assert "tests/a.py::t" not in r.f2p_tests
