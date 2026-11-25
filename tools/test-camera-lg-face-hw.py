#!/usr/bin/env python3
"""Hardware validation of rear camera LG AWB face assist and FWB, metadata and 4-camera switching."""

import selectors
import sys
import time

import libcamera as lc

C = lc.controls
DRAFT = lc.controls.draft


def run_capture(cm, cam, size, frames, request_fn):
    cfg = cam.generate_configuration([lc.StreamRole.Viewfinder])
    sc = cfg.at(0)
    sc.size = lc.Size(*size)
    cam.configure(cfg)
    stream = sc.stream
    allocator = lc.FrameBufferAllocator(cam)
    assert allocator.allocate(stream) > 0, "Failed to allocate buffers"

    requests = []
    for buffer in allocator.buffers(stream):
        req = cam.create_request()
        req.add_buffer(stream, buffer)
        requests.append(req)

    submitted = 0
    submitted_info = {}

    def queue_req(req):
        nonlocal submitted
        controls = request_fn(submitted)
        for key, value in controls.items():
            req.set_control(key, value)
        submitted_info[req] = (submitted, controls)
        cam.queue_request(req)
        submitted += 1

    cam.start()
    for req in requests:
        queue_req(req)

    selector = selectors.DefaultSelector()
    selector.register(cm.event_fd, selectors.EVENT_READ)
    results = []
    deadline = time.monotonic() + frames / 20.0 + 15.0

    try:
        while len(results) < frames:
            assert time.monotonic() < deadline, f"Capture timed out ({len(results)}/{frames} frames)"
            if not selector.select(1):
                continue
            for req in cm.get_ready_requests():
                fb = req.buffers[stream]
                assert fb.metadata.status == lc.FrameMetadata.Status.Success
                md = req.metadata
                req_idx, req_ctrls = submitted_info[req]
                results.append({
                    'req_idx': req_idx,
                    'req_ctrls': req_ctrls,
                    'seq': fb.metadata.sequence,
                    'timestamp': fb.metadata.timestamp,
                    'sensor_timestamp': md.get(C.SensorTimestamp),
                    'awb': md.get(C.AwbEnable),
                    'wb': md.get(C.ColourGains),
                    'cct': md.get(C.ColourTemperature),
                    'ccm': md.get(C.ColourCorrectionMatrix),
                    'face_mode': md.get(DRAFT.FaceDetectMode),
                    'face_rects': md.get(DRAFT.FaceDetectFaceRectangles),
                    'face_scores': md.get(DRAFT.FaceDetectFaceScores),
                })
                if len(results) < frames:
                    req.reuse()
                    queue_req(req)
        results.sort(key=lambda r: r['req_idx'])
        return results[:frames]
    finally:
        cam.stop()
        cm.get_ready_requests()
        selector.close()


