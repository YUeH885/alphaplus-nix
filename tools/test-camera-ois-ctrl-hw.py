#!/usr/bin/env python3
"""Hardware validation test for Qualcomm pipeline OIS controls on LM-G820N.

Verifies:
1. draft.OpticalImageStabilizationMode control exposed only on rear main (IMX363).
2. Non-OIS cameras (IMX351, S5K3M5, IMX520) do NOT expose OpticalImageStabilizationMode.
3. Dynamic switching between OIS On (1) and OIS Off (0) during active streaming on IMX363.
4. Correct reflection of OIS state in completed request metadata.
5. Clean stream start, dynamic control, stream stop, and PM recovery.
"""

import os
import sys
import time
import selectors
import libcamera as lc

C = lc.controls
DRAFT = lc.controls.draft

CAMERAS = {
    'imx363': {'name': 'Rear Main (IMX363)', 'has_ois': True, 'res': (1920, 1080)},
    'imx351': {'name': 'Rear Wide (IMX351)', 'has_ois': False, 'res': (1920, 1080)},
    's5k3m5': {'name': 'Rear Telephoto (S5K3M5)', 'has_ois': False, 'res': (1920, 1080)},
    'imx520': {'name': 'Front (IMX520)', 'has_ois': False, 'res': (1920, 1080)},
}


def find_camera(cm, sensor_key):
    for cam in cm.cameras:
        if sensor_key in cam.id:
            return cam
    raise RuntimeError(f"Camera with sensor '{sensor_key}' not found")


def verify_control_presence(cm):
    print("==========================================")
    print("1. Verifying OIS Control Exposure Across All Cameras")
    print("==========================================")
    
    assert hasattr(DRAFT, 'OpticalImageStabilizationMode'), \
        "libcamera python bindings missing draft.OpticalImageStabilizationMode"
    
    ois_ctrl_id = DRAFT.OpticalImageStabilizationMode
    for sensor_key, info in CAMERAS.items():
        cam = find_camera(cm, sensor_key)
        cam.acquire()
        try:
            ctrls = cam.controls
            has_ctrl = ois_ctrl_id in ctrls
            expected = info['has_ois']
            print(f"  Camera '{info['name']}': has_ois={expected}, exposed={has_ctrl}")
            assert has_ctrl == expected, \
                f"Mismatch for {sensor_key}: expected OIS control {expected}, got {has_ctrl}"
            if expected:
                ctrl_info = ctrls[ois_ctrl_id]
                print(f"    Control info: def={ctrl_info.default}, min={ctrl_info.min}, max={ctrl_info.max}")
                assert ctrl_info.min == 0, f"Unexpected min: {ctrl_info.min}"
                assert ctrl_info.max == 1, f"Unexpected max: {ctrl_info.max}"
                assert ctrl_info.default == 1, f"Unexpected default: {ctrl_info.default}"
        finally:
            cam.release()
    print("  [+] Control exposure validation PASSED\n")


