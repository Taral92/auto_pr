"""Regression: PostImage.locate must not raise on a malformed PostImage.

Found by the agent itself on PR #3. Unreachable via post_images(), because
splitlines() guarantees no line contains a newline — but the invariant was
unstated and load-bearing.
"""

import random

from core.diff import PostImage, post_images


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
