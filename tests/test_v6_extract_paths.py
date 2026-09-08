"""A capability name is not a file, and one file is not two.

Both come from the completion gate's path extractor, and both were paid for in
cycles.

`bash` is in the artifact-extension list, so `exec.bash.run` - the CAPABILITY,
named in almost every goal that runs anything - matched the bare-filename
pattern as a file called `exec.bash`. The gate probed it, found it absent (it
was never a file) and appended a whole remediation step: "Create the missing
file: exec.bash". Census runs 35, 38 and 44.

And a goal naming its output twice, once with a directory and once bare, made
the gate append a step for each. Census 45, build-multifile: steps 4 and 5 were
"Create the missing file: /workspace/statkit/stats.py" and "Create the missing
file: stats.py" - the same file. That goal spent 17 cycles on one step, the
worst in the run.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.dag import dag_workshop_capabilities as W  # noqa: E402

pytestmark = pytest.mark.critical


# â”€â”€ a dotted capability name is not a filename â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@pytest.mark.parametrize("text", [
    "use exec.bash.run to run the tests",
    "run the suite with exec.bash.run and report",
    "call exec.python.run then exec.bash.run",
])
def test_capability_names_are_not_extracted_as_files(text):
    assert W._v6_extract_paths(text) == [], text


def test_a_real_bare_filename_is_still_extracted():
    assert "stats.py" in W._v6_extract_paths("create stats.py in the package")


def test_a_filename_at_the_end_of_a_sentence_still_works():
    """The guard must key on a following dotted SEGMENT, not on any dot -
    sentence punctuation is not another extension."""
    assert "stats.py" in W._v6_extract_paths("the deliverable is stats.py.")


def test_several_real_files_still_come_through():
    got = W._v6_extract_paths("write report.md and clock.html")
    assert "report.md" in got and "clock.html" in got


def test_a_workspace_path_is_unaffected():
    got = W._v6_extract_paths("save it at /workspace/statkit/stats.py")
    assert "/workspace/statkit/stats.py" in got


def test_a_double_extension_is_not_truncated_to_the_wrong_file():
    """config.yaml.bak is not config.yaml. Extracting the latter names a file
    that was never asked for."""
    assert "config.yaml" not in W._v6_extract_paths("see config.yaml.bak for the old one")