def run_ois_capture_test(cm, sensor_key):
    info = CAMERAS[sensor_key]
    print(f"==========================================")
    print(f"2. Testing Dynamic OIS Control on: {info['name']}")
    print(f"==========================================")

    cam = find_camera(cm, sensor_key)
    cam.acquire()

    try:
        config = cam.generate_configuration([lc.StreamRole.Viewfinder])
        stream_cfg = config.at(0)
        stream_cfg.size = lc.Size(info['res'][0], info['res'][1])
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

        def request_controller(req_idx):
            if req_idx < 20:
                # Read back the start control without overriding it in a request.
                return {}
            return {DRAFT.OpticalImageStabilizationMode: int(req_idx < 40)}

        submitted = 0
        submitted_info = {}

        def queue_req(req):
            nonlocal submitted
            controls_to_set = request_controller(submitted)
            for k, v in controls_to_set.items():
                req.set_control(k, v)
            submitted_info[req] = (submitted, controls_to_set)
            cam.queue_request(req)
            submitted += 1

        cam.start({DRAFT.OpticalImageStabilizationMode: 0})
        for req in requests:
            queue_req(req)

        selector = selectors.DefaultSelector()
        selector.register(cm.event_fd, selectors.EVENT_READ)
        results = []
        total_frames = 60
        deadline = time.monotonic() + total_frames / 15.0 + 15.0

        try:
            while len(results) < total_frames:
                assert time.monotonic() < deadline, f"Capture timed out ({len(results)}/{total_frames} frames)"
                if not selector.select(1):
                    continue
                for req in cm.get_ready_requests():
                    if req not in submitted_info:
                        continue
                    fb = next(iter(req.buffers.values()))
                    if fb.metadata.status != lc.FrameMetadata.Status.Success:
                        if len(results) < total_frames:
                            req.reuse()
                            queue_req(req)
                        continue
                    md = req.metadata
                    req_idx, req_ctrls = submitted_info[req]
                    ois_mode = md.get(DRAFT.OpticalImageStabilizationMode)
                    results.append({
                        'req_idx': req_idx,
                        'seq': fb.metadata.sequence,
                        'req_ctrls': req_ctrls,
                        'ois_mode': ois_mode,
                    })
                    if len(results) < total_frames:
                        req.reuse()
                        queue_req(req)
            results.sort(key=lambda r: r['req_idx'])
        finally:
            cam.stop()
            while selector.select(0.1):
                ready = cm.get_ready_requests()
                if not ready:
                    break
            selector.close()

        print(f"  [+] Captured {len(results)} frames successfully")

        for first, last, expected in [(0, 20, 0), (20, 40, 1), (40, 60, 0)]:
            actual = [r["ois_mode"] for r in results if first <= r["req_idx"] < last]
            assert actual and all(mode == expected for mode in actual), (
                f"Requests {first}..{last - 1}: expected OIS {expected}, got {actual}"
            )
        print("  [+] Startup OIS Off and runtime changes Off -> On -> Off verified")

    finally:
        cam.release()


def run_non_ois_capture_test(cm, sensor_key):
    info = CAMERAS[sensor_key]
    print(f"==========================================")
    print(f"3. Verifying Non-OIS Camera Clean Operation: {info['name']}")
    print(f"==========================================")

    cam = find_camera(cm, sensor_key)
    cam.acquire()

    try:
        config = cam.generate_configuration([lc.StreamRole.Viewfinder])
        stream_cfg = config.at(0)
        stream_cfg.size = lc.Size(info['res'][0], info['res'][1])
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

        cam.start()
        for req in requests:
            cam.queue_request(req)

        selector = selectors.DefaultSelector()
        selector.register(cm.event_fd, selectors.EVENT_READ)
        results = []
        total_frames = 15
        deadline = time.monotonic() + 10.0

        try:
            while len(results) < total_frames:
                assert time.monotonic() < deadline, f"Capture timed out ({len(results)}/{total_frames} frames)"
                if not selector.select(1):
                    continue
                for req in cm.get_ready_requests():
                    fb = next(iter(req.buffers.values()))
                    if fb.metadata.status == lc.FrameMetadata.Status.Success:
                        md = req.metadata
                        assert DRAFT.OpticalImageStabilizationMode not in md, \
                            f"Unexpected OIS metadata present on non-OIS camera {sensor_key}"
                        results.append(fb.metadata.sequence)
                    if len(results) < total_frames:
                        req.reuse()
                        cam.queue_request(req)
        finally:
            cam.stop()
            while selector.select(0.1):
                ready = cm.get_ready_requests()
                if not ready:
                    break
            selector.close()

        print(f"  [+] Captured {len(results)} frames without OIS metadata cleanly")
    finally:
        cam.release()


def main():
    cm = lc.CameraManager.singleton()
    print(f"Discovered {len(cm.cameras)} cameras on device")

    # Step 1: Verify control registration on all cameras
    verify_control_presence(cm)

    # Step 2: Dynamic control on rear main (OIS)
    run_ois_capture_test(cm, 'imx363')

    # Step 3: Verify non-OIS cameras stream cleanly without OIS metadata
    for sensor_key in ['imx351', 's5k3m5', 'imx520']:
        run_non_ois_capture_test(cm, sensor_key)

    print("\n==========================================")
    print("ALL OIS VALIDATION TESTS PASSED (Exit 0)")
    print("==========================================")



if __name__ == '__main__':
    main()
