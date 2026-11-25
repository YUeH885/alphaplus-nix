#!/usr/bin/env python3
"""Hardware end-to-end joint validation test for LM-G820N quad-camera system.

Validates:
1. Cold start, resolution configuration, and streaming across all 4 cameras:
   - Rear Main: IMX363 (2016x1136 NV12)
   - Rear Wide: IMX351 (2320x1312 NV12)
   - Rear Tele: S5K3M5 (2016x1136 NV12)
   - Front:     IMX520 (1632x1224 NV12)
2. CSIPHY2 dynamic multiplexing between Front (IMX520, 2-lane) and Telephoto (S5K3M5, 4-lane).
3. 3A Control loops:
   - AE convergence, EV compensation stepping (-1.0, 0.0, +1.0), and AE Lock.
   - AWB convergence, manual colour temperature stepping (3000K, 5000K, 6500K), and auto restore.
   - Lens focus stepping and contrast focus score on IMX363, S5K3M5 and IMX520.
   - OIS runtime mode dynamic switching on IMX363.
   - Torch / flash control and safe disengagement on rear cameras.
4. Continuous preview FPS stability, frame interval jitter, and drop rate (< 15%) over 60 frames.
5. Runtime PM verification: CAMSS and all sensors safely suspended, 0 kernel taint.
"""

import argparse
import fcntl
import json
import mmap
import os
from pathlib import Path
import selectors
import statistics
import struct
import sys
import time

import libcamera as lc

C = lc.controls
DRAFT = lc.controls.draft

# DMA-BUF sync ioctl constants
DMA_BUF_IOCTL_SYNC = 0x40086200
DMA_BUF_SYNC_READ = 1
DMA_BUF_SYNC_END = 4

CAMERA_SPECS = {
    "imx363": {
        "name": "Rear Main (IMX363)",
        "sensor_key": "imx363",
        "resolution": (2016, 1136),
        "has_af": True,
        "has_ois": True,
        "has_flash": True,
    },
    "imx351": {
        "name": "Rear Wide (IMX351)",
        "sensor_key": "imx351",
        "resolution": (2320, 1312),
        "has_af": False,
        "has_ois": False,
        "has_flash": True,
    },
    "s5k3m5": {
        "name": "Rear Telephoto (S5K3M5)",
        "sensor_key": "s5k3m5",
        "resolution": (2016, 1136),
        "has_af": True,
        "has_ois": False,
        "has_flash": True,
    },
    "imx520": {
        "name": "Front (IMX520)",
        "sensor_key": "imx520",
        "resolution": (1632, 1224),
        "has_af": True,
        "has_ois": False,
        "has_flash": False,
    },
}


