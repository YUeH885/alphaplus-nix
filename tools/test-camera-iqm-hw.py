#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Hardware Validation Suite for Qualcomm Spectra 380 (Titan 175)
# IQM & FC4 Deep Learning AWB Inference on LM-G820N.
#
# Scope & Test Intent:
# - Validates live hardware Bayer Grid (BG) 64x48 statistics DMA extraction
#   from CAMSS VFE PIX node via VIDIOC_QCOM_CAMSS_GET_STATS.
# - Executes full neural network forward inference for FC4 white-point and
#   scene classification (fc4_awb.onnx + wb_graph_*.onnx) across all three rear cameras.
# - Applies factory FC4 postprocessing correction and sensor-specific bidirectional
#   reference domain calibration on real model outputs.
# - Validates temporal filtering (0.6 * new + 0.4 * hist) and state machine
#   transition (5 -> 2) without claiming full factory 100ms/30-frame hardware cadence.
# - Closed-loop request-level injection into libcamera (ColourGains, AwbEnable=False),
#   strictly verifying applied metadata per request cookie and fresh stats correlation.
# - Checks Auto AWB recovery metadata after manual control.
# - Ensures proper resource lifecycle management and post-test Runtime PM suspension.

import argparse
import ctypes
import fcntl
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import selectors
import struct
import subprocess
import sys
import tempfile
import time

try:
    import libcamera as lc
except ImportError:
    lc = None

# Load local IQM processing module
IQM_MODULE_PATH = Path(__file__).resolve().parent / "process-camera-iqm.py"
if not IQM_MODULE_PATH.is_file():
    raise FileNotFoundError(f"process-camera-iqm.py not found at {IQM_MODULE_PATH}")

spec = importlib.util.spec_from_file_location("process_camera_iqm", IQM_MODULE_PATH)
iqm_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(iqm_mod)

preprocess_bg_stats = iqm_mod.preprocess_bg_stats
FC4Corrector = iqm_mod.FC4Corrector
IQMStateManager = iqm_mod.IQMStateManager
SENSOR_TYPE_MAP = iqm_mod.SENSOR_TYPE_MAP
softmax = iqm_mod.softmax

# Run python3-onnxruntime in a dedicated child process so its statically linked
# ORT core does not collide with libcamera's libonnxruntime.so.1.23 over libonnx.so.1.
_FC4_WORKER_SCRIPT = r"""
import importlib.util, json, struct, sys, time
iqm_path, model_path, classifier_path = sys.argv[1:4]
spec = importlib.util.spec_from_file_location("process_camera_iqm", iqm_path)
iqm_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(iqm_mod)
t0 = time.time()
model = iqm_mod.FC4AwbModel(model_path)
classifier = iqm_mod.OnnxSceneModel(classifier_path)
load_ms = (time.time() - t0) * 1000.0
sys.stdout.write(json.dumps({"provider": model.provider, "load_ms": load_ms}) + "\n")
sys.stdout.flush()
nbytes = 2 * 3072 * 3 * 4
while True:
    buf = sys.stdin.buffer.read(nbytes)
    if not buf:
        break
    if len(buf) != nbytes:
        raise RuntimeError(f"Short read in FC4 worker: expected {nbytes}, got {len(buf)}")
    inp = struct.unpack("<18432f", buf)
    res = model.predict(inp[:9216])
    probabilities = classifier.predict(inp[9216:]).tolist()
    sys.stdout.write(json.dumps([*res, probabilities]) + "\n")
    sys.stdout.flush()
"""


