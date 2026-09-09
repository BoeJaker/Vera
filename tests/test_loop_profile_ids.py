"""Every loop profile must be reachable, not merely listed.

_BY_ID is a dict comprehension keyed on the profile id, so a duplicate does not
raise - it keeps the LAST declaration and silently removes the earlier one while
`list_profiles()` still returns both. Two profiles were called "operator" (the
container/VM one and the Web one) for the whole life of both, so
`loops.run(profile="operator")` always reached the Web one and the container/VM
profile could not be run at all.

This reads the source rather than importing the module, because importing it
pulls in capability_orchestration and the whole app. The property under test is
a property of the declarations, so the declarations are what it reads.
"""
import os
import re

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "vera", "dag", "loop_profiles.py")
_ID_RE = re.compile(r'^\s{8}"id"\s*:\s*"([a-z0-9_-]+)"', re.M)


def _declared_ids():
    return _ID_RE.findall(open(_SRC, encoding="utf-8").read())


def test_the_source_still_declares_profiles_at_all():
    """Guards the regex itself: if the literal's indentation changes, this file
    would otherwise pass by finding nothing."""
    assert len(_declared_ids()) >= 16


def test_no_profile_id_is_declared_twice():
    ids = _declared_ids()
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert not dupes, ("these ids are declared twice, so only the last is "
                       "reachable while both are listed: %s" % ", ".join(dupes))


def test_both_operator_profiles_are_addressable():
    ids = _declared_ids()
    assert "operator" in ids           # Web Operator keeps the plain name
    assert "operator-infra" in ids     # container/VM, previously shadowed
