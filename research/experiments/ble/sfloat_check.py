"""IEEE-11073 medfloat16/medfloat32 decode, and packet decodes for the six profiles.

Run: uv run python research/experiments/ble/sfloat_check.py

No radio is touched. Every byte string below is hand-constructed from the Bluetooth SIG
GATT Specification Supplement (2026-02-05) and the service specifications, so this file
is a check on the *reader*, not on any device.
"""

from __future__ import annotations

import struct

# --- IEEE 11073-20601 special values (GSS 2026-02-05 Table 2.1, PLXS v1.0.1 Table 5.1) ---
SFLOAT_NAN, SFLOAT_NRES = 0x07FF, 0x0800
SFLOAT_PINF, SFLOAT_NINF, SFLOAT_RFU = 0x07FE, 0x0802, 0x0801
FLOAT_NAN, FLOAT_NRES = 0x007FFFFF, 0x00800000
FLOAT_PINF, FLOAT_NINF, FLOAT_RFU = 0x007FFFFE, 0x00800002, 0x00800001


def sfloat(raw: int) -> float:
    """medfloat16 -> float. raw is the 16-bit value already read little-endian."""
    if raw in (SFLOAT_NAN, SFLOAT_NRES, SFLOAT_RFU):
        return float("nan")
    if raw == SFLOAT_PINF:
        return float("inf")
    if raw == SFLOAT_NINF:
        return float("-inf")
    exp = raw >> 12
    mant = raw & 0x0FFF
    if exp >= 0x8:            # 4-bit two's complement
        exp -= 0x10
    if mant >= 0x800:         # 12-bit two's complement
        mant -= 0x1000
    return mant * (10.0**exp)


def float32(raw: int) -> float:
    """medfloat32 -> float. raw is the 32-bit value already read little-endian."""
    if raw in (FLOAT_NAN, FLOAT_NRES, FLOAT_RFU):
        return float("nan")
    if raw == FLOAT_PINF:
        return float("inf")
    if raw == FLOAT_NINF:
        return float("-inf")
    exp = raw >> 24
    mant = raw & 0x00FFFFFF
    if exp >= 0x80:           # 8-bit two's complement
        exp -= 0x100
    if mant >= 0x800000:      # 24-bit two's complement
        mant -= 0x1000000
    return mant * (10.0**exp)


def enc_sfloat(mant: int, exp: int) -> bytes:
    return struct.pack("<H", ((exp & 0xF) << 12) | (mant & 0x0FFF))


def enc_float(mant: int, exp: int) -> bytes:
    return struct.pack("<I", ((exp & 0xFF) << 24) | (mant & 0x00FFFFFF))


def enc_datetime(y, mo, d, h, mi, s) -> bytes:
    return struct.pack("<HBBBBB", y, mo, d, h, mi, s)


def rd_sfloat(b: bytes, i: int) -> tuple[float, int]:
    return sfloat(struct.unpack_from("<H", b, i)[0]), i + 2


# ------------------------------ per-profile decoders ------------------------------

def decode_bp(b: bytes) -> dict:
    """0x2A35 Blood Pressure Measurement (also 0x2A36 Intermediate Cuff Pressure)."""
    f, i = b[0], 1
    kpa = bool(f & 0x01)
    sys, i = rd_sfloat(b, i)
    dia, i = rd_sfloat(b, i)
    mapv, i = rd_sfloat(b, i)
    out: dict = {"unit": "kPa" if kpa else "mmHg"}
    if kpa:                              # 1 kPa = 7.50062 mmHg
        sys, dia, mapv = (v * 7.50061682704 for v in (sys, dia, mapv))
    out |= {"systolic_mmHg": sys, "diastolic_mmHg": dia, "map_mmHg": mapv}
    if f & 0x02:
        out["timestamp"] = struct.unpack_from("<HBBBBB", b, i); i += 7
    if f & 0x04:
        out["pulse_bpm"], i = rd_sfloat(b, i)
    if f & 0x08:
        out["user_id"] = b[i]; i += 1
    if f & 0x10:
        out["status"] = struct.unpack_from("<H", b, i)[0]; i += 2
    assert i == len(b), (i, len(b))
    return out


