#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
# Copyright (c) 2026 The LM-G820N Linux contributors
"""Infineon REAL3 IRS2381C ToF Raw12 Calibrated Demodulator and Depth Estimator.

Parses V4L2_PIX_FMT_SBGGR12P (pBCC) ToF superframes captured from msm_vfe3_video0
via CSIPHY3 in either 9-phase dual-frequency (224x1903) or 5-phase single-frequency
(224x1211 / 224x1038 / 224x865) modes, and applies per-device factory calibration
read directly from the sensor's onboard Zwetschge SPI Flash (PMDJGF container).

Hardware Structure:
  - Each subframe has 173 rows x 224 columns:
      * Row 0: PseudoData (telemetry, ROI, sequence index, exposure, NTC ADC,
               eye-safety status, module serial).
      * Rows 1..172: 224x172 12-bit differential demodulation raw pixels.
  - 9-phase dual-frequency modes (e.g. MODE_9_5FPS / 10FPS / 15FPS / 30FPS):
      * Subframe 0: Ambient / Gray frame.
      * Subframes 1..4: 80.32 MHz modulation, phases 0°, 90°, 180°, 270°.
      * Subframes 5..8: 60.24 MHz modulation, phases 0°, 90°, 180°, 270°.
      * Subframes 9..10: Blanking / padding.
  - 5-phase single-frequency modes (e.g. MODE_5_15FPS / MODE_5_30FPS):
      * Subframe 0: Ambient / Gray frame.
      * Subframes 1..4: 80.32 MHz modulation, phases 0°, 90°, 180°, 270°.
      * Remaining subframes (if any): Blanking / padding.

Calibration Pipeline (matches PMD Spectre WsRefBackend):
  1. Phase extraction: phi = atan2(s270 - s90, s0 - s180) mod 2*pi
  2. Harmonic phase wiggling correction: phi' = phi + off_w + sum(A_k * sin(h_k * phi + p_k))
  3. Phase to distance: d = phi' * c / (4 * pi * f_mod)
  4. Temperature compensation: d -= ((c3 * dT + c2) * dT + c1) * dT, dT = T_C - t0
  5. Per-pixel FPPN + global offset: d_cal = d + fppn[y, x] + offset
  6. Dual-frequency unwrapping (9-phase, beat range ~7.465 m) or wrap to [0, D_max1) (5-phase)
  7. Per-device Brown-Conrady lens undistortion -> axial Z projection
"""

import argparse
from dataclasses import dataclass, field
import glob
import json
import math
from pathlib import Path
import struct
import sys
from typing import NamedTuple
import zlib

import numpy as np

SPEED_OF_LIGHT = 299792458.0  # m/s
FREQ1 = 80320000.0            # 80.32 MHz
FREQ2 = 60240000.0            # 60.24 MHz
D_MAX1 = SPEED_OF_LIGHT / (2.0 * FREQ1)             # ~1.8662 m
D_MAX2 = SPEED_OF_LIGHT / (2.0 * FREQ2)             # ~2.4883 m
D_BEAT = SPEED_OF_LIGHT / (2.0 * (FREQ1 - FREQ2))   # ~7.4650 m

SENSOR_WIDTH = 224
ACTIVE_HEIGHT = 172
SUBFRAME_ROWS = ACTIVE_HEIGHT + 1

# LG LM-G820N IRS2381C NTC thermistor parameters (from ModuleConfigCustom_IRS2381C_LGE)
NTC_R_REF = 6800.0
NTC_R_0 = 100000.0
NTC_T_REF_C = 25.0
NTC_BETA = 4200.0
DEFAULT_TEMP_C = 30.0


class Intrinsics(NamedTuple):
    fx: float
    fy: float
    cx: float
    cy: float
    k1: float
    k2: float
    p1: float
    p2: float
    k3: float


# Fallback reference intrinsics when no nvmem/PMDJGF calibration is supplied.
REFERENCE_INTRINSICS = Intrinsics(
    167.96358, 167.96358, 114.96949, 87.91890,
    0.207316, -0.629454, -0.0007538, -0.0009207, 0.517092,
)


@dataclass
class FreqCalibration:
    freq_hz: int
    fppn: np.ndarray                                # (172, 224) float32 in meters
    temp_comp: tuple[float, float, float, float]    # (t0, c1, c2, c3)
    offset: float                                   # global distance offset in meters
    wiggling_offset: float = 0.0
    wiggling_phases: tuple[float, ...] = ()
    wiggling_amps: tuple[float, ...] = ()
    wiggling_harmonics: tuple[float, ...] = ()


