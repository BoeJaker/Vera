"""intel's modes as switchboard switches (four-minds FM-13.3): a set switch maps onto intel's own config key; a switch
at its default changes nothing."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.intel import intel_switches_core as ISW  # noqa: E402
from vera.minds import switchboard_core as SB  # noqa: E402

pytestmark = pytest.mark.critical


def test_switches_at_their_default_change_nothing():
    modes = {"intel.nlp_triage": "off", "intel.brief": "extractive", "intel.calibrated_gate": "off"}
    assert ISW.overrides(modes, {}) == {}
    assert ISW.overrides(modes, {s: "default" for s in ISW.SWITCHES}) == {}


def test_each_set_switch_maps_onto_its_config_key():
    src = {s: "global" for s in ISW.SWITCHES}
    assert ISW.overrides({"intel.nlp_triage": "off", "intel.brief": "extractive", "intel.auto_research": "on",
                          "intel.material_alerts": "off", "intel.calibrated_gate": "on"}, src) == {
        "nlp": False, "brief_synthesize": False, "auto_research": True, "alerts": False,
        "calibrated_gate": True, "calibration_off": False}
    assert ISW.overrides({"intel.calibrated_gate": "shadow"}, src) == {"calibrated_gate": False,
                                                                       "calibration_off": False}
    assert ISW.overrides({"intel.calibrated_gate": "off"}, src)["calibration_off"] is True
    assert ISW.overrides({"intel.brief": "nonsense"}, src) == {}


def test_the_switches_are_registered_with_todays_behaviour_as_default():
    want = {"intel.nlp_triage": "on", "intel.brief": "llm", "intel.auto_research": "off",
            "intel.material_alerts": "on", "intel.calibrated_gate": "shadow"}
    assert set(want) == set(ISW.SWITCHES)
    for sid, default in want.items():
        f = SB.REGISTRY[sid]
        assert f.default == default and f.package == "FM-13.3" and f.surface == "intel" and f.owner == "intelligence"
