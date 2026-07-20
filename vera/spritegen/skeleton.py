"""skeleton.py — procedural OpenPose skeletons for animation cycles (pure PIL)
==============================================================================
Text prompts alone can't make SD move a character's arms and legs consistently
frame to frame — CLIP simply doesn't encode limb angles. This module draws
OpenPose-format control images (the coloured 18-keypoint stick figure ControlNet
openpose was trained on) for parametric locomotion cycles, so the pose of every
limb is *dictated* per frame instead of hoped for:

  walk / run — side view, facing right (matches the sheet-flip convention)
  idle       — front view, subtle breathing bob
  jump       — front view, crouch → launch → tuck → land
  attack     — side view, windup → overhead raise → strike lunge → recover
  cast       — side view, gather → arms high → forward thrust → settle
  hurt       — side view, recoil back → doubled over → part-recover
  death      — side view, struck → stagger → collapse to the ground

`render_pose(anim, index, total, width, height)` returns the control PNG for one
frame; `supports(anim)` says whether a cycle exists. Everything is deterministic
(same inputs → same skeleton), which keeps seed-locked diffusion stable.

Keypoint order (BODY_18 / COCO): 0 nose, 1 neck, 2 Rsho, 3 Relb, 4 Rwri,
5 Lsho, 6 Lelb, 7 Lwri, 8 Rhip, 9 Rkne, 10 Rank, 11 Lhip, 12 Lkne, 13 Lank,
14 Reye, 15 Leye, 16 Rear, 17 Lear.
"""

from __future__ import annotations

import base64
import io
import math
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageDraw

# Standard OpenPose limb connections + colours (RGB).
_LIMBS = [(1, 2), (1, 5), (2, 3), (3, 4), (5, 6), (6, 7), (1, 8), (8, 9),
          (9, 10), (1, 11), (11, 12), (12, 13), (1, 0), (0, 14), (14, 15),
          (0, 16), (16, 17)]
_COLORS = [(255, 0, 0), (255, 85, 0), (255, 170, 0), (255, 255, 0),
           (170, 255, 0), (85, 255, 0), (0, 255, 0), (0, 255, 85),
           (0, 255, 170), (0, 255, 255), (0, 170, 255), (0, 85, 255),
           (0, 0, 255), (85, 0, 255), (170, 0, 255), (255, 0, 255),
           (255, 0, 170), (255, 0, 85)]

# Segment lengths as fractions of total body height (rough human proportions,
# slightly leggy so pixelated results still read).
_L = {
    "head":      0.115,   # neck → nose
    "eye":       0.035,
    "torso":     0.28,    # neck → hip centre
    "shoulder":  0.10,    # neck → shoulder (half width)
    "hip":       0.055,   # hip centre → hip (half width)
    "upper_arm": 0.16,
    "fore_arm":  0.145,
    "thigh":     0.24,
    "shin":      0.225,
}

SKELETON_ANIMS = ("walk", "run", "idle", "jump", "attack", "cast", "hurt", "death")


def supports(anim: str) -> bool:
    return anim in SKELETON_ANIMS


def _polar(origin: Tuple[float, float], angle: float, length: float) -> Tuple[float, float]:
    """Point at `angle` (radians, 0 = straight DOWN, positive = toward +x/front)
    and `length` from origin. Y grows downward (image space)."""
    return (origin[0] + length * math.sin(angle),
            origin[1] + length * math.cos(angle))


def _leg(hip, thigh_angle, knee_bend):
    """Knee + ankle from a hip: thigh_angle swings the whole leg (0 = vertical,
    + = forward), knee_bend folds the shin BACKWARD relative to the thigh."""
    knee = _polar(hip, thigh_angle, _L["thigh"])
    ankle = _polar(knee, thigh_angle - knee_bend, _L["shin"])
    return knee, ankle


