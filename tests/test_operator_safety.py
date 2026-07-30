"""Safety: host classification + the allow / dry-run / destructive gate."""

from vera.operator import safety as S


def test_host_of():
    assert S.host_of("http://localhost:8998/x") == "localhost"
    assert S.host_of("example.com/path") == "example.com"
    assert S.host_of("") == ""


def test_is_local_host():
    assert S.is_local_host("localhost")
    assert S.is_local_host("127.0.0.1")
    assert S.is_local_host("192.168.0.5")
    assert S.is_local_host("host.docker.internal")
    assert not S.is_local_host("example.com")


def test_local_target_allows_mutation():
    pol = S.SafetyPolicy.for_target("sandbox")
    d = S.evaluate(pol, "http://localhost:8998", "click", {"ref": "e1"})
    assert d["allowed"] and not d["dry_run"]


def test_readonly_action_always_allowed():
    pol = S.SafetyPolicy.for_target("url", base_url="https://example.com")
    d = S.evaluate(pol, "https://example.com", "scroll", {"dy": 400})
    assert d["allowed"] and not d["dry_run"]


def test_external_mutation_blocked_by_default():
    pol = S.SafetyPolicy.for_target("url", base_url="https://example.com")
    d = S.evaluate(pol, "https://example.com", "click", {"ref": "e1"})
    assert not d["allowed"]


def test_external_allowlisted_executes():
    # Allowlisting a host is the permission to operate it — no second gate.
    pol = S.SafetyPolicy.for_target("url", base_url="https://example.com",
                                    allowlist=["example.com"])
    d = S.evaluate(pol, "https://example.com", "click", {"ref": "e1"})
    assert d["allowed"] and not d["dry_run"]


def test_dry_run_previews_even_when_allowed():
    pol = S.SafetyPolicy.for_target("url", base_url="https://example.com",
                                    allowlist=["example.com"], dry_run=True)
    d = S.evaluate(pol, "https://example.com", "type", {"text": "hi"})
    assert d["allowed"] and d["dry_run"]


def test_goto_checks_destination_host():
    pol = S.SafetyPolicy.for_target("sandbox")  # local, permissive
    d = S.evaluate(pol, "http://localhost:8998", "goto", {"url": "https://evil.example"})
    assert not d["allowed"]  # navigating OFF to a non-allowlisted external host