@dataclass
class TofCalibration:
    width: int
    height: int
    serial: str
    lens: Intrinsics
    bad_pixel_mask: np.ndarray                      # (172, 224) bool, True = bad pixel
    fpn: np.ndarray | None = None                   # (172, 224) float32 ambient FPN
    freqs: dict[int, FreqCalibration] = field(default_factory=dict)


def _decode_pmd_f32(raw4: bytes) -> float:
    """Decode PMD custom signed-magnitude big-endian 32-bit float used in PMDJGF."""
    s = struct.unpack(">i", raw4)[0]
    if s == 0:
        return 0.0
    sign = -1.0 if s < 0 else 1.0
    mag = abs(s)
    mant = (mag & 0x7FFFFF) + (1 << 23)
    exp = (mag >> 23) - 127
    return sign * (mant * (2.0 ** -24)) * (2.0 ** exp)


def _unpack_pmd_bits_lsb(data: bytes, count: int, bits: int) -> np.ndarray:
    """Unpack LSB-first bit-packed unsigned integers from big-endian u32 words."""
    words = np.frombuffer(data, dtype=">u4")
    # Vectorized bit extraction over 64-bit sliding windows
    idx = np.arange(count, dtype=np.int64) * bits
    w_idx = idx >> 5
    b_off = (idx & 31).astype(np.uint64)
    w0 = words[w_idx].astype(np.uint64)
    w1_idx = np.minimum(w_idx + 1, len(words) - 1)
    w1 = words[w1_idx].astype(np.uint64)
    combined = (w0 >> b_off) | np.where(b_off + bits > 32, w1 << (32 - b_off), 0)
    mask = np.uint64((1 << bits) - 1)
    return (combined & mask).astype(np.uint32)


def extract_pmdjgf_from_zwetschge(blob: bytes) -> tuple[bytes, str]:
    """Extract and CRC-verify the PMDJGF calibration payload from a Zwetschge image."""
    if blob.startswith(b"PMDJGF"):
        return blob, ""
    if len(blob) < 0x58 or not blob.startswith(b"ZWETSCHGE"):
        raise ValueError("Unsupported calibration container (expected ZWETSCHGE or PMDJGF)")

    toc_crc = struct.unpack_from("<I", blob, 0x09)[0]
    calc_toc_crc = zlib.crc32(blob[0x0D:0x58]) & 0xFFFFFFFF
    if calc_toc_crc != toc_crc:
        raise ValueError(f"Zwetschge ToC CRC32 mismatch: 0x{calc_toc_crc:08x} != 0x{toc_crc:08x}")

    def le24(off: int) -> int:
        return blob[off] | (blob[off + 1] << 8) | (blob[off + 2] << 16)

    cal_addr = le24(0x34)
    cal_size = le24(0x37)
    cal_crc = struct.unpack_from("<I", blob, 0x3A)[0]
    serial = blob[0x45:0x58].decode("ascii", errors="replace").strip("\x00")

    base = 0x2000
    if cal_addr < base or (cal_addr - base + cal_size) > len(blob):
        raise ValueError(f"Calibration slice 0x{cal_addr:x}+{cal_size} exceeds Zwetschge dump ({len(blob)} B)")

    cal = blob[cal_addr - base : cal_addr - base + cal_size]
    calc_cal_crc = zlib.crc32(cal) & 0xFFFFFFFF
    if calc_cal_crc != cal_crc:
        raise ValueError(f"Calibration CRC32 mismatch: 0x{calc_cal_crc:08x} != 0x{cal_crc:08x}")

    return cal, serial