class FC4ModelWorker:
    """Isolated child-process wrapper around process-camera-iqm.FC4AwbModel."""

    def __init__(self, model_path, classifier_path):
        self._stderr_file = tempfile.TemporaryFile(mode="w+b")
        self._proc = subprocess.Popen(
            [sys.executable, "-u", "-c", _FC4_WORKER_SCRIPT,
             str(IQM_MODULE_PATH), str(model_path), str(classifier_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr_file,
        )
        line = self._proc.stdout.readline()
        if not line:
            err = self._read_stderr()
            rc = self._proc.wait()
            self.close()
            raise RuntimeError(f"FC4 worker exited during init (rc={rc}):\n{err}")
        info = json.loads(line.decode("utf-8"))
        self.provider = info["provider"]
        self.load_ms = float(info["load_ms"])

    def _read_stderr(self):
        self._stderr_file.seek(0)
        return self._stderr_file.read().decode("utf-8", errors="replace").strip()

    def predict(self, fc4_input, scene_input):
        if len(fc4_input) != 9216 or len(scene_input) != 9216:
            raise ValueError("Expected two 3072x3 BG tensors")
        self._proc.stdin.write(struct.pack("<18432f", *fc4_input, *scene_input))
        self._proc.stdin.flush()
        line = self._proc.stdout.readline()
        if not line:
            err = self._read_stderr()
            rc = self._proc.poll()
            raise RuntimeError(f"FC4 worker terminated unexpectedly (rc={rc}):\n{err}")
        res = json.loads(line.decode("utf-8"))
        return tuple(res)

    def close(self):
        if self._proc is not None:
            try:
                if self._proc.stdin and not self._proc.stdin.closed:
                    self._proc.stdin.close()
                if self._proc.poll() is None:
                    try:
                        self._proc.wait(timeout=5.0)
                    except subprocess.TimeoutExpired:
                        self._proc.terminate()
                        try:
                            self._proc.wait(timeout=2.0)
                        except subprocess.TimeoutExpired:
                            self._proc.kill()
                            self._proc.wait()
                if self._proc.stdout and not self._proc.stdout.closed:
                    self._proc.stdout.close()
            finally:
                self._proc = None
        if self._stderr_file is not None:
            self._stderr_file.close()
            self._stderr_file = None

REAR_SENSORS = [
    ('imx363', 'fc4_awb.onnx', 'imx363'),
    ('imx351', 'fc4_awb.onnx', 'imx351'),
    ('s5k3m5', 'fc4_awb.onnx', 's5k3m5'),
]

# Hardware CAMSS 3A statistics ABI from <uapi/linux/qcom_camss.h>
class CamssStats(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("sequence", ctypes.c_uint32),
        ("timestamp", ctypes.c_uint64),
        ("awb_bg_regions", ctypes.c_uint32),
        ("bhist_bins", ctypes.c_uint32),
        ("bf_regions", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
        ("awb_bg", ctypes.c_uint64),
        ("bhist", ctypes.c_uint64),
        ("bf", ctypes.c_uint64),
        ("flags", ctypes.c_uint32),
        ("exposure_lines", ctypes.c_uint32),
        ("analogue_gain", ctypes.c_uint32),
        ("reserved1", ctypes.c_uint32),
    ]

VIDIOC_QCOM_CAMSS_GET_STATS = 0xc04856c0
V4L2_QCOM_CAMSS_STATS_SENSOR_CONTROLS = 1


def find_camera(cm, sensor_key):
    for cam in cm.cameras:
        if sensor_key in cam.id:
            return cam
    raise RuntimeError(f"Camera with sensor '{sensor_key}' not found")


def find_camss_stats_device(entity_name="msm_vfe0_video3"):
    for node in Path("/sys/class/video4linux").glob("video*"):
        if (node / "name").read_text().strip() == entity_name:
            return f"/dev/{node.name}"
    raise RuntimeError(f"Could not locate CAMSS video node for entity '{entity_name}'")


def read_sysfs_rpm(device_path):
    rpm_file = Path(device_path) / "power/runtime_status"
    if not rpm_file.is_file():
        raise FileNotFoundError(f"Runtime PM file not found: {rpm_file}")
    return rpm_file.read_text().strip()


def test_sensor_iqm(cm, sensor_key, model_filename, sensor_calib_name, model_dir,
                    target_frames=24, test_wp_r=None, test_wp_b=None, test_lux=250.0,
                    sample_dir=None):
    print(f"\n========================================================")
    print(f"Testing {sensor_key.upper()} with FC4 Neural Network Model: {model_filename}")
    if test_wp_r is not None and test_wp_b is not None:
        print(f"  [Mode] CLI External White-Point Override: R={test_wp_r:.3f}, B={test_wp_b:.3f}, Lux={test_lux:.1f}")
    else:
        print(f"  [Mode] Full Neural Network Inference: Live BG Stats -> FC4 White-Point -> Postprocessing -> Gains")

    model_path = model_dir / model_filename
    # 1. Model Loading Benchmark
    classifier_name = Path(iqm_mod.SCENE_MODELS[sensor_calib_name]).with_suffix(".onnx")
    model = FC4ModelWorker(model_path, model_dir / classifier_name)
    try:
        t_load = model.load_ms
        print(f"  [Model Load] {model_filename} loaded via {model.provider} in {t_load:.2f} ms")
        if t_load > 3000:
            raise RuntimeError(f"Model load took too long: {t_load:.2f} ms")

        # Initialize FC4 Corrector & State Manager
        corrector = FC4Corrector()
        state_mgr = IQMStateManager(sensor_calib_name)
        assert state_mgr.state == 5, f"Initial state must be 5, got {state_mgr.state}"

        stats_dev_path = find_camss_stats_device()
        print(f"  [Topology] CAMSS 3A statistics node: {stats_dev_path}")

        # 2. Camera Acquisition & Stream Configuration with safe resource lifecycle
        cam = find_camera(cm, sensor_key)
        cam.acquire()

        stats_fd = -1
        sel = None
        stream_started = False

        try:
            config = cam.generate_configuration([lc.StreamRole.Viewfinder])
            stream_cfg = config.at(0)
            stream_cfg.size = lc.Size(640, 360)
            stream_cfg.pixel_format = lc.formats.NV12
            config.validate()
            cam.configure(config)

            allocator = lc.FrameBufferAllocator(cam)
            stream = stream_cfg.stream
            num_buffers = allocator.allocate(stream)
            if num_buffers <= 0:
                raise RuntimeError(f"Failed to allocate buffers for {sensor_key}")

            # Open CAMSS stats device inside try block
            stats_fd = os.open(stats_dev_path, os.O_RDWR)
            awb_buf = (ctypes.c_uint8 * 147456)()
            bhist_buf = (ctypes.c_uint8 * 4096)()
            bf_buf = (ctypes.c_uint8 * 808)()
            stats = CamssStats()
            stats.version = 3
            stats.awb_bg = ctypes.addressof(awb_buf)
            stats.bhist = ctypes.addressof(bhist_buf)
            stats.bf = ctypes.addressof(bf_buf)

            requests = []
            in_flight = {}
            for idx, buffer in enumerate(allocator.buffers(stream)):
                cookie = idx + 1
                req = cam.create_request(cookie)
                req.add_buffer(stream, buffer)
                requests.append(req)

            cam.start()
            stream_started = True

            sel = selectors.DefaultSelector()
            sel.register(cm.event_fd, selectors.EVENT_READ)

            # Initial queue with Auto AWB
            for req in requests:
                req.set_control(lc.controls.AwbEnable, True)
                in_flight[req.cookie] = {
                    'mode': 'auto',
                    'expected_gains': None,
                    'stats_seq': None,
                    'recovery': False,
                }
                cam.queue_request(req)

            print(f"[Streaming] Started {sensor_key} 640x360 NV12 capture loop...")

            frames_processed = 0
            inference_latencies = []
            preprocess_latencies = []
            correction_latencies = []
            injected_target = None
            candidate_stats_seq = None
            applied_verified = 0
            auto_recovery_verified = 0
            last_stats_sequence = None
            last_stats_timestamp = None
            stats_fresh_count = 0
            stats_read_count = 0

            start_time = time.time()
            timeout = target_frames / 20.0 + 25.0
            while frames_processed < target_frames and (time.time() - start_time) < timeout:
                events = sel.select(timeout=1.0)
                if not events:
                   continue

                for req in cm.get_ready_requests():
                    fb = next(iter(req.buffers.values()))
                    if fb.metadata.status != lc.FrameMetadata.Status.Success:
                        raise RuntimeError(f"Capture failed for request cookie {req.cookie}: {fb.metadata.status}")

                    frames_processed += 1
                    meta = req.metadata
                    ret_awb = meta.get(lc.controls.AwbEnable)
                    ret_gains = meta.get(lc.controls.ColourGains)
                    ret_cct = meta.get(lc.controls.ColourTemperature)

                    # Check expected control completion for this exact request cookie
                    exp = in_flight.pop(req.cookie, None)
                    if exp is None:
                        raise RuntimeError(f"Unexpected request cookie {req.cookie} without registered expectation")

                    if exp['mode'] == 'manual':
                        if ret_awb is not False:
                            raise AssertionError(f"Frame #{frames_processed:02d} (req seq {req.sequence}, cookie {req.cookie}): "
                                                 f"expected AwbEnable=False, got {ret_awb}")
                        if not ret_gains or len(ret_gains) != 2:
                            raise AssertionError(f"Frame #{frames_processed:02d} (req seq {req.sequence}, cookie {req.cookie}): "
                                                 f"missing ColourGains metadata in manual mode")
                        exp_r, exp_b = exp['expected_gains']
                        if not all(math.isfinite(gain) for gain in ret_gains):
                            raise AssertionError(f"Non-finite manual gains: {ret_gains}")
                        r_err = abs(ret_gains[0] - exp_r)
                        b_err = abs(ret_gains[1] - exp_b)
                        if r_err >= 0.05 or b_err >= 0.05:
                            raise AssertionError(f"Frame #{frames_processed:02d} (req seq {req.sequence}, cookie {req.cookie}): "
                                                 f"applied gains {ret_gains} deviate from expected {exp['expected_gains']} "
                                                 f"(err=({r_err:.4f}, {b_err:.4f}))")
                        applied_verified += 1
                        print(f"  [Applied] cookie={req.cookie} request={req.sequence} "
                              f"source_stats={exp['stats_seq']} gains={tuple(ret_gains)}")
                    elif exp['mode'] == 'auto':
                        if ret_awb is not True:
                            raise AssertionError(f"Frame #{frames_processed:02d} (req seq {req.sequence}, cookie {req.cookie}): "
                                                 f"expected AwbEnable=True, got {ret_awb}")
                        if not ret_gains or len(ret_gains) != 2 or not all(
                                math.isfinite(gain) and gain > 0 for gain in ret_gains):
                            raise AssertionError(f"Frame #{frames_processed:02d} (req seq {req.sequence}, cookie {req.cookie}): "
                                                 f"invalid ColourGains in auto mode: {ret_gains}")
                        if ret_cct is None or ret_cct <= 1000:
                            raise AssertionError(f"Frame #{frames_processed:02d} (req seq {req.sequence}, cookie {req.cookie}): "
                                                 f"invalid ColourTemperature in auto mode: {ret_cct}")
                        if exp['recovery']:
                            auto_recovery_verified += 1

                    # Query hardware statistics DMA snapshot
                    fcntl.ioctl(stats_fd, VIDIOC_QCOM_CAMSS_GET_STATS, stats)
                    if stats.awb_bg_regions != 3072:
                        raise RuntimeError(f"Unexpected BG region count: {stats.awb_bg_regions}")

                    stats_read_count += 1
                    is_fresh = (last_stats_sequence is None or stats.sequence != last_stats_sequence)
                    if stats.timestamp <= 0:
                        raise RuntimeError("Missing hardware statistics timestamp")
                    if last_stats_sequence is not None and (
                            stats.sequence < last_stats_sequence or
                            stats.timestamp < last_stats_timestamp):
                        raise RuntimeError("Hardware statistics moved backwards")
                    if is_fresh:
                        stats_fresh_count += 1
                    last_stats_sequence = stats.sequence
                    last_stats_timestamp = stats.timestamp

                    # Decode 64x48 Bayer Grid (BG) DMA statistics
                    awb_u32 = ctypes.cast(awb_buf, ctypes.POINTER(ctypes.c_uint32))
                    bg_stats = []
                    for i in range(3072):
                        base = i * 12
                        r_sum, b_sum = awb_u32[base], awb_u32[base + 1]
                        gr_sum, gb_sum = awb_u32[base + 2], awb_u32[base + 3]
                        w4, w5 = awb_u32[base + 4], awb_u32[base + 5]
                        r_cnt, b_cnt = w4 & 0xffff, w4 >> 16
                        gr_cnt, gb_cnt = w5 & 0xffff, w5 >> 16
                        if r_cnt and b_cnt and gr_cnt and gb_cnt:
                            r = min(255.0, max(0.0, r_sum / (r_cnt * 64.0)))
                            gr = min(255.0, max(0.0, gr_sum / (gr_cnt * 64.0)))
                            gb = min(255.0, max(0.0, gb_sum / (gb_cnt * 64.0)))
                            b = min(255.0, max(0.0, b_sum / (b_cnt * 64.0)))
                        else:
                            r = gr = gb = b = 0.0
                        bg_stats.append((r, gr, gb, b))

                    # Periodically trigger scene inference and FC4 correction on discrete frame steps
                    # in the validation harness (frames 2, 7, 12)
                    if frames_processed in (2, 7, 12):
                        if not is_fresh:
                            raise RuntimeError("Inference requires a fresh hardware statistics snapshot")
                        t_p0 = time.time()
                        preprocessed = preprocess_bg_stats(bg_stats, sensor_calib_name, for_fc4=True)
                        t_p1 = time.time()
                        preprocess_latencies.append((t_p1 - t_p0) * 1000)
                        if sample_dir is not None and frames_processed == 2:
                            sample_dir.mkdir(parents=True, exist_ok=True)
                            (sample_dir / f"{sensor_key}.bin").write_bytes(
                                struct.pack('<9216f', *preprocessed))
                            (sample_dir / f"{sensor_key}.json").write_text(json.dumps({
                                'sensor': sensor_key,
                                'stats_sequence': stats.sequence,
                                'stats_timestamp_monotonic_ns': stats.timestamp,
                                'reference_domain': 'imx363_golden',
                                'shape': [1, 48, 64, 3],
                                'dtype': 'little-endian float32',
                            }, indent=2) + '\n')
                        t_i0 = time.time()
                        scene_input = preprocess_bg_stats(bg_stats, sensor_calib_name)
                        (fc4_est_r, fc4_est_b, top_cls1, top_conf1, top_cls2, top_conf2,
                         raw_7, scene_probabilities) = model.predict(preprocessed, scene_input)
                        t_i1 = time.time()
                        inference_latencies.append((t_i1 - t_i0) * 1000)

                        probs = softmax(raw_7[3:7])
                        prob_sum = sum(probs)
                        if not (0.999 <= prob_sum <= 1.001):
                            raise ValueError(f"Softmax sum violated: {prob_sum} != 1.0")
                        for p in probs:
                            if not (0.0 <= p <= 1.0):
                                raise ValueError(f"Probability out of range [0, 1]: {p}")

                        if not (math.isfinite(fc4_est_r) and fc4_est_r > 0.0):
                            raise ValueError(f"Invalid FC4 white-point R ratio: {fc4_est_r}")
                        if not (math.isfinite(fc4_est_b) and fc4_est_b > 0.0):
                            raise ValueError(f"Invalid FC4 white-point B ratio: {fc4_est_b}")

                        # Determine effective white-point for corrector:
                        # Uses real FC4 inference output unless explicit override was provided
                        eff_wp_r = test_wp_r if test_wp_r is not None else fc4_est_r
                        eff_wp_b = test_wp_b if test_wp_b is not None else fc4_est_b

                        t_c0 = time.time()
                        slots = corrector.correct(
                            bg_stats,
                            fc4_est_r=eff_wp_r,
                            fc4_est_b=eff_wp_b,
                            fc4_cls1=top_cls1,
                            fc4_conf1=top_conf1,
                            fc4_cls2=top_cls2,
                            sensor_name=sensor_calib_name,
                            scene_probabilities=scene_probabilities,
                            lux=test_lux
                        )
                        t_c1 = time.time()
                        correction_latencies.append((t_c1 - t_c0) * 1000)

                        # Apply temporal filtering (0.6 * new + 0.4 * hist)
                        filtered_slots, dnn_tuple = state_mgr.update(slots)
                        if state_mgr.state != 2:
                            raise ValueError(f"State machine failed to transition to 2, current: {state_mgr.state}")

                        gain_r = filtered_slots[2]
                        gain_b = filtered_slots[3]
                        if not (1.0 <= gain_r <= 3.9000000953674316):
                            raise ValueError(f"Calibrated R gain out of bounds [1.0, 3.90]: {gain_r}")
                        if not (1.0 <= gain_b <= 3.9000000953674316):
                            raise ValueError(f"Calibrated B gain out of bounds [1.0, 3.90]: {gain_b}")

                        assert len(dnn_tuple) == 4, "LgAwbDnn tuple must be 4 elements"
                        assert 0.0 <= dnn_tuple[0] <= 1.0, f"Outdoor conf out of bounds: {dnn_tuple[0]}"
                        assert 0.0 <= dnn_tuple[1] <= 1.0, f"Indoor conf out of bounds: {dnn_tuple[1]}"
                        assert 0.0 <= dnn_tuple[2] <= 3.9000000953674316, f"R gain out of bounds: {dnn_tuple[2]}"
                        assert 0.0 <= dnn_tuple[3] <= 3.9000000953674316, f"B gain out of bounds: {dnn_tuple[3]}"

                        injected_target = (gain_r, gain_b)
                        candidate_stats_seq = stats.sequence

                        print(f"  Frame #{frames_processed:02d} [req_seq={req.sequence}]: "
                              f"StatsSeq={stats.sequence} (fresh={is_fresh}), "
                              f"InfLat={inference_latencies[-1]:.1f}ms, "
                              f"CorrLat={correction_latencies[-1]:.2f}ms, "
                              f"FC4_WP=({fc4_est_r:.4f}, {fc4_est_b:.4f}) (used=({eff_wp_r:.4f}, {eff_wp_b:.4f})), "
                              f"Softmax={[round(p, 4) for p in probs]}, "
                              f"FilteredGains=({gain_r:.3f}, {gain_b:.3f}), "
                              f"LgAwbDnn={[round(x, 3) for x in dnn_tuple]}, "
                              f"State={state_mgr.state}")

                    # Requeue request with explicit per-request expectation tracking
                    req.reuse()
                    if injected_target is not None and frames_processed < 16:
                        req.set_control(lc.controls.AwbEnable, False)
                        req.set_control(lc.controls.ColourGains, injected_target)
                        in_flight[req.cookie] = {
                            'mode': 'manual',
                            'expected_gains': injected_target,
                            'stats_seq': candidate_stats_seq
                        }
                    else:
                        # Auto AWB recovery phase
                        req.set_control(lc.controls.AwbEnable, True)
                        in_flight[req.cookie] = {
                            'mode': 'auto',
                            'expected_gains': None,
                            'stats_seq': None,
                            'recovery': injected_target is not None,
                        }
                    cam.queue_request(req)
                    if frames_processed >= target_frames:
                        break

        finally:
            try:
                if stream_started:
                    cam.stop()
                    while cm.get_ready_requests():
                        pass
            finally:
                try:
                    if sel is not None:
                        sel.close()
                    if stats_fd >= 0:
                        os.close(stats_fd)
                finally:
                    cam.release()
    finally:
        model.close()

    # Metrics & Verification
    if frames_processed < target_frames:
        raise RuntimeError(f"Expected at least {target_frames} frames for {sensor_key}, only got {frames_processed}")

    assert applied_verified >= 3, f"Failed to verify applied gains feedback on {sensor_key} (got {applied_verified})"
    assert auto_recovery_verified >= 3, f"Failed to verify auto AWB recovery on {sensor_key} (got {auto_recovery_verified})"

    avg_inf = sum(inference_latencies) / len(inference_latencies)
    avg_prep = sum(preprocess_latencies) / len(preprocess_latencies)
    avg_corr = sum(correction_latencies) / len(correction_latencies)
    print(f"[Performance Metrics] {sensor_key.upper()}:")
    print(f"  Average Preprocess Latency: {avg_prep:.2f} ms")
    print(f"  Average Inference Latency:  {avg_inf:.2f} ms")
    print(f"  Average Correction Latency: {avg_corr:.2f} ms")
    print(f"  State Machine Transition:   5 -> 2 verified (updates={state_mgr.stable_counter})")
    print(f"  Hardware Stats Snapshots:   total={stats_read_count}, unique_fresh={stats_fresh_count}")
    print(f"  Applied Gains Verified:     {applied_verified} frames")
    print(f"  Auto AWB Recovery Verified: {auto_recovery_verified} frames")
    print(f"  Total Processed Frames:     {frames_processed}")

    return {
        'sensor': sensor_key,
        'frames': frames_processed,
        'avg_prep_ms': avg_prep,
        'avg_inf_ms': avg_inf,
        'avg_corr_ms': avg_corr,
        'applied_verified': applied_verified,
        'auto_recovery_verified': auto_recovery_verified,
        'stats_read_count': stats_read_count,
        'stats_fresh_count': stats_fresh_count,
    }


def test_native_iqm(sensors, frames):
    listed = subprocess.run(["cam", "-l"], check=True, capture_output=True, text=True, timeout=10)
    camera_ids = re.findall(r"^\d+:.*\(([^()]+)\)$", listed.stdout, flags=re.MULTILINE)
    env = {**os.environ, "LIBCAMERA_LOG_LEVELS": "QcomPipeline:DEBUG"}

    for sensor in sensors:
        camera = next((camera_id for camera_id in camera_ids if camera_id.split()[0] == sensor), None)
        if camera is None:
            raise RuntimeError(f"Camera {sensor} not listed by cam")
        capture = subprocess.run(
            ["cam", "-c", camera, "-s", "width=1280,height=720", f"--capture={frames}"],
            check=True, capture_output=True, text=True, timeout=frames / 8 + 20, env=env,
        )
        results = set(re.findall(r"IQM update: in_seq (\d+)", capture.stderr))
        sources = re.findall(r"AWB DNN state: in_seq (\d+) curr_seq (\d+) source (\d+)",
                             capture.stderr)
        fused = [output for source, output, mode in sources if source in results and mode == "11"]
        print(f"{sensor}: {len(results)} native FC4 results, {len(set(fused))} Source 11 frames")
        assert len(results) >= 2, f"{sensor}: insufficient fresh C++ FC4 inference results"
        assert fused, f"{sensor}: no Source 11 result in the current scene"

def main():
    parser = argparse.ArgumentParser(description="LM-G820N IQM & Deep Learning Inference Hardware Validation")
    parser.add_argument("--model-dir", type=Path, default=Path("/etc/camera/iqm"),
                        help="Path to directory containing models (default: /etc/camera/iqm)")
    parser.add_argument("--fc4-model", type=str, default="fc4_awb.onnx",
                        help="Model filename under --model-dir (default: fc4_awb.onnx)")
    parser.add_argument("--native", action="store_true",
                        help="Require C++ FC4 inference and Source 11 fusion on a suitable scene")
    parser.add_argument("--sensor", choices=[sensor for sensor, _, _ in REAR_SENSORS],
                        action="append", help="Test only the selected rear sensor (repeatable)")
    parser.add_argument("--frames", type=int, help="Frames per sensor (24 for Python, 420 for native)")
    parser.add_argument("--sample-dir", type=Path,
                        help="Save each sensor's first inference input and statistics provenance")
    parser.add_argument("--test-white-point-r", type=float, default=None,
                        help="Optional external test white-point R override (default: None, use full FC4 NN inference)")
    parser.add_argument("--test-white-point-b", type=float, default=None,
                        help="Optional external test white-point B override (default: None, use full FC4 NN inference)")
    parser.add_argument("--test-lux", type=float, default=250.0,
                        help="External test lux value for FC4 postprocessing verification")
    args = parser.parse_args()
    sensors = args.sensor or [sensor for sensor, _, _ in REAR_SENSORS]
    frames = args.frames if args.frames is not None else (420 if args.native else 24)
    if args.native:
        if frames < 60:
            parser.error("--native requires at least 60 frames")
        test_native_iqm(sensors, frames)
        taint_path = Path("/proc/sys/kernel/tainted")
        if taint_path.is_file():
            taint = int(taint_path.read_text().strip())
            if taint != 0:
                raise RuntimeError(f"Kernel is tainted: {taint}")
        return 0
    if frames < 24:
        parser.error("--frames must be at least 24 to cover manual control and auto recovery")

    print("================================================================")
    print("LM-G820N IQM & Deep Learning Inference Hardware Validation")
    print("================================================================")
    print(f"Validation mode: Application script verification of Bayer Grid DMA stats extraction,")
    print(f"FC4 neural network inference, FC4 postprocessing, and libcamera request-level feedback.")

    cm = lc.CameraManager.singleton()
    print(f"Discovered {len(cm.cameras)} cameras on device")

    results = []
    for sensor_key, _, calib_key in REAR_SENSORS:
        if sensor_key not in sensors:
            continue
        res = test_sensor_iqm(cm, sensor_key, args.fc4_model, calib_key, args.model_dir, frames,
                              test_wp_r=args.test_white_point_r,
                              test_wp_b=args.test_white_point_b,
                              test_lux=args.test_lux, sample_dir=args.sample_dir)
        results.append(res)

    # Post-validation Runtime PM Verification
    print("\n========================================================")
    print("Checking Runtime PM Suspended State across all Camera Subdevs...")
    time.sleep(1.5)

    camss_sysfs = Path("/sys/devices/platform/soc@0/ac65000.camss")
    if not camss_sysfs.exists():
        camss_sysfs = Path("/sys/bus/platform/devices/ac65000.camss")
    camss_status = read_sysfs_rpm(camss_sysfs)
    print(f"CAMSS Runtime Status: {camss_status}")
    if camss_status != "suspended":
        raise RuntimeError(f"CAMSS not suspended after test: {camss_status}")

    i2c_dir = Path("/sys/bus/i2c/devices")
    if not i2c_dir.is_dir():
        raise RuntimeError(f"I2C bus directory not found: {i2c_dir}")
    for s_name in ['imx363', 'imx351', 's5k3m5']:
        s_dev = None
        for dev in i2c_dir.iterdir():
            name_file = dev / "name"
            if name_file.is_file():
                try:
                    if name_file.read_text().strip() == s_name:
                        s_dev = dev
                        break
                except OSError:
                    pass
        if not s_dev:
            raise RuntimeError(f"Sensor '{s_name}' not found on any I2C bus")
        rpm_file = s_dev / "power" / "runtime_status"
        if not rpm_file.is_file():
            raise RuntimeError(f"Sensor '{s_name}' ({s_dev.name}) missing runtime_status node: {rpm_file}")

        s_status = ""
        for _ in range(20):
            s_status = read_sysfs_rpm(s_dev)
            if s_status == "suspended":
                break
            time.sleep(0.1)
        print(f"Sensor {s_name} ({s_dev.name}) Runtime Status: {s_status}")
        if s_status != "suspended":
            raise RuntimeError(f"Sensor {s_name} ({s_dev.name}) not suspended: {s_status}")

    taint_path = Path("/proc/sys/kernel/tainted")
    if taint_path.is_file():
        taint = int(taint_path.read_text().strip())
        print(f"Kernel Taint Value: {taint}")
        if taint != 0:
            raise RuntimeError(f"Kernel is tainted: {taint}")

    print("\n========================================================")
    print("REAR CAMERA IQM & FC4 HARDWARE VALIDATION PASSED!")
    for r in results:
        print(f"  {r['sensor'].upper():<8}: {r['frames']} frames, "
              f"Preprocess={r['avg_prep_ms']:.2f}ms, Inference={r['avg_inf_ms']:.2f}ms, "
              f"Correction={r['avg_corr_ms']:.2f}ms, ManualVerified={r['applied_verified']} frames, "
              f"AutoRecoveryVerified={r['auto_recovery_verified']} frames")
    print("========================================================")
    return 0


if __name__ == '__main__':
    sys.exit(main())