def _arm(shoulder, swing, elbow_bend):
    """Elbow + wrist from a shoulder: swing rotates the upper arm (0 = hanging,
    + = forward), elbow_bend folds the forearm FORWARD."""
    elbow = _polar(shoulder, swing, _L["upper_arm"])
    wrist = _polar(elbow, swing + elbow_bend, _L["fore_arm"])
    return elbow, wrist


def _side_cycle(t: float, run: bool) -> List[Optional[Tuple[float, float]]]:
    """One phase of a side-view (facing right, +x = forward) walk/run cycle.
    Returns 18 keypoints in normalised body space (hip centre near origin)."""
    two_pi = 2 * math.pi
    # Phase-shift so frame 0 is the CONTACT pose (front leg extended) — that is
    # what the pose-text prompts describe for frame 0, and the classic cycle key.
    t = t + 0.25
    # Amplitudes: run swings harder, leans forward, and gets airborne.
    leg_amp = math.radians(52 if run else 32)
    arm_amp = math.radians(48 if run else 26)
    lean = math.radians(14 if run else 4)
    # Vertical bob, 2× stride frequency, peaking at the PASSING pose (body
    # rides highest over the planted leg); runs add flight-phase lift.
    bob = (0.030 if run else 0.014) * (1 + math.cos(2 * two_pi * t)) / 2
    hipc_y = -(_L["thigh"] + _L["shin"]) - bob
    hipc = (0.0, hipc_y)
    # up = angle π; π - lean tips the torso FORWARD (+x, the facing direction)
    neck = _polar(hipc, math.pi - lean, _L["torso"])
    nose = _polar(neck, math.pi - lean * 1.4, _L["head"])

    # Legs — opposite phases; knees fold most as the leg passes underneath.
    swing_r = leg_amp * math.sin(two_pi * t)
    swing_l = leg_amp * math.sin(two_pi * t + math.pi)
    base_bend = math.radians(24 if run else 8)
    bend_r = base_bend + math.radians(78 if run else 46) * max(0.0, -math.cos(two_pi * t))
    bend_l = base_bend + math.radians(78 if run else 46) * max(0.0, math.cos(two_pi * t))
    # Slight depth offset so both legs stay visible in profile.
    depth = 0.02
    hip_r = (hipc[0] + depth, hipc[1])
    hip_l = (hipc[0] - depth, hipc[1])
    knee_r, ank_r = _leg(hip_r, swing_r, bend_r)
    knee_l, ank_l = _leg(hip_l, swing_l, bend_l)

    # Arms — counter-phase to the same-side leg; runners keep elbows folded.
    sho_r = (neck[0] + depth, neck[1] + 0.01)
    sho_l = (neck[0] - depth, neck[1] + 0.01)
    elbow_fold = math.radians(95 if run else 24)
    elb_r, wri_r = _arm(sho_r, arm_amp * math.sin(two_pi * t + math.pi), elbow_fold)
    elb_l, wri_l = _arm(sho_l, arm_amp * math.sin(two_pi * t), elbow_fold)

    # Profile head: one eye/ear toward the camera, nose pushed forward.
    eye = (nose[0] + 0.01, nose[1] - 0.01)
    ear = (nose[0] - _L["eye"], nose[1] - 0.005)
    return [nose, neck, sho_r, elb_r, wri_r, sho_l, elb_l, wri_l,
            hip_r, knee_r, ank_r, hip_l, knee_l, ank_l,
            eye, None, ear, None]


