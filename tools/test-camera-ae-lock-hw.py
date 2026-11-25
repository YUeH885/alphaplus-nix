#!/usr/bin/env python3
"""Hardware validation test for Qualcomm pipeline AE lock semantics on LM-G820N.

Verifies:
1. draft.AeLock control registration on all cameras.
2. Setting AE lock freezes exposure time and analogue gain (0 drift).
3. Modifying ExposureValue while locked shifts exposure proportionally (+1 EV -> 2x, -1 EV -> 0.5x).
4. Returning ExposureValue to locked baseline restores original exposure.
5. Unlocking AE allows convergence to resume.
6. Manual exposure override during lock and recovery.
"""

import importlib.util
from pathlib import Path
import statistics
import time
import selectors
import libcamera as lc

C = lc.controls
DRAFT = lc.controls.draft

spec = importlib.util.spec_from_file_location("qcom_camera_test", Path(__file__).with_name("test-camera-qcom.py"))
qcom_test = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qcom_test)

CAMERA_NAMES = {
    'imx363': 'Rear Main (IMX363)',
    'imx351': 'Rear Wide (IMX351)',
    's5k3m5': 'Rear Telephoto (S5K3M5)',
    'imx520': 'Front (IMX520)',
}


def find_camera(cm, sensor_key):
    for cam in cm.cameras:
        if sensor_key in cam.id:
            return cam
    raise RuntimeError(f"Camera with sensor '{sensor_key}' not found")


def run_capture(cm, cam, resolution, frames, request_fn, on_result=None):
    config = cam.generate_configuration([lc.StreamRole.Viewfinder])
    stream_cfg = config.at(0)
    stream_cfg.size = lc.Size(resolution[0], resolution[1])
    stream_cfg.pixel_format = lc.formats.NV12
    config.validate()
    cam.configure(config)

    allocator = lc.FrameBufferAllocator(cam)
    stream = stream_cfg.stream
    allocator.allocate(stream)

    requests = []
    for buffer in allocator.buffers(stream):
        req = cam.create_request()
        req.add_buffer(stream, buffer)
        requests.append(req)

    submitted = 0
    submitted_info = {}

    def queue_req(req):
        nonlocal submitted
        controls_to_set = request_fn(submitted)
        for key, value in controls_to_set.items():
            req.set_control(key, value)
        submitted_info[req] = (submitted, controls_to_set)
        cam.queue_request(req)
        submitted += 1

    cam.start()
    for req in requests:
        queue_req(req)

    selector = selectors.DefaultSelector()
    selector.register(cm.event_fd, selectors.EVENT_READ)
    results = []
    deadline = time.monotonic() + frames / 15.0 + 15.0

    try:
        while len(results) < frames:
            assert time.monotonic() < deadline, f"Capture timed out ({len(results)}/{frames} frames)"
            if not selector.select(1):
                continue
            for req in cm.get_ready_requests():
                if req not in submitted_info:
                    continue
                fb = next(iter(req.buffers.values()))
                if fb.metadata.status != lc.FrameMetadata.Status.Success:
                    if len(results) < frames:
                        req.reuse()
                        queue_req(req)
                    continue
                md = req.metadata
                req_idx, req_ctrls = submitted_info[req]
                row = {
                    'req_idx': req_idx,
                    'req_ctrls': req_ctrls,
                    'seq': fb.metadata.sequence,
                    'timestamp': fb.metadata.timestamp,
                    'exposure': md.get(C.ExposureTime),
                    'gain': md.get(C.AnalogueGain),
                    'ae_enabled': md.get(C.AeEnable),
                    'ev': md.get(C.ExposureValue),
                    'ae_locked': md.get(DRAFT.AeLock),
                    'ae_state': md.get(C.AeState),
                    'luma': qcom_test.sample_pixels(fb, stream_cfg)['y'],
                }
                results.append(row)
                if on_result is not None:
                    on_result(row)
                if len(results) < frames:
                    req.reuse()
                    queue_req(req)
        results.sort(key=lambda r: r['req_idx'])
        return results[:frames]
    finally:
        cam.stop()
        # Drain all remaining requests for this session
        while selector.select(0.1):
            ready = cm.get_ready_requests()
            if not ready:
                break
        selector.close()


