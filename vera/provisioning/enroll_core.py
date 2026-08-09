"""
enroll_core.py — pure, dependency-free enrolment helpers
========================================================

`_ssh_enrol_cmd` decides how the (root-requiring) enrolment script is invoked over
SSH: a non-root SSH user (e.g. cloud-init's default 'vera' on a provisioned VM,
which has passwordless sudo) runs it via `sudo -n`; root runs it directly. It's
pure (base64 only), so it lives here — app-free — and can be unit-tested via the
lowercase `vera.provisioning.enroll_core` path without booting the orchestrator.
enroll_capabilities.py imports it from here so there is ONE implementation.
"""
from __future__ import annotations

import base64


def _ssh_enrol_cmd(base_script: str, ssh_user: str) -> str:
    """Wrap the (root-requiring) enrol script for the SSH path: a non-root SSH user
    (e.g. cloud-init's default 'vera' on a provisioned VM, which has passwordless
    sudo) runs it via `sudo -n`; root runs it directly. base64 so arbitrary script
    content survives the sudo shell."""
    if (ssh_user or "root") == "root":
        return base_script
    b = base64.b64encode(base_script.encode()).decode()
    return f"echo {b} | base64 -d | sudo -n bash"
