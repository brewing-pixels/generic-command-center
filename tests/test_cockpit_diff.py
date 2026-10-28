from __future__ import annotations

from command_center.cockpit.diff import parse_patch, side_rows


def test_parse_and_pair_unified_diff() -> None:
    patch = """diff --git a/src/x.py b/src/x.py
--- a/src/x.py
+++ b/src/x.py
@@ -1,3 +1,4 @@
 old
-remove
+add one
+add two
 tail
"""
    files = parse_patch(patch)
    assert len(files) == 1
    file = files[0]
    assert file.display_name == "src/x.py"
    assert file.additions == 2
    assert file.deletions == 1
    rows = side_rows(file.hunks[0])
    assert rows[0].kind == "hunk"
    rendered = [(row.left, row.right, row.kind) for row in rows[1:]]
    assert rendered == [
        ("old", "old", "context"),
        ("remove", "add one", "replace"),
        ("", "add two", "add"),
        ("tail", "tail", "context"),
    ]


def test_parse_new_file_and_binary() -> None:
    patch = """diff --git a/hello.txt b/hello.txt
new file mode 100644
--- /dev/null
+++ b/hello.txt
@@ -0,0 +1 @@
+hello
diff --git a/logo.png b/logo.png
Binary files a/logo.png and b/logo.png differ
"""
    files = parse_patch(patch)
    assert [file.display_name for file in files] == ["hello.txt", "logo.png"]
    assert files[0].old_path == "/dev/null"
    assert files[1].binary is True
    assert files[0].additions == 1
