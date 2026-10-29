"""Parse the small units used in scenario files: durations, CPU, memory, sizes, percentages.

    duration("30m") -> 1800.0      duration("1.2s") -> 1.2     duration("2h") -> 7200.0
    cpu("100m") -> 0.1             cpu(1.5) -> 1.5
    memory("256Mi") -> 268435456   memory("1Gi") -> 1073741824
    percent("10%") -> 0.10
"""
import re

_DUR = {"ms": 0.001, "s": 1.0, "m": 60.0, "min": 60.0, "h": 3600.0, "d": 86400.0}
_MEM = {"": 1, "B": 1, "K": 1000, "M": 1000 ** 2, "G": 1000 ** 3, "T": 1000 ** 4,
        "Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4}


class UnitError(ValueError):
    pass


def duration(v):
    """Seconds (float). Accepts numbers (already seconds) or strings like '90s', '30m', '2h', '1h30m'."""
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    parts = re.findall(r"(\d+(?:\.\d+)?)\s*(ms|min|s|m|h|d)", s)
    if not parts or "".join(a + b for a, b in parts) != s.replace(" ", ""):
        raise UnitError(f"not a duration: {v!r} (use e.g. 90s, 30m, 2h)")
    return sum(float(a) * _DUR[b] for a, b in parts)


def cpu(v):
    """Cores (float). '100m' = 0.1 core, '1.5' or 1.5 = 1.5 cores."""
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if s.endswith("m"):
        return float(s[:-1]) / 1000.0
    try:
        return float(s)
    except ValueError:
        raise UnitError(f"not a CPU amount: {v!r} (use e.g. 100m or 1.5)")


def memory(v):
    """Bytes (float). Accepts Ki/Mi/Gi (binary) and K/M/G (decimal)."""
    if isinstance(v, (int, float)):
        return float(v)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(Ki|Mi|Gi|Ti|K|M|G|T|B)?\s*", str(v))
    if not m:
        raise UnitError(f"not a memory amount: {v!r} (use e.g. 256Mi or 1Gi)")
    return float(m.group(1)) * _MEM[m.group(2) or ""]


def rate_per_minute(v):
    """Memory leak like '5Mi/min' -> bytes per second."""
    m = re.fullmatch(r"\s*(.+?)\s*/\s*(s|min|h)\s*", str(v))
    if not m:
        raise UnitError(f"not a rate: {v!r} (use e.g. 5Mi/min)")
    return memory(m.group(1)) / {"s": 1.0, "min": 60.0, "h": 3600.0}[m.group(2)]


def percent(v):
    """Fraction. '10%' -> 0.1; 0.1 -> 0.1."""
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if s.endswith("%"):
        return float(s[:-1]) / 100.0
    return float(s)


def fmt_duration(sec):
    sec = int(round(sec))
    sign = "-" if sec < 0 else ""
    sec = abs(sec)
    return f"{sign}{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def fmt_mem(b):
    for unit, size in (("Gi", 1024 ** 3), ("Mi", 1024 ** 2), ("Ki", 1024)):
        if abs(b) >= size:
            return f"{b / size:.1f}{unit}"
    return f"{b:.0f}B"
