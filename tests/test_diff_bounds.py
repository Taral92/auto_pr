"""Regression: PostImage.locate must not raise on a malformed PostImage.

Found by the agent itself on PR #3. Unreachable via post_images(), because
splitlines() guarantees no line contains a newline — but the invariant was
unstated and load-bearing.
"""

import random

from core.diff import (
    PostImage,
    _header_path,
    changed_paths,
    hunk_ranges,
    post_images,
)

TWO_FILES = """diff --git a/app/a.py b/app/a.py
--- a/app/a.py
+++ b/app/a.py
@@ -18,11 +18,17 @@
 context
+added
@@ -100,2 +106,1 @@
 context
diff --git a/app/b.py b/app/b.py
--- a/app/b.py
+++ b/app/b.py
@@ -2724,3 +2724,18 @@
 context
"""


def test_hunk_ranges_are_post_image_lines():
    assert hunk_ranges(TWO_FILES) == {
        "app/a.py": [(18, 34), (106, 106)],
        "app/b.py": [(2724, 2741)],
    }


def test_a_hunk_header_without_a_length_covers_one_line():
    diff = "--- a/x.py\n+++ b/x.py\n@@ -4 +7 @@\n context\n"
    assert hunk_ranges(diff) == {"x.py": [(7, 7)]}


def test_a_deleted_file_has_no_post_image_range():
    diff = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ /dev/null\n@@ -1,3 +0,0 @@\n-gone\n"
    assert hunk_ranges(diff) == {}


def test_a_zero_length_hunk_is_skipped():
    """A pure deletion covers no line that exists to be read."""
    diff = "--- a/x.py\n+++ b/x.py\n@@ -5,3 +5,0 @@\n-gone\n"
    assert hunk_ranges(diff) == {}


def test_hunk_ranges_and_changed_paths_agree_on_path_spelling():
    """`read_file` looks these up by the same key the scope jail uses."""
    assert set(hunk_ranges(TWO_FILES)) <= set(changed_paths(TWO_FILES))


def test_post_images_still_reads_the_shared_hunk_regex():
    """Both readings of a hunk header come from one pattern now."""
    images = post_images(TWO_FILES)
    assert [i.path for i in images] == ["app/a.py", "app/b.py"]


def test_line_containing_a_newline_does_not_crash():
    p = PostImage("f.py", "\n".join(["a\nb", "c"]), (1, 2))
    assert p.locate("c") is None or isinstance(p.locate("c"), int)


def test_fuzz_never_raises():
    random.seed(1)
    for _ in range(5000):
        lines = [
            "".join(random.choices("ab\n\t +-", k=random.randint(0, 5)))
            for _ in range(random.randint(1, 6))
        ]
        p = PostImage(
            "f", "\n".join(lines),
            tuple(random.choice([None, 1]) for _ in lines),
        )
        p.locate("".join(random.choices("ab\n +", k=random.randint(0, 6))))


def test_post_images_never_produces_a_line_with_a_newline():
    """The invariant the guard protects. If this ever fails, the guard is
    doing real work rather than documenting an assumption."""
    diff = (
        "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n"
        "@@ -1,2 +1,3 @@\n a\n+b\n c\n"
    )
    for image in post_images(diff):
        assert image.text.count("\n") == len(image.line_numbers) - 1


# -- git's C-style quoted paths --------------------------------------------
#
# Git wraps a path in double quotes and escapes it C-style whenever it contains
# a byte it must escape: a quote, a backslash, a control character, or (with
# core.quotePath, the default) any byte >= 0x80. Undecoded, `café.py` arrives
# as `"b/caf\303\251.py"` - a scope key matching nothing on disk, so the
# manifest shows the model a mangled path, read_file refuses the real name, and
# the file is silently unreviewable.
#
# Separately: when an UNQUOTED path contains a space, git appends a TAB after
# it. `changed_paths` used to .strip() that away while `_plus_path` kept it, so
# scope keys and hunk keys disagreed for any filename with a space.