def parse_pmdjgf_calibration(blob: bytes) -> TofCalibration:
    """Parse a PMDJGF (or Zwetschge-wrapped PMDJGF) calibration container."""
    cal, toc_serial = extract_pmdjgf_from_zwetschge(blob)
    if len(cal) < 15 or not cal.startswith(b"PMDJGF"):
        raise ValueError("Invalid PMDJGF calibration header")

    width = struct.unpack_from(">H", cal, 7)[0]
    height = struct.unpack_from(">H", cal, 9)[0]
    npix = width * height

    lens = REFERENCE_INTRINSICS
    bad_mask = np.zeros((height, width), dtype=bool)
    fpn = None
    serial = toc_serial
    slot_data: dict[int, dict] = {}

    pos = 0x0F
    while pos < len(cal):
        tag = cal[pos]
        if tag == 0xFF:
            break
        if tag == 0x80:
            cnt = cal[pos + 2]
            if cnt == 36 and pos + 3 + cnt <= len(cal):
                lens = Intrinsics(*struct.unpack_from("<9f", cal, pos + 3))
            pos += 3 + cnt
            continue

        dtype = cal[pos + 1]
        cnt_byte = cal[pos + 2]
        cnt = npix if cnt_byte == 0 else cnt_byte
        pos += 3

        if dtype == 0:
            raw = cal[pos : pos + cnt]
            pos += cnt
            if tag == 0x06:
                serial = raw.decode("ascii", errors="replace").strip("\x00")
        elif dtype == 1:
            pos += cnt * 2
        elif dtype in (2, 3):
            fmt = ">I" if dtype == 2 else ">i"
            vals = [struct.unpack_from(fmt, cal, pos + i * 4)[0] for i in range(cnt)]
            pos += cnt * 4
            if tag >= 0x10 and (tag - 0x10) % 7 == 0:
                slot = (tag - 0x10) // 7
                slot_data.setdefault(slot, {})["freq_hz"] = int(vals[0])
        elif dtype == 4:
            vals = [_decode_pmd_f32(cal[pos + i * 4 : pos + (i + 1) * 4]) for i in range(cnt)]
            pos += cnt * 4
            if tag == 0x01 and len(vals) >= 9:
                lens = Intrinsics(*vals[:9])
            elif tag >= 0x10:
                slot = (tag - 0x10) // 7
                sub = (tag - 0x10) % 7
                entry = slot_data.setdefault(slot, {})
                if sub == 2 and len(vals) >= 4:
                    entry["temp_comp"] = (vals[0], vals[1], vals[2], vals[3])
                elif sub == 4 and len(vals) >= 1:
                    entry["offset"] = vals[0]
                elif sub == 5 and len(vals) >= 4 and (len(vals) - 1) % 3 == 0:
                    n = (len(vals) - 1) // 3
                    entry["wiggling_offset"] = vals[0]
                    entry["wiggling_phases"] = tuple(vals[1 : 1 + n])
                    entry["wiggling_amps"] = tuple(vals[1 + n : 1 + 2 * n])
                    entry["wiggling_harmonics"] = tuple(vals[1 + 2 * n : 1 + 3 * n])
        elif dtype == 5:
            bits = cal[pos]
            pos += 1
            nbytes = math.ceil(cnt * bits / 32) * 4
            if tag == 0x02 and cnt == npix:
                bad_mask = _unpack_pmd_bits_lsb(cal[pos : pos + nbytes], npix, bits).reshape(height, width) != 0
            pos += nbytes
        elif dtype == 6:
            bits = cal[pos]
            pos += 1
            scale = _decode_pmd_f32(cal[pos : pos + 4])
            bias = _decode_pmd_f32(cal[pos + 4 : pos + 8])
            pos += 8
            nbytes = math.ceil(cnt * bits / 32) * 4
            if cnt == npix:
                arr = (_unpack_pmd_bits_lsb(cal[pos : pos + nbytes], npix, bits).astype(np.float32) * scale + bias).reshape(height, width)
                if tag == 0x03:
                    fpn = arr
                elif tag >= 0x10 and (tag - 0x10) % 7 == 1:
                    slot = (tag - 0x10) // 7
                    slot_data.setdefault(slot, {})["fppn"] = arr
            pos += nbytes
        else:
            raise ValueError(f"Unsupported PMDJGF dtype {dtype} for tag 0x{tag:02x}")

    freqs: dict[int, FreqCalibration] = {}
    for entry in slot_data.values():
        if "freq_hz" in entry and "fppn" in entry:
            fhz = entry["freq_hz"]
            freqs[fhz] = FreqCalibration(
                freq_hz=fhz,
                fppn=entry["fppn"],
                temp_comp=entry.get("temp_comp", (0.0, 0.0, 0.0, 0.0)),
                offset=entry.get("offset", 0.0),
                wiggling_offset=entry.get("wiggling_offset", 0.0),
                wiggling_phases=entry.get("wiggling_phases", ()),
                wiggling_amps=entry.get("wiggling_amps", ()),
                wiggling_harmonics=entry.get("wiggling_harmonics", ()),
            )

    return TofCalibration(
        width=width,
        height=height,
        serial=serial,
        lens=lens,
        bad_pixel_mask=bad_mask,
        fpn=fpn,
        freqs=freqs,
    )


def find_default_nvmem_path() -> Path | None:
    """Locate the IRS2381C Zwetschge nvmem sysfs file on the target device."""
    patterns = [
        "/sys/bus/i2c/drivers/irs2381c/*/2-*/nvmem",
        "/sys/bus/i2c/drivers/irs2381c/*/*/nvmem",
        "/sys/bus/i2c/drivers/irs2381c/*/eeprom",
        "/sys/bus/nvmem/devices/2-003d*/nvmem",
    ]
    for pat in patterns:
        matches = sorted(glob.glob(pat))
        if matches:
            return Path(matches[0])
    return None


