#!/usr/bin/env python3
"""Hardware validation test for IMX363 Optical Image Stabilization (OIS) efficacy.

Tests optical stabilization performance under repeatable physical micro-vibration:
1. Controlled Physical Vibration:
   - Uses device onboard DW7800 linear resonant haptic actuator (/dev/input/by-path/platform-890000.i2c-event).
   - Generates repeatable sine-wave physical vibration (~167 Hz, FF_RUMBLE magnitude 36000).
2. Fixed Exposure & Illumination Conditions:
   - Illumination: FlashMode = Torch (onboard flash LED) ensures sharp, high-contrast texture.
   - Exposure: manual fixed ExposureTime (6000 µs), AnalogueGain (2.0), fixed lens position.
3. Multi-Phase Streaming Comparative Evaluation:
   - Phase 1: Static Baseline (Vibration Off, OIS Off) -> 30 frames.
   - Phase 2: Vibration with OIS Off (Vibration On, OIS Off) -> 40 frames.
   - Phase 3: Vibration with OIS On (Vibration On, OIS On) -> 40 frames.
   - Phase 4: Recovery (Vibration Off, OIS Off) -> 20 frames.
4. Quantitative Image Metrics:
   - Variance of Laplacian (Sharpness / edge preservation under motion blur).
   - Gray-level Projection 1D cross-correlation with sub-pixel interpolation:
     computes inter-frame displacement (dx, dy), jitter standard deviation, and RMS motion.
   - Jitter Suppression Ratio = RMS(OIS Off) / RMS(OIS On).
   - Sharpness Retention Ratio = MeanSharpness(OIS On) / MeanSharpness(OIS Off).
5. Hardware & Metadata Integrity:
   - Completed request metadata verifies draft.OpticalImageStabilizationMode per phase.
   - LC898124EP1 I2C register verification (0xf012 enable, 0xf010 center, 0xf100 status).
   - Post-test runtime PM recovery: all camera devices suspended, kernel taint = 0.
"""

import argparse
import errno
import fcntl
import json
import math
import mmap
import os
import re
import selectors
import struct
import subprocess
import sys
import time

import libcamera as lc

C = lc.controls
DRAFT = lc.controls.draft

# DMA-BUF sync ioctl constants
DMA_BUF_IOCTL_SYNC = 0x40086200
DMA_BUF_SYNC_READ = 1
DMA_BUF_SYNC_END = 4

# Linux input force-feedback ioctl constants
EVIOCSFF = 0x40304580
EV_FF = 0x15
FF_RUMBLE = 0x50

HAPTIC_DEV = "/dev/input/by-path/platform-890000.i2c-event"


class HapticActuator:
    """Controls onboard DW7800 haptic vibrator via Linux input FF_RUMBLE."""

    def __init__(self, dev_path=HAPTIC_DEV, magnitude=36000):
        self.dev_path = dev_path
        self.magnitude = magnitude
        self.fd = None
        self.effect_id = -1

    def open(self):
        if not os.path.exists(self.dev_path):
            raise FileNotFoundError(f"Haptic device not found: {self.dev_path}")
        self.fd = os.open(self.dev_path, os.O_RDWR)
        try:
            # struct ff_effect: 48 bytes
            buf = bytearray(48)
            # type(u16)=0x50, id(s16)=-1, dir(u16)=0, trigger(button=0, interval=0),
            # replay(length=10000, delay=0), u.rumble(strong, weak)
            struct.pack_into(
                "<h h H H H H H H H H", buf, 0,
                FF_RUMBLE, -1, 0, 0, 0, 10000, 0, 0,
                self.magnitude, self.magnitude,
            )
            fcntl.ioctl(self.fd, EVIOCSFF, buf, True)
            self.effect_id = struct.unpack_from("<h", buf, 2)[0]
        except Exception:
            try:
                os.close(self.fd)
            except Exception:
                pass
            self.fd = None
            raise

    def start(self):
        if self.fd is not None and self.effect_id >= 0:
            ev = struct.pack("<qqHHi", 0, 0, EV_FF, self.effect_id, 1)
            os.write(self.fd, ev)

    def stop(self):
        if self.fd is not None and self.effect_id >= 0:
            ev = struct.pack("<qqHHi", 0, 0, EV_FF, self.effect_id, 0)
            os.write(self.fd, ev)

    def close(self):
        if self.fd is not None:
            try:
                self.stop()
            except Exception:
                pass
            try:
                os.close(self.fd)
            except Exception:
                pass
            self.fd = None


