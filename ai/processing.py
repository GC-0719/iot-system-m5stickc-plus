#!/usr/bin/env python3
"""
Shared signal-processing core for the AI framework (HW2, requirement 2).

Pure functions over a window of samples. No I/O, no MQTT, no InfluxDB -- so the
*exact same* analysis runs whether the data arrived live over MQTT
(main_ai_mqtt.py) or was read back from storage (main_ai_influx.py). That is the
point of factoring it out: one definition of "RMS", not two that can drift.

Every analysis takes a dict of field_name -> list[float] (one window) and
returns a dict of result_name -> float. The config file (config.yaml) decides
which analyses run on which measurement, so nothing here is hardcoded to imu or
audio.
"""

from __future__ import annotations
import math
from typing import Callable

try:
    import numpy as np
    _HAVE_NP = True
except Exception:            # numpy optional; FFT falls back to a DFT
    _HAVE_NP = False


# ---------------------------------------------------------------- helpers --
def _flatten(window: dict[str, list[float]]) -> list[list[float]]:
    """Return the field series as a list of equal-length lists."""
    return [v for v in window.values() if v]


def _rms_one(series: list[float]) -> float:
    if not series:
        return 0.0
    return math.sqrt(sum(x * x for x in series) / len(series))


# ---------------------------------------------------------------- analyses --
def sma(window: dict[str, list[float]], **_) -> dict[str, float]:
    """Signal Magnitude Area: mean over time of (|f1| + |f2| + ... ) across the
    configured fields. The standard low-cost movement/intensity measure for a
    multi-axis accelerometer, but defined over whatever fields the config names."""
    series = _flatten(window)
    if not series:
        return {"sma": 0.0}
    n = min(len(s) for s in series)
    if n == 0:
        return {"sma": 0.0}
    total = 0.0
    for i in range(n):
        total += sum(abs(s[i]) for s in series)
    return {"sma": total / n}


def rms(window: dict[str, list[float]], **_) -> dict[str, float]:
    """Root-mean-square per field, plus the combined magnitude RMS."""
    out: dict[str, float] = {}
    for name, series in window.items():
        out[f"rms_{name}"] = _rms_one(series)
    # combined vector magnitude RMS, useful when fields are axes of one signal
    series = _flatten(window)
    if series:
        n = min(len(s) for s in series)
        mags = [math.sqrt(sum(s[i] * s[i] for s in series)) for i in range(n)]
        out["rms_mag"] = _rms_one(mags)
    return out


def variance(window: dict[str, list[float]], **_) -> dict[str, float]:
    out: dict[str, float] = {}
    for name, series in window.items():
        if not series:
            out[f"var_{name}"] = 0.0
            continue
        m = sum(series) / len(series)
        out[f"var_{name}"] = sum((x - m) ** 2 for x in series) / len(series)
    return out


def rolling_mean(window: dict[str, list[float]], **_) -> dict[str, float]:
    out: dict[str, float] = {}
    for name, series in window.items():
        out[f"mean_{name}"] = (sum(series) / len(series)) if series else 0.0
    return out


def dominant_freq(window: dict[str, list[float]], sample_hz: float = 50.0,
                  **_) -> dict[str, float]:
    """Dominant frequency (Hz) of the combined magnitude signal, via FFT.

    sample_hz comes from the config, not a constant here -- a different sensor
    at a different rate still gets a correct frequency axis."""
    series = _flatten(window)
    if not series:
        return {"dominant_freq": 0.0}
    n = min(len(s) for s in series)
    if n < 4:
        return {"dominant_freq": 0.0}
    mag = [math.sqrt(sum(s[i] * s[i] for s in series)) for i in range(n)]
    mean = sum(mag) / n
    sig = [m - mean for m in mag]                     # remove DC / gravity bias

    if _HAVE_NP:
        spec = np.abs(np.fft.rfft(sig))
        freqs = np.fft.rfftfreq(n, d=1.0 / sample_hz)
        if len(spec) > 1:
            k = int(np.argmax(spec[1:]) + 1)          # skip DC bin
            return {"dominant_freq": float(freqs[k])}
        return {"dominant_freq": 0.0}

    # numpy-free DFT fallback (O(n^2), fine for 1-5 s windows)
    best_f, best_p = 0.0, -1.0
    half = n // 2
    for k in range(1, half):
        re = sum(sig[t] * math.cos(-2 * math.pi * k * t / n) for t in range(n))
        im = sum(sig[t] * math.sin(-2 * math.pi * k * t / n) for t in range(n))
        p = re * re + im * im
        if p > best_p:
            best_p, best_f = p, k * sample_hz / n
    return {"dominant_freq": best_f}


def activity(window: dict[str, list[float]], sample_hz: float = 50.0,
             **_) -> dict[str, float]:
    """Coarse activity label from motion intensity. Returns a numeric code so it
    stores cleanly as an InfluxDB field: 0 still, 1 walking, 2 active.

    Keyed on the AC energy of the combined magnitude -- the RMS of the signal
    after its mean (gravity, for an accelerometer) is removed. A still sensor
    has near-zero AC energy regardless of its orientation; dominant frequency is
    only meaningful once there is real motion, so it is reported but not used to
    gate rest (the FFT of noise would pick a spurious peak)."""
    series = _flatten(window)
    if not series:
        return {"activity_code": 0.0, "activity_intensity": 0.0, "activity_freq": 0.0}
    n = min(len(s) for s in series)
    mag = [math.sqrt(sum(s[i] * s[i] for s in series)) for i in range(n)]
    mean = sum(mag) / n if n else 0.0
    ac_rms = _rms_one([m - mean for m in mag])        # motion intensity
    f = dominant_freq(window, sample_hz=sample_hz)["dominant_freq"]
    if ac_rms < 0.05:
        code = 0.0                                    # at rest
    elif ac_rms < 0.5:
        code = 1.0                                    # walking-scale motion
    else:
        code = 2.0                                    # vigorous / shaking
    return {"activity_code": code, "activity_intensity": ac_rms, "activity_freq": f}


# Registry: config strings -> function. main_* look analyses up here by name,
# so adding an analysis means adding one entry, never editing the entry points.
REGISTRY: dict[str, Callable[..., dict[str, float]]] = {
    "sma": sma,
    "rms": rms,
    "variance": variance,
    "rolling_mean": rolling_mean,
    "dominant_freq": dominant_freq,
    "activity": activity,
}


def run_analyses(names: list[str], window: dict[str, list[float]],
                 sample_hz: float = 50.0) -> dict[str, float]:
    """Run the named analyses over one window and merge their outputs."""
    out: dict[str, float] = {}
    for name in names:
        fn = REGISTRY.get(name)
        if fn is None:
            raise KeyError(f"unknown analysis '{name}'. "
                           f"known: {sorted(REGISTRY)}")
        out.update(fn(window, sample_hz=sample_hz))
    return out