def unpack_mipi_12bit(raw_bytes: bytes, width: int = SENSOR_WIDTH, height: int | None = None) -> np.ndarray:
    """Unpack V4L2_PIX_FMT_SBGGR12P (pBCC) to a uint16 array."""
    row_bytes = width * 3 // 2
    if width % 2 or len(raw_bytes) == 0 or len(raw_bytes) % row_bytes != 0:
        raise ValueError(f"Invalid RAW12 payload size {len(raw_bytes)} for width={width}")

    inferred_height = len(raw_bytes) // row_bytes
    if height is not None and height != inferred_height:
        raise ValueError(f"RAW12 height mismatch: expected {height}, got {inferred_height}")
    height = inferred_height

    packed = np.frombuffer(raw_bytes, dtype=np.uint8).reshape(height, width // 2, 3)
    pixels = np.empty((height, width), dtype=np.uint16)
    pixels[:, ::2] = (packed[..., 0].astype(np.uint16) << 4) | (packed[..., 2] & 0x0F)
    pixels[:, 1::2] = (packed[..., 1].astype(np.uint16) << 4) | (packed[..., 2] >> 4)
    return pixels


def undistort(xd, yd, lens: Intrinsics):
    """Invert Brown-Conrady distortion for the camera rays."""
    x, y = xd, yd
    for _ in range(12):
        radius2 = x * x + y * y
        radial = 1 + radius2 * (lens.k1 + radius2 * (lens.k2 + radius2 * lens.k3))
        if np.any(np.abs(radial) < 1e-12):
            raise ValueError("Singular radial distortion factor during undistortion")
        xy = x * y
        tx = 2 * lens.p1 * xy + lens.p2 * (radius2 + 2 * x * x)
        ty = lens.p1 * (radius2 + 2 * y * y) + 2 * lens.p2 * xy
        x, y = (xd - tx) / radial, (yd - ty) / radial
    if not (np.isfinite(x).all() and np.isfinite(y).all()):
        raise ValueError("Undistortion diverged to non-finite coordinates")
    return x, y


def ray_projection(height: int, width: int, lens: Intrinsics) -> np.ndarray:
    y, x = np.ogrid[:height, :width]
    x, y = undistort((x - lens.cx) / lens.fx, (y - lens.cy) / lens.fy, lens)
    return 1.0 / np.sqrt(1.0 + x * x + y * y)


def compute_ntc_temperature_c(v_ref1: int, v_ntc1: int, v_ref2: int, v_ntc2: int) -> float | None:
    """Compute sensor temperature in Celsius using LG IRS2381C NtcTempAlgo parameters."""
    d_ref = v_ref1 - v_ref2
    d_ntc = v_ntc1 - v_ntc2
    if d_ntc <= 0 or d_ref <= d_ntc:
        return None
    ratio = (NTC_R_REF / NTC_R_0) / (d_ref / d_ntc - 1.0)
    if ratio <= 0.0:
        return None
    inv_t = 1.0 / (NTC_T_REF_C + 273.15) + math.log(ratio) / NTC_BETA
    return 1.0 / inv_t - 273.15


def parse_pseudodata(pseudo_row) -> dict:
    """Parse telemetry fields from the first row of an M2453_B11 subframe."""
    pseudo_row = list(map(int, pseudo_row))
    seq_index = pseudo_row[5] if len(pseudo_row) > 5 else 0
    reconfig_index = pseudo_row[2] if len(pseudo_row) > 2 else 0
    frame_number = pseudo_row[3] if len(pseudo_row) > 3 else 0
    h_size = (pseudo_row[24] - pseudo_row[23] + 1) if len(pseudo_row) > 24 else 0
    v_size = (pseudo_row[26] - pseudo_row[25] + 1) if len(pseudo_row) > 26 else 0
    exp_code = 0
    if len(pseudo_row) > 29:
        exp_code = (pseudo_row[28] & 0x0FFF) | ((pseudo_row[29] & 0x000F) << 12)
    eye_safety = 0
    if len(pseudo_row) > 43:
        eye_safety = (pseudo_row[42] & 0x0FFF) | ((pseudo_row[43] & 0x0F00) << 8)

    temp_c = None
    if len(pseudo_row) > 50:
        v_ref2 = pseudo_row[47] & 0x0FFF
        v_ref1 = pseudo_row[48] & 0x0FFF
        v_ntc2 = pseudo_row[49] & 0x0FFF
        v_ntc1 = pseudo_row[50] & 0x0FFF
        temp_c = compute_ntc_temperature_c(v_ref1, v_ntc1, v_ref2, v_ntc2)

    return {
        "seq_index": seq_index,
        "reconfig_index": reconfig_index,
        "frame_number": frame_number,
        "width": h_size,
        "height": v_size,
        "exposure_code": exp_code,
        "eye_safety_error": eye_safety,
        "temperature_c": temp_c,
    }


def _apply_freq_calibration(phi: np.ndarray, freq_hz: float,
                            fcal: FreqCalibration | None,
                            temp_c: float,
                            use_offset: bool = True) -> np.ndarray:
    """Convert raw phase [0, 2*pi) to calibrated wrapped radial distance in meters."""
    d_max = SPEED_OF_LIGHT / (2.0 * freq_hz)
    if fcal is None:
        return (phi % math.tau) * (d_max / math.tau)

    phi_corr = phi + fcal.wiggling_offset
    for ph, amp, harm in zip(fcal.wiggling_phases, fcal.wiggling_amps, fcal.wiggling_harmonics):
        if amp != 0.0:
            phi_corr = phi_corr + amp * np.sin(phi * harm + ph)

    dist = phi_corr * (d_max / math.tau)
    t0, c1, c2, c3 = fcal.temp_comp
    dt = temp_c - t0
    dist = dist - (((c3 * dt + c2) * dt + c1) * dt)
    dist = dist + fcal.fppn
    if use_offset:
        dist = dist + fcal.offset
    return np.mod(dist, d_max)


def compute_tof_frame(grid, min_amplitude: float = 1.5, roi_half_size: int = 10,
                      lens: Intrinsics | None = None,
                      calib: TofCalibration | None = None,
                      temp_c: float | None = None) -> dict:
    """Demodulate 9-phase or 5-phase raw frame into axial depth, amplitude and telemetry."""
    grid = np.asarray(grid)
    if grid.ndim != 2 or grid.shape[1] != SENSOR_WIDTH or grid.shape[0] % SUBFRAME_ROWS != 0:
        raise ValueError(f"Unexpected grid dimensions: {grid.shape}")

    total_blocks = grid.shape[0] // SUBFRAME_ROWS
    all_blocks = grid.reshape(total_blocks, SUBFRAME_ROWS, SENSOR_WIDTH)

    # Detect valid subframe count (9 for dual-freq, 5 for single-freq) from telemetry
    valid_blocks = 0
    for idx in range(total_blocks):
        if int(all_blocks[idx, 0, 24]) == SENSOR_WIDTH - 1 and int(all_blocks[idx, 0, 26]) == ACTIVE_HEIGHT - 1:
            valid_blocks += 1
        else:
            break

    if valid_blocks not in (5, 9):
        raise ValueError(f"Unsupported valid subframe count {valid_blocks} (total blocks={total_blocks})")

    blocks = all_blocks[:valid_blocks]
    telemetry = [parse_pseudodata(block[0]) for block in blocks]
    seq_indices = [t["seq_index"] for t in telemetry]
    if seq_indices != list(range(valid_blocks)):
        raise ValueError(f"Corrupted subframe sequence order: {seq_indices}")
    eye_errors = [t["eye_safety_error"] for t in telemetry if t["eye_safety_error"]]
    if eye_errors:
        raise RuntimeError(f"Eye safety error detected: {eye_errors}")

    # Resolve sensor temperature from PseudoData NTC ADC if available
    measured_temps = [t["temperature_c"] for t in telemetry if t["temperature_c"] is not None]
    eff_temp_c = temp_c if temp_c is not None else (
        float(np.mean(measured_temps)) if measured_temps else DEFAULT_TEMP_C
    )

    eff_lens = lens if lens is not None else (calib.lens if calib is not None else REFERENCE_INTRINSICS)
    ambient = blocks[0, 1:]
    height, width = ambient.shape
    num_freqs = (valid_blocks - 1) // 4
    samples = blocks[1:, 1:].astype(np.float64).reshape(num_freqs, 4, height, width)

    # M2453_B11 / Spectre quadrature convention: I = s0 - s180, Q = s270 - s90
    i_comp = samples[:, 0] - samples[:, 2]
    q_comp = samples[:, 3] - samples[:, 1]
    amplitudes = np.hypot(i_comp, q_comp) / 2.0
    phases = np.arctan2(q_comp, i_comp) % math.tau

    fcal1 = calib.freqs.get(int(FREQ1)) if calib is not None else None
    fcal2 = calib.freqs.get(int(FREQ2)) if calib is not None else None

    if num_freqs == 2:
        d1 = _apply_freq_calibration(phases[0], FREQ1, fcal1, eff_temp_c)
        d2 = _apply_freq_calibration(phases[1], FREQ2, fcal2, eff_temp_c)
        # Dual-frequency unwrapping over 4 * D_MAX1 == 3 * D_MAX2 == D_BEAT (~7.465 m)
        best_err = np.full((height, width), np.inf, dtype=np.float64)
        distance = np.zeros((height, width), dtype=np.float64)
        for k1 in range(4):
            c1 = d1 + k1 * D_MAX1
            for k2 in range(3):
                c2 = d2 + k2 * D_MAX2
                err = np.abs(c1 - c2)
                better = err < best_err
                best_err = np.where(better, err, best_err)
                # Frequency-weighted blend (f1 has 4/3 higher phase SNR than f2)
                blended = (4.0 * c1 + 3.0 * c2) / 7.0
                distance = np.where(better, blended, distance)
        dual_freq_disagreement_m = best_err
    else:
        distance = _apply_freq_calibration(phases[0], FREQ1, fcal1, eff_temp_c)
        dual_freq_disagreement_m = np.zeros((height, width), dtype=np.float64)

    depth_map = distance * ray_projection(height, width, eff_lens)
    amplitude_map = amplitudes.mean(axis=0)
    validity_map = (
        (amplitudes >= min_amplitude).all(axis=0)
        & ((samples > 10) & (samples < 4090)).all(axis=(0, 1))
    )
    if calib is not None and calib.bad_pixel_mask.shape == (height, width):
        validity_map &= ~calib.bad_pixel_mask

    roi = (
        slice(max(0, int(eff_lens.cy) - roi_half_size), min(height, int(eff_lens.cy) + roi_half_size)),
        slice(max(0, int(eff_lens.cx) - roi_half_size), min(width, int(eff_lens.cx) + roi_half_size)),
    )
    roi_valid = validity_map[roi]
    roi_depths = depth_map[roi][roi_valid]
    roi_amplitudes = amplitude_map[roi][roi_valid]
    valid_count = int(validity_map.sum())
    total_pixels = height * width

    return {
        "mode_subframes": valid_blocks,
        "calibrated": calib is not None and bool(calib.freqs),
        "calibration_serial": calib.serial if calib is not None else "",
        "temperature_c": eff_temp_c,
        "temperature_measured": bool(measured_temps),
        "telemetry": telemetry,
        "width": width,
        "height": height,
        "total_pixels": total_pixels,
        "valid_pixels": valid_count,
        "valid_percentage": valid_count / total_pixels * 100.0,
        "global_mean_depth_m": float(depth_map[validity_map].mean()) if valid_count else 0.0,
        "global_mean_amplitude": float(amplitude_map[validity_map].mean()) if valid_count else 0.0,
        "dual_freq_median_err_mm": float(np.median(dual_freq_disagreement_m[validity_map]) * 1000.0)
        if valid_count and num_freqs == 2 else 0.0,
        "ambient_mean_raw": float(ambient.mean()),
        "roi": {
            "cx": eff_lens.cx,
            "cy": eff_lens.cy,
            "half_size": roi_half_size,
            "valid_samples": int(roi_depths.size),
            "mean_depth_m": float(roi_depths.mean()) if roi_depths.size else 0.0,
            "std_depth_m": float(roi_depths.std(ddof=1)) if roi_depths.size > 1 else 0.0,
            "mean_amplitude": float(roi_amplitudes.mean()) if roi_amplitudes.size else 0.0,
        },
        "amplitude_map": amplitude_map,
        "depth_map": depth_map,
        "validity_map": validity_map,
    }


def _synth_grid(target_z: float, num_subframes: int, total_blocks: int,
                lens: Intrinsics, calib: TofCalibration | None = None,
                temp_c: float = 30.0) -> np.ndarray:
    """Generate a synthetic 9-phase or 5-phase raw grid with optional calibration distortions."""
    h, w = ACTIVE_HEIGHT, SENSOR_WIDTH
    proj = ray_projection(h, w, lens)
    d_radial = target_z / proj
    grid = np.zeros((total_blocks, SUBFRAME_ROWS, w), dtype=np.uint16)

    for s in range(num_subframes):
        grid[s, 0, 0] = 2
        grid[s, 0, 3] = 42
        grid[s, 0, 5] = s
        grid[s, 0, 23] = 0
        grid[s, 0, 24] = w - 1
        grid[s, 0, 25] = 0
        grid[s, 0, 26] = h - 1
        grid[s, 0, 28] = 974
        grid[s, 0, 29] = 9
        grid[s, 0, 43] = 0x0034

    grid[0, 1:] = 2048
    freqs = [FREQ1, FREQ2] if num_subframes == 9 else [FREQ1]
    for f_idx, fhz in enumerate(freqs):
        d_max = SPEED_OF_LIGHT / (2.0 * fhz)
        d_eff = d_radial.copy()
        if calib is not None and int(fhz) in calib.freqs:
            fc = calib.freqs[int(fhz)]
            t0, c1, c2, c3 = fc.temp_comp
            dt = temp_c - t0
            d_eff = d_eff - fc.fppn - fc.offset + (((c3 * dt + c2) * dt + c1) * dt)
        phi = (d_eff * (math.tau / d_max)) % math.tau
        for k in range(4):
            phase_shift = k * (math.pi / 2.0)
            # Hardware cos(phi + k*pi/2) convention
            val = np.rint(2048.0 + 50.0 * np.cos(phi + phase_shift)).astype(np.uint16)
            grid[1 + f_idx * 4 + k, 1:] = val

    return grid.reshape(total_blocks * SUBFRAME_ROWS, w)


def run_unit_tests():
    """Verify demodulator, calibration pipeline, NTC math, and 5/9-phase modes."""
    print("Running ToF unit tests...")
    h, w = ACTIVE_HEIGHT, SENSOR_WIDTH

    # 1. Verify Brown-Conrady undistortion against the forward model.
    ux = np.linspace(-0.5, 0.5, 17)[None, :]
    uy = np.linspace(-0.4, 0.4, 15)[:, None]
    r2 = ux * ux + uy * uy
    radial = 1.0 + r2 * (
        REFERENCE_INTRINSICS.k1
        + r2 * (REFERENCE_INTRINSICS.k2 + r2 * REFERENCE_INTRINSICS.k3)
    )
    dx = ux * radial + 2 * REFERENCE_INTRINSICS.p1 * ux * uy + REFERENCE_INTRINSICS.p2 * (r2 + 2 * ux * ux)
    dy = uy * radial + REFERENCE_INTRINSICS.p1 * (r2 + 2 * uy * uy) + 2 * REFERENCE_INTRINSICS.p2 * ux * uy
    rx, ry = undistort(dx, dy, REFERENCE_INTRINSICS)
    assert np.max(np.abs(rx - ux)) < 1e-6 and np.max(np.abs(ry - uy)) < 1e-6

    # 2. Verify NTC temperature formula at 25°C (R_ntc == R_0 == 100k, R_ref == 6.8k).
    # At 25°C: dRef/dNtc = 1 + R_ref/R_0 = 1.068
    t_25 = compute_ntc_temperature_c(2068, 2000, 1000, 1000)
    assert t_25 is not None and abs(t_25 - 25.0) < 0.05, f"NTC 25C mismatch: {t_25}"

    # 3. Verify synthetic 9-phase dual-frequency with synthetic FPPN + offset + tempComp
    yy, xx = np.mgrid[:h, :w]
    synth_cal = TofCalibration(
        width=w, height=h, serial="TEST-0001", lens=REFERENCE_INTRINSICS,
        bad_pixel_mask=np.zeros((h, w), dtype=bool),
        freqs={
            int(FREQ1): FreqCalibration(
                freq_hz=int(FREQ1),
                fppn=(-1.14 + 0.01 * np.sin(xx / 30.0) * np.cos(yy / 25.0)).astype(np.float32),
                temp_comp=(0.0, 0.001231, 0.0, 0.0),
                offset=0.07135,
            ),
            int(FREQ2): FreqCalibration(
                freq_hz=int(FREQ2),
                fppn=(-1.42 + 0.01 * np.cos(xx / 28.0) * np.sin(yy / 22.0)).astype(np.float32),
                temp_comp=(0.0, 0.001407, 0.0, 0.0),
                offset=0.08808,
            ),
        },
    )

    test_distances = [0.35, 0.75, 1.25, 2.50, 3.80, 5.20]
    for target_z in test_distances:
        grid = _synth_grid(target_z, 9, 11, REFERENCE_INTRINSICS, synth_cal, temp_c=35.0)
        res = compute_tof_frame(grid, min_amplitude=1.5, roi_half_size=5, calib=synth_cal, temp_c=35.0)
        err = abs(res["roi"]["mean_depth_m"] - target_z)
        assert err < 0.002, f"9-phase {target_z}m failed: {res['roi']['mean_depth_m']}m (err={err}m)"
        assert np.percentile(np.abs(res["depth_map"] - target_z), 99) < 0.005
        assert res["valid_percentage"] > 99.0
        assert abs(res["roi"]["mean_amplitude"] - 50.0) < 1.0

    # 4. Verify synthetic 5-phase single-frequency mode (within 0..1.86 m)
    for target_z in [0.40, 0.90, 1.40]:
        grid5 = _synth_grid(target_z, 5, 7, REFERENCE_INTRINSICS, synth_cal, temp_c=35.0)
        res5 = compute_tof_frame(grid5, min_amplitude=1.5, roi_half_size=5, calib=synth_cal, temp_c=35.0)
        err5 = abs(res5["roi"]["mean_depth_m"] - target_z)
        assert res5["mode_subframes"] == 5
        assert err5 < 0.002, f"5-phase {target_z}m failed: {res5['roi']['mean_depth_m']}m"

    print(f"PASS: 9-phase ({len(test_distances)} vectors), 5-phase (3 vectors), FPPN/tempComp, NTC, and lens tests passed (< 2 mm error).")


def main():
    parser = argparse.ArgumentParser(description="Process REAL3 IRS2381C ToF raw frame")
    parser.add_argument("--input", "-i", type=Path, default=Path("/tmp/tof.raw"),
                        help="Path to raw SBGGR12P frame")
    parser.add_argument("--calib", "-c", type=Path,
                        help="Path to Zwetschge nvmem dump or PMDJGF file (auto-detected if omitted)")
    parser.add_argument("--no-calib", action="store_true",
                        help="Disable factory calibration (raw uncalibrated demodulation)")
    parser.add_argument("--temp", type=float,
                        help="Override sensor temperature in Celsius")
    parser.add_argument("--min-amp", type=float, default=1.5,
                        help="Minimum amplitude threshold")
    parser.add_argument("--roi-size", type=int, default=10,
                        help="Half size of central ROI")
    parser.add_argument("--intrinsics", type=Path,
                        help="JSON file with fx, fy, cx, cy, k1, k2, p1, p2 and k3")
    parser.add_argument("--json", action="store_true",
                        help="Print JSON summary")
    parser.add_argument("--test", action="store_true",
                        help="Run built-in validation tests")
    args = parser.parse_args()

    if args.test:
        run_unit_tests()
        return

    if not args.input.exists():
        sys.exit(f"Error: input file {args.input} does not exist.")

    calib = None
    if not args.no_calib:
        calib_path = args.calib if args.calib else find_default_nvmem_path()
        if calib_path and calib_path.exists():
            calib = parse_pmdjgf_calibration(calib_path.read_bytes())

    lens = None
    if args.intrinsics:
        try:
            lens = Intrinsics(**json.loads(args.intrinsics.read_text()))
            if (any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in lens)
                    or not all(math.isfinite(value) for value in lens)
                    or min(lens.fx, lens.fy) <= 0):
                raise ValueError("Non-finite values or non-positive focal lengths")
        except (OSError, TypeError, ValueError) as error:
            parser.error(f"Invalid intrinsics: {error}")

    grid = unpack_mipi_12bit(args.input.read_bytes())
    result = compute_tof_frame(grid, min_amplitude=args.min_amp,
                               roi_half_size=args.roi_size,
                               lens=lens, calib=calib, temp_c=args.temp)

    if args.json:
        out = {k: v for k, v in result.items() if not k.endswith("_map")}
        print(json.dumps(out, indent=2))
    else:
        print(f"=== IRS2381C ToF Depth Analysis ({args.input.name}) ===")
        print(f"Calibration:       {'PMDJGF (' + result['calibration_serial'] + ')' if result['calibrated'] else 'Uncalibrated'}")
        print(f"Sensor Temp:       {result['temperature_c']:.2f} C ({'measured' if result['temperature_measured'] else 'nominal'})")
        print(f"Telemetry:")
        print(f"  Subframes:         {result['mode_subframes']} (seq {', '.join(str(t['seq_index']) for t in result['telemetry'])})")
        print(f"  Frame Number:      {result['telemetry'][0]['frame_number']}")
        print(f"  Eye Safety Error:  {result['telemetry'][0]['eye_safety_error']} (Normal)")
        print(f"Coverage:")
        print(f"  Valid Pixels:      {result['valid_pixels']}/{result['total_pixels']} ({result['valid_percentage']:.1f}%)")
        print(f"  Ambient Baseline:  {result['ambient_mean_raw']:.1f} raw DN")
        if result["mode_subframes"] == 9:
            print(f"  Dual-Freq Err:     {result['dual_freq_median_err_mm']:.1f} mm (median)")
        print(f"Center ROI ({result['roi']['half_size']*2}x{result['roi']['half_size']*2} around ({result['roi']['cx']:.1f}, {result['roi']['cy']:.1f})):")
        print(f"  Mean Z Distance:   {result['roi']['mean_depth_m']:.3f} m")
        print(f"  Std Deviation:     {result['roi']['std_depth_m']*1000.0:.1f} mm")
        print(f"  Mean Amplitude:    {result['roi']['mean_amplitude']:.2f} DN")
        print(f"Global Scene:")
        print(f"  Mean Z Distance:   {result['global_mean_depth_m']:.3f} m")
        print(f"  Mean Amplitude:    {result['global_mean_amplitude']:.2f} DN")


if __name__ == "__main__":
    main()