def run_ae_lock_test(cm, sensor_key):
    desc = CAMERA_NAMES.get(sensor_key, sensor_key)
    print(f"\n==========================================")
    print(f"Testing AE Lock on: {desc}")
    print(f"==========================================")

    cam = find_camera(cm, sensor_key)
    cam.acquire()

    try:
        # 1. Verify control registration
        ctrl_map = cam.controls
        assert DRAFT.AeLock in ctrl_map, f"draft.AeLock not exposed on {sensor_key}"
        lock_info = ctrl_map[DRAFT.AeLock]
        print(f"  [+] draft.AeLock registered: def={lock_info.default}, min={lock_info.min}, max={lock_info.max}")

        total_frames = 230

        override_exp = 6000
        override_gain = 3.0

        def choose_override(row):
            nonlocal override_exp, override_gain
            if row['req_idx'] == 20 and row['exposure'] and row['gain']:
                natural_energy = row['exposure'] * row['gain']
                if 9000.0 <= natural_energy <= 36000.0:
                    override_exp = 1000
                    override_gain = 1.5

        # Schedule phases:
        # Frames 0..24: Auto AE (unlocked)
        # Frames 25..39: Establish mid-range exposure baseline via manual mode (4000 us, 2.0x gain)
        # Frames 40..69: AE locked at baseline (draft.AeLock = True, AeEnable = True, EV = 0.0)
        # Frames 70..89: AE locked + EV shift (+1.0 EV)
        # Frames 90..109: AE locked + EV shift (-1.0 EV)
        # Frames 110..129: AE locked + return to EV 0.0
        # Frames 130..154: Unlock AE (draft.AeLock = False) -> Auto AE convergence resumes
        # Frames 155..174: Manual override during lock. Frames 175..229: Auto recovery.
        def request_controller(req_idx):
            if req_idx < 25:
                return {DRAFT.AeLock: False, C.AeEnable: True, C.ExposureValue: 0.0}
            elif req_idx < 40:
                return {
                    DRAFT.AeLock: False,
                    C.AeEnable: False,
                    C.ExposureTime: 4000,
                    C.AnalogueGain: 2.0,
                    C.ExposureValue: 0.0,
                }
            elif req_idx < 70:
                return {DRAFT.AeLock: True, C.AeEnable: True, C.ExposureValue: 0.0}
            elif req_idx < 90:
                return {DRAFT.AeLock: True, C.AeEnable: True, C.ExposureValue: 1.0}
            elif req_idx < 110:
                return {DRAFT.AeLock: True, C.AeEnable: True, C.ExposureValue: -1.0}
            elif req_idx < 130:
                return {DRAFT.AeLock: True, C.AeEnable: True, C.ExposureValue: 0.0}
            elif req_idx < 155:
                return {DRAFT.AeLock: False, C.AeEnable: True, C.ExposureValue: 0.0}
            elif req_idx < 175:
                return {
                    DRAFT.AeLock: True,
                    C.AeEnable: False,
                    C.ExposureTime: override_exp,
                    C.AnalogueGain: override_gain,
                }
            else:
                return {DRAFT.AeLock: False, C.AeEnable: True, C.ExposureValue: 0.0}

        print("  -> Capturing frames across AE lock & EV phases...")
        results = run_capture(cm, cam, (640, 360), total_frames, request_controller, on_result=choose_override)
        assert len(results) == total_frames

        # Phase 1 verification: Unlocked AE
        unlocked_p1 = [r for r in results if r['req_idx'] in range(15, 24)]
        assert all(r['ae_locked'] is False for r in unlocked_p1)
        conv_exp = unlocked_p1[-1]['exposure']
        conv_gain = unlocked_p1[-1]['gain']
        print(f"  [+] Phase 1 (Initial Auto AE): exp={conv_exp} us, gain={conv_gain:.4f}")

        # Phase 2 verification: Mid-range manual baseline setup
        manual_p2 = [r for r in results if r['req_idx'] in range(32, 39)]
        assert all(r['ae_enabled'] is False for r in manual_p2)
        base_exp = manual_p2[-1]['exposure']
        base_gain = manual_p2[-1]['gain']
        print(f"  [+] Phase 2 (Manual Baseline): exp={base_exp} us, gain={base_gain:.4f}")

        # Phase 3 verification: Locked AE
        locked_p3 = [r for r in results if r['req_idx'] in range(50, 68)]
        assert all(r['ae_locked'] is True for r in locked_p3), "Expected draft.AeLock == True"
        exps = [r['exposure'] for r in locked_p3]
        gains = [r['gain'] for r in locked_p3]
        exp_span = max(exps) - min(exps)
        gain_span = max(gains) - min(gains)
        print(f"  [+] Phase 3 (Locked AE): exp span={exp_span} us, gain span={gain_span:.6f}")
        assert exp_span == 0, f"Exposure drifted while locked: {exps}"
        assert gain_span < 1e-5, f"Gain drifted while locked: {gains}"
        baseline_total = exps[0] * gains[0]

        # Phase 4 verification: +1.0 EV while locked
        locked_plus = [r for r in results if r['req_idx'] in range(76, 88)]
        assert all(r['ae_locked'] is True for r in locked_plus)
        assert all(abs(r['ev'] - 1.0) < 1e-4 for r in locked_plus)
        plus_total = locked_plus[-1]['exposure'] * locked_plus[-1]['gain']
        ratio_plus = plus_total / baseline_total
        print(f"  [+] Phase 4 (+1 EV): total={plus_total:.1f} vs baseline={baseline_total:.1f} (ratio={ratio_plus:.3f})")
        assert 1.7 <= ratio_plus <= 2.3, f"+1 EV did not scale exposure correctly: {ratio_plus}"

        # Phase 5 verification: -1.0 EV while locked
        locked_minus = [r for r in results if r['req_idx'] in range(96, 108)]
        assert all(r['ae_locked'] is True for r in locked_minus)
        assert all(abs(r['ev'] - (-1.0)) < 1e-4 for r in locked_minus)
        minus_total = locked_minus[-1]['exposure'] * locked_minus[-1]['gain']
        ratio_minus = minus_total / baseline_total
        print(f"  [+] Phase 5 (-1 EV): total={minus_total:.1f} vs baseline={baseline_total:.1f} (ratio={ratio_minus:.3f})")
        assert 0.4 <= ratio_minus <= 0.6, f"-1 EV did not scale exposure correctly: {ratio_minus}"

        base_luma = statistics.median(r['luma'] for r in locked_p3)
        plus_luma = statistics.median(r['luma'] for r in locked_plus)
        minus_luma = statistics.median(r['luma'] for r in locked_minus)
        noise = statistics.median(abs(r['luma'] - base_luma) for r in locked_p3)
        minimum_change = max(0.01, base_luma * 0.1, noise * 3)
        assert minus_luma + minimum_change < base_luma < plus_luma - minimum_change, (
            f"EV phases did not change pixels: -1 EV Y={minus_luma:.2f}, "
            f"0 EV Y={base_luma:.2f}, +1 EV Y={plus_luma:.2f}"
        )

        # Phase 6 verification: Return to 0.0 EV while locked
        locked_restored = [r for r in results if r['req_idx'] in range(118, 128)]
        assert all(r['ae_locked'] is True for r in locked_restored)
        assert all(abs(r['ev'] - 0.0) < 1e-4 for r in locked_restored)
        restored_total = locked_restored[-1]['exposure'] * locked_restored[-1]['gain']
        print(f"  [+] Phase 6 (Restored EV): total={restored_total:.1f} vs baseline={baseline_total:.1f}")
        assert abs(restored_total - baseline_total) / baseline_total < 0.03, \
            f"Restored exposure {restored_total} drifted from baseline {baseline_total}"

        # Phase 7 verification: Unlock AE
        unlocked_p7 = [r for r in results if r['req_idx'] in range(145, 154)]
        assert all(r['ae_locked'] is False for r in unlocked_p7)
        print(f"  [+] Phase 7 (Unlocked): ae_locked={unlocked_p7[-1]['ae_locked']}")

        # Phase 8: Manual exposure override during lock & recovery
        override_p8 = [r for r in results if r['req_idx'] in range(165, 174)]
        assert all(r['ae_enabled'] is False for r in override_p8)
        assert all(abs(r['exposure'] - override_exp) <= 250 for r in override_p8)
        assert all(abs(r['gain'] - override_gain) <= 0.1 for r in override_p8)
        print(f"  [+] Phase 8 (Manual override): exp={override_p8[-1]['exposure']} us, gain={override_p8[-1]['gain']:.2f}")

        resumed_p8 = [r for r in results if r['req_idx'] in range(210, 229)]
        assert all(r['ae_enabled'] is True and r['ae_locked'] is False for r in resumed_p8)
        manual_energy = statistics.median(r['exposure'] * r['gain'] for r in override_p8)
        auto_energy = [r['exposure'] * r['gain'] for r in resumed_p8]
        assert abs(statistics.median(auto_energy) / manual_energy - 1) > 0.05, (
            "Auto exposure did not move away from the manual override"
        )
        assert max(auto_energy) / min(auto_energy) < 1.1, (
            f"AE did not settle after unlock: {auto_energy}"
        )
        print(f"  [+] Phase 8 (Auto recovery): exp={resumed_p8[-1]['exposure']} us, gain={resumed_p8[-1]['gain']:.2f}")

        print(f"  [✓] {desc} AE lock test PASSED")

    finally:
        cam.release()


def main():
    cm = lc.CameraManager.singleton()
    print(f"Discovered {len(cm.cameras)} cameras on device")

    for sensor in ['s5k3m5', 'imx351', 'imx363', 'imx520']:
        run_ae_lock_test(cm, sensor)

    print("\n==========================================")
    print("ALL FOUR CAMERAS PASSED AE LOCK VALIDATION!")
    print("==========================================")


if __name__ == '__main__':
    main()