def decode_temp(b: bytes) -> dict:
    """0x2A1C Temperature Measurement (also 0x2A1E Intermediate Temperature)."""
    f, i = b[0], 1
    raw = struct.unpack_from("<I", b, i)[0]; i += 4
    v = float32(raw)
    fahrenheit = bool(f & 0x01)
    out = {"raw_unit": "F" if fahrenheit else "C",
           "celsius": (v - 32.0) * 5.0 / 9.0 if fahrenheit else v}
    if f & 0x02:
        out["timestamp"] = struct.unpack_from("<HBBBBB", b, i); i += 7
    if f & 0x04:
        out["temp_type"] = b[i]; i += 1
    assert i == len(b), (i, len(b))
    return out


def decode_glucose(b: bytes) -> dict:
    """0x2A18 Glucose Measurement. Value decodes to kg/L or mol/L, NOT mg/dL or mmol/L."""
    f, i = b[0], 1
    seq = struct.unpack_from("<H", b, i)[0]; i += 2
    base = struct.unpack_from("<HBBBBB", b, i); i += 7
    out: dict = {"seq": seq, "base_time": base}
    if f & 0x01:
        out["time_offset_min"] = struct.unpack_from("<h", b, i)[0]; i += 2
    if f & 0x02:
        v, i = rd_sfloat(b, i)
        if f & 0x04:                       # base unit mol/L
            out["mmol_L"] = v * 1e3
        else:                              # base unit kg/L
            mg_dl = v * 1e5
            out["mg_dL"] = mg_dl
            out["mmol_L"] = mg_dl / 18.0156
        tsl = b[i]; i += 1
        out["type"], out["sample_location"] = tsl & 0x0F, tsl >> 4
    if f & 0x08:
        out["sensor_status"] = struct.unpack_from("<H", b, i)[0]; i += 2
    out["context_follows"] = bool(f & 0x10)
    assert i == len(b), (i, len(b))
    return out


def decode_weight(b: bytes) -> dict:
    """0x2A9D Weight Measurement. NOTE flag bit order differs from Blood Pressure."""
    f, i = b[0], 1
    raw = struct.unpack_from("<H", b, i)[0]; i += 2
    imperial = bool(f & 0x01)
    if raw == 0xFFFF:
        out: dict = {"kg": None, "unsuccessful": True}
    elif imperial:
        out = {"kg": raw * 0.01 * 0.45359237}
    else:
        out = {"kg": raw * 0.005}
    if f & 0x02:
        out["timestamp"] = struct.unpack_from("<HBBBBB", b, i); i += 7
    if f & 0x04:
        out["user_id"] = b[i]; i += 1
    if f & 0x08:
        out["bmi"] = struct.unpack_from("<H", b, i)[0] * 0.1; i += 2
        h = struct.unpack_from("<H", b, i)[0]; i += 2
        out["height_m"] = h * 0.1 * 0.0254 if imperial else h * 0.001
    assert i == len(b), (i, len(b))
    return out


def decode_plx_spot(b: bytes) -> dict:
    """0x2A5E PLX Spot-Check Measurement."""
    f, i = b[0], 1
    spo2, i = rd_sfloat(b, i)
    pr, i = rd_sfloat(b, i)
    out: dict = {"spo2_pct": spo2, "pulse_bpm": pr}
    if f & 0x01:
        out["timestamp"] = struct.unpack_from("<HBBBBB", b, i); i += 7
    if f & 0x02:
        out["meas_status"] = struct.unpack_from("<H", b, i)[0]; i += 2
    if f & 0x04:
        out["dev_status"] = int.from_bytes(b[i:i + 3], "little"); i += 3
    if f & 0x08:
        out["pulse_amp_index"], i = rd_sfloat(b, i)
    out["clock_not_set"] = bool(f & 0x10)
    assert i == len(b), (i, len(b))
    return out


def decode_plx_cont(b: bytes) -> dict:
    """0x2A5F PLX Continuous Measurement."""
    f, i = b[0], 1
    out: dict = {}
    out["spo2_pct"], i = rd_sfloat(b, i)
    out["pulse_bpm"], i = rd_sfloat(b, i)
    if f & 0x01:
        out["spo2_fast"], i = rd_sfloat(b, i); out["pr_fast"], i = rd_sfloat(b, i)
    if f & 0x02:
        out["spo2_slow"], i = rd_sfloat(b, i); out["pr_slow"], i = rd_sfloat(b, i)
    if f & 0x04:
        out["meas_status"] = struct.unpack_from("<H", b, i)[0]; i += 2
    if f & 0x08:
        out["dev_status"] = int.from_bytes(b[i:i + 3], "little"); i += 3
    if f & 0x10:
        out["pulse_amp_index"], i = rd_sfloat(b, i)
    assert i == len(b), (i, len(b))
    return out


