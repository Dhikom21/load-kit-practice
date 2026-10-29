"""Daily patterns: a multiplier over a (compressed) day.  hour 0..24 -> factor (1.0 = base level)."""

# (hour, factor) points; linear interpolation in between, wraps at 24 h
PATTERNS = {
    "flat": [(0, 1.0), (24, 1.0)],
    "office-day": [(0, 0.3), (6, 0.3), (8, 0.9), (10, 1.2), (12, 1.0), (13, 0.85), (15, 1.1),
                   (17, 0.9), (19, 0.5), (22, 0.35), (24, 0.3)],
    "evening-peak": [(0, 0.4), (7, 0.5), (12, 0.8), (17, 1.0), (19, 1.8), (21, 1.6), (23, 0.7), (24, 0.4)],
    "night-heavy": [(0, 1.5), (4, 1.5), (6, 0.6), (18, 0.6), (22, 1.2), (24, 1.5)],
}
PATTERNS["office"] = PATTERNS["office-day"]
PATTERNS["evening"] = PATTERNS["evening-peak"]


def names():
    return sorted(PATTERNS)


def factor(pattern, t, day_length, day_start_hour=0.0):
    """Multiplier at scenario time t (s). The 24 h pattern is squeezed into `day_length` seconds."""
    pts = PATTERNS[pattern]
    hour = (day_start_hour + 24.0 * (t / day_length)) % 24.0
    for (h0, f0), (h1, f1) in zip(pts, pts[1:]):
        if h0 <= hour <= h1:
            return f0 + (f1 - f0) * ((hour - h0) / (h1 - h0) if h1 > h0 else 0.0)
    return pts[-1][1]


def hour_of(t, day_length, day_start_hour=0.0):
    return (day_start_hour + 24.0 * (t / day_length)) % 24.0
