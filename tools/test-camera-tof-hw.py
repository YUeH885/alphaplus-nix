#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
# Copyright (c) 2026 The LM-G820N Linux contributors
"""Infineon REAL3 IRS2381C ToF hardware calibration, use-case, and depth validation.

Performs live verification on target hardware:
  1. Zwetschge / PMDJGF nvmem calibration read, CRC32 validation, and RAM cache check
  2. 9-phase dual-frequency (MODE_9_5FPS / MODE_9_15FPS) capture, telemetry, live NTC
     temperature, calibrated depth, and streaming cadence
  3. 5-phase single-frequency (MODE_5_15FPS / MODE_5_30FPS) capture and high-speed cadence
  4. Dynamic V4L2_CID_EXPOSURE control and hardware eye-safety range clamping
  5. libcamera `cam` enumeration and NV12 / R16 / SBGGR12_CSI2P capture (when available)
  6. Post-streaming runtime suspend and clean kernel taint state
"""

import importlib.util
import os
from pathlib import Path
import re
import shutil
from statistics import median
import struct
import subprocess
import tempfile
import time

# Import calibrated ToF demodulator from process-camera-tof.py
_spec = importlib.util.spec_from_file_location(
    "process_camera_tof", Path(__file__).resolve().parent / "process-camera-tof.py"
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
compute_tof_frame = _mod.compute_tof_frame
parse_pmdjgf_calibration = _mod.parse_pmdjgf_calibration
unpack_mipi_12bit = _mod.unpack_mipi_12bit
parse_pseudodata = _mod.parse_pseudodata
find_default_nvmem_path = _mod.find_default_nvmem_path

FRAME_SIZE_9P = 224 * 1903 * 3 // 2  # 639,408 bytes
FRAME_SIZE_5P = 224 * 1211 * 3 // 2  # 406,896 bytes


def run_cmd(cmd, check=True):
    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C"},
    )
    if check and res.returncode != 0:
        raise RuntimeError(
            f"Command failed ({res.returncode}): {' '.join(cmd)}\n"
            f"STDOUT: {res.stdout}\nSTDERR: {res.stderr}"
        )
    return res


def find_tof_nodes(media="/dev/media0"):
    p = run_cmd(["media-ctl", "-d", media, "-p"])
    sensor_name = None
    for line in p.stdout.splitlines():
        if "entity" in line and "irs2381c" in line:
            parts = line.split(":")
            if len(parts) >= 2:
                tokens = parts[1].strip().split()
                sensor_name = f"{tokens[0]} {tokens[1]}"
            break

    if not sensor_name:
        raise RuntimeError(f"IRS2381C sensor subdevice not found in {media}")

    sensor_node = run_cmd(["media-ctl", "-d", media, "-e", sensor_name]).stdout.strip()
    video_node = run_cmd(["media-ctl", "-d", media, "-e", "msm_vfe3_video0"]).stdout.strip()
    if not sensor_node or not video_node:
        raise RuntimeError("Missing irs2381c subdev or msm_vfe3_video0 node")

    controls = run_cmd(
        ["v4l2-ctl", "-d", sensor_node, "--get-ctrl=camera_orientation,camera_sensor_rotation"]
    ).stdout
    geometry = dict(line.split(": ", 1) for line in controls.splitlines() if ": " in line)
    assert int(geometry["camera_orientation"].split()[0]) == 0, controls
    assert int(geometry["camera_sensor_rotation"]) == 270, controls

    return sensor_name, sensor_node, video_node


def configure_tof_mode(sensor_name, sensor_node, video_node, height, fps, media="/dev/media0"):
    run_cmd(["media-ctl", "-d", media, "-l", '"msm_csiphy3":1 -> "msm_csid3":0 [1]'])
    run_cmd(["media-ctl", "-d", media, "-l", '"msm_csid3":1 -> "msm_vfe3_rdi0":0 [1]'])

    fmt_str = f"[fmt:SBGGR12_1X12/224x{height} field:none]"
    for pad in (
        f'"{sensor_name}":0',
        '"msm_csiphy3":0',
        '"msm_csiphy3":1',
        '"msm_csid3":0',
        '"msm_csid3":1',
        '"msm_vfe3_rdi0":0',
    ):
        run_cmd(["media-ctl", "-d", media, "-V", f"{pad} {fmt_str}"])

    run_cmd(["v4l2-ctl", "-d", sensor_node, f"--set-subdev-fps=pad=0,fps={fps}"])
    run_cmd(
        ["v4l2-ctl", "-d", video_node, f"--set-fmt-video=width=224,height={height},pixelformat=pBCC"]
    )