def read_ois_registers():
    """Reads LC898124EP1 status registers via i2ctransfer."""
    def read_dev_name(node):
        with open(f"/sys/class/video4linux/{node}/name") as f:
            return f.read().strip()

    try:
        name = next(path for path in os.listdir("/sys/class/video4linux")
                    if read_dev_name(path).startswith("lc898124ep1 "))
        bus, addr_str = re.search(r" (\d+)-([0-9a-f]+)", read_dev_name(name)).groups()
        addr = int(addr_str, 16)
    except Exception as e:
        return {"error": f"Failed to locate lc898124ep1: {e}"}

    def read32(reg):
        cmd = ["i2ctransfer", "-f", "-y", bus, f"w2@{addr}",
               str(reg >> 8), str(reg & 255), "r4"]
        out = subprocess.check_output(cmd, text=True)
        return int.from_bytes(bytes(int(v, 16) for v in out.split()), "big")

    try:
        return {
            "enabled": read32(0xf012),
            "mode": read32(0xf013),
            "center": read32(0xf010),
            "status": read32(0xf100),
        }
    except Exception as e:
        return {"error": str(e)}


def extract_roi_y(plane_bytes, stride, cx, cy, w, h):
    """Extracts a central rectangular region of size w*h from Y plane."""
    roi = bytearray(w * h)
    start_y = cy - h // 2
    start_x = cx - w // 2
    for r in range(h):
        row_start = (start_y + r) * stride + start_x
        roi[r * w: (r + 1) * w] = plane_bytes[row_start: row_start + w]
    return roi


def compute_laplacian_variance(roi, w, h):
    """Computes Variance of Laplacian on 2D image roi of size w*h."""
    total = 0
    total_sq = 0
    count = 0
    # 3x3 discrete Laplacian kernel:
    # [ 0,  1,  0]
    # [ 1, -4,  1]
    # [ 0,  1,  0]
    for y in range(1, h - 1):
        row = y * w
        row_up = (y - 1) * w
        row_down = (y + 1) * w
        for x in range(1, w - 1):
            val = (roi[row_up + x] + roi[row_down + x] +
                   roi[row + x - 1] + roi[row + x + 1] -
                   4 * roi[row + x])
            total += val
            total_sq += val * val
            count += 1
    if count == 0:
        return 0.0
    mean = total / count
    var = (total_sq / count) - (mean * mean)
    return max(0.0, var)


def compute_gray_projections(roi, w, h):
    """Computes horizontal (proj_x) and vertical (proj_y) gray projections."""
    proj_x = [0] * w
    proj_y = [0] * h
    for y in range(h):
        row_offset = y * w
        row_sum = sum(roi[row_offset: row_offset + w])
        proj_y[y] = row_sum
        for x in range(w):
            proj_x[x] += roi[row_offset + x]
    return proj_x, proj_y