def _idle_cycle(t: float) -> List[Optional[Tuple[float, float]]]:
    """Front view, subtle breathing: shoulders/head rise a touch mid-cycle."""
    two_pi = 2 * math.pi
    breathe = 0.008 * (1 - math.cos(two_pi * t)) / 2
    hipc = (0.0, -(_L["thigh"] + _L["shin"]))
    neck = (0.0, hipc[1] - _L["torso"] - breathe)
    nose = (0.0, neck[1] - _L["head"])
    sho_r = (-_L["shoulder"], neck[1] + 0.01 - breathe / 2)   # right = viewer-left (-x)
    sho_l = (_L["shoulder"], neck[1] + 0.01 - breathe / 2)
    sway = math.radians(3) * math.sin(two_pi * t)
    elb_r, wri_r = _arm(sho_r, -math.radians(4) + sway, math.radians(6))
    elb_l, wri_l = _arm(sho_l, math.radians(4) + sway, -math.radians(6))
    hip_r = (-_L["hip"], hipc[1])
    hip_l = (_L["hip"], hipc[1])
    knee_r, ank_r = _leg(hip_r, -math.radians(3), math.radians(4))
    knee_l, ank_l = _leg(hip_l, math.radians(3), math.radians(4))
    eye_r = (nose[0] - _L["eye"], nose[1] - 0.01)
    eye_l = (nose[0] + _L["eye"], nose[1] - 0.01)
    ear_r = (nose[0] - 2 * _L["eye"], nose[1])
    ear_l = (nose[0] + 2 * _L["eye"], nose[1])
    return [nose, neck, sho_r, elb_r, wri_r, sho_l, elb_l, wri_l,
            hip_r, knee_r, ank_r, hip_l, knee_l, ank_l,
            eye_r, eye_l, ear_r, ear_l]


def _jump_cycle(t: float) -> List[Optional[Tuple[float, float]]]:
    """Front view jump arc: crouch → launch (arms up) → apex tuck → land."""
    # Phase envelope: 0-.2 crouch, .2-.45 extend, .45-.7 tuck, .7-1 land.
    if t < 0.2:
        crouch, lift, arms = t / 0.2, 0.0, -0.4
    elif t < 0.45:
        p = (t - 0.2) / 0.25
        crouch, lift, arms = 1 - p, 0.30 * p, -0.4 + 1.6 * p
    elif t < 0.7:
        p = (t - 0.45) / 0.25
        crouch, lift, arms = 0.55 * p, 0.30 - 0.05 * p, 1.2
    else:
        p = (t - 0.7) / 0.3
        crouch, lift, arms = 0.55 * (1 - p) + 0.35 * (1 - abs(2 * p - 1)), 0.30 * (1 - p), 1.2 - 1.6 * p
    knee_bend = math.radians(80) * crouch + math.radians(8)
    leg_short = (_L["thigh"] + _L["shin"]) * (1 - 0.22 * crouch)
    hipc = (0.0, -leg_short - lift)
    neck = (0.0, hipc[1] - _L["torso"])
    nose = (0.0, neck[1] - _L["head"])
    sho_r = (-_L["shoulder"], neck[1] + 0.01)
    sho_l = (_L["shoulder"], neck[1] + 0.01)
    # arms: -0.4 = swept back, 1.2 ≈ raised overhead (angle from hanging).
    a = math.radians(150) * max(-0.5, min(1.2, arms))
    elb_r, wri_r = _arm(sho_r, -a * 0.6, -a * 0.5)
    elb_l, wri_l = _arm(sho_l, a * 0.6, a * 0.5)
    hip_r = (-_L["hip"], hipc[1])
    hip_l = (_L["hip"], hipc[1])
    knee_r, ank_r = _leg(hip_r, -math.radians(10) * crouch, knee_bend)
    knee_l, ank_l = _leg(hip_l, math.radians(10) * crouch, knee_bend)
    eye_r = (nose[0] - _L["eye"], nose[1] - 0.01)
    eye_l = (nose[0] + _L["eye"], nose[1] - 0.01)
    ear_r = (nose[0] - 2 * _L["eye"], nose[1])
    ear_l = (nose[0] + 2 * _L["eye"], nose[1])
    return [nose, neck, sho_r, elb_r, wri_r, sho_l, elb_l, wri_l,
            hip_r, knee_r, ank_r, hip_l, knee_l, ank_l,
            eye_r, eye_l, ear_r, ear_l]


