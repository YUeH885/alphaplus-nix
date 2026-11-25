#!/usr/bin/env python3
"""Hardware validation script for Camera Flash & Precapture Measurement on LM-G820N.

Tests across all 4 cameras (IMX363, IMX351, S5K3M5, IMX520):
1. Flash control & metadata exposure:
   - Rear cameras (IMX363, IMX351, S5K3M5) expose draft.FlashMode and draft.AePrecaptureTrigger
   - Front camera (IMX520) does not expose flash controls; reports FlashStateUnavailable
2. Torch mode control:
   - Setting draft.FlashMode = FlashModeTorch lights the LED, verified through the LED class brightness attribute
   - Setting draft.FlashMode = FlashModeOff turns off flash LED
3. Flash precapture sequence (FlashMeasurement):
   - draft.AePrecaptureTrigger = Start engages torch illumination
   - Precapture frames run through enter skip and torch settling
   - AeState reports Searching (1) and then Converged (2)
4. Precapture cancellation:
   - draft.AePrecaptureTrigger = Cancel aborts precapture and turns off torch LED
5. Single still capture strobe (draft.FlashMode = FlashModeSingle):
   - Triggers main flash strobe pulse and reports FlashStateFlashFired (3)
   - Resets to FlashStateReady (2) on subsequent frames
6. Camera stop safety:
   - Flash LED is guaranteed turned off when camera stream stops
"""

import argparse
import fcntl
import mmap
import statistics
import struct
import time
import selectors
import libcamera as lc

C = lc.controls
DRAFT = lc.controls.draft

FLASH_MODE_OFF = 0
FLASH_MODE_SINGLE = 1
FLASH_MODE_TORCH = 2

FLASH_STATE_UNAVAILABLE = 0
FLASH_STATE_CHARGING = 1
FLASH_STATE_READY = 2
FLASH_STATE_FIRED = 3
FLASH_STATE_PARTIAL = 4

PRECAPTURE_TRIGGER_IDLE = 0
PRECAPTURE_TRIGGER_START = 1
PRECAPTURE_TRIGGER_CANCEL = 2

AE_STATE_INACTIVE = 0
AE_STATE_SEARCHING = 1
AE_STATE_CONVERGED = 2
AE_STATE_LOCKED = 3

SYSFS_WHITE_BRIGHTNESS = "/sys/class/leds/white:flash/brightness"
SYSFS_YELLOW_BRIGHTNESS = "/sys/class/leds/yellow:flash/brightness"
FIXED_EXPOSURE_FRAME = 100
AMBIENT_FRAMES = range(105, 110)
STROBE_FRAME = 115
STROBE_EVAL_WINDOW = set(AMBIENT_FRAMES) | set(range(STROBE_FRAME - 2, STROBE_FRAME + 4))

LOW_FPS_BUFFER_COUNT = 4
LOW_FPS_DURATION_US = 66667
LOW_FPS_EXPOSURE_US = 20000
LOW_FPS_AMBIENT_FRAMES = range(8, 13)
LOW_FPS_STROBE_FRAME = 15
LOW_FPS_TOTAL_FRAMES = 22
LOW_FPS_EVAL_WINDOW = set(LOW_FPS_AMBIENT_FRAMES) | set(
    range(LOW_FPS_STROBE_FRAME - 2, LOW_FPS_STROBE_FRAME + 4)
)


def read_sysfs_brightness(path=SYSFS_WHITE_BRIGHTNESS):
    try:
        with open(path, "r") as f:
            return int(f.read().strip())
    except Exception:
        return -1