def find_displacement_1d(p1, p2, max_shift=16):
    """Normalized cross-correlation of 1D projections with sub-pixel interpolation."""
    n = len(p1)
    best_corr = -1e9
    best_shift = 0
    m1 = sum(p1) / n
    v1 = sum((x - m1) ** 2 for x in p1)
    if v1 < 1e-4:
        return 0.0

    corrs = {}
    for s in range(-max_shift, max_shift + 1):
        if s >= 0:
            s1 = p1[s:]
            s2 = p2[:n - s]
        else:
            s1 = p1[:n + s]
            s2 = p2[-s:]
        cnt = len(s1)
        if cnt < 20:
            continue
        m_s1 = sum(s1) / cnt
        m_s2 = sum(s2) / cnt
        cov = sum((a - m_s1) * (b - m_s2) for a, b in zip(s1, s2))
        v_s1 = sum((a - m_s1) ** 2 for a in s1)
        v_s2 = sum((b - m_s2) ** 2 for b in s2)
        denom = (v_s1 * v_s2) ** 0.5
        c = cov / denom if denom > 1e-6 else -1e9
        corrs[s] = c
        if c > best_corr:
            best_corr = c
            best_shift = s

    if best_corr <= -1e8:
        return 0.0

    # Parabolic sub-pixel interpolation
    if best_shift - 1 in corrs and best_shift + 1 in corrs:
        c_left = corrs[best_shift - 1]
        c_mid = corrs[best_shift]
        c_right = corrs[best_shift + 1]
        if c_left > -1e8 and c_mid > -1e8 and c_right > -1e8:
            denom = 2 * (2 * c_mid - c_left - c_right)
            if abs(denom) > 1e-6:
                delta = (c_right - c_left) / denom
                if abs(delta) < 1.0:
                    return float(best_shift + delta)

    return float(best_shift)


def compute_phase_metrics(frames):
    """Computes displacement jitter RMS and sharpness statistics for a sequence of frames."""
    if not frames:
        return {}

    ref_px = frames[0]["proj_x"]
    ref_py = frames[0]["proj_y"]

    displacements = []
    sharpnesses = []

    for f in frames:
        dx = find_displacement_1d(ref_px, f["proj_x"])
        dy = find_displacement_1d(ref_py, f["proj_y"])
        displacements.append((dx, dy))
        sharpnesses.append(f["sharpness"])

    n = len(displacements)
    dx_vals = [d[0] for d in displacements]
    dy_vals = [d[1] for d in displacements]

    mean_dx = sum(dx_vals) / n
    mean_dy = sum(dy_vals) / n
    var_dx = sum((x - mean_dx) ** 2 for x in dx_vals) / n
    var_dy = sum((y - mean_dy) ** 2 for y in dy_vals) / n

    std_dx = math.sqrt(var_dx)
    std_dy = math.sqrt(var_dy)
    # Inter-frame displacement RMS: root mean square deviation from mean
    rms_jitter = math.sqrt(var_dx + var_dy)

    mean_sharpness = sum(sharpnesses) / n
    min_sharpness = min(sharpnesses)
    max_sharpness = max(sharpnesses)

    return {
        "frame_count": n,
        "mean_sharpness": mean_sharpness,
        "min_sharpness": min_sharpness,
        "max_sharpness": max_sharpness,
        "mean_dx": mean_dx,
        "mean_dy": mean_dy,
        "std_dx": std_dx,
        "std_dy": std_dy,
        "rms_jitter": rms_jitter,
        "span_dx": max(dx_vals) - min(dx_vals),
        "span_dy": max(dy_vals) - min(dy_vals),
        "displacements": displacements,
        "sharpnesses": sharpnesses,
    }