def _lerp_keys(keys: List[tuple], t: float) -> List[float]:
    """Piecewise-linear interpolation across a list of keypose tuples, t∈[0,1]."""
    m = len(keys)
    pos = max(0.0, min(1.0, t)) * (m - 1)
    i = min(int(pos), m - 2)
    f = pos - i
    return [a + (b - a) * f for a, b in zip(keys[i], keys[i + 1])]


def _action_pose(lean, arm_swing, elbow_bend, off_arm, front_thigh, front_bend,
                 back_thigh, back_bend, crouch) -> List[Optional[Tuple[float, float]]]:
    """Side-view (facing right) action pose from keypose parameters. The RIGHT
    arm is the acting limb (near side, drawn at +depth like _side_cycle); the
    LEFT leg is the front leg. Angles in radians (0 = hanging/vertical,
    + = forward/+x); crouch ∈ [0,1] sinks the hips and bends both knees."""
    depth = 0.02
    leg_len = (_L["thigh"] + _L["shin"]) * (1 - 0.30 * crouch)
    hipc = (0.0, -leg_len)
    neck = _polar(hipc, math.pi + lean, _L["torso"])
    nose = _polar(neck, math.pi + lean * 1.4, _L["head"])
    sho_r = (neck[0] + depth, neck[1] + 0.01)
    sho_l = (neck[0] - depth, neck[1] + 0.01)
    elb_r, wri_r = _arm(sho_r, arm_swing, elbow_bend)
    elb_l, wri_l = _arm(sho_l, off_arm, math.radians(20))
    hip_r = (depth, hipc[1])
    hip_l = (-depth, hipc[1])
    crouch_bend = math.radians(55) * crouch
    knee_l, ank_l = _leg(hip_l, front_thigh, front_bend + crouch_bend)
    knee_r, ank_r = _leg(hip_r, back_thigh, back_bend + crouch_bend)
    eye = (nose[0] + 0.01, nose[1] - 0.01)
    ear = (nose[0] - _L["eye"], nose[1] - 0.005)
    return [nose, neck, sho_r, elb_r, wri_r, sho_l, elb_l, wri_l,
            hip_r, knee_r, ank_r, hip_l, knee_l, ank_l,
            eye, None, ear, None]


# Keyposes: (lean, arm_swing, elbow_bend, off_arm, front_thigh, front_bend,
#            back_thigh, back_bend, crouch) — see _action_pose.
_ATTACK_KEYS = [
    (-0.05,  0.30, 0.40, -0.20, 0.10, 0.15, -0.10, 0.10, 0.06),   # ready
    (-0.18, -1.90, 1.30, -0.50, 0.05, 0.25, -0.20, 0.10, 0.14),   # windup, coiled back
    (-0.10, -2.90, 0.55, -0.40, 0.15, 0.20, -0.25, 0.08, 0.06),   # weapon raised high
    ( 0.30,  1.05, 0.15,  0.35, 0.55, 0.45, -0.55, 0.05, 0.12),   # strike, lunging
    ( 0.22,  1.35, 0.05,  0.30, 0.50, 0.40, -0.50, 0.05, 0.14),   # full extension
    ( 0.05,  0.60, 0.35,  0.00, 0.15, 0.20, -0.15, 0.10, 0.06),   # recover
]
_CAST_KEYS = [
    (-0.05, -0.60, 0.80, -0.80, 0.08, 0.12, -0.08, 0.10, 0.10),   # gather low
    (-0.12, -2.20, 0.40, -2.00, 0.05, 0.15, -0.12, 0.10, 0.05),   # arms rising
    (-0.15, -3.00, 0.15, -2.90, 0.05, 0.12, -0.12, 0.08, 0.02),   # peak, arms high
    ( 0.22,  1.45, 0.10,  1.30, 0.40, 0.30, -0.40, 0.06, 0.08),   # thrust forward
    ( 0.10,  1.00, 0.25,  0.85, 0.20, 0.20, -0.20, 0.08, 0.06),   # release
    ( 0.00,  0.20, 0.30,  0.10, 0.08, 0.12, -0.08, 0.10, 0.04),   # settle
]
_HURT_KEYS = [
    (-0.30,  0.90, 0.70,  0.70, 0.15, 0.25, -0.20, 0.15, 0.10),   # head snaps back
    (-0.10,  0.50, 1.30,  0.40, 0.10, 0.40, -0.10, 0.30, 0.30),   # doubled over
    (-0.05,  0.20, 0.50,  0.10, 0.08, 0.15, -0.08, 0.12, 0.10),   # regaining footing
]
_DEATH_KEYS = [
    (-0.25,  0.90, 0.50,  0.70, 0.15, 0.20, -0.15, 0.15, 0.08),   # struck, jolting
    (-0.50,  0.60, 0.80,  0.40, 0.25, 0.60, -0.25, 0.50, 0.30),   # knees buckling
    (-0.90, -0.40, 0.90,  1.40, 0.45, 1.10, -0.40, 0.90, 0.55),   # falling backward
    (-1.25,  0.30, 0.40,  0.60, 0.70, 1.50, -0.55, 1.30, 0.80),   # crumpling down
    (-1.35,  0.15, 0.20,  0.30, 0.85, 1.70, -0.65, 1.50, 0.88),   # on the ground
]