def _diff(header_path: str, hunk: str = "@@ -1,1 +1,1 @@\n x\n") -> str:
    return f"--- a/x\n+++ {header_path}\n{hunk}"


def test_a_plain_ascii_path_is_unchanged():
    assert _header_path("b/plain.py") == "b/plain.py"
    assert changed_paths(_diff("b/plain.py")) == {"plain.py": "modified"}


def test_a_path_with_a_space_drops_gits_tab_separator():
    """Git appends a TAB after an unquoted path containing a space."""
    assert _header_path("b/with space.py\t") == "b/with space.py"
    assert changed_paths(_diff("b/with space.py\t")) == {"with space.py": "modified"}


def test_a_filename_ending_in_a_space_keeps_it():
    """Cut at the tab, do not strip whitespace: the space is part of the name."""
    assert _header_path("b/ends with space \t") == "b/ends with space "


def test_a_non_ascii_path_is_decoded_from_octal_bytes():
    """The escapes are UTF-8 BYTES, not characters: \303\251 is one `é`."""
    assert _header_path(r'"b/caf\303\251.py"') == "b/café.py"
    assert changed_paths(_diff(r'"b/caf\303\251.py"')) == {"café.py": "modified"}


def test_a_four_byte_character_is_decoded():
    assert _header_path(r'"b/emoji\360\237\216\257.py"') == "b/emoji🎯.py"


def test_c_style_escapes_are_decoded():
    assert _header_path(r'"b/quote\".py"') == 'b/quote".py'
    assert _header_path(r'"b/back\\slash.py"') == "b/back\\slash.py"
    assert _header_path(r'"b/tab\tchar.py"') == "b/tab\tchar.py"
    assert _header_path(r'"b/nl\nchar.py"') == "b/nl\nchar.py"


#: (old-side header, new-side header) exactly as git writes each pair.
HEADER_PAIRS = [
    ("a/plain.py", "b/plain.py"),
    (r'"a/caf\303\251.py"', r'"b/caf\303\251.py"'),
    ("a/with space.py\t", "b/with space.py\t"),
    (r'"a/quote\".py"', r'"b/quote\".py"'),
    (r'"a/emoji\360\237\216\257.py"', r'"b/emoji\360\237\216\257.py"'),
]
EXPECTED = {"plain.py", "café.py", "with space.py", 'quote".py', "emoji🎯.py"}


def _multi() -> str:
    return "".join(
        f"diff --git {old} {new}\n--- {old}\n+++ {new}\n@@ -1,1 +1,1 @@\n x\n"
        for old, new in HEADER_PAIRS
    )


def test_a_diff_with_many_paths_parses_every_one():
    assert set(changed_paths(_multi())) == EXPECTED


def test_scope_keys_and_hunk_keys_agree_for_every_shape():
    """The bug this closes: read_file looks hunk ranges up by the scope key, so
    a disagreement silently costs a large file its hunk-centred read."""
    diff = _multi()
    assert set(changed_paths(diff)) == set(hunk_ranges(diff)) == EXPECTED
    assert {i.path for i in post_images(diff)} == EXPECTED


def test_a_deleted_non_ascii_file_is_still_recognised():
    diff = ('diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n'
            '--- "a/caf\\303\\251.py"\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-x\n')
    assert changed_paths(diff) == {"café.py": "deleted"}


def test_an_unterminated_quote_does_not_raise():
    assert isinstance(_header_path('"b/broken\\303'), str)


def test_an_unknown_escape_is_kept_literally():
    assert _header_path(r'"b/odd\zchar.py"') == "b/oddzchar.py"


def test_invalid_utf8_bytes_round_trip_instead_of_raising():
    """A filename that is not valid UTF-8 must not crash the parse."""
    out = _header_path(r'"b/bad\377name.py"')
    assert isinstance(out, str) and "name.py" in out