def main():
    parser = argparse.ArgumentParser(description="IMX363 Optical Image Stabilization (OIS) Efficacy Test")
    parser.add_argument("--no-torch", action="store_true", help="Do not engage flash LED torch (use natural ambient light)")
    parser.add_argument("--exp", type=int, default=None, help="Fixed exposure time in microseconds (default: 6000 with torch, 16000 without)")
    parser.add_argument("--gain", type=float, default=None, help="Fixed analogue gain (default: 2.0 with torch, 6.0 without)")
    parser.add_argument("--magnitude", type=int, default=36000, help="DW7800 haptic vibration magnitude (default: 36000)")
    parser.add_argument("--json", type=str, default=None, help="Save structured metrics JSON to file")
    args = parser.parse_args()

    use_torch = not args.no_torch
    exp_time = args.exp if args.exp is not None else (6000 if use_torch else 16000)
    analogue_gain = args.gain if args.gain is not None else (2.0 if use_torch else 6.0)
    vibe_magnitude = args.magnitude

    print("====================================================================")
    print("LM-G820N IMX363 Optical Image Stabilization (OIS) Efficacy Test")
    print(f"  Configuration: Torch={use_torch}, Exposure={exp_time}us, Gain={analogue_gain:.1f}x, Vibration={vibe_magnitude}")
    print("====================================================================")

    # Suspend wireplumber if active to guarantee exclusive camera access
    wireplumber_stopped = False
    haptic = None
    cam = None
    selector = None
    cm = None

    try:
        try:
            status = subprocess.check_output(
                ["systemctl", "--user", "-M", "alphaplus@", "is-active", "wireplumber"],
                text=True, stderr=subprocess.DEVNULL,
            ).strip()
            if status == "active":
                print("  [*] Suspending desktop wireplumber for exclusive camera access...")
                subprocess.run(["systemctl", "--user", "-M", "alphaplus@", "stop", "wireplumber"],
                               check=True)
                wireplumber_stopped = True
                time.sleep(0.5)
        except Exception:
            pass

        haptic = HapticActuator(magnitude=vibe_magnitude)
        try:
            haptic.open()
            print(f"  [+] DW7800 haptic vibrator initialized (magnitude {vibe_magnitude})")
        except Exception as e:
            print(f"  [-] Failed to open haptic vibrator: {e}")
            return 1

        cm = lc.CameraManager.singleton()
        for c in cm.cameras:
            if "imx363" in c.id:
                cam = c
                break

        if not cam:
            print("  [-] IMX363 camera not found")
            return 1

        cam.acquire()
        print("  [+] Acquired IMX363 camera")
        # Check OIS control availability
        assert hasattr(DRAFT, "OpticalImageStabilizationMode"), \
            "Missing draft.OpticalImageStabilizationMode"
        assert DRAFT.OpticalImageStabilizationMode in cam.controls, \
            "IMX363 does not expose OpticalImageStabilizationMode"

        config = cam.generate_configuration([lc.StreamRole.Viewfinder])
        stream_cfg = config.at(0)
        stream_cfg.size = lc.Size(1920, 1080)
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

        # Plan phases:
        # Phase 1 (req 0..29): Static Baseline (No Vibration, OIS Off = 0)
        # Phase 2 (req 30..69): Vibration with OIS Off (Vibration On, OIS Off = 0)
        # Phase 3 (req 70..109): Vibration with OIS On (Vibration On, OIS On = 1)
        # Phase 4 (req 110..129): Recovery (No Vibration, OIS Off = 0)
        phase_ranges = {
            "phase1_static": range(0, 30),
            "phase2_vibe_ois_off": range(30, 70),
            "phase3_vibe_ois_on": range(70, 110),
            "phase4_recovery": range(110, 130),
        }
        total_requests = 130

        submitted = 0
        submitted_info = {}

        def get_controls_for_req(req_idx):
            ctrls = {
                lc.controls.AeEnable: False,
                lc.controls.ExposureTime: exp_time,
                lc.controls.AnalogueGain: analogue_gain,
                DRAFT.FlashMode: 2 if use_torch else 0,
            }
            if req_idx in phase_ranges["phase3_vibe_ois_on"]:
                ctrls[DRAFT.OpticalImageStabilizationMode] = 1
            else:
                ctrls[DRAFT.OpticalImageStabilizationMode] = 0
            return ctrls

        def queue_req(req):
            nonlocal submitted
            ctrls = get_controls_for_req(submitted)
            for k, v in ctrls.items():
                req.set_control(k, v)
            submitted_info[req] = (submitted, ctrls)
            cam.queue_request(req)
            submitted += 1

        cam.start()
        print("  [+] Stream started. Priming request pipeline...")
        for req in requests:
            queue_req(req)

        selector = selectors.DefaultSelector()
        selector.register(cm.event_fd, selectors.EVENT_READ)

        # ROI settings for 1920x1080 Viewfinder (central 480x360 window)
        w, h, stride = stream_cfg.size.width, stream_cfg.size.height, stream_cfg.stride
        roi_w, roi_h = 480, 360
        cx, cy = w // 2, h // 2

        results_by_phase = {
            "phase1_static": [],
            "phase2_vibe_ois_off": [],
            "phase3_vibe_ois_on": [],
            "phase4_recovery": [],
        }

        ois_hw_states = {}
        deadline = time.monotonic() + 25.0
        processed_count = 0

        haptic_state = False

        print("\n  [*] Starting streaming multi-phase comparative capture...")
        while processed_count < total_requests:
            assert time.monotonic() < deadline, \
                f"Capture timed out ({processed_count}/{total_requests} frames)"

            if not selector.select(1):
                continue

            for req in cm.get_ready_requests():
                if req not in submitted_info:
                    continue

                fb = next(iter(req.buffers.values()))
                if fb.metadata.status != lc.FrameMetadata.Status.Success:
                    if submitted < total_requests:
                        req.reuse()
                        queue_req(req)
                    continue

                req_idx, req_ctrls = submitted_info[req]
                ois_mode_md = req.metadata.get(DRAFT.OpticalImageStabilizationMode)
                expected_ois = req_ctrls[DRAFT.OpticalImageStabilizationMode]
                assert ois_mode_md == expected_ois, \
                    f"Req {req_idx}: metadata OIS mode {ois_mode_md} != expected {expected_ois}"

                # Update physical vibrator state based on phase transition
                if req_idx >= 30 and req_idx < 110:
                    if not haptic_state:
                        haptic.start()
                        haptic_state = True
                        print("  [>>>] Haptic micro-vibration ENGAGED")
                else:
                    if haptic_state:
                        haptic.stop()
                        haptic_state = False
                        print("  [<<<] Haptic micro-vibration DISENGAGED")

                # Sample OIS hardware registers once inside Phase 2 and Phase 3
                if req_idx == 45 and "phase2" not in ois_hw_states:
                    ois_hw_states["phase2"] = read_ois_registers()
                    print(f"  [HW] Phase 2 (OIS Off) LC898124EP1: {ois_hw_states['phase2']}")
                elif req_idx == 85 and "phase3" not in ois_hw_states:
                    ois_hw_states["phase3"] = read_ois_registers()
                    print(f"  [HW] Phase 3 (OIS On) LC898124EP1: {ois_hw_states['phase3']}")

                # Read frame pixel data via DMA-BUF mmap
                plane = fb.planes[0]
                plane_fd = plane.fd
                fcntl.ioctl(plane_fd, DMA_BUF_IOCTL_SYNC, struct.pack("Q", DMA_BUF_SYNC_READ))
                try:
                    mm = mmap.mmap(plane_fd, plane.offset + plane.length,
                                   flags=mmap.MAP_SHARED, prot=mmap.PROT_READ)
                    try:
                        plane_view = memoryview(mm)[plane.offset: plane.offset + plane.length]
                        try:
                            # Extract central ROI and compute metrics
                            roi = extract_roi_y(plane_view, stride, cx, cy, roi_w, roi_h)
                            sharpness = compute_laplacian_variance(roi, roi_w, roi_h)
                            proj_x, proj_y = compute_gray_projections(roi, roi_w, roi_h)
                        finally:
                            plane_view.release()
                    finally:
                        mm.close()
                finally:
                    fcntl.ioctl(plane_fd, DMA_BUF_IOCTL_SYNC,
                                struct.pack("Q", DMA_BUF_SYNC_READ | DMA_BUF_SYNC_END))

                frame_record = {
                    "req_idx": req_idx,
                    "seq": fb.metadata.sequence,
                    "ois_mode": ois_mode_md,
                    "sharpness": sharpness,
                    "proj_x": proj_x,
                    "proj_y": proj_y,
                }

                if req_idx in phase_ranges["phase1_static"]:
                    results_by_phase["phase1_static"].append(frame_record)
                elif req_idx in phase_ranges["phase2_vibe_ois_off"]:
                    results_by_phase["phase2_vibe_ois_off"].append(frame_record)
                elif req_idx in phase_ranges["phase3_vibe_ois_on"]:
                    results_by_phase["phase3_vibe_ois_on"].append(frame_record)
                elif req_idx in phase_ranges["phase4_recovery"]:
                    results_by_phase["phase4_recovery"].append(frame_record)

                processed_count += 1
                if submitted < total_requests:
                    req.reuse()
                    queue_req(req)

        # Finished capture
        haptic.stop()
        haptic_state = False

    finally:
        if haptic is not None:
            try:
                haptic.close()
            except Exception:
                pass
        if selector is not None and cm is not None:
            try:
                selector.unregister(cm.event_fd)
            except Exception:
                pass
            try:
                selector.close()
            except Exception:
                pass
        if cam is not None:
            try:
                cam.stop()
            except Exception:
                pass
            if cm is not None:
                try:
                    while cm.get_ready_requests():
                        pass
                except Exception:
                    pass
            try:
                cam.release()
            except Exception:
                pass
        if use_torch:
            for flash_path in ["/sys/class/leds/white:flash/brightness",
                               "/sys/class/leds/yellow:flash/brightness"]:
                try:
                    if os.path.exists(flash_path):
                        with open(flash_path, "w") as f:
                            f.write("0\n")
                except Exception:
                    pass
        if wireplumber_stopped:
            print("  [*] Restoring desktop wireplumber...")
            subprocess.run(["systemctl", "--user", "-M", "alphaplus@", "start", "wireplumber"])

    print("\n====================================================================")
    print("Quantitative Image Analysis & Results")
    print("====================================================================")

    metrics = {}
    for phase_name, frames in results_by_phase.items():
        metrics[phase_name] = compute_phase_metrics(frames)

    m1 = metrics["phase1_static"]
    m2 = metrics["phase2_vibe_ois_off"]
    m3 = metrics["phase3_vibe_ois_on"]
    m4 = metrics["phase4_recovery"]

    print(f"Phase 1 (Static Baseline, OIS Off, N={m1['frame_count']}):")
    print(f"  Sharpness: mean={m1['mean_sharpness']:.2f} (min={m1['min_sharpness']:.2f}, max={m1['max_sharpness']:.2f})")
    print(f"  Displacement Jitter: RMS={m1['rms_jitter']:.4f} px (std_x={m1['std_dx']:.4f}, std_y={m1['std_dy']:.4f})")

    print(f"\nPhase 2 (Vibration, OIS OFF, N={m2['frame_count']}):")
    print(f"  Sharpness: mean={m2['mean_sharpness']:.2f} (min={m2['min_sharpness']:.2f}, max={m2['max_sharpness']:.2f})")
    print(f"  Displacement Jitter: RMS={m2['rms_jitter']:.4f} px (std_x={m2['std_dx']:.4f}, std_y={m2['std_dy']:.4f})")
    print(f"  Peak-to-Peak Span: dx_span={m2['span_dx']:.4f} px, dy_span={m2['span_dy']:.4f} px")

    print(f"\nPhase 3 (Vibration, OIS ON, N={m3['frame_count']}):")
    print(f"  Sharpness: mean={m3['mean_sharpness']:.2f} (min={m3['min_sharpness']:.2f}, max={m3['max_sharpness']:.2f})")
    print(f"  Displacement Jitter: RMS={m3['rms_jitter']:.4f} px (std_x={m3['std_dx']:.4f}, std_y={m3['std_dy']:.4f})")
    print(f"  Peak-to-Peak Span: dx_span={m3['span_dx']:.4f} px, dy_span={m3['span_dy']:.4f} px")

    print(f"\nPhase 4 (Post Recovery, OIS Off, N={m4['frame_count']}):")
    print(f"  Sharpness: mean={m4['mean_sharpness']:.2f} (min={m4['min_sharpness']:.2f}, max={m4['max_sharpness']:.2f})")
    print(f"  Displacement Jitter: RMS={m4['rms_jitter']:.4f} px (std_x={m4['std_dx']:.4f}, std_y={m4['std_dy']:.4f})")

    # Key Efficacy Ratios
    # 1. Jitter Suppression Ratio: RMS(OIS Off) / RMS(OIS On)
    suppression_ratio = m2["rms_jitter"] / max(m3["rms_jitter"], 1e-6)
    # 2. Sharpness Retention / Boost Ratio: MeanSharpness(OIS On) / MeanSharpness(OIS Off)
    sharpness_ratio = m3["mean_sharpness"] / max(m2["mean_sharpness"], 1e-6)
    # 3. Vibration vs Static Sharpness degradation
    blur_degradation_off = (m1["mean_sharpness"] - m2["mean_sharpness"]) / max(m1["mean_sharpness"], 1e-6) * 100.0
    blur_degradation_on = (m1["mean_sharpness"] - m3["mean_sharpness"]) / max(m1["mean_sharpness"], 1e-6) * 100.0

    print("\n--------------------------------------------------------------------")
    print("OIS Optical Stabilization Evaluation Summary:")
    print(f"  1. Inter-frame Motion Jitter Suppression: {suppression_ratio:.2f}x reduction")
    print(f"     (OIS Off RMS: {m2['rms_jitter']:.4f} px -> OIS On RMS: {m3['rms_jitter']:.4f} px)")
    print(f"  2. Sharpness Retention under Vibration:   {sharpness_ratio:.2f}x retention")
    print(f"     (OIS Off: {m2['mean_sharpness']:.2f} -> OIS On: {m3['mean_sharpness']:.2f})")
    print(f"  3. Vibration Blur Degradation:           Off={blur_degradation_off:+.1f}%, On={blur_degradation_on:+.1f}%")
    print("--------------------------------------------------------------------")

    print("\nValidating hardware and efficacy assertions...")
    assert suppression_ratio > 1.20, \
        f"OIS jitter suppression ratio {suppression_ratio:.2f}x is below expected threshold 1.20x"
    assert sharpness_ratio >= 0.98, \
        f"OIS sharpness retention {sharpness_ratio:.2f}x is below expected threshold 0.98x"

    # Hardware register assertions
    if "phase2" in ois_hw_states and "phase3" in ois_hw_states:
        hw_off = ois_hw_states["phase2"]
        hw_on = ois_hw_states["phase3"]
        assert hw_off.get("enabled") == 0, f"Hardware register enable not 0 in Phase 2: {hw_off}"
        assert hw_on.get("enabled") == 1, f"Hardware register enable not 1 in Phase 3: {hw_on}"
        assert hw_on.get("status") == 0, f"Hardware register status non-zero: {hw_on}"
        print("  [+] Hardware register state verified: Phase 2 enabled=0, Phase 3 enabled=1, status=0")

    # Check power management
    time.sleep(1.2)
    with open("/proc/sys/kernel/tainted") as f:
        tainted = int(f.read().strip())
    assert tainted == 0, f"Kernel tainted: {tainted}"

    for path in os.listdir("/sys/class/video4linux"):
        sub_name = open(f"/sys/class/video4linux/{path}/name").read().strip()
        if any(dev in sub_name for dev in ["imx363", "lc898124ep1", "lc898219xi"]):
            # Find i2c client
            m = re.search(r" (\d+)-([0-9a-f]+)", sub_name)
            if m:
                bus, addr = m.groups()
                pm_path = f"/sys/bus/i2c/devices/{bus}-{addr}/power/runtime_status"
                if os.path.exists(pm_path):
                    status = open(pm_path).read().strip()
                    assert status == "suspended", f"Device {sub_name} runtime status '{status}' != suspended"

    if args.json:
        summary_data = {
            "test": "imx363_ois_efficacy",
            "config": {
                "torch": use_torch,
                "exposure_us": exp_time,
                "gain": analogue_gain,
                "vibration_magnitude": vibe_magnitude,
            },
            "summary": {
                "jitter_suppression_ratio": suppression_ratio,
                "sharpness_retention_ratio": sharpness_ratio,
                "blur_degradation_off_pct": blur_degradation_off,
                "blur_degradation_on_pct": blur_degradation_on,
            },
            "phases": {
                k: {
                    sub_k: sub_v for sub_k, sub_v in v.items()
                    if sub_k not in ("displacements", "sharpnesses")
                }
                for k, v in metrics.items()
            },
            "hardware_registers": ois_hw_states,
        }
        with open(args.json, "w") as jf:
            json.dump(summary_data, jf, indent=2)
        print(f"  [+] Saved structured test metrics to {args.json}")

    print("  [+] All devices runtime suspended, kernel tainted=0")
    print("\nPASS: IMX363 Optical Image Stabilization (OIS) efficacy hardware validation")
    return 0


if __name__ == "__main__":
    sys.exit(main())
