"""The changed-file manifest. Without it the model greps for what changed and
gets added-vs-modified wrong."""

from agent.nodes import changed_files

DIFF = """diff --git a/agent/tools.py b/agent/tools.py
index 111..222 100644
--- a/agent/tools.py
+++ b/agent/tools.py
@@ -1,2 +1,2 @@
-old
+new
diff --git a/agent/brand_new.py b/agent/brand_new.py
new file mode 100644
--- /dev/null
+++ b/agent/brand_new.py
@@ -0,0 +1,2 @@
+import os
+os.system("rm -rf /")
diff --git a/agent/gone.py b/agent/gone.py
deleted file mode 100644
--- a/agent/gone.py
+++ /dev/null
@@ -1,1 +0,0 @@
-print("bye")
"""


def test_detects_all_three_statuses():
    m = changed_files(DIFF)
    assert m == [
        "  agent/tools.py (modified)",
        "  agent/brand_new.py (added)",
        "  agent/gone.py (deleted)",
    ]


def test_added_file_is_in_the_manifest():
    """The last run wrongly concluded added files were out of scope."""
    assert any("brand_new.py (added)" in line for line in changed_files(DIFF))


def test_empty_diff():
    assert changed_files("") == []