def _cycle_points(anim: str, t: float) -> List[Optional[Tuple[float, float]]]:
    if anim == "walk":
        return _side_cycle(t, run=False)
    if anim == "run":
        return _side_cycle(t, run=True)
    if anim == "idle":
        return _idle_cycle(t)
    if anim == "jump":
        return _jump_cycle(t)
    keys = {"attack": _ATTACK_KEYS, "cast": _CAST_KEYS,
            "hurt": _HURT_KEYS, "death": _DEATH_KEYS}.get(anim)
    if keys:
        return _action_pose(*_lerp_keys(keys, t))
    raise ValueError(f"no skeleton cycle for {anim!r}")


def render_pose(anim: str, index: int, total: int,
                width: int = 768, height: int = 768) -> Image.Image:
    """OpenPose control image for frame `index` of `total` in `anim`'s cycle.
    Black background, coloured limbs/joints — feed straight to ControlNet."""
    total = max(1, int(total))
    # Looping cycles sample [0,1); one-shot arcs (jump) sample [0,1] inclusive.
    if anim in ("walk", "run", "idle"):
        t = (index % total) / total
    else:
        t = index / max(1, total - 1) if total > 1 else 0.0
    pts = _cycle_points(anim, t)

    # Body-space → pixels: character fills ~78% of the image height (ControlNet
    # guides best with a large figure), feet at ~94%, hips on the centre line.
    body = _L["thigh"] + _L["shin"] + _L["torso"] + _L["head"]
    scale = height * 0.78 / (body + 0.12)
    ox, oy = width / 2.0, height * 0.94

    def px(p):
        return (ox + p[0] * scale, oy + p[1] * scale)

    img = Image.new("RGB", (int(width), int(height)), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    lw = max(3, int(min(width, height) / 110))
    for (a, b), col in zip(_LIMBS, _COLORS[:len(_LIMBS)]):
        pa, pb = pts[a], pts[b]
        if pa is None or pb is None:
            continue
        draw.line([px(pa), px(pb)], fill=col, width=lw)
    r = max(3, int(min(width, height) / 130))
    for p, col in zip(pts, _COLORS):
        if p is None:
            continue
        x, y = px(p)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=col)
    return img


def render_pose_b64(anim: str, index: int, total: int,
                    width: int = 768, height: int = 768) -> str:
    buf = io.BytesIO()
    render_pose(anim, index, total, width, height).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()