def test_lg_awb_face_cadence(cm, cam, cam_id):
    print(f"Testing {cam_id} LG AWB face cadence, multi-face and manual AWB switching...", flush=True)

    # Check control registration
    ctrl_info = cam.controls
    has_fd_mode = any(c.name == 'FaceDetectMode' for c in ctrl_info)
    has_fd_rects = any(c.name == 'FaceDetectFaceRectangles' for c in ctrl_info)
    has_fd_scores = any(c.name == 'FaceDetectFaceScores' for c in ctrl_info)
    print(f"Controls: FaceDetectMode={has_fd_mode}, FaceDetectFaceRectangles={has_fd_rects}, FaceDetectFaceScores={has_fd_scores}")
    assert has_fd_mode and has_fd_rects and has_fd_scores, f"Missing face controls on {cam_id}"

    # Total 120 frames:
    # 0..19: No face baseline
    # 20..49: Single face -> 5-frame debounce -> stable face
    # 50..69: Two faces
    # 70..89: Face cleared
    # 90..104: Manual AWB (AwbEnable = False, ColourGains = (1.5, 2.0))
    # 105..119: Auto AWB resumed
    def request_controls(idx):
        ctrls = {
            C.AeEnable: True,
            C.AwbEnable: True,
            DRAFT.FaceDetectMode: DRAFT.FaceDetectModeEnum.Full,
        }
        if 20 <= idx < 50:
            ctrls[DRAFT.FaceDetectFaceRectangles] = [lc.Rectangle(400, 300, 800, 500)]
            ctrls[DRAFT.FaceDetectFaceScores] = [90]
        elif 50 <= idx < 70:
            ctrls[DRAFT.FaceDetectFaceRectangles] = [
                lc.Rectangle(400, 300, 800, 500),
                lc.Rectangle(100, 100, 300, 300),
            ]
            ctrls[DRAFT.FaceDetectFaceScores] = [90, 80]
        elif 70 <= idx < 90:
            ctrls[DRAFT.FaceDetectMode] = DRAFT.FaceDetectModeEnum.Off
            ctrls[DRAFT.FaceDetectFaceRectangles] = []
        elif 90 <= idx < 105:
            ctrls[C.AwbEnable] = False
            ctrls[C.ColourGains] = (1.5, 2.0)
            ctrls[DRAFT.FaceDetectMode] = DRAFT.FaceDetectModeEnum.Off
        return ctrls

    results = run_capture(cm, cam, (2016, 1136), 120, request_controls)

    # 1. Baseline (requests 0..15)
    for r in results[0:16]:
        assert r['face_rects'] is None or len(r['face_rects']) == 0, f"Expected no face in baseline: {r}"
        assert r['awb'] is True
        assert r['wb'] is not None

    # Print cadence transition around request 20
    print("Transition around request 20:")
    for r in results[18:28]:
        print(f"  req_idx={r['req_idx']}, seq={r['seq']}, face_mode={r['face_mode']}, face_rects={r['face_rects']}, face_scores={r['face_scores']}")

    print("Transition around request 50:")
    for r in results[45:56]:
        print(f"  req_idx={r['req_idx']}, seq={r['seq']}, num_rects={len(r['face_rects']) if r['face_rects'] else 0}, face_scores={r['face_scores']}")

    print("Transition around request 70:")
    for r in results[65:76]:
        print(f"  req_idx={r['req_idx']}, seq={r['seq']}, num_rects={len(r['face_rects']) if r['face_rects'] else 0}, face_scores={r['face_scores']}")

    # 2. Debounce window immediately following first face presentation
    # The first 2-3 completed requests after queueing face at req_idx=20 must NOT have stable face
    for r in results[20:22]:
        assert r['face_rects'] is None or len(r['face_rects']) == 0, f"Expected debounce cadence without face: {r}"

    # 3. Stable single face (requests 28..44)
    stable_results = [r for r in results[28:45] if r['face_rects'] is not None and len(r['face_rects']) > 0]
    assert len(stable_results) >= 14, f"Expected stable face detected, got {len(stable_results)}"
    for r in stable_results:
        assert len(r['face_rects']) == 1
        rect = r['face_rects'][0]
        assert abs(rect.x - 400) <= 2 and abs(rect.y - 300) <= 2, f"Unexpected rect: {rect}"
        assert abs(rect.width - 800) <= 2 and abs(rect.height - 500) <= 2, f"Unexpected rect: {rect}"
        assert len(r['face_scores']) == 1 and r['face_scores'][0] in (50, 100)

    # 4. Two faces (requests 55..65)
    multi_results = [r for r in results[55:66] if r['face_rects'] is not None and len(r['face_rects']) == 2]
    assert len(multi_results) >= 10, f"Expected 2 faces detected, got {len(multi_results)}"
    for r in multi_results:
        assert len(r['face_scores']) == 2
        for s in r['face_scores']:
            assert s in (50, 100)

    # 5. Face cleared (requests 75..88)
    cleared_results = results[75:89]
    for r in cleared_results:
        assert r['face_rects'] is None or len(r['face_rects']) == 0, f"Expected cleared face: {r}"

    # 6. Manual AWB (requests 95..104)
    manual_results = results[95:105]
    for r in manual_results:
        assert r['awb'] is False, f"Expected manual AwbEnable=False, got {r['awb']}"
        assert abs(r['wb'][0] - 1.5) < 0.05 and abs(r['wb'][1] - 2.0) < 0.05, f"Unexpected gains: {r['wb']}"

    # 7. Auto AWB resumed (requests 110..119)
    resumed_results = results[110:120]
    for r in resumed_results:
        assert r['awb'] is True, f"Expected AwbEnable=True, got {r['awb']}"
        assert 1500 <= r['cct'] <= 12000, f"Invalid CCT: {r['cct']}"

    print(f"PASS: {cam_id} face cadence, multi-face and AWB switching.", flush=True)


def test_rear_stop_and_restart(cm, cam, cam_id):
    print(f"Testing {cam_id} stream stop and restart...", flush=True)
    def dummy_requests(_):
        return {C.AeEnable: True, C.AwbEnable: True}

    res1 = run_capture(cm, cam, (2016, 1136), 10, dummy_requests)
    assert len(res1) == 10
    res2 = run_capture(cm, cam, (2016, 1136), 15, dummy_requests)
    assert len(res2) == 15
    print(f"PASS: {cam_id} stream stop and restart reset verified.", flush=True)


def test_four_camera_switching(cm):
    print("Testing four-camera switching sequence...", flush=True)
    camera_ids = ['s5k3m5', 'imx351', 'imx363', 'imx520']
    for sensor in camera_ids:
        cam = next(c for c in cm.cameras if sensor in c.id)
        cam.acquire()
        try:
            size = (2016, 1136) if sensor != 'imx520' else (1632, 924)
            res = run_capture(cm, cam, size, 10, lambda _: {C.AeEnable: True, C.AwbEnable: True})
            assert len(res) == 10, f"Failed streaming {sensor}"
            assert res[-1]['wb'] is not None
            assert res[-1]['cct'] > 0
            print(f"  Camera {sensor} stream OK (CCT={res[-1]['cct']}, WB={res[-1]['wb']})", flush=True)
        finally:
            cam.release()
    print("PASS: Four-camera switching sequence verified.", flush=True)


def main():
    cm = lc.CameraManager.singleton()
    cameras = [c.id for c in cm.cameras]
    print(f"Available cameras: {cameras}")

    # Test rear cameras: IMX363, IMX351, S5K3M5
    rear_sensors = ['imx363', 'imx351', 's5k3m5']
    for sensor in rear_sensors:
        cam = next((c for c in cm.cameras if sensor in c.id), None)
        assert cam is not None, f"Camera {sensor} not found!"
        cam.acquire()
        try:
            test_lg_awb_face_cadence(cm, cam, sensor)
            test_rear_stop_and_restart(cm, cam, sensor)
        finally:
            cam.release()

    # 4-camera switching
    test_four_camera_switching(cm)

    print("All rear camera LG AWB face assist and 4-camera hardware tests PASSED successfully!", flush=True)


if __name__ == '__main__':
    main()
