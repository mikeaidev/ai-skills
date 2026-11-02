# SPDX-License-Identifier: MIT
"""Rational media timestamps and conservative sub-frame rounding allowances."""
from fractions import Fraction
import statistics


def fraction(value):
    try:
        result = Fraction(str(value))
        return result
    except (ValueError, TypeError, ZeroDivisionError):
        return None


def time_base(stream):
    value = fraction(stream.get("time_base"))
    return value if value is not None and value > 0 else None


def packet_time(packet, field, stream):
    """Prefer integer ticks; ffprobe's printed decimal time can lose precision."""
    base = time_base(stream)
    value = packet.get(field)
    if base is not None and value is not None:
        try:
            return int(value) * base
        except (TypeError, ValueError, OverflowError):
            pass
    return fraction(packet.get(field + "_time"))


def nominal_duration(stream, packets):
    durations = [packet_time(p, "duration", stream) for p in packets]
    positive = [d for d in durations if d is not None and d > 0]
    if positive:
        return statistics.median(positive), "native_packet_duration"
    if stream.get("codec_type") == "video":
        for field in ("avg_frame_rate", "r_frame_rate"):
            rate = fraction(stream.get(field))
            if rate is not None and rate > 0:
                return 1 / rate, field
    pts = sorted(set(x for p in packets if (x := packet_time(p, "pts", stream)) is not None))
    steps = [b - a for a, b in zip(pts, pts[1:]) if b > a]
    return (statistics.median(steps), "inferred_pts_step") if steps else (None, "unknown")


def rounding_allowance(stream, nominal=None):
    """Never let coarse ticks erase a whole nominal frame or packet interval.

    Fine-container rounding gets up to two ticks, bounded by one sixteenth of
    the native interval. Unknown cadence gets only a tiny decimal-print floor;
    callers must report duration uncertainty separately rather than widen it.
    """
    floor = Fraction(2, 1_000_000)
    if nominal is None or nominal <= 0:
        return floor
    base = time_base(stream) or Fraction(0)
    return min(max(2 * base, floor), nominal / 16)


def timing_policy(stream, packets):
    nominal, basis = nominal_duration(stream, packets)
    allowance = rounding_allowance(stream, nominal)
    return {
        "nominal": nominal,
        "nominal_basis": basis,
        "allowance": allowance,
        "time_base": time_base(stream),
    }