def teardown_tof_links(media="/dev/media0"):
    run_cmd(["media-ctl", "-d", media, "-l", '"msm_csid3":1 -> "msm_vfe3_rdi0":0 [0]'], check=False)
    run_cmd(["media-ctl", "-d", media, "-l", '"msm_csiphy3":1 -> "msm_csid3":0 [0]'], check=False)


def capture_tof_frame(video_node, output_path, count=1):
    cmd = [
        "v4l2-ctl",
        "-d", video_node,
        "--stream-mmap",
        "--stream-poll",
        "--verbose",
        f"--stream-count={count}",
        f"--stream-to={output_path}",
    ]
    t0 = time.monotonic()
    result = run_cmd(cmd)
    dt = time.monotonic() - t0
    buffers = re.findall(
        r"^cap dqbuf:.*?\bseq:\s+(\d+)\b.*?\bts:\s+(\d+\.\d+)",
        result.stdout + result.stderr,
        re.MULTILINE,
    )
    assert len(buffers) == count, f"Expected {count} dequeue timestamps, got {buffers}"
    sequences = [int(seq) for seq, _ in buffers]
    assert all(b - a == 1 for a, b in zip(sequences, sequences[1:])), sequences
    return dt, [float(ts) for _, ts in buffers]


def verify_nvmem_calibration():
    calib_path = find_default_nvmem_path()
    assert calib_path is not None, "IRS2381C nvmem/eeprom calibration sysfs node not found"

    t0 = time.monotonic()
    calib = parse_pmdjgf_calibration(calib_path.read_bytes())
    dt1 = time.monotonic() - t0
    assert calib.width == 224 and calib.height == 172, (calib.width, calib.height)
    assert 80320000 in calib.freqs and 60240000 in calib.freqs, calib.freqs.keys()
    assert calib.serial, "Missing module serial in Zwetschge/PMDJGF calibration"

    # Second read must hit the kernel RAM cache.
    t1 = time.monotonic()
    calib2 = parse_pmdjgf_calibration(calib_path.read_bytes())
    dt2 = time.monotonic() - t1
    assert calib2.serial == calib.serial
    assert dt2 < 0.5, f"Cached nvmem read too slow: {dt2:.3f}s"

    print(
        f"Calibration nvmem verified: {calib_path} serial={calib.serial} "
        f"(1st read {dt1:.3f}s, cached {dt2 * 1000:.1f} ms)"
    )
    return calib


def verify_libcamera_tof(tmpdir):
    cam_bin = shutil.which("cam")
    if not cam_bin:
        print("Skipping libcamera `cam` checks (`cam` not in PATH)")
        return

    listed = run_cmd([cam_bin, "-l"]).stdout
    tof_match = re.search(r"^\s*(\d+):\s+.*irs2381c.*$", listed, re.MULTILINE)
    assert tof_match, f"irs2381c not enumerated by `cam -l`:\n{listed}"
    cam_idx = tof_match.group(1)
    print(f"libcamera enumerated ToF camera index {cam_idx}: {tof_match.group(0).strip()}")

    # 1. NV12 calibrated depth preview
    nv12_prefix = Path(tmpdir) / "tof_nv12-#.bin"
    run_cmd([
        cam_bin, "-c", cam_idx, "-C3",
        "-s", "width=224,height=172,pixelformat=NV12",
        f"-F{nv12_prefix}",
    ])
    nv12_file = Path(tmpdir) / "tof_nv12-000002.bin"
    if not nv12_file.exists():
        nv12_files = sorted(Path(tmpdir).glob("tof_nv12-*.bin"))
        assert nv12_files, "No NV12 frame captured by cam"
        nv12_file = nv12_files[-1]
    assert nv12_file.stat().st_size == 224 * 172 * 3 // 2, nv12_file.stat().st_size
    print(f"libcamera NV12 depth preview verified ({nv12_file.stat().st_size} B)")

    # 2. R16 16-bit millimeter depth stream
    r16_prefix = Path(tmpdir) / "tof_r16-#.bin"
    out = run_cmd([
        cam_bin, "-c", cam_idx, "-C3",
        "-s", "width=224,height=172,pixelformat=R16",
        "--metadata",
        f"-F{r16_prefix}",
    ]).stdout
    r16_files = sorted(Path(tmpdir).glob("tof_r16-*.bin"))
    assert r16_files, "No R16 depth frame captured by cam"
    r16_bytes = r16_files[-1].read_bytes()
    assert len(r16_bytes) == 224 * 172 * 2, len(r16_bytes)
    depths_mm = struct.unpack(f"<{224 * 172}H", r16_bytes)
    valid_mm = [d for d in depths_mm if 50 <= d <= 7500]
    valid_pct = 100.0 * len(valid_mm) / len(depths_mm)
    assert valid_pct >= 20.0, f"R16 valid depth coverage too low: {valid_pct:.1f}%"
    mean_mm = sum(valid_mm) / len(valid_mm)
    assert "SensorTemperature" in out, f"Missing SensorTemperature metadata:\n{out}"
    print(
        f"libcamera R16 depth stream verified: {valid_pct:.1f}% valid, "
        f"mean={mean_mm:.0f} mm, SensorTemperature reported"
    )

    # 3. Raw SBGGR12_CSI2P stream
    raw_prefix = Path(tmpdir) / "tof_raw-#.bin"
    run_cmd([
        cam_bin, "-c", cam_idx, "-C2",
        "-s", "role=raw,width=224,height=1903,pixelformat=SBGGR12_CSI2P",
        f"-F{raw_prefix}",
    ])
    raw_files = sorted(Path(tmpdir).glob("tof_raw-*.bin"))
    assert raw_files, "No RAW12 frame captured by cam"
    assert raw_files[-1].stat().st_size == FRAME_SIZE_9P, raw_files[-1].stat().st_size
    print(f"libcamera RAW12 stream verified ({raw_files[-1].stat().st_size} B)")