def sample_bands(buffer, config):
    plane = buffer.planes[0]
    w, h, stride = config.size.width, config.size.height, config.stride
    bands = {
        "center": (h // 4, h * 3 // 4),
        "top": (h * 15 // 100, h * 35 // 100),
        "mid": (h * 40 // 100, h * 60 // 100),
        "bot": (h * 65 // 100, h * 85 // 100),
    }
    fcntl.ioctl(plane.fd, 0x40086200, struct.pack("Q", 1))
    try:
        with mmap.mmap(plane.fd, plane.offset + plane.length, flags=mmap.MAP_SHARED,
                       prot=mmap.PROT_READ) as data:
            return {
                name: tuple(
                    data[plane.offset + y * stride + x]
                    for y in range(y0, y1, 16)
                    for x in range(w // 4, w * 3 // 4, 16)
                )
                for name, (y0, y1) in bands.items()
            }
    finally:
        fcntl.ioctl(plane.fd, 0x40086200, struct.pack("Q", 5))


def upper_rise(samples, reference):
    return statistics.quantiles([a - b for a, b in zip(samples, reference)], n=20)[-1]


def verify_strobe_isolation(results, ambient_range, strobe_idx, label):
    by_idx = {r["req_idx"]: r for r in results}
    strobe_frame = by_idx.get(strobe_idx)
    assert strobe_frame is not None, f"{label}: request {strobe_idx} not completed"
    assert strobe_frame["flash_state"] == FLASH_STATE_FIRED, (
        f"{label}: expected FlashStateFlashFired (3) on req {strobe_idx}, "
        f"got {strobe_frame['flash_state']}"
    )

    ambient = [by_idx[i]["bands"] for i in ambient_range if i in by_idx and by_idx[i]["bands"]]
    assert len(ambient) >= 3, f"{label}: too few fixed-exposure baseline frames"
    ref_center = [statistics.median(s) for s in zip(*(b["center"] for b in ambient))]

    baseline_noise = max(upper_rise(b["center"], ref_center) for b in ambient)
    flash_rise = upper_rise(strobe_frame["bands"]["center"], ref_center)
    assert flash_rise >= max(2.0, baseline_noise + 1.0), (
        f"{label}: strobe frame did not brighten: 95th percentile Y increased by {flash_rise:.1f}, "
        f"ambient variation {baseline_noise:.1f}. Aim the camera at a nearby matte target."
    )

    band_rises = {}
    for band in ("top", "mid", "bot"):
        ref_band = [statistics.median(s) for s in zip(*(b[band] for b in ambient))]
        band_rises[band] = upper_rise(strobe_frame["bands"][band], ref_band)

    window_rises = {}
    for offset in (-2, -1, 0, 1, 2, 3):
        rec = by_idx.get(strobe_idx + offset)
        if rec and rec["bands"]:
            window_rises[offset] = upper_rise(rec["bands"]["center"], ref_center)

    isolation_limit = max(2.0, baseline_noise + 1.5, flash_rise * 0.25)
    for offset in (-2, 2, 3):
        if offset in window_rises:
            assert window_rises[offset] < isolation_limit, (
                f"{label}: SOF isolation failed at offset {offset:+d}: "
                f"Y95 rise {window_rises[offset]:.1f} >= limit {isolation_limit:.1f} "
                f"(strobe {flash_rise:.1f}, window={window_rises})"
            )

    print(
        f"  [+] {label} strobe Y95 rise={flash_rise:.1f} "
        f"(top={band_rises['top']:.1f}, mid={band_rises['mid']:.1f}, bot={band_rises['bot']:.1f}; "
        f"pre-2={window_rises.get(-2, 0.0):.1f}, pre-1={window_rises.get(-1, 0.0):.1f}, "
        f"post+1={window_rises.get(1, 0.0):.1f}, post+2={window_rises.get(2, 0.0):.1f}, "
        f"post+3={window_rises.get(3, 0.0):.1f})"
    )


def capture_sequence(cm, cam, frames, request_fn, eval_window, buffer_count=None):
    config = cam.generate_configuration([lc.StreamRole.Viewfinder])
    stream_cfg = config.at(0)
    stream_cfg.size = lc.Size(1920, 1080)
    stream_cfg.pixel_format = lc.formats.NV12
    if buffer_count is not None:
        stream_cfg.buffer_count = buffer_count
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
    deadline = time.monotonic() + frames / 10.0 + 20.0

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
                req_idx, _ = submitted_info[req]
                hw_brightness = read_sysfs_brightness(SYSFS_WHITE_BRIGHTNESS)
                yellow_brightness = read_sysfs_brightness(SYSFS_YELLOW_BRIGHTNESS)
                results.append({
                    'req_idx': req_idx,
                    'seq': fb.metadata.sequence,
                    'flash_state': md.get(DRAFT.FlashState) if DRAFT.FlashState in md else None,
                    'flash_mode': md.get(DRAFT.FlashMode) if DRAFT.FlashMode in md else None,
                    'ae_state': md.get(C.AeState) if C.AeState in md else None,
                    'precapture_trigger': (
                        md.get(DRAFT.AePrecaptureTrigger)
                        if DRAFT.AePrecaptureTrigger in md else None
                    ),
                    'hw_brightness': hw_brightness,
                    'yellow_brightness': yellow_brightness,
                    'bands': sample_bands(fb, stream_cfg) if req_idx in eval_window else None,
                })
                if len(results) < frames:
                    req.reuse()
                    queue_req(req)

        results.sort(key=lambda r: r['req_idx'])
        return results
    finally:
        cam.stop()
        while selector.select(0.1):
            ready = cm.get_ready_requests()
            if not ready:
                break
        selector.unregister(cm.event_fd)
        selector.close()


def find_camera(cm, sensor_key):
    for cam in cm.cameras:
        if sensor_key in cam.id:
            return cam
    raise RuntimeError(f"Camera with sensor '{sensor_key}' not found")


def run_sensor_test(cm, sensor_key):
    cam = find_camera(cm, sensor_key)
    cam.acquire()

    is_front = (sensor_key == "imx520")
    has_flash = not is_front

    print(f"\n========================================================")
    print(f"Testing {sensor_key} (has_flash={has_flash})")
    print(f"========================================================")

    try:
        ctrl_map = cam.controls
        if has_flash:
            assert DRAFT.FlashMode in ctrl_map, f"draft.FlashMode not exposed on {sensor_key}"
            assert DRAFT.AePrecaptureTrigger in ctrl_map, f"draft.AePrecaptureTrigger not exposed on {sensor_key}"
            print("  [+] draft.FlashMode and draft.AePrecaptureTrigger registered")
        else:
            assert DRAFT.FlashMode not in ctrl_map
            assert DRAFT.AePrecaptureTrigger not in ctrl_map
            print("  [+] Front camera: flash controls omitted as expected")

        def request_fn(req_idx):
            ctrls = {}
            if not has_flash:
                return ctrls
            # Test schedule:
            # Req 0..19: Preview baseline (no flash controls)
            # Req 20..39: Torch mode test (FlashMode = Torch)
            # Req 40: Explicit Torch off (FlashMode = Off once)
            # Req 41..59: Preview baseline without controls
            # Req 60: Single edge trigger Precapture Start (Trigger = Start once)
            # Req 61..89: Precapture convergence preview without controls
            # Req 90: Precapture Start once
            # Req 93: Precapture Cancel once
            # Req 94..99: Preview baseline without controls
            # Req 100..114: Fixed manual exposure baseline before strobe
            # Req 115: Single capture strobe (FlashMode = Single once)
            # Req 116..140: Preview baseline without controls (verify auto torch off & no state leak)
            if 20 <= req_idx < 40:
                ctrls[DRAFT.FlashMode] = FLASH_MODE_TORCH
            elif req_idx == 40:
                ctrls[DRAFT.FlashMode] = FLASH_MODE_OFF
            elif req_idx == 60:
                ctrls[DRAFT.AePrecaptureTrigger] = PRECAPTURE_TRIGGER_START
            elif req_idx == 90:
                ctrls[DRAFT.AePrecaptureTrigger] = PRECAPTURE_TRIGGER_START
            elif req_idx == 93:
                ctrls[DRAFT.AePrecaptureTrigger] = PRECAPTURE_TRIGGER_CANCEL
            if req_idx == FIXED_EXPOSURE_FRAME:
                ctrls.update({C.AeEnable: False, C.ExposureTime: 5000, C.AnalogueGain: 1.0})
            elif req_idx == STROBE_FRAME:
                ctrls[DRAFT.FlashMode] = FLASH_MODE_SINGLE
            return ctrls

        frames = 140 if has_flash else 30
        results = capture_sequence(cm, cam, frames, request_fn, STROBE_EVAL_WINDOW)

        low_fps_results = None
        if has_flash:
            def low_fps_request_fn(req_idx):
                ctrls = {}
                if req_idx == 0:
                    ctrls.update({
                        C.FrameDurationLimits: (LOW_FPS_DURATION_US, LOW_FPS_DURATION_US),
                        C.AeEnable: False,
                        C.ExposureTime: LOW_FPS_EXPOSURE_US,
                        C.AnalogueGain: 1.0,
                    })
                elif req_idx == LOW_FPS_STROBE_FRAME:
                    ctrls[DRAFT.FlashMode] = FLASH_MODE_SINGLE
                return ctrls

            low_fps_results = capture_sequence(
                cm, cam, LOW_FPS_TOTAL_FRAMES, low_fps_request_fn,
                LOW_FPS_EVAL_WINDOW, buffer_count=LOW_FPS_BUFFER_COUNT,
            )

    finally:
        cam.release()

    final_brightness = read_sysfs_brightness()
    assert final_brightness == 0, f"Flash LED remained ON ({final_brightness}) after camera stop!"
    print(f"  [+] Final hardware sysfs brightness = {final_brightness} (Off)")

    if not has_flash:
        states = [r['flash_state'] for r in results]
        assert all(s == FLASH_STATE_UNAVAILABLE for s in states), f"Front camera reported unexpected flash state: {states}"
        print("  [+] Front camera correctly reports FlashStateUnavailable (0) on all frames")
        print(f"[PASS] {sensor_key} validation complete!")
        return

    torch_active_seen = any(
        r['hw_brightness'] > 0 and (r['yellow_brightness'] < 0 or r['yellow_brightness'] > 0)
        for r in results if 22 <= r['req_idx'] <= 38
    )
    precapture_torch_seen = any(
        r['hw_brightness'] > 0 and (r['yellow_brightness'] < 0 or r['yellow_brightness'] > 0)
        for r in results if 60 <= r['req_idx'] <= 75
    )
    cancelled_torch_cleared = any(
        r['hw_brightness'] == 0 and (r['yellow_brightness'] < 0 or r['yellow_brightness'] == 0)
        for r in results if 95 <= r['req_idx'] <= 105
    )

    # Rear camera assertions
    preview_states = [r['flash_state'] for r in results[:20]]
    assert all(s == FLASH_STATE_READY for s in preview_states), f"Preview flash states unexpected: {preview_states}"
    print(f"  [+] Baseline preview: FlashState = Ready (2) on all {len(preview_states)} frames")

    assert torch_active_seen, "Torch mode failed to illuminate hardware LED"
    torch_off_brightness = [r['hw_brightness'] for r in results[45:55]]
    assert all(b == 0 for b in torch_off_brightness), f"Torch failed to turn off: {torch_off_brightness}"
    print("  [+] Torch mode ON/OFF hardware validation passed (LED active -> 0)")

    assert precapture_torch_seen, "Precapture trigger failed to activate torch LED"
    # Verify edge trigger semantics: frame 60 reports Start, subsequent frames auto-reset to Idle
    frame_60 = next((r for r in results if r['req_idx'] == 60), None)
    assert frame_60 is not None and frame_60['precapture_trigger'] == PRECAPTURE_TRIGGER_START, f"Frame 60 expected Start trigger, got {frame_60}"
    post_trigger_frames = [r for r in results if 61 <= r['req_idx'] <= 89]
    assert all(r['precapture_trigger'] == PRECAPTURE_TRIGGER_IDLE for r in post_trigger_frames), "Precapture trigger failed to auto-reset to Idle"
    print("  [+] Edge trigger auto-reset passed (Req 60 Start -> Req 61..89 Idle)")

    precapture_ae_states = [r['ae_state'] for r in results if 60 <= r['req_idx'] <= 89]
    assert AE_STATE_SEARCHING in precapture_ae_states, f"AE did not search: {precapture_ae_states}"
    assert precapture_ae_states[-1] == AE_STATE_CONVERGED, f"AE did not converge: {precapture_ae_states}"
    first_searching = precapture_ae_states.index(AE_STATE_SEARCHING)
    assert AE_STATE_CONVERGED in precapture_ae_states[first_searching + 1:], (
        f"AE did not converge after searching: {precapture_ae_states}"
    )
    print("  [+] Precapture measurement sequence passed (Searching -> Converged)")

    assert cancelled_torch_cleared, "Precapture cancel failed to extinguish torch LED"
    print("  [+] Precapture cancellation passed (LED extinguished immediately)")

    verify_strobe_isolation(results, AMBIENT_FRAMES, STROBE_FRAME, "30fps")

    post_strobe_frames = [r for r in results if 116 <= r['req_idx'] <= 125]
    assert all(r['flash_state'] == FLASH_STATE_READY for r in post_strobe_frames), f"Subsequent frames polluted: {[r['flash_state'] for r in post_strobe_frames]}"
    # Verify torch was extinguished during main capture and did not remain on
    assert all(r['hw_brightness'] == 0 for r in post_strobe_frames), f"Torch remained active after strobe capture: {[r['hw_brightness'] for r in post_strobe_frames]}"
    print("  [+] Single capture strobe fire passed (Req 115 FlashFired=3, Req 116..125 Ready=2, Torch extinguished=0)")

    verify_strobe_isolation(low_fps_results, LOW_FPS_AMBIENT_FRAMES, LOW_FPS_STROBE_FRAME, "15fps")
    low_fps_post = [r for r in low_fps_results if LOW_FPS_STROBE_FRAME < r['req_idx'] < LOW_FPS_TOTAL_FRAMES]
    assert all(r['flash_state'] == FLASH_STATE_READY for r in low_fps_post), (
        f"15fps subsequent frames polluted: {[r['flash_state'] for r in low_fps_post]}"
    )
    assert all(r['hw_brightness'] == 0 for r in low_fps_post), (
        f"15fps flash LED remained active: {[r['hw_brightness'] for r in low_fps_post]}"
    )
    print(f"[PASS] {sensor_key} flash & precapture validation complete!")


def main():
    cameras = ['imx363', 'imx351', 's5k3m5', 'imx520']
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sensor", action="append", choices=cameras,
                        help="Run for a selected sensor only (repeatable)")
    args = parser.parse_args()
    cm = lc.CameraManager.singleton()
    print(f"Discovered {len(cm.cameras)} cameras on device")

    for sensor in args.sensor or cameras:
        run_sensor_test(cm, sensor)

    print("\n========================================================")
    print("FLASH & PRECAPTURE MEASUREMENT TEST PASSED!")
    print("========================================================")


if __name__ == '__main__':
    main()