def decode_hr(b: bytes) -> dict:
    """0x2A37 Heart Rate Measurement. Value is a plain uint, NOT an SFLOAT."""
    f, i = b[0], 1
    if f & 0x01:
        hr = struct.unpack_from("<H", b, i)[0]; i += 2
    else:
        hr = b[i]; i += 1
    out: dict = {"bpm": hr,
                 "contact_supported": bool(f & 0x04),
                 "contact_detected": bool(f & 0x02) if f & 0x04 else None}
    if f & 0x08:
        out["energy_j"] = struct.unpack_from("<H", b, i)[0]; i += 2
    if f & 0x10:
        rr = []
        while i < len(b):
            rr.append(struct.unpack_from("<H", b, i)[0] / 1024.0); i += 2
        out["rr_s"] = rr
    assert i == len(b), (i, len(b))
    return out


# ------------------------------------ checks ------------------------------------

def approx(a: float, b: float, tol: float = 1e-9) -> bool:
    import math
    if math.isinf(a) or math.isinf(b):
        return a == b
    return abs(a - b) <= tol * max(1.0, abs(b))


def main() -> int:
    fails: list[str] = []

    def check(name: str, got, want) -> None:
        ok = approx(got, want) if isinstance(want, float) else got == want
        print(f"{'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
        if not ok:
            fails.append(name)

    print("--- medfloat16 (SFLOAT) ---")
    check("120, exp 0", sfloat(0x0078), 120.0)
    check("5.5 as 55e-1", sfloat(0xF037), 5.5)
    check("0.0055 as 55e-4", sfloat(0xC037), 0.0055)
    check("-3.1 as -31e-1", sfloat(0xFFE1), -3.1)
    check("36.5 as 365e-1", sfloat(0xF16D), 36.5)
    check("NaN raw 0x07FF", str(sfloat(0x07FF)), "nan")
    check("NRes raw 0x0800", str(sfloat(0x0800)), "nan")
    check("+inf raw 0x07FE", sfloat(0x07FE), float("inf"))
    check("-inf raw 0x0802", sfloat(0x0802), float("-inf"))
    # The trap: 0x0FFF is NOT NaN. It is exponent 0, mantissa -1.
    check("0x0FFF is -1.0 not NaN", sfloat(0x0FFF), -1.0)
    # The other trap: 0x8000 is exponent -8, mantissa 0 -> 0.0, and 0x0000 is 0.0
    check("0x0000 is 0.0", sfloat(0x0000), 0.0)
    check("0x8000 is 0.0", sfloat(0x8000), 0.0)

    print("\n--- medfloat32 (FLOAT) ---")
    check("36.5 as 365e-1", float32(0xFF00016D), 36.5)
    check("97.7 as 977e-1", float32(0xFF0003D1), 97.7)
    check("NaN", str(float32(0x007FFFFF)), "nan")
    check("-inf", float32(0x00800002), float("-inf"))

    print("\n--- 0x2A35 Blood Pressure Measurement ---")
    pkt = (bytes([0x1E])                       # ts + pulse + user + status, mmHg
           + enc_sfloat(120, 0) + enc_sfloat(80, 0) + enc_sfloat(93, 0)
           + enc_datetime(2026, 9, 7, 8, 15, 0)
           + enc_sfloat(62, 0) + bytes([0x01]) + struct.pack("<H", 0x0004))
    d = decode_bp(pkt)
    print(" ", pkt.hex(), "->", d)
    check("systolic", d["systolic_mmHg"], 120.0)
    check("diastolic", d["diastolic_mmHg"], 80.0)
    check("pulse", d["pulse_bpm"], 62.0)
    check("irregular pulse bit", bool(d["status"] & 0x04), True)
    check("packet length 1+2+2+2+7+2+1+2", len(pkt), 19)

    # Minimal legal packet: flags 0x00, three SFLOATs only.
    d2 = decode_bp(bytes([0x00]) + enc_sfloat(118, 0) + enc_sfloat(77, 0) + enc_sfloat(90, 0))
    check("minimal packet len 7 systolic", d2["systolic_mmHg"], 118.0)

    # kPa device: 16.0 kPa / 10.7 kPa. 16 kPa = 120.01 mmHg.
    d3 = decode_bp(bytes([0x01]) + enc_sfloat(160, -1) + enc_sfloat(107, -1) + enc_sfloat(124, -1))
    print("  kPa ->", d3)
    check("kPa systolic to mmHg", round(d3["systolic_mmHg"], 2), 120.01)

    print("\n--- 0x2A1C Temperature Measurement ---")
    dc = decode_temp(bytes([0x00]) + enc_float(365, -1))
    check("celsius device", dc["celsius"], 36.5)
    df = decode_temp(bytes([0x01]) + enc_float(977, -1))
    print("  fahrenheit ->", df)
    check("fahrenheit device -> C", round(df["celsius"], 4), 36.5)
    dt = decode_temp(bytes([0x06]) + enc_float(3812, -2) + enc_datetime(2026, 9, 7, 8, 20, 5) + bytes([0x06]))
    check("ts+type packet len 13", len(bytes([0x06]) + enc_float(3812, -2) + enc_datetime(2026, 9, 7, 8, 20, 5) + bytes([0x06])), 13)
    check("38.12 C", dt["celsius"], 38.12)

    print("\n--- 0x2A18 Glucose Measurement ---")
    # kg/L device reporting 100 mg/dL: 100 mg/dL = 1e-3 kg/L = 100e-5
    g1 = decode_glucose(bytes([0x02]) + struct.pack("<H", 7) + enc_datetime(2026, 9, 7, 8, 0, 0)
                        + enc_sfloat(100, -5) + bytes([0x11]))
    print(" ", g1)
    check("mg/dL", round(g1["mg_dL"], 6), 100.0)
    check("mmol/L from mg/dL", round(g1["mmol_L"], 3), 5.551)
    # mol/L device reporting 5.5 mmol/L: 5.5 mmol/L = 5.5e-3 mol/L = 55e-4
    g2 = decode_glucose(bytes([0x06]) + struct.pack("<H", 8) + enc_datetime(2026, 9, 7, 8, 0, 0)
                        + enc_sfloat(55, -4) + bytes([0x11]))
    print(" ", g2)
    check("mmol/L direct", round(g2["mmol_L"], 6), 5.5)
    # The bug this file exists to prevent: reading the mantissa as if it were mg/dL.
    naive = struct.unpack("<H", enc_sfloat(100, -5))[0] & 0x0FFF
    check("naive mantissa read happens to be 100", naive, 100)

    print("\n--- 0x2A9D Weight Measurement ---")
    w1 = decode_weight(bytes([0x00]) + struct.pack("<H", 16000))
    check("SI 80.0 kg", w1["kg"], 80.0)
    w2 = decode_weight(bytes([0x01]) + struct.pack("<H", 17637))
    print("  imperial ->", w2)
    check("imperial 176.37 lb -> kg", round(w2["kg"], 3), 80.0)
    w3 = decode_weight(bytes([0x0F]) + struct.pack("<H", 16000) + enc_datetime(2026, 9, 7, 7, 0, 0)
                       + bytes([0x01]) + struct.pack("<H", 247) + struct.pack("<H", 705))
    print("  full imperial ->", w3)
    w4 = decode_weight(bytes([0x0E]) + struct.pack("<H", 16000) + enc_datetime(2026, 9, 7, 7, 0, 0)
                       + bytes([0x01]) + struct.pack("<H", 247) + struct.pack("<H", 1800))
    check("SI height 1.8 m", w4["height_m"], 1.8)
    check("BMI 24.7", round(w4["bmi"], 1), 24.7)
    w5 = decode_weight(bytes([0x00]) + struct.pack("<H", 0xFFFF))
    check("measurement unsuccessful", w5["unsuccessful"], True)

    print("\n--- 0x2A5E / 0x2A5F Pulse Oximeter ---")
    p1 = decode_plx_spot(bytes([0x00]) + enc_sfloat(98, 0) + enc_sfloat(61, 0))
    check("spot spo2", p1["spo2_pct"], 98.0)
    p2 = decode_plx_spot(bytes([0x03]) + enc_sfloat(97, 0) + enc_sfloat(72, 0)
                         + enc_datetime(2026, 9, 7, 8, 30, 0) + struct.pack("<H", 0x0020))
    print(" ", p2)
    p3 = decode_plx_spot(bytes([0x00]) + struct.pack("<H", SFLOAT_NAN) + enc_sfloat(72, 0))
    check("NaN spo2 is nan", str(p3["spo2_pct"]), "nan")
    c1 = decode_plx_cont(bytes([0x00]) + enc_sfloat(96, 0) + enc_sfloat(70, 0))
    check("cont spo2", c1["spo2_pct"], 96.0)
    c2 = decode_plx_cont(bytes([0x03]) + enc_sfloat(96, 0) + enc_sfloat(70, 0)
                         + enc_sfloat(97, 0) + enc_sfloat(71, 0)
                         + enc_sfloat(95, 0) + enc_sfloat(69, 0))
    check("cont slow spo2", c2["spo2_slow"], 95.0)

    print("\n--- 0x2A37 Heart Rate Measurement ---")
    h1 = decode_hr(bytes([0x00, 62]))
    check("uint8 hr", h1["bpm"], 62)
    h2 = decode_hr(bytes([0x01]) + struct.pack("<H", 300))
    check("uint16 hr", h2["bpm"], 300)
    h3 = decode_hr(bytes([0x16, 62]) + struct.pack("<HH", 1024, 990))
    print(" ", h3)
    check("rr first", h3["rr_s"][0], 1.0)
    check("contact detected", h3["contact_detected"], True)
    h4 = decode_hr(bytes([0x04, 62]))
    check("contact supported, not detected", h4["contact_detected"], False)

    print("\n--- PLAUSIBLE-table cross-check (packages/protocol/health.py) ---")
    plaus = {"bp_systolic": (50.0, 260.0), "spo2": (50.0, 100.0),
             "body_temperature": (30.0, 45.0), "blood_glucose": (1.0, 40.0),
             "weight": (2.0, 400.0), "heart_rate": (20.0, 250.0)}
    # A uint16 HR of 300 is legal on the wire and outside PLAUSIBLE -> must be rejected.
    lo, hi = plaus["heart_rate"]
    check("hr 300 rejected by PLAUSIBLE", lo <= 300 <= hi, False)
    # A Fahrenheit reading decoded as Celsius (97.7) is outside PLAUSIBLE -> caught.
    lo, hi = plaus["body_temperature"]
    check("97.7 (unconverted F) rejected", lo <= 97.7 <= hi, False)
    # But a kPa systolic read as mmHg (16.0) is ALSO rejected -> caught.
    lo, hi = plaus["bp_systolic"]
    check("16.0 (unconverted kPa) rejected", lo <= 16.0 <= hi, False)
    # And the mg/dL-as-mmol/L bug (100.0) is rejected...
    lo, hi = plaus["blood_glucose"]
    check("100.0 (mg/dL read as mmol/L) rejected", lo <= 100.0 <= hi, False)
    # ...but the reverse, mmol/L read as mg/dL, is 5.5 and passes silently. Not caught.
    check("5.5 (mmol/L) accepted -- unit bug NOT caught by range", lo <= 5.5 <= hi, True)
    # And an lb value read as kg: 176.37 lb -> 176.37 "kg" is inside PLAUSIBLE. Not caught.
    lo, hi = plaus["weight"]
    check("176.37 (lb read as kg) accepted -- NOT caught by range", lo <= 176.37 <= hi, True)
    # Worse: the 0xFFFF "measurement unsuccessful" sentinel decodes to a plausible weight.
    check("0xFFFF SI decodes to kg", 0xFFFF * 0.005, 327.675)
    check("327.675 accepted -- sentinel NOT caught by range", lo <= 327.675 <= hi, True)
    check("0xFFFF imperial decodes to kg", round(0xFFFF * 0.01 * 0.45359237, 3), 297.262)
    check("297.262 accepted -- sentinel NOT caught by range", lo <= 297.262 <= hi, True)

    print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