def main():
    print("=== IRS2381C ToF Live Hardware Verification ===")

    sensor_name, sensor_node, video_node = find_tof_nodes()
    print(f"Found ToF sensor: {sensor_name} ({sensor_node}) -> {video_node}")

    runtime_sysfs = Path("/sys/bus/i2c/devices") / sensor_name.split()[-1] / "power/runtime_status"
    if runtime_sysfs.exists():
        print(f"Initial runtime status: {runtime_sysfs.read_text().strip()}")

    # 1. Verify nvmem Zwetschge/PMDJGF calibration while idle
    calib = verify_nvmem_calibration()

    with tempfile.TemporaryDirectory() as tmpdir:
        # 2. MODE_9_5FPS calibrated 9-phase capture + live NTC temperature (2 frames)
        configure_tof_mode(sensor_name, sensor_node, video_node, height=1903, fps=5)
        m9_path = Path(tmpdir) / "m9_5fps.raw"
        dt, _ = capture_tof_frame(video_node, m9_path, count=2)
        payload = m9_path.read_bytes()
        assert len(payload) == FRAME_SIZE_9P * 2, len(payload)
        # Use frame #1 where NTC ADC has settled
        grid = unpack_mipi_12bit(payload[FRAME_SIZE_9P:])
        res = compute_tof_frame(grid, min_amplitude=1.5, roi_half_size=10, calib=calib)

        assert len(res["telemetry"]) == 9, len(res["telemetry"])
        assert [t["seq_index"] for t in res["telemetry"]] == list(range(9))
        assert all(t["eye_safety_error"] == 0 for t in res["telemetry"])
        exp_default_f1 = res["telemetry"][1]["exposure_code"]
        assert res["temperature_measured"], "NTC temperature not measured on frame 1"
        temp_c = res["temperature_c"]
        assert 5.0 <= temp_c <= 70.0, f"NTC temperature out of range: {temp_c} C"
        assert 0.1 <= res["global_mean_depth_m"] <= 7.4, res["global_mean_depth_m"]
        assert res["valid_percentage"] >= 20.0, res["valid_percentage"]
        print(
            f"MODE_9_5FPS verified ({dt:.2f}s): valid={res['valid_percentage']:.1f}%, "
            f"mean={res['global_mean_depth_m']:.3f}m, ROI={res['roi']['mean_depth_m']:.3f}m, "
            f"T={temp_c:.1f}C, exp80={hex(exp_default_f1)}"
        )

        # 3. Dynamic V4L2_CID_EXPOSURE and eye-safety clamping
        run_cmd(["v4l2-ctl", "-d", sensor_node, "--set-ctrl=exposure=500"])
        exp_low_path = Path(tmpdir) / "m9_5fps_500us.raw"
        capture_tof_frame(video_node, exp_low_path, count=1)
        grid_low = unpack_mipi_12bit(exp_low_path.read_bytes())
        t_low = [parse_pseudodata(grid_low[i * 173]) for i in range(9)]
        assert all(t["eye_safety_error"] == 0 for t in t_low), t_low
        assert t_low[1]["exposure_code"] != exp_default_f1, "Exposure control did not take effect"
        # Verify kernel clamps out-of-range exposure to eye-safety max (2020 us)
        run_cmd(["v4l2-ctl", "-d", sensor_node, "--set-ctrl=exposure=99999"], check=False)
        exp_readback = int(
            run_cmd(["v4l2-ctl", "-d", sensor_node, "--get-ctrl=exposure"]).stdout.split(":")[1]
        )
        assert exp_readback <= 2020, f"Eye-safety exposure clamp failed: {exp_readback}"
        print(
            f"Exposure control verified: 500us code={hex(t_low[1]['exposure_code'])} "
            f"(vs default {hex(exp_default_f1)}), clamped max={exp_readback} us"
        )

        # 4. MODE_9_15FPS streaming cadence
        configure_tof_mode(sensor_name, sensor_node, video_node, height=1903, fps=15)
        m9_15_path = Path(tmpdir) / "m9_15fps.raw"
        dt_9_15, ts_9_15 = capture_tof_frame(video_node, m9_15_path, count=3)
        period_9_15 = median([b - a for a, b in zip(ts_9_15, ts_9_15[1:])])
        assert period_9_15 < 0.08, f"MODE_9_15FPS too slow: {period_9_15 * 1000:.1f} ms"
        print(
            f"MODE_9_15FPS verified: 3 frames in {dt_9_15:.2f}s, "
            f"interval {period_9_15 * 1000:.1f} ms ({1 / period_9_15:.1f} fps)"
        )

        # 5. MODE_5_15FPS calibrated 5-phase capture
        configure_tof_mode(sensor_name, sensor_node, video_node, height=1211, fps=15)
        m5_15_path = Path(tmpdir) / "m5_15fps.raw"
        capture_tof_frame(video_node, m5_15_path, count=2)
        payload_5 = m5_15_path.read_bytes()
        assert len(payload_5) == FRAME_SIZE_5P * 2, len(payload_5)
        grid_5 = unpack_mipi_12bit(payload_5[FRAME_SIZE_5P:])
        res_5 = compute_tof_frame(grid_5, min_amplitude=1.5, roi_half_size=10, calib=calib)
        assert len(res_5["telemetry"]) == 5, len(res_5["telemetry"])
        assert [t["seq_index"] for t in res_5["telemetry"]] == list(range(5))
        assert all(t["eye_safety_error"] == 0 for t in res_5["telemetry"])
        assert res_5["valid_percentage"] >= 20.0, res_5["valid_percentage"]
        print(
            f"MODE_5_15FPS verified: 5 subframes, valid={res_5['valid_percentage']:.1f}%, "
            f"mean={res_5['global_mean_depth_m']:.3f}m"
        )

        # 6. MODE_5_30FPS high-speed streaming cadence
        configure_tof_mode(sensor_name, sensor_node, video_node, height=1211, fps=30)
        m5_30_path = Path(tmpdir) / "m5_30fps.raw"
        dt_5_30, ts_5_30 = capture_tof_frame(video_node, m5_30_path, count=3)
        period_5_30 = median([b - a for a, b in zip(ts_5_30, ts_5_30[1:])])
        assert period_5_30 < 0.04, f"MODE_5_30FPS too slow: {period_5_30 * 1000:.1f} ms"
        print(
            f"MODE_5_30FPS verified: 3 frames in {dt_5_30:.2f}s, "
            f"interval {period_5_30 * 1000:.1f} ms ({1 / period_5_30:.1f} fps)"
        )

        # Tear down raw V4L2 links before handing over to libcamera
        teardown_tof_links()

        # 7. libcamera end-to-end ToF depth & raw capture
        verify_libcamera_tof(tmpdir)

    # 8. Verify runtime suspend after streaming stops
    if runtime_sysfs.exists():
        deadline = time.monotonic() + 3.0
        status = runtime_sysfs.read_text().strip()
        while time.monotonic() < deadline:
            status = runtime_sysfs.read_text().strip()
            if status == "suspended":
                break
            time.sleep(0.1)
        print(f"Post-streaming runtime status: {status}")
        assert status == "suspended", f"Device failed to enter suspended state: {status}"

    # 9. Check kernel taint
    taint = Path("/proc/sys/kernel/tainted").read_text().strip()
    print(f"Kernel tainted value: {taint}")
    assert taint == "0", f"Kernel tainted: {taint}"

    print("PASS: IRS2381C ToF calibration, multi-use-case, exposure, and libcamera verified.")


if __name__ == "__main__":
    main()
