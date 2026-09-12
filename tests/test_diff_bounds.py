"""Regression: PostImage.locate must not raise on a malformed PostImage.

Found by the agent itself on PR #3. Unreachable via post_images(), because
splitlines() guarantees no line contains a newline — but the invariant was
unstated and load-bearing.
"""

import random

from core.diff import PostImage, changed_paths, hunk_ranges, post_images

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
