#!/usr/bin/env python3
"""Hardware validation of front camera QTI AWB face assist, metadata and camera switching."""

import json
import math
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
    deadline = time.monotonic() + frames / 20.0 + 10.0

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
        # Sort results strictly by request index
        results.sort(key=lambda r: r['req_idx'])
        return results[:frames]
    finally:
        cam.stop()
        cm.get_ready_requests()
        selector.close()


def test_front_face_cadence(cm, cam):
    print("Testing IMX520 face cadence, FaceSizeSort and manual AWB switching...", flush=True)
    # Total 120 frames:
    # 0..19: No face baseline
    # 20..49: Single face (400, 300, 800, 500) -> cadence 10 frames to stable
    # 50..69: Two faces: small (100, 100, 200, 200) vs large (300, 200, 1000, 600) -> FaceSizeSort
    # 70..89: Face removed -> disappearance
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
                lc.Rectangle(100, 100, 200, 200),
                lc.Rectangle(300, 200, 1000, 600),
            ]
            ctrls[DRAFT.FaceDetectFaceScores] = [70, 95]
        elif 70 <= idx < 90:
            ctrls[DRAFT.FaceDetectFaceRectangles] = []
        elif 90 <= idx < 105:
            ctrls[C.AwbEnable] = False
            ctrls[C.ColourGains] = (1.5, 2.0)
        return ctrls

    results = run_capture(cm, cam, (1632, 924), 120, request_controls)

    # 1. Baseline (requests 0..15)
    for r in results[0:16]:
        assert r['face_rects'] is None, f"Expected no face rect in baseline, got {r['face_rects']}"
        assert r['face_scores'] is None, f"Expected no face score in baseline, got {r['face_scores']}"
        assert r['awb'] is True

    # 2. Face presentation cadence (requests 20..25)
    for r in results[20:26]:
        assert r['face_rects'] is None or r['face_scores'] is None, f"Expected cadence period without stable face: {r}"

    # Stable single face detection (requests 28..45)
    stable_results = [r for r in results[28:46] if r['face_rects'] is not None]
    assert len(stable_results) >= 15, f"Expected face detected in stable period, got {len(stable_results)}"
    for r in stable_results:
        assert len(r['face_rects']) == 1
        rect = r['face_rects'][0]
        assert abs(rect.x - 400) <= 2 and abs(rect.y - 300) <= 2, f"Unexpected rect: {rect}"
        assert abs(rect.width - 800) <= 2 and abs(rect.height - 500) <= 2, f"Unexpected rect: {rect}"
        assert len(r['face_scores']) == 1 and r['face_scores'][0] > 0

    # 3. Two faces area sort (requests 50..65)
    # The larger face is (300, 200, 1000, 600)
    sorted_results = [r for r in results[50:66] if r['face_rects'] is not None]
    assert len(sorted_results) >= 12, f"Expected faces detected in two-face period, got {len(sorted_results)}"
    for r in sorted_results:
        assert len(r['face_rects']) == 1
        rect = r['face_rects'][0]
        assert abs(rect.x - 300) <= 2 and abs(rect.y - 200) <= 2, f"FaceSizeSort failed: {rect}"
        assert abs(rect.width - 1000) <= 2 and abs(rect.height - 600) <= 2, f"FaceSizeSort failed: {rect}"

    # 4. Face removed (requests 70..85)
    # Metadata should clear face rects/scores once input has no face
    cleared_results = results[70:86]
    for r in cleared_results:
        assert r['face_rects'] is None and r['face_scores'] is None, f"Expected cleared face: {r}"

    # 5. Manual AWB (requests 95..104)
    manual_results = results[95:105]
    for r in manual_results:
        assert r['awb'] is False, f"Expected manual AwbEnable=False, got {r['awb']}"
        assert abs(r['wb'][0] - 1.5) < 0.05 and abs(r['wb'][1] - 2.0) < 0.05, f"Unexpected gains: {r['wb']}"

    # 6. Auto AWB resumed (requests 110..119)
    resumed_results = results[110:120]
    for r in resumed_results:
        assert r['awb'] is True, f"Expected AwbEnable=True, got {r['awb']}"
        assert 1500 <= r['cct'] <= 12000, f"Invalid CCT: {r['cct']}"

    print("PASS: Front camera face cadence, selection and AWB switching.", flush=True)


def test_stop_and_restart(cm, cam):
    print("Testing IMX520 stream stop and restart...", flush=True)
    def dummy_requests(_):
        return {C.AeEnable: True, C.AwbEnable: True}

    res1 = run_capture(cm, cam, (1632, 924), 10, dummy_requests)
    assert len(res1) == 10
    assert res1[0]['cct'] == 5000, f"Expected initial CCT 5000, got {res1[0]['cct']}"

    # Second run after stop
    res2 = run_capture(cm, cam, (1632, 924), 15, dummy_requests)
    assert len(res2) == 15
    assert res2[0]['cct'] == 5000, f"Expected clean reset CCT 5000 on restart, got {res2[0]['cct']}"
    assert res2[-1]['cct'] > 0
    print("PASS: Stream stop and restart reset verified.", flush=True)


def test_four_camera_switching(cm):
    print("Testing four-camera switching sequence...", flush=True)
    # Order: s5k3m5 -> imx351 -> imx363 -> imx520
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
            print(f"  Camera '{sensor}' verified: CCT={res[-1]['cct']} Gains={res[-1]['wb']}", flush=True)
        finally:
            cam.release()

    # Re-verify imx520 face after switching
    front_cam = next(c for c in cm.cameras if 'imx520' in c.id)
    front_cam.acquire()
    try:
        def face_req(idx):
            ctrls = {C.AeEnable: True, C.AwbEnable: True, DRAFT.FaceDetectMode: DRAFT.FaceDetectModeEnum.Full}
            if idx >= 5:
                ctrls[DRAFT.FaceDetectFaceRectangles] = [lc.Rectangle(500, 400, 600, 500)]
                ctrls[DRAFT.FaceDetectFaceScores] = [88]
            return ctrls
        res = run_capture(cm, front_cam, (1632, 924), 25, face_req)
        stable = [r for r in res[16:] if r['face_rects'] is not None]
        assert len(stable) >= 5, f"Expected stable face after 4-camera switch, got {len(stable)}"
        print("  Front camera face assist verified after full four-camera cycle.", flush=True)
    finally:
        front_cam.release()

    print("PASS: Four-camera switching validated cleanly.", flush=True)


def main():
    cm = lc.CameraManager.singleton()
    front_cam = next((c for c in cm.cameras if 'imx520' in c.id), None)
    assert front_cam is not None, "IMX520 front camera not found"

    front_cam.acquire()
    try:
        test_front_face_cadence(cm, front_cam)
        test_stop_and_restart(cm, front_cam)
    finally:
        front_cam.release()

    test_four_camera_switching(cm)

    print("ALL HARDWARE TESTS PASSED: QTI AWB face assist combination chain validated.", flush=True)


if __name__ == '__main__':
    main()
