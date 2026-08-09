"""Unit tests for enroll.guest's SSH sudo-wrap — lets a non-root cloud-init user
(e.g. 'vera' on a provisioned VM) run the root-requiring enrol script via sudo."""
import base64

# lowercase `vera.*` path so it resolves to THIS worktree's app-free core (not the
# main checkout) and needs no app dependencies — see enroll_core.py header.
from vera.provisioning.enroll_core import _ssh_enrol_cmd


def test_root_runs_directly():
    assert _ssh_enrol_cmd("echo hi", "root") == "echo hi"
    assert _ssh_enrol_cmd("echo hi", "") == "echo hi"      # default = root


def test_nonroot_sudo_wraps():
    c = _ssh_enrol_cmd("echo hi; whoami", "vera")
    assert "sudo -n bash" in c
    assert "base64 -d" in c
    assert base64.b64encode(b"echo hi; whoami").decode() in c


def test_nonroot_preserves_arbitrary_script_via_b64():
    script = "set -e\ninstall -d -m700 /root/.ssh\necho 'a  b' > /x\n"
    c = _ssh_enrol_cmd(script, "debian")
    blob = c.split("echo ", 1)[1].split(" |", 1)[0]
    assert base64.b64decode(blob).decode() == script
