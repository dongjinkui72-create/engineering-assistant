"""Sheet metal: simple base flange + upstand flanges + unfold (K-factor)."""
from __future__ import annotations

import math

K_FACTOR_DEFAULT = 0.44


def bend_allowance(angle_deg: float, radius: float, thickness: float,
                   k: float = K_FACTOR_DEFAULT) -> float:
    """Bend allowance BA = angle(rad) * (R + K*T)."""
    return math.radians(angle_deg) * (radius + k * thickness)


def unfold_size(L: float, W: float, t: float, flange_h: float, radius: float,
                angle_deg: float = 90.0, k: float = K_FACTOR_DEFAULT):
    """Return (blank_length, blank_width) for a U-channel formed on the two long edges."""
    ba = bend_allowance(angle_deg, radius, t, k)
    leg = max(flange_h - t - radius, 0.0)
    blank_w = 2 * (leg + ba) + W
    blank_l = L
    return (blank_l, blank_w)


def sheet_volume(L: float, W: float, t: float, flange_h: float) -> float:
    """Base plate + two upstand flanges (sharp-corner geometric equivalence)."""
    return L * W * t + 2.0 * (max(flange_h - t, 0.0) * t * L)


def u_channel_profile(L: float, W: float, t: float, flange_h: float):
    """Closed polygon of the U section in the W-Z plane (for revolve/loft/extrude)."""
    # outer path: (z, w) pairs, counter-clockwise
    return [(-W / 2, 0.0), (W / 2, 0.0), (W / 2, flange_h),
            (W / 2 - t, flange_h), (W / 2 - t, t), (-W / 2 + t, t),
            (-W / 2 + t, flange_h), (-W / 2, flange_h)]