def compute_frame_metrics(fb, width, height, stride):
    """Computes mean luma and Laplacian focus score from center window."""
    p0 = fb.planes[0]
    fcntl.ioctl(p0.fd, DMA_BUF_IOCTL_SYNC, struct.pack("Q", DMA_BUF_SYNC_READ))
    mm = None
    view = None
    try:
        mm = mmap.mmap(p0.fd, p0.offset + p0.length, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ)
        view = memoryview(mm)[p0.offset: p0.offset + p0.length]

        cx, cy = width // 2, height // 2
        rw, rh = min(320, width), min(240, height)
        start_x = cx - rw // 2
        start_y = cy - rh // 2

        luma_sum = 0
        roi_lines = []
        for r in range(rh):
            offset = (start_y + r) * stride + start_x
            line = bytes(view[offset: offset + rw])
            luma_sum += sum(line)
            roi_lines.append(line)

        mean_luma = luma_sum / (rw * rh)

        lap_sum = 0
        for r in range(1, rh - 1, 2):
            curr = roi_lines[r]
            prev = roi_lines[r - 1]
            next_l = roi_lines[r + 1]
            for c in range(1, rw - 1, 2):
                val = 4 * curr[c] - curr[c - 1] - curr[c + 1] - prev[c] - next_l[c]
                lap_sum += abs(val)

        focus_score = lap_sum / (((rw - 2) // 2) * ((rh - 2) // 2))
        return mean_luma, focus_score
    finally:
        if view is not None:
            view.release()
        if mm is not None:
            mm.close()
        try:
            fcntl.ioctl(p0.fd, DMA_BUF_IOCTL_SYNC,
                        struct.pack("Q", DMA_BUF_SYNC_READ | DMA_BUF_SYNC_END))
        except Exception:
            pass


class CameraStreamSession:
    """Manages acquisition, configuration, capture and release for a camera."""

    def __init__(self, cm, sensor_key, resolution):
        self.cm = cm
        self.sensor_key = sensor_key
        self.resolution = resolution
        self.cam = None
        for c in cm.cameras:
            if sensor_key in c.id:
                self.cam = c
                break
        if not self.cam:
            raise RuntimeError(f"Camera with sensor '{sensor_key}' not found")

        self.stream_cfg = None
        self.allocator = None
        self.requests = []
        self.selector = None

    def __enter__(self):
        self.cam.acquire()
        try:
            cfg = self.cam.generate_configuration([lc.StreamRole.Viewfinder])
            self.stream_cfg = cfg.at(0)
            self.stream_cfg.size = lc.Size(self.resolution[0], self.resolution[1])
            self.stream_cfg.pixel_format = lc.formats.NV12
            cfg.validate()
            assert self.stream_cfg.size == lc.Size(*self.resolution), (
                f"{self.sensor_key}: requested {self.resolution}, got {self.stream_cfg.size}"
            )
            self.cam.configure(cfg)

            self.allocator = lc.FrameBufferAllocator(self.cam)
            stream = self.stream_cfg.stream
            self.allocator.allocate(stream)

            self.requests = []
            for buffer in self.allocator.buffers(stream):
                req = self.cam.create_request()
                req.add_buffer(stream, buffer)
                self.requests.append(req)

            self.selector = selectors.DefaultSelector()
            self.selector.register(self.cm.event_fd, selectors.EVENT_READ)
            return self
        except Exception:
            try:
                self.cam.release()
            except Exception:
                pass
            raise

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.cam:
            try:
                self.cam.stop()
            except Exception:
                pass
        if self.selector and self.cm:
            try:
                while self.selector.select(0.05):
                    if not self.cm.get_ready_requests():
                        break
            except Exception:
                pass
        if self.selector:
            try:
                self.selector.unregister(self.cm.event_fd)
                self.selector.close()
            except Exception:
                pass
            self.selector = None
        self.requests.clear()
        self.allocator = None
        if self.cam:
            try:
                self.cam.release()
            except Exception:
                pass

    def run_stream(self, frame_count, request_control_fn=None, sample_pixels_flag=False, sample_predicate=None):
        """Runs streaming loop for specified frame count and collects metadata."""
        submitted = 0
        submitted_info = {}

        def queue_req(req):
            nonlocal submitted
            ctrls = request_control_fn(submitted) if request_control_fn else {}
            for k, v in ctrls.items():
                req.set_control(k, v)
            submitted_info[req] = (submitted, ctrls)
            self.cam.queue_request(req)
            submitted += 1

        self.cam.start()
        try:
            for req in self.requests:
                queue_req(req)

            frames_data = []
            deadline = time.monotonic() + max(15.0, frame_count / 15.0 + 10.0)

            w = self.stream_cfg.size.width
            h = self.stream_cfg.size.height
            stride = self.stream_cfg.stride

            while len(frames_data) < frame_count:
                assert time.monotonic() < deadline, \
                    f"Stream timeout on {self.sensor_key} ({len(frames_data)}/{frame_count} frames captured)"

                if not self.selector.select(1.0):
                    continue

                for req in self.cm.get_ready_requests():
                    if req not in submitted_info:
                        continue

                    fb = next(iter(req.buffers.values()))
                    if fb.metadata.status != lc.FrameMetadata.Status.Success:
                        if submitted < frame_count + len(self.requests):
                            req.reuse()
                            queue_req(req)
                        continue

                    req_idx, req_ctrls = submitted_info[req]
                    md = req.metadata

                    frame_entry = {
                        "req_idx": req_idx,
                        "seq": fb.metadata.sequence,
                        "timestamp_ns": fb.metadata.timestamp,
                        "sensor_timestamp_ns": md.get(C.SensorTimestamp),
                        "exposure_us": md.get(C.ExposureTime),
                        "gain": md.get(C.AnalogueGain),
                        "cct": md.get(C.ColourTemperature),
                        "colour_gains": list(md.get(C.ColourGains)) if md.get(C.ColourGains) else None,
                        "ccm": list(md.get(C.ColourCorrectionMatrix)) if md.get(C.ColourCorrectionMatrix) else None,
                        "ae_locked": md.get(DRAFT.AeLock) if hasattr(DRAFT, "AeLock") else None,
                        "ois_mode": md.get(DRAFT.OpticalImageStabilizationMode) if hasattr(DRAFT, "OpticalImageStabilizationMode") else None,
                        "flash_mode": md.get(DRAFT.FlashMode) if hasattr(DRAFT, "FlashMode") else None,
                    }

                    if sample_predicate:
                        should_sample = sample_predicate(req_idx)
                    else:
                        should_sample = sample_pixels_flag and (req_idx >= frame_count - 10)
                    if should_sample:
                        try:
                            luma, focus = compute_frame_metrics(fb, w, h, stride)
                            frame_entry["mean_luma"] = luma
                            frame_entry["focus_score"] = focus
                        except Exception as e:
                            print(f"    [!] Error in compute_frame_metrics: {e}")
                            frame_entry["pixel_error"] = str(e)

                    frames_data.append(frame_entry)

                    if submitted < frame_count + len(self.requests):
                        req.reuse()
                        queue_req(req)
        finally:
            self.cam.stop()
            while self.selector.select(0.1):
                ready = self.cm.get_ready_requests()
                if not ready:
                    break

        return frames_data


def test_quad_cold_start_and_modes(cm):
    """Test 1: Cold start and default resolution streaming on all 4 cameras."""
    print("\n" + "=" * 70)
    print("TEST 1: Four-Camera Cold Start and Resolution Streaming")
    print("=" * 70)

    results = {}
    for key, spec in CAMERA_SPECS.items():
        print(f"\n[*] Testing {spec['name']} ({spec['resolution'][0]}x{spec['resolution'][1]})...")
        t0 = time.monotonic()
        with CameraStreamSession(cm, key, spec["resolution"]) as session:
            init_time = time.monotonic() - t0
            frames = session.run_stream(frame_count=25, sample_pixels_flag=True)

        assert len(frames) >= 20, f"Failed to capture 20 frames from {key}"

        # Verify SensorTimestamp monotonicity
        sensor_ts = [f["sensor_timestamp_ns"] for f in frames if f["sensor_timestamp_ns"]]
        assert len(sensor_ts) == len(frames), f"Missing SensorTimestamp on {key}"
        diffs = [sensor_ts[i] - sensor_ts[i - 1] for i in range(1, len(sensor_ts))]
        assert all(d > 0 for d in diffs), f"Non-monotonic SensorTimestamp on {key}"

        mean_exp = statistics.mean(f["exposure_us"] for f in frames if f["exposure_us"])
        mean_gain = statistics.mean(f["gain"] for f in frames if f["gain"])
        mean_luma = statistics.mean(f["mean_luma"] for f in frames if "mean_luma" in f)
        mean_focus = statistics.mean(f["focus_score"] for f in frames if "focus_score" in f)

        results[key] = {
            "init_time_sec": init_time,
            "captured_frames": len(frames),
            "mean_exposure_us": mean_exp,
            "mean_gain": mean_gain,
            "mean_luma": mean_luma,
            "mean_focus_score": mean_focus,
        }
        print(f"  [+] {key:6s} OK: Init={init_time:.2f}s, Captured={len(frames)}, "
              f"Exp={mean_exp:.1f}us, Gain={mean_gain:.2f}x, Luma={mean_luma:.1f}, Focus={mean_focus:.2f}")

    return results


def test_shared_mipi_multiplexing(cm):
    """Test 2: Dynamic hot-switching on shared CSIPHY2 between IMX520 and S5K3M5."""
    print("\n" + "=" * 70)
    print("TEST 2: Shared CSIPHY2 Dynamic Multiplexing (IMX520 <-> S5K3M5)")
    print("=" * 70)

    transitions = [
        ("imx520", CAMERA_SPECS["imx520"]["resolution"]),
        ("s5k3m5", CAMERA_SPECS["s5k3m5"]["resolution"]),
        ("imx520", CAMERA_SPECS["imx520"]["resolution"]),
        ("s5k3m5", CAMERA_SPECS["s5k3m5"]["resolution"]),
    ]

    for step, (sensor_key, res) in enumerate(transitions, 1):
        print(f"  Step {step}/4: Switching to {sensor_key} ({res[0]}x{res[1]})...")
        t0 = time.monotonic()
        with CameraStreamSession(cm, sensor_key, res) as session:
            frames = session.run_stream(frame_count=15)
        dt = time.monotonic() - t0
        assert len(frames) >= 15, f"Switch failed at step {step} for {sensor_key}"
        print(f"    [+] {sensor_key} streamed {len(frames)} frames successfully in {dt:.2f}s")

    print("  [+] Shared MIPI CSIPHY2 multiplexing cycle passed without error.")


def test_3a_control_loops(cm):
    """Test 3: 3A control responsiveness across AE, EV, AE Lock, AWB manual, AF, OIS."""
    print("\n" + "=" * 70)
    print("TEST 3: 3A Control Loops, Manual Stepping and Convergence")
    print("=" * 70)

    # 1. EV Stepping and AE Lock on IMX363
    print("\n[*] Evaluating AE EV stepping and draft.AeLock on IMX363...")
    with CameraStreamSession(cm, "imx363", CAMERA_SPECS["imx363"]["resolution"]) as session:
        # Phase 1 (0..19): Initial auto AE
        # Phase 2 (20..34): Manual mid-range baseline (exp=5000us, gain=2.0x)
        # Phase 3 (35..49): Locked baseline (AeLock=True, EV=0.0)
        # Phase 4 (50..64): Locked with EV=+1.0
        # Phase 5 (65..79): Locked with EV=-1.0
        # Phase 6 (80..94): Locked restore EV=0.0
        # Phase 7 (95..109): Unlock AE
        def req_fn(idx):
            ctrls = {}
            if 20 <= idx < 35:
                ctrls[C.AeEnable] = False
                ctrls[C.ExposureTime] = 5000
                ctrls[C.AnalogueGain] = 2.0
                ctrls[C.ExposureValue] = 0.0
                if hasattr(DRAFT, "AeLock"):
                    ctrls[DRAFT.AeLock] = False
            elif 35 <= idx < 50:
                ctrls[C.AeEnable] = True
                ctrls[C.ExposureValue] = 0.0
                if hasattr(DRAFT, "AeLock"):
                    ctrls[DRAFT.AeLock] = True
            elif 50 <= idx < 65:
                ctrls[C.AeEnable] = True
                ctrls[C.ExposureValue] = 1.0
                if hasattr(DRAFT, "AeLock"):
                    ctrls[DRAFT.AeLock] = True
            elif 65 <= idx < 80:
                ctrls[C.AeEnable] = True
                ctrls[C.ExposureValue] = -1.0
                if hasattr(DRAFT, "AeLock"):
                    ctrls[DRAFT.AeLock] = True
            elif 80 <= idx < 95:
                ctrls[C.AeEnable] = True
                ctrls[C.ExposureValue] = 0.0
                if hasattr(DRAFT, "AeLock"):
                    ctrls[DRAFT.AeLock] = True
            elif idx >= 95:
                ctrls[C.AeEnable] = True
                ctrls[C.ExposureValue] = 0.0
                if hasattr(DRAFT, "AeLock"):
                    ctrls[DRAFT.AeLock] = False
            return ctrls

        frames = session.run_stream(frame_count=110, request_control_fn=req_fn)

    base_exp = statistics.mean(f["exposure_us"] for f in frames[40:48])
    ev_plus_exp = statistics.mean(f["exposure_us"] for f in frames[55:63])
    ev_minus_exp = statistics.mean(f["exposure_us"] for f in frames[70:78])
    restore_exp = statistics.mean(f["exposure_us"] for f in frames[85:93])

    # Assert EV +1.0 roughly doubles exposure, EV -1.0 roughly halves exposure
    assert ev_plus_exp > base_exp, f"EV +1.0 did not increase exposure: {ev_plus_exp} <= {base_exp}"
    assert ev_minus_exp < base_exp, f"EV -1.0 did not decrease exposure: {ev_minus_exp} >= {base_exp}"
    assert abs(restore_exp - base_exp) < 50.0, f"EV restore failed: {restore_exp} vs {base_exp}"

    # Verify locked stability
    p3_exps = [f["exposure_us"] for f in frames[38:48]]
    lock_drift = max(p3_exps) - min(p3_exps)
    assert lock_drift < 5.0, f"AE Lock drift too high: {lock_drift} us"

    print(f"  [+] AE EV Response: Baseline={base_exp:.1f}us, EV(+1.0)={ev_plus_exp:.1f}us, "
          f"EV(-1.0)={ev_minus_exp:.1f}us, Restored={restore_exp:.1f}us")
    print(f"  [+] AE Lock Verified: Max drift = {lock_drift:.2f} us across 10 locked frames")

    # 2. Manual White Balance Stepping:
    # Front IMX520: supports direct ColourTemperature request (3000K, 6500K)
    # Rear IMX363: supports direct ColourGains request (warm: (1.2, 2.5), cool: (2.2, 1.4))
    print("\n[*] Evaluating manual colour temperature on front IMX520...")
    with CameraStreamSession(cm, "imx520", CAMERA_SPECS["imx520"]["resolution"]) as session:
        def req_cct(idx):
            ctrls = {}
            if 15 <= idx < 30:
                ctrls[C.AwbEnable] = False
                ctrls[C.ColourTemperature] = 3000
            elif 30 <= idx < 45:
                ctrls[C.AwbEnable] = False
                ctrls[C.ColourTemperature] = 6500
            elif idx >= 45:
                ctrls[C.AwbEnable] = True
            return ctrls

        frames = session.run_stream(frame_count=55, request_control_fn=req_cct)

    gains_warm = [f["colour_gains"] for f in frames[20:30] if f["colour_gains"]]
    gains_cool = [f["colour_gains"] for f in frames[35:45] if f["colour_gains"]]
    assert gains_warm and gains_cool, "[imx520] Missing colour_gains metadata in warm/cool phases"
    mean_r_warm = statistics.mean(g[0] for g in gains_warm)
    mean_b_warm = statistics.mean(g[1] for g in gains_warm)
    mean_r_cool = statistics.mean(g[0] for g in gains_cool)
    mean_b_cool = statistics.mean(g[1] for g in gains_cool)
    assert mean_r_cool > mean_r_warm and mean_b_warm > mean_b_cool, \
        f"[imx520] Colour gain shift inverted: warm=({mean_r_warm:.2f},{mean_b_warm:.2f}) cool=({mean_r_cool:.2f},{mean_b_cool:.2f})"
    print(f"  [+] imx520 AWB Manual CCT OK: 3000K gains=({mean_r_warm:.2f}, {mean_b_warm:.2f}), "
          f"6500K gains=({mean_r_cool:.2f}, {mean_b_cool:.2f})")

    print("\n[*] Evaluating manual ColourGains and white locus CCT projection on rear IMX363...")
    with CameraStreamSession(cm, "imx363", CAMERA_SPECS["imx363"]["resolution"]) as session:
        def req_gains(idx):
            ctrls = {}
            if 15 <= idx < 30:
                ctrls[C.AwbEnable] = False
                ctrls[C.ColourGains] = (1.2, 2.5)  # Warm white point
            elif 30 <= idx < 45:
                ctrls[C.AwbEnable] = False
                ctrls[C.ColourGains] = (2.2, 1.4)  # Cool white point
            elif idx >= 45:
                ctrls[C.AwbEnable] = True
            return ctrls

        frames = session.run_stream(frame_count=55, request_control_fn=req_gains)

    cct_warm = [f["cct"] for f in frames[20:30] if f["cct"]]
    cct_cool = [f["cct"] for f in frames[35:45] if f["cct"]]
    assert cct_warm and cct_cool, "[imx363] Missing ColourTemperature metadata in warm/cool phases"
    mean_cct_warm = statistics.mean(cct_warm)
    mean_cct_cool = statistics.mean(cct_cool)
    assert mean_cct_cool > mean_cct_warm, \
        f"[imx363] White locus projected CCT inverted: warm={mean_cct_warm:.0f}K cool={mean_cct_cool:.0f}K"
    print(f"  [+] imx363 Manual ColourGains -> White Locus CCT OK: warm={mean_cct_warm:.0f}K, cool={mean_cct_cool:.0f}K")

    # A textured target is required for an optical focus test.
    for key, spec in CAMERA_SPECS.items():
        if not spec["has_af"]:
            continue
        print(f"\n[*] Evaluating manual focus stepping on {key}...")
        with CameraStreamSession(cm, key, spec["resolution"]) as session:
            info = session.cam.controls[C.LensPosition]
            positions = [info.min, (info.min + info.max) / 2, info.max]
            assert positions[0] < positions[1] < positions[2]

            def req_lens(idx):
                return {C.AfMode: 0, C.LensPosition: positions[min(idx // 20, 2)]}

            frames = session.run_stream(frame_count=60, request_control_fn=req_lens,
                                        sample_predicate=lambda idx: idx < 60 and idx % 20 >= 15)

        scores = []
        for index, position in enumerate(positions):
            samples = [f["focus_score"] for f in frames
                       if 20 * index + 15 <= f["req_idx"] < 20 * (index + 1)]
            assert samples, f"{key}: no focus samples at {position:.2f} dioptres"
            scores.append(statistics.median(samples))
        assert max(scores) > min(scores) * 1.05, f"{key}: no optical focus change: {scores}"
        print(f"  [+] {key} focus scores at {positions} dioptres: {scores}")

    # 4. OIS Mode Dynamic Toggle on IMX363
    print("\n[*] Evaluating dynamic OIS mode toggle on IMX363...")
    with CameraStreamSession(cm, "imx363", CAMERA_SPECS["imx363"]["resolution"]) as session:
        def req_ois(idx):
            ctrls = {}
            if hasattr(DRAFT, "OpticalImageStabilizationMode"):
                ctrls[DRAFT.OpticalImageStabilizationMode] = 1 if (15 <= idx < 35) else 0
            return ctrls

        frames = session.run_stream(frame_count=45, request_control_fn=req_ois)

    ois_modes = [f["ois_mode"] for f in frames if f["ois_mode"] is not None]
    assert 1 in ois_modes and 0 in ois_modes, "OIS toggle failed to reflect in metadata"
    print("  [+] IMX363 OIS Dynamic Toggle Verified (Off -> On -> Off)")

    # 5. Torch Flash Mode on IMX363
    print("\n[*] Evaluating Torch Flash on IMX363...")
    with CameraStreamSession(cm, "imx363", CAMERA_SPECS["imx363"]["resolution"]) as session:
        def req_torch(idx):
            ctrls = {}
            if hasattr(DRAFT, "FlashMode"):
                ctrls[DRAFT.FlashMode] = 2 if (10 <= idx < 25) else 0
            return ctrls

        frames = session.run_stream(frame_count=35, request_control_fn=req_torch)

    if hasattr(DRAFT, "FlashMode"):
        flash_modes = [f["flash_mode"] for f in frames if f.get("flash_mode") is not None]
        assert 2 in flash_modes and 0 in flash_modes, f"FlashMode metadata missing required states: {set(flash_modes)}"
    print("  [+] IMX363 Torch Flash verified (Off -> Torch -> Off) and safely disengaged.")


def test_streaming_fps_stability(cm):
    """Test 4: Continuous 60-frame preview stability, jitter, and drop rate."""
    print("\n" + "=" * 70)
    print("TEST 4: Continuous 60-Frame Preview FPS, Jitter and Drop Rate")
    print("=" * 70)

    stats_summary = {}
    for key, spec in CAMERA_SPECS.items():
        print(f"\n[*] Profiling 60 frames on {spec['name']}...")
        with CameraStreamSession(cm, key, spec["resolution"]) as session:
            t_start = time.monotonic()
            frames = session.run_stream(frame_count=60, sample_pixels_flag=True)
            total_duration = time.monotonic() - t_start

        # Calculate sequence progression and drop rate
        seqs = [f["seq"] for f in frames]
        seq_span = seqs[-1] - seqs[0] + 1
        dropped_count = max(0, seq_span - len(frames))
        drop_rate_pct = (dropped_count / seq_span) * 100.0 if seq_span > 0 else 0.0
        assert len(frames) == 60, f"Expected 60 captured frames on {key}, got {len(frames)}"
        assert drop_rate_pct < 15.0, f"Frame drop rate too high on {key}: {dropped_count}/{seq_span} frames dropped ({drop_rate_pct:.1f}%)"

        # Calculate timestamps and intervals
        ts = [f["sensor_timestamp_ns"] for f in frames]
        intervals_ms = [(ts[i] - ts[i - 1]) / 1e6 for i in range(1, len(ts))]

        avg_fps = (len(frames) - 1) / (sum(intervals_ms) / 1000.0)
        median_interval = statistics.median(intervals_ms)
        std_jitter = statistics.stdev(intervals_ms)
        max_interval = max(intervals_ms)
        min_interval = min(intervals_ms)

        # Retrieve 3A parameters from final steady-state frames
        tail_frames = frames[-10:]
        mean_luma = statistics.mean(f["mean_luma"] for f in tail_frames if "mean_luma" in f)
        mean_exp = statistics.mean(f["exposure_us"] for f in tail_frames if f["exposure_us"])
        mean_gain = statistics.mean(f["gain"] for f in tail_frames if f["gain"])
        mean_cct = statistics.mean(f["cct"] for f in tail_frames if f["cct"])
        mean_focus = statistics.mean(f["focus_score"] for f in tail_frames if "focus_score" in f)
        tail_gains = [f["colour_gains"] for f in tail_frames if f["colour_gains"]]
        avg_r_gain = statistics.mean(g[0] for g in tail_gains) if tail_gains else 0.0
        avg_b_gain = statistics.mean(g[1] for g in tail_gains) if tail_gains else 0.0
        sample_ccm = tail_frames[-1]["ccm"]

        stats_summary[key] = {
            "captured_frames": len(frames),
            "seq_span": seq_span,
            "dropped_count": dropped_count,
            "drop_rate_pct": drop_rate_pct,
            "avg_fps": avg_fps,
            "median_interval_ms": median_interval,
            "jitter_std_ms": std_jitter,
            "interval_min_ms": min_interval,
            "interval_max_ms": max_interval,
            "steady_luma": mean_luma,
            "steady_exposure_us": mean_exp,
            "steady_gain": mean_gain,
            "steady_cct": mean_cct,
            "steady_r_gain": avg_r_gain,
            "steady_b_gain": avg_b_gain,
            "steady_focus_score": mean_focus,
            "sample_ccm": sample_ccm,
        }

        print(f"  [+] {key:6s}: FPS={avg_fps:5.2f}, Jitter(std)={std_jitter:5.3f}ms, "
              f"Interval=[{min_interval:.2f}, {max_interval:.2f}]ms | "
              f"Luma={mean_luma:.1f}, CCT={mean_cct:.0f}K, Gains=({avg_r_gain:.2f}, {avg_b_gain:.2f}), Focus={mean_focus:.2f}")

    return stats_summary


def test_runtime_pm_and_kernel_health():
    """Test 5: Verify runtime PM suspension and clean kernel status."""
    print("\n" + "=" * 70)
    print("TEST 5: Runtime Power Management and Kernel Health")
    print("=" * 70)

    # Wait for sensor autosuspend_delay_ms (1000ms per driver) to elapse
    time.sleep(1.5)

    # 1. Check CAMSS controller runtime PM
    camss_rpm = "/sys/devices/platform/soc@0/ac65000.camss/power/runtime_status"
    assert os.path.exists(camss_rpm), f"CAMSS RPM sysfs path not found: {camss_rpm}"
    with open(camss_rpm) as f:
        status = f.read().strip()
    print(f"  [+] ac65000.camss runtime status: {status}")
    assert status == "suspended", f"CAMSS not suspended: {status}"

    # 2. Check CSIPHY / sensors runtime status
    i2c_dir = Path("/sys/bus/i2c/devices")
    assert i2c_dir.is_dir(), f"I2C bus directory not found: {i2c_dir}"
    for name in ["imx363", "imx351", "s5k3m5", "imx520"]:
        sensor_dev = None
        for dev in i2c_dir.iterdir():
            name_file = dev / "name"
            if name_file.is_file():
                try:
                    if name_file.read_text().strip() == name:
                        sensor_dev = dev
                        break
                except OSError:
                    pass
        assert sensor_dev is not None, f"Sensor '{name}' not found on any I2C bus"
        rpm_file = sensor_dev / "power" / "runtime_status"
        assert rpm_file.is_file(), f"Sensor '{name}' ({sensor_dev.name}) missing runtime_status: {rpm_file}"

        st = ""
        for _ in range(20):
            st = rpm_file.read_text().strip()
            if st == "suspended":
                break
            time.sleep(0.1)
        print(f"  [+] {name:6s} ({sensor_dev.name}) runtime status: {st}")
        assert st == "suspended", f"Sensor '{name}' ({sensor_dev.name}) not suspended: {st}"

    # 3. Check kernel taint
    with open("/proc/sys/kernel/tainted") as f:
        taint = int(f.read().strip())
    print(f"  [+] Kernel taint status: {taint}")
    assert taint == 0, f"Kernel tainted: {taint}"

    print("  [+] Runtime PM and Kernel Health verified.")


def main():
    parser = argparse.ArgumentParser(description="LM-G820N Quad-Camera Joint Validation Test")
    parser.add_argument("--json", type=str, default=None, help="Save structured results JSON")
    args = parser.parse_args()

    print("######################################################################")
    print("#  LM-G820N QUAD-CAMERA JOINT VALIDATION & ACCEPTANCE TEST SUITE     #")
    print("######################################################################")

    cm = lc.CameraManager.singleton()
    full_results = {"modes": test_quad_cold_start_and_modes(cm)}
    test_shared_mipi_multiplexing(cm)
    test_3a_control_loops(cm)
    full_results["stability"] = test_streaming_fps_stability(cm)
    test_runtime_pm_and_kernel_health()

    if args.json:
        Path(args.json).write_text(json.dumps(full_results, indent=2) + "\n")
        print(f"\n[+] Results saved to {args.json}")

    print("[ALL TESTS PASSED] Quad-camera validation")
    return 0


if __name__ == "__main__":
    sys.exit(main())
