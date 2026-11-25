#!/usr/bin/env python3
"""Validate native CAMSS mode timing, frame metadata and continuous AF on hardware."""

import argparse
from contextlib import ExitStack, contextmanager
import ctypes
import fcntl
import json
import math
import mmap
import os
from pathlib import Path
import re
import selectors
import statistics
import struct
import subprocess
import time

import libcamera as lc

C = lc.controls
DMA_BUF_IOCTL_SYNC = 0x40086200
DMA_BUF_SYNC_READ = 1
DMA_BUF_SYNC_END = 4


@contextmanager
def mapped_planes(buffer):
    fds = {plane.fd for plane in buffer.planes}
    for fd in fds:
        fcntl.ioctl(fd, DMA_BUF_IOCTL_SYNC, struct.pack('Q', DMA_BUF_SYNC_READ))
    try:
        with ExitStack() as stack:
            planes = []
            for plane in buffer.planes:
                data = stack.enter_context(mmap.mmap(
                    plane.fd, plane.offset + plane.length,
                    flags=mmap.MAP_SHARED, prot=mmap.PROT_READ))
                view = memoryview(data)[plane.offset:plane.offset + plane.length]
                stack.callback(view.release)
                planes.append(view)
            yield planes
    finally:
        for fd in fds:
            fcntl.ioctl(fd, DMA_BUF_IOCTL_SYNC,
                        struct.pack('Q', DMA_BUF_SYNC_READ | DMA_BUF_SYNC_END))


def sample_pixels(buffer, config):
    width, height, stride = config.size.width, config.size.height, config.stride
    with mapped_planes(buffer) as (y, uv):
        mean_y = statistics.mean(y[row * stride + col]
                                 for row in range(0, height, 16)
                                 for col in range(0, width, 16))
        mean_u = statistics.mean(uv[row * stride + col]
                                 for row in range(0, height // 2, 8)
                                 for col in range(0, width, 16))
        mean_v = statistics.mean(uv[row * stride + col + 1]
                                 for row in range(0, height // 2, 8)
                                 for col in range(0, width, 16))
    return {'y': mean_y, 'vu': mean_v - mean_u}


def sample_bars(buffer, config):
    width, height, stride = config.size.width, config.size.height, config.stride
    with mapped_planes(buffer) as (y, uv):
        y_start, uv_start = height // 2 * stride, height // 4 * stride
        positions = [(2 * bar + 1) * width // 16 for bar in range(8)]
        return {'bars': [(y[y_start + x], uv[uv_start + (x & ~1)],
                          uv[uv_start + (x & ~1) + 1]) for x in positions]}


def sample_focus_preview(buffer, config):
    width, height, stride = config.size.width, config.size.height, config.stride
    left, right = width // 4, 3 * width // 4
    top, bottom = height // 4, 3 * height // 4
    values = []
    edges = []
    with mapped_planes(buffer) as (y, _):
        for row in range(top, bottom - 8, 8):
            for col in range(left, right - 8, 8):
                value = y[row * stride + col]
                values.append(value)
                edges.append(abs(value - y[row * stride + col + 8]) +
                             abs(value - y[(row + 8) * stride + col]))
    return {'preview_y': statistics.mean(values),
            'preview_edge': statistics.mean(edges)}


def pattern(sensor, value):
    controls = subprocess.check_output(
        ['v4l2-ctl', '-d', sensor, '--list-ctrls'], text=True)
    if 'test_pattern' not in controls:
        if value:
            raise RuntimeError('Sensor does not support test patterns')
        return
    subprocess.run(['v4l2-ctl', '-d', sensor,
                    '--set-ctrl=test_pattern=' + str(value)], check=True)


def lens_device(camera_id):
    match = re.search(r'\b([0-9]+)-[0-9a-f]{4}\b', camera_id)
    if not match:
        raise RuntimeError(f'camera I2C bus not found in {camera_id}')
    bus = match.group(1)
    out = subprocess.check_output(['media-ctl', '-p'], text=True)
    for line in out.splitlines():
        if (line.startswith('- entity') and 'lc898219xi' in line and
                f' {bus}-' in line):
            name = line.split(': ', 1)[1].split(' (', 1)[0]
            return subprocess.check_output(['media-ctl', '-e', name],
                                           text=True).strip()
    raise RuntimeError('lens entity not found')


def lens_code(sensor):
    actual = subprocess.check_output(
        ['v4l2-ctl', '-d', sensor, '--get-ctrl=focus_absolute'], text=True)
    return int(actual.split(':', 1)[1])


def i2c_register(bus, address, register, address_size, value_size):
    output = subprocess.check_output(
        ['i2ctransfer', '-f', '-y', bus, f'w{address_size}@0x{address}',
         *(str(byte) for byte in register.to_bytes(address_size, 'big')),
         f'r{value_size}'], text=True)
    return int.from_bytes(bytes(int(value, 16) for value in output.split()), 'big')


def lens_hardware_code(camera_id):
    bus = re.search(r'\b([0-9]+)-', camera_id).group(1)
    raw = i2c_register(bus, '72', 0x84, 1, 2) & 0xfff
    return raw - 4096 if raw & 0x800 else raw


def has_autofocus(cam):
    return C.AfMode in cam.controls


def manual_focus_controls(cam, position=None):
    if not has_autofocus(cam):
        return {}
    controls = {C.AfMode: C.AfModeEnum.Manual}
    if position is not None:
        controls[C.LensPosition] = position
    return controls


def continuous_focus_controls(cam):
    if not has_autofocus(cam):
        return {}
    return {C.AfMode: C.AfModeEnum.Continuous}


def capture(cm, cam, size, frames, controls, step=None, sampler=None,
            request_controls=None, frame_duration_sensor=None,
            frame_duration_mode=None, before_start=None, timeout=None):
    cfg = cam.generate_configuration([lc.StreamRole.Viewfinder])
    sc = cfg.at(0)
    sc.size = lc.Size(*size)
    sc.color_space = lc.ColorSpace.Rec2020()
    cfg.validate()
    assert sc.color_space.range == lc.ColorSpace.Range.Full
    assert sc.color_space.ycbcrEncoding == lc.ColorSpace.YcbcrEncoding.Rec601
    assert sc.color_space.primaries == lc.ColorSpace.Primaries.Rec709
    assert sc.color_space.transferFunction == lc.ColorSpace.TransferFunction.Srgb
    cam.configure(cfg)
    expected_frame_duration_us = None
    if frame_duration_sensor and frame_duration_mode:
        output = subprocess.check_output(
            ['v4l2-ctl', '-d', frame_duration_sensor,
             '--get-ctrl=pixel_rate,horizontal_blanking,vertical_blanking'],
            text=True)
        sensor_controls = {key.strip(): int(value)
                           for key, value in (line.split(':', 1)
                                              for line in output.splitlines())}
        sensor_width, sensor_height = frame_duration_mode['sensor_size']
        line_length = sensor_width + sensor_controls['horizontal_blanking']
        frame_length = sensor_height + sensor_controls['vertical_blanking']
        assert sensor_controls['pixel_rate'] == frame_duration_mode['pixel_rate'], \
            (cam.id, size, sensor_controls, frame_duration_mode)
        assert line_length == frame_duration_mode['hts'], \
            (cam.id, size, line_length, frame_duration_mode)
        assert frame_length == frame_duration_mode['vts'], \
            (cam.id, size, frame_length, frame_duration_mode)
        frame_duration_ns = round(line_length * frame_length * 1e9 /
                                  sensor_controls['pixel_rate'])
        expected_frame_duration_us = frame_duration_ns // 1000
    stream = sc.stream
    allocator = lc.FrameBufferAllocator(cam)
    assert allocator.allocate(stream) > 0
    requests = []
    for buffer in allocator.buffers(stream):
        req = cam.create_request()
        req.add_buffer(stream, buffer)
        requests.append(req)
    submitted = 0

    def queue(req):
        nonlocal submitted
        if request_controls:
            for key, value in request_controls(submitted).items():
                req.set_control(key, value)
        cam.queue_request(req)
        submitted += 1

    with ExitStack() as cleanup:
        selector = cleanup.enter_context(selectors.DefaultSelector())
        selector.register(cm.event_fd, selectors.EVENT_READ)
        if before_start:
            before_start()
        cam.start(controls)
        cleanup.callback(cm.get_ready_requests)
        cleanup.callback(cam.stop)
        for req in requests:
            queue(req)
        results = []
        deadline = time.monotonic() + (timeout if timeout is not None else frames / 25 + 8)
        while len(results) < frames:
            assert time.monotonic() < deadline, ('Capture timed out', len(results), frames)
            if not selector.select(1):
                continue
            for req in cm.get_ready_requests():
                fb = req.buffers[stream]
                assert fb.metadata.status == lc.FrameMetadata.Status.Success
                md = req.metadata
                result = {'frame': fb.metadata.sequence,
                          'timestamp': fb.metadata.timestamp,
                          'sensor_timestamp': md.get(C.SensorTimestamp),
                          'frame_duration': md.get(C.FrameDuration),
                          'frame_duration_limits': md.get(C.FrameDurationLimits),
                          'flicker_mode': md.get(C.AeFlickerMode),
                          'flicker_period': md.get(C.AeFlickerPeriod),
                          'flicker_detected': md.get(C.AeFlickerDetected),
                          'lux': md.get(C.Lux),
                          'exposure': md.get(C.ExposureTime),
                          'gain': md.get(C.AnalogueGain),
                          'exposure_mode': md.get(C.ExposureTimeMode),
                          'gain_mode': md.get(C.AnalogueGainMode),
                          'ae': md.get(C.AeEnable),
                          'ev': md.get(C.ExposureValue),
                          'awb': md.get(C.AwbEnable),
                          'awb_mode': md.get(C.AwbMode),
                          'awb_locked': md.get(C.AwbLocked),
                          'scaler_crop': md.get(C.ScalerCrop),
                          'wb': md.get(C.ColourGains),
                          'digital_gain': md.get(C.DigitalGain),
                          'cct': md.get(C.ColourTemperature),
                          'ccm': md.get(C.ColourCorrectionMatrix),
                          'af': md.get(C.AfState),
                          'pause': md.get(C.AfPauseState),
                          'lens': md.get(C.LensPosition),
                          'fom': md.get(C.FocusFoM)}
                if sampler:
                    result.update(sampler(fb, sc))
                results.append(result)
                updates = step(len(results), result) if step else {}
                if len(results) < frames:
                    req.reuse()
                    for key, value in (updates or {}).items():
                        req.set_control(key, value)
                    queue(req)
        rows = results[:frames]
        if expected_frame_duration_us is not None:
            durations = [row['frame_duration'] for row in rows]
            assert all(duration and duration > 0 for duration in durations), (cam.id, size, durations)
            actual = statistics.median(durations)
            assert abs(actual - expected_frame_duration_us) <= max(200, expected_frame_duration_us * 0.01), (
                cam.id, size, actual, expected_frame_duration_us
            )
        return rows


def modes(cm, cam, sensor):
    if 'imx363' in cam.id:
        modes = [((4032, 3024), 8832, 836266667, 3156, 10),
                 ((2016, 1136), 5520, 409600000, 1236, 10)]
    elif 'imx351' in cam.id:
        modes = [((4656, 3492), 6032, 652800000, 3604, 20),
                 ((2016, 1136), 3220, 556800000, 2882, 20)]
    elif 'imx520' in cam.id:
        modes = [((3280, 2460), 3872, 318577778, 2742, 10),
                 ((1632, 924), 3872, 300088889, 2582, 10)]
    else:
        modes = [((4032, 3024), 5072, 483200000, 3172, 4),
                 ((2016, 1136), 6368, 483200000, 1264, 4)]
    for size, hts, pixel_rate, vts, exposure_margin in modes * 2:
        rows = capture(cm, cam, size, 50, {
            C.AeEnable: False, C.ExposureTime: 5000, C.AnalogueGain: 2.0,
            C.AwbEnable: False, C.ColourGains: (1.5, 1.25),
            **manual_focus_controls(cam, 0.0),
        })
        actual = subprocess.check_output(['v4l2-ctl', '-d', sensor,
                                         '--get-ctrl=exposure,pixel_rate'], text=True)
        sensor_controls = {key.strip(): int(value)
                           for key, value in (line.split(':', 1)
                                              for line in actual.splitlines())}
        lines = sensor_controls['exposure']
        deltas = [b['timestamp'] - a['timestamp'] for a, b in zip(rows, rows[1:])]
        frame_interval_ns = statistics.median(deltas)
        reported_interval_ns = hts * vts * 1e9 / sensor_controls['pixel_rate']
        assert abs(reported_interval_ns / frame_interval_ns - 1) < 0.01, \
            ('Pixel rate disagrees with captured frame timing',
             sensor_controls['pixel_rate'], reported_interval_ns, frame_interval_ns)
        assert sensor_controls['pixel_rate'] == pixel_rate
        line_duration_us = hts * 1e6 / pixel_rate
        expected = int(lines * line_duration_us)
        for row in rows[-10:]:
            assert row['exposure'] == expected, (size, row, expected)
            assert row['lens'] == (0.0 if has_autofocus(cam) else None), row
            assert tuple(row['wb']) == (1.5, 1.25), row
        assert abs(expected - 5000) < line_duration_us
        ctrl_info = subprocess.check_output(['v4l2-ctl', '-d', sensor, '--list-ctrls'], text=True)
        exp_max_match = re.search(r'\bexposure\b.*?\bmax=(\d+)', ctrl_info)
        assert exp_max_match, f"Missing exposure max in v4l2-ctl output: {ctrl_info}"
        limit = int(int(exp_max_match.group(1)) * line_duration_us)
        assert cam.controls[C.ExposureTime].max == limit
        fps = 1e9 / frame_interval_ns
        expected_fps = 30 if size[0] in (3280, 4032, 4656) else (
            30 if 'imx520' in cam.id else 60)
        assert abs(fps - expected_fps) < 1
        print(json.dumps({'test': 'mode', 'size': size, 'exposure_us': expected,
                          'lines': lines, 'lens': rows[-1]['lens'],
                          'pixel_rate': sensor_controls['pixel_rate'],
                          'reported_frame_interval_ns': reported_interval_ns,
                          'frame_interval_ns': frame_interval_ns,
                          'exposure_max_us': limit, 'fps': fps}), flush=True)


def frame_duration_modes(cam):
    if 'imx363' in cam.id:
        native = {'size': (2016, 1136), 'sensor_size': (2016, 1136),
                  'hts': 5520, 'pixel_rate': 409600000, 'vts': 1236}
        full = {'size': (4032, 3024), 'sensor_size': (4032, 3024),
                'hts': 8832, 'pixel_rate': 836266667, 'vts': 3156}
    elif 'imx351' in cam.id:
        native = {'size': (2016, 1136), 'sensor_size': (2328, 1312),
                  'hts': 3220, 'pixel_rate': 556800000, 'vts': 1440}
        full = {'size': (4656, 3492), 'sensor_size': (4656, 3492),
                'hts': 6032, 'pixel_rate': 652800000, 'vts': 3604}
    elif 'imx520' in cam.id:
        native = {'size': (1632, 924), 'sensor_size': (1640, 924),
                  'hts': 3872, 'pixel_rate': 300088889, 'vts': 2582}
        full = {'size': (3280, 2460), 'sensor_size': (3280, 2460),
                'hts': 3872, 'pixel_rate': 318577778, 'vts': 2742}
    else:
        native = {'size': (2016, 1136), 'sensor_size': (2016, 1136),
                  'hts': 6368, 'pixel_rate': 483200000, 'vts': 1264}
        full = {'size': (4032, 3024), 'sensor_size': (4032, 3024),
                'hts': 5072, 'pixel_rate': 483200000, 'vts': 3172}
    return [native, full, native]


def frame_duration_audit(cm, cam, sensor):
    audit_modes = frame_duration_modes(cam)
    # Transition from full resolution so repeated native audits reset sensor VBLANK.
    prime_cfg = cam.generate_configuration([lc.StreamRole.Viewfinder])
    prime_cfg.at(0).size = lc.Size(*audit_modes[1]['size'])
    prime_cfg.validate()
    cam.configure(prime_cfg)
    for mode in audit_modes:
        line_duration_us = mode['hts'] * 1e6 / mode['pixel_rate']
        expected_duration_ns = round(mode['hts'] * mode['vts'] *
                                     1e9 / mode['pixel_rate'])
        expected_duration_us = expected_duration_ns // 1000
        controls = {
            C.AeEnable: False, C.ExposureTime: 5000,
            C.AnalogueGain: 2.0, C.AwbEnable: False,
            C.ColourGains: (1.5, 1.25), **manual_focus_controls(cam, 0.0),
        }
        if expected_duration_us < 16000:
            controls[C.FrameDurationLimits] = (expected_duration_us, expected_duration_us)
        rows = capture(
            cm, cam, mode['size'], 12,
            controls,
            request_controls=lambda index: {C.AwbEnable: index >= 4},
            frame_duration_sensor=sensor, frame_duration_mode=mode)
        for index, row in enumerate(rows):
            assert row['ae'] is False, (cam.id, mode, row)
            assert row['awb'] is (index >= 4), (cam.id, mode, index, row)
            assert row['exposure'] is not None and \
                abs(row['exposure'] - 5000) <= line_duration_us + 1, \
                (cam.id, mode, row)
            assert row['gain'] is not None and abs(row['gain'] - 2.0) < 0.05, \
                (cam.id, mode, row)
        measured = statistics.median(row['frame_duration'] for row in rows)
        assert abs(measured - expected_duration_us) <= max(200, expected_duration_us * 0.01), (
            cam.id, mode, measured, expected_duration_us
        )
        print(json.dumps({'test': 'frame_duration', 'camera': cam.id,
                          'output_size': mode['size'],
                          'sensor_mode': mode['sensor_size'],
                          'hts': mode['hts'], 'vts': mode['vts'],
                          'pixel_rate': mode['pixel_rate'],
                          'expected_us': expected_duration_us,
                          'requests': len(rows),
                          'manual_exposure_and_awb': 'passed'}), flush=True)


def extended_modes(cm, cam, sensor, _size):
    """Validate 4K60, 120 fps, and 4:3 binned extended rear sensor modes."""
    specs = {
        'imx363': [((3840, 2160), 16667), ((1920, 1080), 8333), ((1440, 1080), 8333)],
        'imx351': [((3840, 2160), 16667), ((1920, 1080), 8333), ((1440, 1080), 33333)],
        's5k3m5': [((3840, 2160), 33333), ((1440, 1080), 33333), ((1008, 568), 8333)],
    }

    def sample_luma(buffer, config):
        # Stride to ~256 samples so pure-Python inspection keeps up with 4K60.
        width, height, stride = config.size.width, config.size.height, config.stride
        step_y = max(16, height // 16)
        step_x = max(16, width // 16)
        with mapped_planes(buffer) as (y, _uv):
            mean_y = statistics.mean(
                y[row * stride + col]
                for row in range(0, height, step_y)
                for col in range(0, width, step_x)
            )
        return {'y': mean_y}

    model = next(name for name in specs if name in cam.id)
    for out_size, target_us in specs[model]:
        rows = capture(
            cm, cam, out_size, 36,
            {C.AeEnable: True,
             C.FrameDurationLimits: (target_us, target_us),
             **manual_focus_controls(cam, 0.0)},
            sampler=sample_luma,
        )
        segment = rows[6:-2]
        duration = statistics.median(row['frame_duration'] for row in segment)
        cadence = statistics.median(
            (b['sensor_timestamp'] - a['sensor_timestamp']) / 1000
            for a, b in zip(segment, segment[1:]) if b['frame'] == a['frame'] + 1
        )
        y_mean = statistics.median(row['y'] for row in segment)
        assert abs(duration - target_us) <= target_us * 0.04, (cam.id, out_size, duration, target_us)
        assert abs(cadence - target_us) <= target_us * 0.04, (cam.id, out_size, cadence, target_us)
        assert y_mean > 1.0, (cam.id, out_size, y_mean)
        print(json.dumps({'test': 'extended_mode', 'camera': cam.id,
                          'size': out_size, 'target_us': target_us,
                          'frame_duration_us': duration,
                          'sof_cadence_us': cadence,
                          'y_mean': round(y_mean, 2)}), flush=True)


def ae_modes(cm, cam, sensor, size):
    """Keep each manual control fixed while the other dimension is automatic."""
    exposure_mode = C.ExposureTimeModeEnum
    gain_mode = C.AnalogueGainModeEnum
    phases = (
        {C.AeEnable: False, C.ExposureTime: 6000, C.AnalogueGain: 2.0},
        {C.AeEnable: False, C.AnalogueGainMode: gain_mode.Auto, C.ExposureTime: 10000},
        {C.AeEnable: True, C.AnalogueGainMode: gain_mode.Manual, C.AnalogueGain: 2.0},
    )
    phase_frames = 18

    def requests(index):
        phase, offset = divmod(index, phase_frames)
        return phases[phase] if offset == 0 and phase < len(phases) else {}

    rows = capture(cm, cam, size, phase_frames * len(phases), {},
                   request_controls=requests)
    expected = ((exposure_mode.Manual, gain_mode.Manual, 6000, 2.0),
                (exposure_mode.Manual, gain_mode.Auto, 10000, None),
                (exposure_mode.Auto, gain_mode.Manual, None, 2.0))
    for phase, (exposure, gain, time_us, fixed_gain) in enumerate(expected):
        segment = rows[phase * phase_frames + 5:(phase + 1) * phase_frames]
        assert all(row["exposure_mode"] == exposure and row["gain_mode"] == gain
                   for row in segment), segment
        if time_us is not None:
            assert all(abs(row["exposure"] - time_us) < 100 for row in segment), segment
        if fixed_gain is not None:
            assert all(abs(row["gain"] - fixed_gain) < 0.03 for row in segment), segment
    print(f"{cam.id}: independent AEGC modes and AeEnable precedence passed")


def frame_rate_requests(cm, cam, sensor, size):
    """Check measured SOF cadence through live VBLANK changes."""
    assert C.FrameDurationLimits in cam.controls, f"{cam.id}: no frame duration control"
    durations = (33333, 41667, 66667)
    phase_length = 20

    def requests(index):
        duration = durations[min(index // phase_length, len(durations) - 1)]
        return {C.FrameDurationLimits: (duration, duration)}

    rows = capture(cm, cam, size, phase_length * len(durations),
                   {C.AeEnable: False, C.ExposureTime: 5000, C.AnalogueGain: 1.0,
                    **manual_focus_controls(cam, 0.0)}, request_controls=requests)
    for phase, target in enumerate(durations):
        start = phase * phase_length + 4
        segment = rows[start:(phase + 1) * phase_length - 2]
        limits = [row["frame_duration_limits"] for row in segment]
        assert all(len(pair) == 2 and max(abs(value - target) for value in pair) < 100
                   for pair in limits), limits
        duration = statistics.median(row["frame_duration"] for row in segment)
        cadence = statistics.median(
            (b["sensor_timestamp"] - a["sensor_timestamp"]) / 1000
            for a, b in zip(segment, segment[1:]) if b["frame"] == a["frame"] + 1
        )
        assert abs(duration - target) < target * 0.02, (duration, target)
        assert abs(cadence - target) < target * 0.02, (cadence, target)
        print(f"{cam.id}: {target} us requested, SOF period {cadence:.0f} us")

    rows = capture(cm, cam, size, 12, {
        C.AeEnable: False, C.FrameDurationLimits: (33333, 120000),
        C.ExposureTime: 100000, C.AnalogueGain: 1.0,
        **manual_focus_controls(cam, 0.0),
    })
    assert all(abs(row["exposure"] - 100000) < 100 for row in rows), rows
    assert all(98000 <= row["frame_duration"] <= 105000 for row in rows[2:]), rows
    print(f"{cam.id}: 100 ms exposure captured within the requested range")

    def alternating_requests(index):
        duration = durations[index % len(durations)]
        return {C.FrameDurationLimits: (duration, duration),
                C.ExposureTime: int(duration * 0.8)}

    rows = capture(cm, cam, size, 30, {
        C.AeEnable: False, C.AnalogueGain: 1.0,
        **manual_focus_controls(cam, 0.0),
    }, request_controls=alternating_requests)
    for index, row in enumerate(rows[3:], start=3):
        expected = durations[index % len(durations)]
        lower, upper = row["frame_duration_limits"]
        assert abs(lower - expected) < 100 and abs(upper - expected) < 100, row
        assert abs(row["exposure"] - int(expected * 0.8)) < 100, row
        assert abs(row["frame_duration"] - expected) < expected * 0.08, row
    print(f"{cam.id}: exposure and frame duration follow alternating requests")


def flicker_metering(cm, cam, sensor, size):
    """Use a dim scene that requests at least one lighting cycle of exposure."""
    assert C.AeFlickerMode in cam.controls, f"{cam.id}: no anti-flicker control"
    for period in (10000, 8333):
        rows = capture(cm, cam, size, 80, {
            C.AeEnable: True, C.AeFlickerMode: C.AeFlickerModeEnum.Manual,
            C.AeFlickerPeriod: period, **manual_focus_controls(cam, 0.0),
        }, timeout=20)
        assert all(row["flicker_mode"] == C.AeFlickerModeEnum.Manual for row in rows)
        assert all(row["flicker_period"] == period for row in rows)
        aligned = [row["exposure"] for row in rows[20:] if row["exposure"] >= period - 50]
        assert aligned, f"{cam.id}: fixture is too bright for {period} us anti-flicker"
        assert all(abs(exposure - round(exposure / period) * period) < 50
                   for exposure in aligned), (period, aligned)
        print(f"{cam.id}: {len(aligned)} exposures aligned to {period} us")

def colours(cm, cam, sensor, size):
    expected = [(1, 1, 1), (1, 1, 0), (0, 1, 1), (0, 1, 0),
                (1, 0, 1), (1, 0, 0), (0, 0, 1), (0, 0, 0)]
    for run in range(2):
        rows = capture(cm, cam, size, 60, {
            C.AeEnable: False, C.ExposureTime: 5000, C.AnalogueGain: 2.0,
            C.AwbEnable: False, C.ColourGains: (1.0, 1.0),
            C.draft.TestPatternMode: C.draft.TestPatternModeEnum.ColorBars,
            **manual_focus_controls(cam),
        }, sampler=sample_bars)
        for row in rows:
            for (y, u, v), bits in zip(row['bars'], expected):
                rgb = (y + 1.402 * (v - 128),
                       y - 0.344136 * (u - 128) - 0.714136 * (v - 128),
                       y + 1.772 * (u - 128))
                assert all(value > 160 if bit else value < 100
                           for value, bit in zip(rgb, bits)), (row, rgb, bits)
        print(json.dumps({'test': 'colour_bars', 'size': size, 'run': run,
                          'frames_checked': len(rows),
                          'bars_yuv': rows[-1]['bars']}), flush=True)


def white_balance_requests(cm, cam, sensor, size, temperatures=None):
    pattern(sensor, 0 if temperatures else 2)
    gains = [(0.25, 0.25), (0.75, 0.75)]
    sequence = [0, 1, 0, 0, 1, 1, 1, 0, 1, 0, 1, 1, 0, 0, 0, 1]
    sampled = 0

    def label(index):
        return 0 if index < 16 else 1 if index < 32 else sequence[(index - 32) % len(sequence)]

    def sample(buffer, config):
        nonlocal sampled
        width, height, stride = config.size.width, config.size.height, config.stride
        values = []
        with mapped_planes(buffer) as (y, uv):
            for row in (height // 8, height // 2, 7 * height // 8):
                for bar in range(8):
                    col = (2 * bar + 1) * width // 16
                    pos = (row // 2) * stride + (col & ~1)
                    if temperatures:
                        luminance = [y[r * stride + c]
                                     for r in range(row - 8, row + 8, 2)
                                     for c in range(col - 8, col + 8, 2)]
                        chroma = [(uv[(r // 2) * stride + (c & ~1)],
                                   uv[(r // 2) * stride + (c & ~1) + 1])
                                  for r in range(row - 8, row + 8, 2)
                                  for c in range(col - 8, col + 8, 2)]
                        values.extend((statistics.mean(luminance),
                                       statistics.mean(u for u, _ in chroma),
                                       statistics.mean(v for _, v in chroma)))
                    else:
                        values.extend((y[row * stride + col], uv[pos], uv[pos + 1]))
        sampled += 1
        if sampled == 40:
            time.sleep(0.12)
        return {'pattern_yuv': values}

    def request_controls(index):
        state = label(index)
        return ({C.ColourTemperature: temperatures[state]} if temperatures else
                {C.ColourGains: gains[state]})

    rows = capture(cm, cam, size, 80, {
        C.AeEnable: False, C.ExposureTime: 12000 if temperatures else 5000,
        C.AnalogueGain: 4.0 if temperatures else 2.0,
        C.AwbEnable: False, **request_controls(0),
        **manual_focus_controls(cam, 0.0),
    }, sampler=sample, request_controls=request_controls)
    references = [
        [statistics.median(column) for column in zip(*(row['pattern_yuv'] for row in group))]
        for group in (rows[4:16], rows[20:32])
    ]
    separations = []
    for band in range(3):
        a, b = (ref[band * 24:(band + 1) * 24] for ref in references)
        distance = sum((x - y) ** 2 for x, y in zip(a, b))
        assert math.sqrt(distance / 24) >= 2, ('Colour states are not distinguishable', band)
        separations.append(math.sqrt(distance / 24))
    for index, row in enumerate(rows):
        expected = label(index)
        assert row['awb'] is False and row['digital_gain'] == 1.0, row
        if temperatures:
            assert row['cct'] == temperatures[expected], (index, row)
        else:
            assert tuple(row['wb']) == gains[expected], (index, row)
        if index < 4:
            continue
        for band in range(3):
            values = row['pattern_yuv'][band * 24:(band + 1) * 24]
            distances = [sum((x - y) ** 2 for x, y in zip(values, ref[band * 24:(band + 1) * 24]))
                         for ref in references]
            assert distances[expected] < distances[1 - expected] * 0.25, \
                ('Pixel colour differs from the request', index, band, distances, row)
    deltas = [b['timestamp'] - a['timestamp'] for a, b in zip(rows, rows[1:])]
    gaps = sum(b['frame'] - a['frame'] - 1 for a, b in zip(rows, rows[1:]))
    pause_gaps = sum(rows[index]['frame'] - rows[index - 1]['frame'] - 1
                     for index in range(40, 48))
    assert pause_gaps > 0, 'Capture did not exercise queue starvation after the pause'
    fps = 1e9 / statistics.median(deltas)
    reported_fps = 1e6 / statistics.median(row['frame_duration'] for row in rows)
    assert abs(fps / reported_fps - 1) < 0.01, (fps, reported_fps)
    print(json.dumps({'test': 'colour_temperature_pixels' if temperatures else 'awb_requests',
                      'size': size, 'frames': len(rows),
                      'pixel_frames_checked': len(rows) - 4, 'row_bands': 3,
                      'colour_separation_rms': separations, 'median_fps': fps,
                      'discarded_input_frames': gaps, 'pause_frame_gaps': pause_gaps}), flush=True)


def colour_temperature_requests(cm, cam, sensor, size):
    assert C.ColourTemperature in cam.controls, 'Camera does not support manual colour temperature'
    temperatures = [1500, 2300, 2850, 4000, 5000, 6500, 7500, 12000]
    sampled = 0

    def request_controls(index):
        if index < 48:
            cct = temperatures[index] if index < len(temperatures) else 1500 + index * 167
            return {C.AwbEnable: False, C.ColourTemperature: cct}
        if index < 80:
            return {C.AwbEnable: False, C.ColourTemperature: 1500 + index * 97,
                    C.ColourGains: (1.5, 1.25)}
        if index < 112:
            return {C.AwbEnable: False, C.ColourGains: (1 + index / 256, 2 - index / 256)}
        if index < 160:
            return {C.AwbEnable: True, C.ColourTemperature: 12000, C.ColourGains: (0.25, 0.25)}
        return {C.AwbEnable: False, C.ColourTemperature: temperatures[index % len(temperatures)]}

    def sample(buffer, config):
        nonlocal sampled
        sampled += 1
        if sampled == 96:
            time.sleep(0.12)
        return {}

    fixed_controls = {
        C.AeEnable: False, C.ExposureTime: 5000, C.AnalogueGain: 2.0,
        **manual_focus_controls(cam, 0.0),
    }
    initial_auto, = capture(cm, cam, size, 1, {**fixed_controls, C.AwbEnable: True})
    rows = capture(cm, cam, size, 192, {
        **fixed_controls, **request_controls(0),
    }, sampler=sample, request_controls=request_controls)
    checked = []
    for index, row in enumerate(rows):
        requested = request_controls(index)
        automatic = requested[C.AwbEnable]
        assert row['awb'] is automatic, (index, row)
        assert row['cct'] > 0 and all(math.isfinite(v) and v > 0 for v in row['wb']), row
        cct = requested.get(C.ColourTemperature)
        gains = requested.get(C.ColourGains)
        if not automatic:
            assert row['digital_gain'] == 1.0, row
            if cct is not None:
                assert row['cct'] == cct, (index, cct, row)
            if gains is not None:
                assert list(row['wb']) == [math.floor(v * 128 + 0.5) / 128 for v in gains], (index, row)
        elif index == 112:
            for key in ('cct', 'wb', 'ccm', 'digital_gain'):
                assert row[key] == initial_auto[key], (key, initial_auto, row)
        checked.append({'index': index, 'frame': row['frame'], 'automatic': automatic,
                        'requested_cct': cct, 'requested_gains': gains,
                        'cct': row['cct'], 'gains': list(row['wb']),
                        'ccm': list(row['ccm'])})
    gaps = sum(rows[i]['frame'] - rows[i - 1]['frame'] - 1 for i in range(96, 104))
    assert gaps > 0, 'Capture did not exercise queue starvation after the pause'
    print(json.dumps({'test': 'colour_temperature', 'camera': cam.id, 'size': size,
                      'frames': len(rows), 'pause_frame_gaps': gaps,
                      'metadata': checked}), flush=True)
    white_balance_requests(cm, cam, sensor, size, temperatures=[2300, 7500])


def scaler_crop_requests(cm, cam, sensor, size):
    assert C.ScalerCrop in cam.controls, 'Camera does not support per-request crop'
    assert size[0] * 3 == size[1] * 4, 'The colour-bar fixture requires 4:3 output'
    states = []

    def setup():
        bounds = cam.properties[lc.properties.ScalerCropMaximum]
        width, height = bounds.width // 2, bounds.height // 2
        centre = lc.Rectangle(bounds.x + width // 2, bounds.y + height // 2, width, height)
        corner = lc.Rectangle(bounds.x, bounds.y, width, height)
        states.extend((bounds, centre, corner))

    def state(index):
        return 0 if index < 12 else (index - 12) % len(states)

    def request_controls(index):
        return {C.ScalerCrop: states[state(index)]}

    def sample(buffer, config):
        height, stride = config.size.height, config.stride
        with mapped_planes(buffer) as (luma, _):
            rows = [bytes(luma[y * stride:y * stride + config.size.width])
                    for y in range(height // 4, 3 * height // 4, 12)]
            return {'bars': [statistics.mean(values) for values in zip(*rows)]}

    rows = capture(cm, cam, size, 60, {
        C.draft.TestPatternMode: C.draft.TestPatternModeEnum.ColorBars,
        C.AeEnable: False, C.ExposureTime: 5000, C.AnalogueGain: 2.0,
        C.AwbEnable: False, C.ColourGains: (1.0, 1.0),
        **manual_focus_controls(cam, 0.0),
    }, sampler=sample, request_controls=request_controls, before_start=setup)
    control = subprocess.check_output(
        ['v4l2-ctl', '-d', sensor, '--get-ctrl=test_pattern'], text=True)
    assert int(control.split(':', 1)[1].split()[0]) == 0, control

    full = rows[6]['scaler_crop']
    reference = rows[6]['bars']
    assert max(reference) - min(reference) >= 50, reference
    columns = len(reference)
    maximum_error = 0.0
    for index, row in enumerate(rows[4:], start=4):
        applied = row['scaler_crop']
        expected = states[state(index)]
        assert abs(applied.width - expected.width) <= 16 and abs(applied.x - expected.x) <= 16, \
            (index, expected, applied)
        assert abs(applied.height - expected.height) <= 16 and abs(applied.y - expected.y) <= 16, \
            (index, expected, applied)
        predicted = [reference[max(0, min(columns - 1, int(
            (applied.x - full.x + (column + 0.5) * applied.width / columns)
            * columns / full.width)))] for column in range(columns)]
        rms = math.sqrt(statistics.mean((observed - target) ** 2
                                       for observed, target in zip(row['bars'], predicted)))
        assert rms < 8, (index, expected, applied, rms)
        maximum_error = max(maximum_error, rms)
    print(json.dumps({'test': 'scaler_crop_requests', 'camera': cam.id, 'size': size,
                      'pixel_frames_checked': len(rows) - 4, 'maximum_luma_rms': maximum_error}),
          flush=True)


def sensor_requests(cm, cam, sensor, size):
    pattern(sensor, 0)
    sequence = [0, 1, 0, 0, 1, 1, 1, 0, 1, 0, 1, 1, 0, 0, 0, 1]

    def label(index):
        return 0 if index < 16 else 1 if index < 32 else sequence[(index - 32) % len(sequence)]

    for name, states in [('exposure', [(2000, 4.0), (12000, 4.0)]),
                         ('gain', [(5000, 1.0), (5000, 8.0)])]:
        sampled = 0

        def sample(buffer, config):
            nonlocal sampled
            width, height, stride = config.size.width, config.size.height, config.stride
            with mapped_planes(buffer) as (y, _):
                bands = [statistics.mean(y[row * stride + col]
                                         for row in range(centre - 16, centre + 16, 4)
                                         for col in range(width // 8, 7 * width // 8, 16))
                         for centre in (height // 8, height // 2, 7 * height // 8)]
            sampled += 1
            if sampled == 40:
                time.sleep(0.12)
            return {'band_y': bands}

        def request_controls(index):
            exposure, gain = states[label(index)]
            return {C.AeEnable: False, C.ExposureTime: exposure, C.AnalogueGain: gain}

        rows = capture(cm, cam, size, 80, {
            **request_controls(0), C.AwbEnable: False, C.ColourGains: (1.0, 1.0),
            **manual_focus_controls(cam, 0.0),
        }, sampler=sample, request_controls=request_controls)
        references = [[statistics.median(column) for column in
                       zip(*(row['band_y'] for row in group))]
                      for group in (rows[4:16], rows[20:32])]
        separations = [b - a for a, b in zip(*references)]
        assert min(separations) >= 1.0, ('Sensor states are not distinguishable', name, references)
        for index, row in enumerate(rows):
            expected = label(index)
            exposure, gain = states[expected]
            assert row['ae'] is False and row['awb'] is False, row
            assert abs(row['exposure'] - exposure) <= 14 and abs(row['gain'] - gain) < 1e-6, \
                (name, index, states[expected], row)
            if index < 4:
                continue
            for band, value in enumerate(row['band_y']):
                own = abs(value - references[expected][band])
                other = abs(value - references[1 - expected][band])
                assert own * 2 < other, \
                    ('Pixel brightness differs from the request', name, index, band, row, references)
        pause_gaps = sum(rows[index]['frame'] - rows[index - 1]['frame'] - 1
                         for index in range(40, 48))
        assert pause_gaps > 0, 'Capture did not exercise queue starvation after the pause'
        fps = 1e9 / statistics.median(b['timestamp'] - a['timestamp']
                                    for a, b in zip(rows, rows[1:]))
        reported_fps = 1e6 / statistics.median(row['frame_duration'] for row in rows)
        assert abs(fps / reported_fps - 1) < 0.01, (fps, reported_fps)
        print(json.dumps({'test': 'sensor_requests', 'control': name, 'camera': cam.id,
                          'size': size, 'frames': len(rows), 'pixel_frames_checked': len(rows) - 4,
                          'row_bands': 3, 'reference_band_y': references,
                          'brightness_separation': separations, 'median_fps': fps,
                          'pause_frame_gaps': pause_gaps}), flush=True)


def controls(cm, cam, sensor, size):
    focus = has_autofocus(cam)
    maximum = cam.controls[C.LensPosition].max if focus else 0.0
    near = maximum * 0.8
    middle = maximum * 0.4

    def step(n, _):
        if n in (30, 60, 90):
            return {C.ColourGains: (1.0, 0.5) if n in (30, 90) else (0.5, 1.0),
                    **manual_focus_controls(cam, middle if n in (30, 90) else near)}
        return {}

    pattern(sensor, 2)
    try:
        rows = capture(cm, cam, size, 120, {
            C.AeEnable: False, C.ExposureTime: 5000, C.AnalogueGain: 2.0,
            C.AwbEnable: False, C.ColourGains: (0.5, 1.0),
            **manual_focus_controls(cam, 0.0),
        }, step, sampler=sample_pixels)
    finally:
        pattern(sensor, 0)
    groups = {(0.5, 1.0): [], (1.0, 0.5): []}
    for row in rows:
        assert row['awb'] is False, row
        assert row['awb_locked'] is None, row
        assert row['ccm'] is not None and len(row['ccm']) == 9, row
        groups[tuple(row['wb'])].append((row['y'], row['vu']))
    centres = {k: (statistics.median(y for y, _ in v),
                   statistics.median(vu for _, vu in v)) for k, v in groups.items()}
    noise = max(statistics.median(math.dist(v, centres[k]) for v in values)
                for k, values in groups.items())
    separation = math.dist(*centres.values())
    assert separation > 5 * max(noise, 0.05), (centres, noise)
    errors = []
    for gains in groups:
        own = centres[gains]
        other = centres[(gains[1], gains[0])]
        errors.extend(row for row in rows if tuple(row['wb']) == gains and
                      math.dist((row['y'], row['vu']), own) >
                      math.dist((row['y'], row['vu']), other))
    assert not errors, errors
    colour_states = {
        gains: {(row['cct'], tuple(row['ccm'])) for row in rows
                if tuple(row['wb']) == gains}
        for gains in groups
    }
    assert all(len(states) == 1 for states in colour_states.values()), \
        colour_states
    assert colour_states[(0.5, 1.0)] != colour_states[(1.0, 0.5)], \
        colour_states
    if focus:
        assert any(abs(row['lens'] - middle) < .02 for row in rows)
        assert any(abs(row['lens'] - near) < .02 for row in rows)
    print(json.dumps({'test': 'frame_controls', 'frames': len(rows),
                      'pixel_separation': separation, 'pixel_noise': noise,
                      'metadata_mismatches': len(errors),
                      'colour_states': {str(key): list(value)
                                        for key, value in colour_states.items()}}), flush=True)


def gain(cm, cam, sensor, size):
    pattern(sensor, 0)
    maximum_gain = 8.0 if 'imx363' in cam.id else 16.0
    assert cam.controls[C.AnalogueGain].min == 1.0
    assert cam.controls[C.AnalogueGain].max == maximum_gain

    def quantized(requested):
        if 'imx363' in cam.id:
            code = int(512 - 512 / requested)
            return code, 512 / (512 - code)
        if any(s in cam.id for s in ('imx351', 'imx520')):
            code = round(1024 - 1024 / requested)
            return code, 1024 / (1024 - code)
        code = int(requested * 32)
        return code, code / 32

    for requested in (1.0, 1.02, 1.03125, 1.5, 1.99, 2.5,
                      maximum_gain - 0.03125, maximum_gain):
        rows = capture(cm, cam, size, 24, {
            C.AeEnable: False, C.ExposureTime: 3000, C.AnalogueGain: requested,
            C.AwbEnable: False, **manual_focus_controls(cam),
        })
        actual = subprocess.check_output(['v4l2-ctl', '-d', sensor,
                                         '--get-ctrl=analogue_gain'], text=True)
        code = int(actual.split(':')[1])
        expected_code, expected_gain = quantized(requested)
        assert code == expected_code, (requested, code, expected_code)
        assert all(abs(row['gain'] - expected_gain) < 1e-6
                   for row in rows[-10:]), rows[-10:]
        print(json.dumps({'test': 'gain_code', 'requested': requested,
                          'code': code, 'metadata_gain': rows[-1]['gain']}), flush=True)

    high_requested = 6.5 if 'imx363' in cam.id else 8.5

    def step(n, _):
        if n in (30, 60, 90):
            return {C.AnalogueGain: high_requested if n in (30, 90) else 2.5}
        return {}

    rows = capture(cm, cam, size, 120, {
        C.AeEnable: False, C.ExposureTime: 5000, C.AnalogueGain: 2.5,
        C.AwbEnable: False, C.ColourGains: (1.0, 1.0),
        **manual_focus_controls(cam),
    }, step, sampler=sample_pixels)[10:]
    low_gain = quantized(2.5)[1]
    high_gain = quantized(high_requested)[1]
    groups = {
        low_gain: [row['y'] for row in rows
                   if abs(row['gain'] - low_gain) < 1e-6],
        high_gain: [row['y'] for row in rows
                    if abs(row['gain'] - high_gain) < 1e-6],
    }
    assert sum(map(len, groups.values())) == len(rows), rows
    low, high = (statistics.median(groups[g]) for g in (low_gain, high_gain))
    # Use a relative response check so dark scenes do not fail on absolute Y levels.
    gain_ratio = high / max(low, 0.05)
    assert high > low + 0.1 and gain_ratio > 1.5, (low, high, gain_ratio)
    errors = [row for row in rows if
              (row['y'] > (low + high) / 2) !=
              (abs(row['gain'] - high_gain) < 1e-6)]
    assert not errors, errors
    print(json.dumps({'test': 'gain_pixels', 'frames_checked': len(rows),
                      'low_y': low, 'high_y': high, 'gain_ratio': gain_ratio,
                      'metadata_mismatches': len(errors)}), flush=True)


def autofocus(cm, cam, sensor, size):
    pattern(sensor, 1)
    transitions = []

    def step(n, row):
        if not transitions or row['af'] != transitions[-1]['af']:
            transitions.append({'frame': n, 'af': row['af'], 'fom': row['fom']})
        if n == 300:
            pattern(sensor, 0)
        return {}

    rows = capture(cm, cam, size, 720, {
        C.AeEnable: False, C.ExposureTime: 10000, C.AnalogueGain: 8.0,
        C.AwbEnable: False, C.AfMode: C.AfModeEnum.Continuous,
    }, step)
    assert any(row['af'] == 3 for row in rows[100:300]), transitions
    assert any(row['af'] == 1 for row in rows[310:]), transitions
    assert any(row['af'] == 2 for row in rows[310:]), transitions
    print(json.dumps({'test': 'af_recovery', 'transitions': transitions}), flush=True)


def af_scene(cm, cam, sensor, size):
    pattern(sensor, 1)

    def step(n, _):
        if n == 300:
            pattern(sensor, 0)
        return {}

    rows = capture(cm, cam, size, 360, {
        C.AeEnable: False, C.ExposureTime: 10000, C.AnalogueGain: 8.0,
        C.AwbEnable: False, C.AfMode: C.AfModeEnum.Continuous,
    }, step)
    assert any(row['af'] == 3 for row in rows[100:300])

    changes = []
    for index in range(1, len(rows)):
        previous, current = rows[index - 1]['fom'], rows[index]['fom']
        maximum = max(previous, current)
        changes.append((index, abs(current - previous) / maximum
                        if maximum else None))
    jump = next(index for index, change in changes
                if index >= 300 and change is not None and change > 0.5)
    stable_count = 0
    stable = None
    for index, change in changes:
        if index <= jump:
            continue
        if change is not None and change < 0.1:
            stable_count += 1
            if stable_count == 4:
                stable = index
                break
        else:
            stable_count = 0
    assert stable is not None
    scanning = next(index for index in range(jump, len(rows))
                    if rows[index]['af'] == 1)
    assert stable <= scanning <= stable + 3, (jump, stable, scanning)
    print(json.dumps({'test': 'af_scene_stability',
                      'jump_capture': jump + 1,
                      'stable_capture': stable + 1,
                      'scanning_capture': scanning + 1,
                      'stable_frames': 4}), flush=True)


def exposure(cm, cam, sensor, size):
    pattern(sensor, 0)

    def step(n, _):
        if n in (30, 60, 90):
            return {C.ExposureTime: 12000 if n in (30, 90) else 3000}
        return {}

    rows = capture(cm, cam, size, 120, {
        C.AeEnable: False, C.ExposureTime: 3000, C.AnalogueGain: 8.0,
        C.AwbEnable: False, C.ColourGains: (1.0, 1.0),
        **manual_focus_controls(cam),
    }, step, sampler=sample_pixels)[10:]
    groups = {}
    for row in rows:
        groups.setdefault(row['exposure'], []).append(row['y'])
    assert len(groups) == 2, groups.keys()
    values = sorted(groups)
    low = statistics.median(groups[values[0]])
    high = statistics.median(groups[values[1]])
    exposure_ratio = high / max(low, 0.05)
    assert high > low + 0.1 and exposure_ratio > 2.0, \
        (low, high, exposure_ratio)
    errors = [row for row in rows if
              (abs(row['y'] - high) < abs(row['y'] - low)) !=
              (row['exposure'] == values[1])]
    assert not errors, errors
    print(json.dumps({'test': 'sensor_frame_controls', 'frames': len(rows),
                      'y_medians': [low, high], 'pixel_ratio': exposure_ratio,
                      'metadata_mismatches': len(errors)}), flush=True)


def auto_exposure(cm, cam, sensor, size):
    pattern(sensor, 0)
    initial = capture(cm, cam, size, 12, {
        C.AeEnable: False, C.ExposureTime: 1000, C.AnalogueGain: 1.0,
        C.AwbEnable: False, C.ColourGains: (1.0, 1.0),
        **manual_focus_controls(cam),
    }, sampler=sample_pixels)[-5:]
    rows = capture(cm, cam, size, 240, {
        C.AeEnable: True, C.AwbEnable: False, C.ColourGains: (1.0, 1.0),
        **manual_focus_controls(cam),
    }, sampler=sample_pixels)
    settled = rows[-30:]
    initial_total = statistics.median(row['exposure'] * row['gain']
                                      for row in initial)
    settled_totals = [row['exposure'] * row['gain'] for row in settled]
    settled_total = statistics.median(settled_totals)
    initial_y = statistics.median(row['y'] for row in initial)
    settled_y = statistics.median(row['y'] for row in settled)
    assert max(initial_total, settled_total) / min(initial_total, settled_total) > 1.5, \
        (initial_total, settled_total)
    assert max(settled_totals) / min(settled_totals) < 1.1, settled_totals
    pixel_ratio = settled_y / max(initial_y, 0.05)
    assert settled_y > initial_y + 0.1 and pixel_ratio > 1.5, \
        (initial_y, settled_y, pixel_ratio)
    print(json.dumps({'test': 'auto_exposure',
                      'initial_total_us': initial_total,
                      'settled_total_us': settled_total,
                      'settled_exposure_us': settled[-1]['exposure'],
                      'settled_gain': settled[-1]['gain'],
                      'initial_y': initial_y, 'settled_y': settled_y,
                      'pixel_ratio': pixel_ratio,
                      'settled_span': max(settled_totals) / min(settled_totals)}),
          flush=True)


def exposure_value(cm, cam, sensor, size):
    pattern(sensor, 0)
    info = cam.controls[C.ExposureValue]
    assert (info.min, info.max, info.default) == (-2.0, 2.0, 0.0), info
    controls = {C.AwbEnable: False, C.ColourGains: (1.0, 1.0),
                **manual_focus_controls(cam)}
    capture(cm, cam, size, 12, {
        **controls, C.AeEnable: False, C.ExposureTime: 1000, C.AnalogueGain: 1.0,
    })
    phases = [(0.0, 0.0), (-1.0, -1.0), (-2.0, -2.0),
              (1.0, 1.0), (2.0, 2.0), (0.0, 0.0), (0.09, 1 / 6)]
    frames = 120

    def step(count, row):
        if count % frames == 0 and count // frames < len(phases):
            return {C.ExposureValue: phases[count // frames][0]}
        return {}

    rows = capture(cm, cam, size, frames * len(phases), {
        **controls, C.AeEnable: True, C.ExposureValue: phases[0][0],
    }, step, sampler=sample_pixels)
    samples = []
    for index, (requested, expected) in enumerate(phases):
        section = rows[(index + 1) * frames - 16:(index + 1) * frames]
        assert all(row['ae'] is True and abs(row['ev'] - expected) < 1e-6
                   for row in section), (requested, section)
        totals = [row['exposure'] * row['gain'] for row in section]
        span = max(totals) / min(totals)
        assert span < 1.1, (requested, span)
        samples.append({'requested_ev': requested, 'actual_ev': section[-1]['ev'],
                        'total_us': statistics.median(totals),
                        'exposure_us': section[-1]['exposure'],
                        'gain': section[-1]['gain'], 'span': span,
                        'y': statistics.median(row['y'] for row in section)})
    ordered = sorted(samples[:5], key=lambda sample: sample['actual_ev'])
    totals = [sample['total_us'] for sample in ordered]
    assert all(a <= b * 1.1 for a, b in zip(totals, totals[1:])), samples
    assert totals[-1] / totals[0] > 1.5, \
        ('Scene needs enough light for EV exposure adjustment', samples)
    assert ordered[-1]['y'] > ordered[0]['y'] + 0.1, samples
    repeat = samples[0]['total_us'] / samples[5]['total_us']
    assert 0.9 < repeat < 1.1, samples

    exposure, gain = rows[-1]['exposure'], rows[-1]['gain']

    def manual_step(count, row):
        return {C.ExposureValue: 2.0} if count == 32 else {}

    manual = capture(cm, cam, size, 64, {
        **controls, C.AeEnable: False, C.ExposureTime: exposure,
        C.AnalogueGain: gain, C.ExposureValue: -2.0,
    }, manual_step)
    assert all(row['ae'] is False and row['ev'] == 0.0
               for row in manual), manual
    assert len({(row['exposure'], row['gain']) for row in manual[8:]}) == 1, manual
    assert abs(manual[-1]['exposure'] - exposure) <= 1 and \
        abs(manual[-1]['gain'] - gain) < 0.01, (manual[-1], exposure, gain)

    resumed = capture(cm, cam, size, 120, {**controls, C.AeEnable: True})
    assert all(row['ae'] is True and abs(row['ev'] - 1 / 6) < 1e-6
               for row in resumed), resumed
    reset = capture(cm, cam, size, 120, {
        **controls, C.AeEnable: True, C.ExposureValue: 0.0,
    })
    assert all(row['ae'] is True and row['ev'] == 0.0 for row in reset), reset
    print(json.dumps({'test': 'exposure_value', 'samples': samples,
                      'manual_frames': len(manual),
                      'resumed_ev': resumed[-1]['ev'],
                      'reset_ev': reset[-1]['ev']}), flush=True)


def auto_exposure_limit(cm, cam, sensor, size):
    pattern(sensor, 0)
    controls = {C.AwbEnable: False, C.ColourGains: (1.0, 1.0),
                **manual_focus_controls(cam)}
    capture(cm, cam, size, 12, {
        **controls, C.AeEnable: False, C.ExposureTime: 1000, C.AnalogueGain: 1.0,
    })
    maximum_exposure = cam.controls[C.ExposureTime].max
    maximum_gain = cam.controls[C.AnalogueGain].max
    rows = capture(cm, cam, size, 240, {**controls, C.AeEnable: True},
                   sampler=sample_pixels)
    assert all(row['exposure'] <= maximum_exposure and
               row['gain'] <= maximum_gain for row in rows), rows
    assert all(row['exposure'] == maximum_exposure and
               row['gain'] == maximum_gain for row in rows[-30:]), \
        ('Scene must be dark enough to reach both exposure limits', rows[-30:])
    print(json.dumps({'test': 'auto_exposure_limit', 'frames': len(rows),
                      'maximum_exposure_us': maximum_exposure,
                      'maximum_gain': maximum_gain,
                      'settled_y': statistics.median(row['y'] for row in rows[-30:])}),
          flush=True)


def auto_white_balance(cm, cam, sensor, size):
    pattern(sensor, 0)
    settled_runs = []
    frames = 1200 if 'imx363' in cam.id else 240
    exposure_controls = ({C.AeEnable: False, C.ExposureTime: 16000,
                          C.AnalogueGain: 8.0}
                         if 'imx363' in cam.id else {C.AeEnable: True})
    for initial_gains in ((0.5, 4.0), (4.0, 0.5)):
        capture(cm, cam, size, 12, {
            **exposure_controls, C.AwbEnable: False,
            C.ColourGains: initial_gains,
            **manual_focus_controls(cam),
        })
        rows = capture(cm, cam, size, frames, {
            **exposure_controls, C.AwbEnable: True,
            **manual_focus_controls(cam),
        }, sampler=sample_pixels)
        settled = rows[-60:]
        assert all(row['awb'] is True for row in settled), settled
        assert rows[0]['awb_locked'] is False, rows[0]
        assert all(isinstance(row['awb_locked'], bool) for row in rows), rows[-1]
        assert all(row['ccm'] is not None and len(row['ccm']) == 9
                   for row in settled), settled
        gains = tuple(statistics.median(row['wb'][channel]
                                        for row in settled)
                      for channel in range(2))
        cct = statistics.median(row['cct'] for row in settled)
        spans = tuple((max(row['wb'][channel] for row in settled) -
                       min(row['wb'][channel] for row in settled)) / gains[channel]
                      for channel in range(2))
        assert math.dist(gains, initial_gains) > 0.25, \
            (initial_gains, gains)
        assert all(0.25 <= gain <= 8.0 for gain in gains), gains
        minimum_cct, maximum_cct = (1500, 12000) if 'imx520' in cam.id else (2856, 7500)
        assert minimum_cct <= cct <= maximum_cct, cct
        assert max(spans) < 0.1, (gains, spans)
        settled_runs.append({'initial': initial_gains, 'gains': gains,
                             'cct': cct, 'spans': spans,
                             'locked_frames': sum(row['awb_locked'] for row in settled),
                             'ccm': tuple(settled[-1]['ccm']),
                             'y': statistics.median(row['y']
                                                    for row in settled),
                             'vu': statistics.median(row['vu']
                                                     for row in settled)})

    convergence = math.dist(settled_runs[0]['gains'],
                            settled_runs[1]['gains'])
    assert convergence < 0.1, settled_runs
    assert abs(settled_runs[0]['cct'] - settled_runs[1]['cct']) < 100, \
        settled_runs
    print(json.dumps({'test': 'auto_white_balance',
                      'runs': settled_runs,
                      'gain_convergence': convergence}), flush=True)


def native_white_balance(cm, cam, sensor, size):
    assert 'imx520' in cam.id, 'Native QTI AWB validation requires IMX520'
    pattern(sensor, 0)
    controls = {C.AeEnable: False, C.ExposureTime: 10000, C.AnalogueGain: 4.0,
                C.AwbEnable: True, **manual_focus_controls(cam, 0.0)}

    def switch(count, row):
        if count == 32:
            return {C.AwbEnable: False, C.ColourGains: (1.25, 2.0)}
        if count == 64:
            return {C.AwbEnable: True}
        if count == 160:
            return {C.AeEnable: True}
        return {}

    rows = capture(cm, cam, size, 240, controls, switch)
    manual = rows[40:60]
    manual_cct = manual[0]['cct']
    assert 1500 <= manual_cct <= 12000, manual
    assert all(row['awb'] is False and row['digital_gain'] == 1.0 and row['cct'] == manual_cct and
               row['awb_locked'] is None and
               tuple(row['wb']) == (1.25, 2.0) for row in manual), manual
    assert all(row['ae'] is False for row in rows[8:150]), rows[8:150]
    assert all(row['ae'] is True for row in rows[170:]), rows[170:]
    assert all(row['awb'] is True for row in rows[72:]), rows[72:]
    assert all(isinstance(row['awb_locked'], bool) for row in rows[72:]), rows[-1]
    for row in rows:
        assert row['digital_gain'] >= 1 and math.isfinite(row['digital_gain']), row
        assert all(g > 0 and math.isfinite(g) for g in row['wb']), row
        assert 1500 <= row['cct'] <= 12000 and len(row['ccm']) == 9, row

    initial = []
    for _ in range(2):
        replay = capture(cm, cam, size, 64, controls)
        first = replay[0]
        assert first['digital_gain'] == 1.0 and first['cct'] == 5000, first
        initial.append({'gains': tuple(first['wb']), 'digital_gain': first['digital_gain'],
                        'cct': first['cct']})
    capture(cm, cam, size, 24, {**controls, C.AwbEnable: False,
                                   C.ColourGains: (1.25, 2.0)})
    resumed = capture(cm, cam, size, 64, controls)
    first = resumed[0]
    assert first['digital_gain'] == 1.0 and first['cct'] == 5000, first
    initial.append({'gains': tuple(first['wb']), 'digital_gain': first['digital_gain'],
                    'cct': first['cct']})
    assert all(value == initial[0] for value in initial), initial
    print(json.dumps({'test': 'native_white_balance', 'frames': len(rows),
                      'manual_frames': len(manual), 'manual_cct': manual_cct, 'manual_exposure_frames': 160,
                      'automatic_exposure_frames': 80, 'restart_frames': 192,
                      'manual_stream_frames': 24,
                      'initial': initial, 'final_gains': tuple(rows[-1]['wb']),
                      'final_digital_gain': rows[-1]['digital_gain'],
                      'final_cct': rows[-1]['cct']}), flush=True)


def three_a(cm, cam, sensor, size):
    pattern(sensor, 0)
    capture(cm, cam, size, 12, {
        C.AeEnable: False, C.ExposureTime: 1000, C.AnalogueGain: 1.0,
        C.AwbEnable: False, C.ColourGains: (0.5, 4.0),
        **manual_focus_controls(cam, 0.0),
    })
    frames = 1200 if 'imx363' in cam.id else 600
    rows = capture(cm, cam, size, frames, {
        C.AeEnable: True, C.AwbEnable: True,
        **continuous_focus_controls(cam),
    }, sampler=sample_pixels)
    settled = rows[-60:]
    exposure_totals = [row['exposure'] * row['gain'] for row in settled]
    gain_spans = [
        (max(row['wb'][channel] for row in settled) -
         min(row['wb'][channel] for row in settled)) /
        statistics.median(row['wb'][channel] for row in settled)
        for channel in range(2)
    ]
    assert max(exposure_totals) / min(exposure_totals) < 1.1, exposure_totals
    assert max(gain_spans) < 0.1, gain_spans
    assert all(row['awb'] is True and row['ccm'] is not None
               for row in settled), settled
    if has_autofocus(cam):
        assert any(row['af'] == 2 for row in rows), 'AF did not reach Focused'
        assert all(row['af'] == 2 for row in rows[-8:]), rows[-8:]
    else:
        assert C.AfMode not in cam.controls
        assert all(row['af'] is None and row['pause'] is None and
                   row['lens'] is None for row in rows)
    transitions = []
    for index, row in enumerate(rows):
        if not transitions or row['af'] != transitions[-1]['af']:
            transitions.append({'capture': index + 1, 'af': row['af'],
                                'lens': row['lens'], 'fom': row['fom']})
    print(json.dumps({
        'test': 'three_a',
        'fixed_focus': not has_autofocus(cam),
        'frames': len(rows),
        'settled_exposure_us': statistics.median(
            row['exposure'] for row in settled),
        'settled_gain': statistics.median(row['gain'] for row in settled),
        'exposure_span': max(exposure_totals) / min(exposure_totals),
        'settled_wb': [statistics.median(row['wb'][channel]
                                         for row in settled)
                       for channel in range(2)],
        'wb_spans': gain_spans,
        'settled_cct': statistics.median(row['cct'] for row in settled),
        'final_lens': rows[-1]['lens'],
        'final_fom': rows[-1]['fom'],
        'af_transitions': transitions,
    }), flush=True)


def lens_positions(cm, cam, sensor, size):
    pattern(sensor, 0)
    maximum = cam.controls[C.LensPosition].max
    samples = []
    for position in (0.0, maximum * 0.8, maximum, 0.0):
        measured = {}

        def step(n, _):
            if n == 8:
                measured['code'] = lens_hardware_code(cam.id)
            return {}

        rows = capture(cm, cam, size, 16, {
            C.AeEnable: False, C.ExposureTime: 5000, C.AnalogueGain: 1.0,
            C.AwbEnable: False, C.ColourGains: (1.0, 1.0),
            **manual_focus_controls(cam, position),
        }, step)
        assert all(abs(row['lens'] - position) < 0.02 for row in rows[-4:]), rows[-4:]
        samples.append({'requested': position, 'lens': rows[-1]['lens'], **measured})
    assert samples[0]['code'] < samples[1]['code'] < samples[2]['code'], samples
    assert abs(samples[0]['code'] - samples[-1]['code']) <= 2, samples
    print(json.dumps({'test': 'lens_positions', 'sensor': cam.id,
                      'samples': samples}), flush=True)


def camera_power(cm, size):
    cameras = [next(cam for cam in cm.cameras if sensor in cam.id)
               for sensor in ('imx363', 'imx351', 's5k3m5', 'imx520')]
    devices = {}
    camera_devices = {}
    ois_devices = {}
    for cam in cameras:
        bus, address = re.search(r'\b([0-9]+)-([0-9a-f]{4})\b', cam.id).groups()
        sensor = Path(f'/sys/bus/i2c/devices/{bus}-{address}')
        ancillary = [path for path in sensor.parent.glob(f'{bus}-*')
                     if (path / 'name').read_text().strip() in
                     ('lc898219xi', 'lc898124ep1')]
        assert len(ancillary) == (2 if 'imx363' in cam.id else
                                  1 if has_autofocus(cam) else 0), ancillary
        camera_devices[cam.id] = {path.name for path in [sensor, *ancillary]}
        devices.update({path.name: path / 'power/runtime_status'
                        for path in [sensor, *ancillary]})
        for path in ancillary:
            if (path / 'name').read_text().strip() == 'lc898124ep1':
                ois_devices[cam.id] = path

    def check_power(active_camera=None):
        active = camera_devices[active_camera.id] if active_camera else set()
        expected = {name: 'active' if name in active else 'suspended'
                    for name in devices}
        expected_workers = int(active_camera is not None and 'imx363' in active_camera.id)
        deadline = time.monotonic() + 2
        while True:
            states = {name: path.read_text().strip()
                      for name, path in devices.items()}
            workers = 0
            for path in Path('/proc/self/task').glob('*/comm'):
                try:
                    workers += path.read_text().strip() == 'qcom-accel'
                except FileNotFoundError:
                    continue
            if states == expected and workers == expected_workers:
                return {'devices': states, 'accelerometer_workers': workers}
            assert time.monotonic() < deadline, (states, expected, workers, expected_workers)
            time.sleep(0.01)

    idle = check_power()
    runs = []
    for cam in cameras:
        cam.acquire()
        try:
            check_power()
            sensor = subprocess.check_output(['media-ctl', '-e', cam.id],
                                             text=True).strip()
            pattern(sensor, 0)
            lens = lens_device(cam.id) if has_autofocus(cam) else None
            last_position = None
            positions = (0.0, cam.controls[C.LensPosition].max, None) \
                if has_autofocus(cam) else (None, None, None)
            for cycle, position in enumerate(positions, 1):
                measured = {}

                def step(n, _):
                    if n != 8:
                        return {}
                    measured['power'] = check_power(cam)
                    if has_autofocus(cam):
                        measured['lens_code'] = lens_hardware_code(cam.id)
                        assert measured['lens_code'] == lens_code(lens), \
                            measured
                    if cam.id in ois_devices:
                        bus, address = ois_devices[cam.id].name.split('-')
                        measured['ois'] = {
                            'enabled': i2c_register(bus, address, 0xf012, 2, 4),
                            'mode': i2c_register(bus, address, 0xf013, 2, 4),
                            'center': i2c_register(bus, address, 0xf010, 2, 4),
                            'status': i2c_register(bus, address, 0xf100, 2, 4),
                        }
                        assert measured['ois'] == {'enabled': 1, 'mode': 0,
                                                   'center': 3, 'status': 0}, measured
                    return {}

                rows = capture(cm, cam, size, 16, {
                    C.AeEnable: False, C.ExposureTime: 5000, C.AnalogueGain: 1.0,
                    C.AwbEnable: False, C.ColourGains: (1.0, 1.0),
                    **manual_focus_controls(cam, position),
                }, step)
                check_power()
                if has_autofocus(cam):
                    expected = position if position is not None else last_position
                    assert all(abs(row['lens'] - expected) < 0.02
                               for row in rows[-4:]), rows[-4:]
                    # Main-camera gravity calibration is refreshed on each start.
                    if position is None and 'imx363' not in cam.id:
                        assert measured['lens_code'] == runs[-1]['lens_code'], measured
                    last_position = rows[-1]['lens']
                runs.append({'sensor': cam.id, 'cycle': cycle,
                             'frames': len(rows), 'lens': rows[-1]['lens'], **measured})
        finally:
            cam.release()
        check_power()
    print(json.dumps({'test': 'camera_power', 'idle': idle, 'runs': runs,
                      'final_idle': check_power()}), flush=True)


def camera_switch(cm, size):
    order = ('imx363', 'imx351', 's5k3m5', 'imx520', 'imx351')
    captures = []
    for model in order:
        cam = next(c for c in cm.cameras if model in c.id)
        cam.acquire()
        try:
            target_size = (1632, 924) if model == 'imx520' and size == (2016, 1136) else size
            rows = capture(cm, cam, target_size, 16, {
                C.AeEnable: True, C.AwbEnable: True,
                **manual_focus_controls(cam, 0.0),
            })
        finally:
            cam.release()
        assert len(rows) == 16
        assert all(row['exposure'] is not None and row['gain'] is not None
                   and row['wb'] is not None for row in rows)
        captures.append({'sensor': model, 'first_frame': rows[0]['frame'],
                         'last_frame': rows[-1]['frame'],
                         'fixed_focus': not has_autofocus(cam)})
    print(json.dumps({'test': 'camera_switch', 'captures': captures}), flush=True)


def interleaved_configure(cm):
    first = next(c for c in cm.cameras if "s5k3m5" in c.id)
    second = next(c for c in cm.cameras if "imx520" in c.id)
    external = next(c for c in cm.cameras if "imx363" in c.id)

    first.acquire()
    try:
        capture(cm, first, (640, 480), 4, {})
    finally:
        first.release()

    # The other process changes the kernel route behind this camera manager.
    subprocess.run(["cam", "--camera", external.id, "--capture=6",
                    "--stream", "width=640,height=480,pixelformat=NV12"],
                   check=True, timeout=15)

    def configure_second():
        config = second.generate_configuration([lc.StreamRole.Viewfinder])
        config.at(0).size = lc.Size(1280, 720)
        assert config.validate() != lc.CameraConfiguration.Status.Invalid
        second.configure(config)

    first.acquire()
    try:
        second.acquire()
        try:
            rows = capture(cm, first, (640, 480), 32, {}, before_start=configure_second)
            assert len(rows) == 32 and all(row["sensor_timestamp"] for row in rows)
        finally:
            second.release()
    finally:
        first.release()

    print("PASS: First camera captured after route changes in both camera managers", flush=True)


def af_controls(cm, cam, sensor, size):
    pattern(sensor, 0)
    capture(cm, cam, size, 12, {
        C.AeEnable: False, C.ExposureTime: 10000, C.AnalogueGain: 8.0,
        C.AwbEnable: False, C.AfMode: C.AfModeEnum.Manual,
        C.LensPosition: 1.0,
    })
    stage = 'trigger'
    original = None
    paused_position = None
    paused_frames = 0
    # Gravity compensation on imx363 can shift restored lens position by ~1 VCM step (~0.0165 D).
    lens_tol = 0.05
    checks = []

    def step(n, row):
        nonlocal stage, original, paused_position, paused_frames
        if original is None:
            original = row['lens']
        if stage == 'trigger' and n == 10:
            stage = 'cancel'
            return {C.AfTrigger: C.AfTriggerEnum.Start}
        if stage == 'cancel' and row['af'] == 1 and abs(row['lens'] - original) > lens_tol:
            stage = 'cancelled'
            return {C.AfTrigger: C.AfTriggerEnum.Cancel}
        if stage == 'cancelled' and row['af'] == 0 and abs(row['lens'] - original) <= lens_tol:
            checks.append('trigger_cancel_restore')
            stage = 'pause'
            pattern(sensor, 1)
            return {C.AfMode: C.AfModeEnum.Continuous}
        if stage == 'pause' and row['af'] == 1 and abs(row['lens'] - original) > lens_tol:
            stage = 'paused'
            return {C.AfPause: C.AfPauseEnum.Immediate}
        if stage == 'paused' and row['pause'] == 2:
            if paused_position is None:
                paused_position = row['lens']
            assert abs(row['lens'] - paused_position) <= lens_tol, (row['lens'], paused_position)
            paused_frames += 1
            if paused_frames == 10:
                checks.append('immediate_pause_hold')
                stage = 'resumed'
                return {C.AfPause: C.AfPauseEnum.Resume}
        if stage == 'resumed' and row['pause'] == 0:
            checks.append('resume')
            stage = 'deferred'
            return {C.AfPause: C.AfPauseEnum.Deferred}
        if stage == 'deferred' and row['pause'] == 2:
            assert row['af'] == 3 and abs(row['lens'] - original) <= lens_tol, row
            checks.append('deferred_pause_failed_restore')
            stage = 'done'
        return {}

    capture(cm, cam, size, 480, {
        C.AeEnable: False, C.ExposureTime: 10000, C.AnalogueGain: 8.0,
        C.AwbEnable: False, C.AfMode: C.AfModeEnum.Auto,
    }, step)
    pattern(sensor, 0)
    assert stage == 'done', (stage, checks)
    print(json.dumps({'test': 'af_controls', 'checks': checks}), flush=True)


def af_converge(cm, cam, sensor, size, auto_exposure=False):
    pattern(sensor, 0)
    controls = {
        C.AeEnable: auto_exposure,
        C.AwbEnable: False, C.AfMode: C.AfModeEnum.Manual,
    }
    if not auto_exposure:
        controls.update({C.ExposureTime: 16605, C.AnalogueGain: 5.0})
    lens_max = cam.controls[C.LensPosition].max
    initial = {**controls, C.LensPosition: lens_max}
    if auto_exposure:
        initial.update({C.AeEnable: False, C.ExposureTime: 1000, C.AnalogueGain: 1.0})
    capture(cm, cam, size, 12, initial)

    def step(n, _):
        return {C.AfTrigger: C.AfTriggerEnum.Start} if n == 10 else {}

    rows = capture(cm, cam, size, 600,
                   {**controls, C.AfMode: C.AfModeEnum.Auto}, step)
    focused = [row for row in rows if row['af'] == 2]
    transitions = []
    for index, row in enumerate(rows):
        if not transitions or row['af'] != transitions[-1]['af']:
            transitions.append({'capture': index + 1, 'af': row['af'],
                                'lens': row['lens'], 'fom': row['fom']})
    lens_groups = []
    for index, row in enumerate(rows):
        if not lens_groups or row['lens'] != lens_groups[-1]['lens']:
            lens_groups.append({'start': index + 1, 'end': index + 1,
                                'lens': row['lens'], 'fom': [row['fom']]})
        else:
            lens_groups[-1]['end'] = index + 1
            lens_groups[-1]['fom'].append(row['fom'])
    lens_groups = [
        {**group, 'fom': statistics.median([v for v in group['fom'][-4:] if v is not None] or [0])}
        for group in lens_groups
    ]
    assert focused, ('AF did not reach Focused', transitions,
                     lens_groups)
    assert all(row['af'] == 2 for row in rows[-8:]), 'AF did not hold focus'
    first_scanning = next(index for index, row in enumerate(rows)
                          if index >= 10 and row['af'] == 1)
    first_focused = next(index for index, row in enumerate(rows)
                         if index >= first_scanning and row['af'] == 2)
    trigger_timestamp = rows[9]['timestamp']
    scan_timestamp = rows[first_scanning]['timestamp']
    focus_timestamp = rows[first_focused]['timestamp']
    final_fom = statistics.median(row['fom'] for row in rows[-8:])
    code = lens_code(lens_device(cam.id))
    comparison = {
        C.AeEnable: False, C.AwbEnable: False, C.AfMode: C.AfModeEnum.Manual,
        C.ExposureTime: rows[-1]['exposure'], C.AnalogueGain: rows[-1]['gain'],
    }
    reference = []
    for index in range(11):
        position = index * lens_max / 10
        samples = capture(cm, cam, size, 24,
                          {**comparison, C.LensPosition: position},
                          sampler=sample_focus_preview)
        reference.append({'lens': position,
                          'fom': statistics.median(row['fom'] for row in samples[-8:]),
                          'total_exposure': statistics.median(
                              row['exposure'] * row['gain'] for row in samples[-8:]),
                          'preview_y': statistics.median(
                              row['preview_y'] for row in samples[-8:]),
                          'preview_edge': statistics.median(
                              row['preview_edge'] for row in samples[-8:])})
    best = max(reference, key=lambda row: row['fom'])
    assert best['fom'] > min(row['fom'] for row in reference) * 1.10, \
        ('Target does not provide a distinct focus curve', reference)
    exposure_totals = [point['total_exposure'] for point in reference]
    exposure_totals += [row['exposure'] * row['gain'] for row in rows[-8:]]
    assert max(exposure_totals) / min(exposure_totals) < 1.1, exposure_totals
    assert final_fom >= best['fom'] * 0.90, (final_fom, best)
    capture(cm, cam, size, 24, {**comparison, C.LensPosition: rows[-1]['lens']})
    restored_code = lens_code(lens_device(cam.id))
    print(json.dumps({'test': 'af_converge', 'frames': len(rows),
                      'auto_exposure': auto_exposure,
                      'reference_exposure_us': comparison[C.ExposureTime],
                      'reference_gain': comparison[C.AnalogueGain],
                      'exposure_span': max(exposure_totals) / min(exposure_totals),
                      'focused_frames': len(focused),
                      'transitions': transitions,
                      'lens_groups': lens_groups,
                      'reference': reference, 'best_manual': best,
                      'scanning_capture': first_scanning + 1,
                      'focused_capture': first_focused + 1,
                      'trigger_to_scanning_s':
                          (scan_timestamp - trigger_timestamp) / 1e9,
                      'trigger_to_focused_s':
                          (focus_timestamp - trigger_timestamp) / 1e9,
                      'scan_to_focused_s':
                          (focus_timestamp - scan_timestamp) / 1e9,
                      'lens': rows[-1]['lens'], 'fom': final_fom,
                      'code': code, 'restored_code': restored_code}), flush=True)


def af_converge_auto(cm, cam, sensor, size):
    af_converge(cm, cam, sensor, size, auto_exposure=True)


def af_travel(cm, cam, sensor, size):
    pattern(sensor, 0)
    lens = lens_device(cam.id)
    codes = {}
    controls = {
        C.AeEnable: False, C.ExposureTime: 16605, C.AnalogueGain: 5.0,
        C.AwbEnable: False, C.AfMode: C.AfModeEnum.Manual,
        C.LensPosition: 0.0,
    }
    macro = cam.controls[C.LensPosition].max

    def step(n, _):
        if n in (110, 230, 350):
            codes[n] = lens_code(lens)
        if n == 120:
            return {C.LensPosition: macro}
        if n == 240:
            return {C.LensPosition: 0.0}
        return {}

    rows = capture(cm, cam, size, 360, controls, step,
                   sampler=sample_focus_preview)
    slices = [
        ('infinity_before', 0.0, rows[60:110]),
        ('macro', macro, rows[180:230]),
        ('infinity_after', 0.0, rows[300:350]),
    ]
    samples = []
    for name, position, section in slices:
        assert all(row['lens'] == position for row in section), (name, section)
        samples.append({
            'name': name,
            'lens': position,
            'fom': statistics.median(row['fom'] for row in section),
            'preview_y': statistics.median(row['preview_y'] for row in section),
            'preview_edge': statistics.median(
                row['preview_edge'] for row in section),
        })
    infinity_edge = statistics.mean((samples[0]['preview_edge'],
                                     samples[2]['preview_edge']))
    preview_edge_change = abs(samples[1]['preview_edge'] - infinity_edge)
    infinity_repeat_error = abs(samples[0]['preview_edge'] -
                                samples[2]['preview_edge'])
    assert preview_edge_change > infinity_repeat_error * 10, samples
    transitions = []
    for index, row in enumerate(rows):
        if not transitions or row['lens'] != transitions[-1]['lens']:
            transitions.append({'capture': index + 1, 'lens': row['lens']})
    code = lens_code(lens)
    assert codes[110] != codes[230], codes
    assert abs(codes[110] - codes[350]) <= 2, codes
    assert code == codes[350], (code, codes)
    print(json.dumps({
        'test': 'af_travel',
        'frames': len(rows),
        'duration_s': (rows[-1]['timestamp'] - rows[0]['timestamp']) / 1e9,
        'transitions': transitions,
        'samples': samples,
        'preview_edge_change': preview_edge_change,
        'infinity_repeat_error': infinity_repeat_error,
        'final_code': code,
        'position_codes': codes,
    }), flush=True)


def awb_mode(cm, cam, sensor, size):
    assert C.AwbMode in cam.controls, f'{cam.id} missing AwbMode'
    pattern(sensor, 0)
    presets = [
        (C.AwbModeEnum.Incandescent, 2850),
        (C.AwbModeEnum.Tungsten, 3200),
        (C.AwbModeEnum.Indoor, 3500),
        (C.AwbModeEnum.Fluorescent, 4000),
        (C.AwbModeEnum.Daylight, 5500),
        (C.AwbModeEnum.Cloudy, 7000),
    ]
    per_mode = 10
    total = per_mode * (len(presets) + 1)

    def request_controls(index):
        slot = index // per_mode
        if slot < len(presets):
            return {C.AwbEnable: True, C.AwbMode: presets[slot][0]}
        return {C.AwbEnable: True, C.AwbMode: C.AwbModeEnum.Auto}

    rows = capture(cm, cam, size, total, {
        C.AeEnable: False, C.ExposureTime: 5000, C.AnalogueGain: 2.0,
        C.AwbEnable: True, C.AwbMode: presets[0][0],
        **manual_focus_controls(cam, 0.0),
    }, request_controls=request_controls)

    summary = []
    for idx, (mode, expected_cct) in enumerate(presets):
        section = rows[idx * per_mode + 2:(idx + 1) * per_mode]
        assert all(r['awb'] is True and r['awb_mode'] == mode and r['awb_locked'] is True
                   and r['cct'] == expected_cct for r in section), (mode, section[-1])
        wb = section[-1]['wb']
        summary.append({'mode': int(mode), 'cct': section[-1]['cct'],
                        'gains': list(wb), 'rb_ratio': wb[0] / wb[1]})
    assert summary[-1]['rb_ratio'] > summary[0]['rb_ratio'] * 1.5, summary
    auto_tail = rows[len(presets) * per_mode + 2:]
    assert all(r['awb'] is True and r['awb_mode'] == C.AwbModeEnum.Auto for r in auto_tail), auto_tail[-1]
    print(json.dumps({'test': 'awb_mode', 'camera': cam.id, 'presets': summary}), flush=True)


def lux_metadata(cm, cam, sensor, size):
    pattern(sensor, 0)
    rows = capture(cm, cam, size, 24, {
        C.AeEnable: True, C.AwbEnable: True,
        **manual_focus_controls(cam, 0.0),
    })
    lux_values = [r['lux'] for r in rows[6:]]
    assert all(v is not None and math.isfinite(v) and v > 0.0 for v in lux_values), lux_values
    print(json.dumps({'test': 'lux_metadata', 'camera': cam.id,
                      'median_lux': statistics.median(lux_values),
                      'min_lux': min(lux_values), 'max_lux': max(lux_values)}), flush=True)


def flicker_auto(cm, cam, sensor, size):
    assert C.AeFlickerModeEnum.Auto in cam.controls[C.AeFlickerMode].values, \
        cam.controls[C.AeFlickerMode]
    pattern(sensor, 0)
    rows = capture(cm, cam, size, 48, {
        C.AeEnable: True, C.AwbEnable: True,
        C.AeFlickerMode: C.AeFlickerModeEnum.Auto,
        **manual_focus_controls(cam, 0.0),
    })
    assert all(r['flicker_mode'] == C.AeFlickerModeEnum.Auto for r in rows), rows[-1]
    detected = [r['flicker_detected'] for r in rows]
    assert all(v in (0, 8333, 10000) for v in detected), detected
    print(json.dumps({'test': 'flicker_auto', 'camera': cam.id,
                      'detected_values': sorted(set(detected))}), flush=True)


def bf_roi(cm, cam, sensor, size):
    import ctypes
    assert has_autofocus(cam), f'{cam.id} has no autofocus'
    pattern(sensor, 0)
    VIDIOC_QCOM_CAMSS_GET_STATS = 0xC04856C0
    vfe_nodes = [subprocess.check_output(['media-ctl', '-e', name], text=True).strip()
                 for name in ('msm_vfe0_video3', 'msm_vfe1_video3')]
    active_node = [vfe_nodes[0]]
    awb_buf = (ctypes.c_uint32 * (64 * 48 * 12))()
    bhist_buf = (ctypes.c_uint32 * 1024)()
    bf_buf = (ctypes.c_uint64 * (1 + 25 * 4))()

    def detect_active_vfe_node():
        topo = subprocess.check_output(['media-ctl', '-p'], text=True)
        for idx, name in enumerate(('msm_vfe0_pix', 'msm_vfe1_pix')):
            if f'-> "{name}":0 [ENABLED]' in topo:
                return vfe_nodes[idx]
        return vfe_nodes[0]

    def read_bf_center_count():
        fd = Path(active_node[0]).open('rb+', buffering=0)
        try:
            payload = bytearray(struct.pack(
                '<IIQIIIIQQQIIII',
                3, 0, 0, 0, 0, 0, 0,
                ctypes.addressof(awb_buf), ctypes.addressof(bhist_buf), ctypes.addressof(bf_buf),
                0, 0, 0, 0,
            ))
            fcntl.ioctl(fd.fileno(), VIDIOC_QCOM_CAMSS_GET_STATS, payload, True)
            center = bf_buf[1 + 12 * 4]
            return (center >> 39) & 0x7fffff
        finally:
            fd.close()

    small_rect = [None]
    counts = {}

    def setup():
        active_node[0] = detect_active_vfe_node()
        bounds = cam.properties.get(lc.properties.ScalerCropMaximum,
                                    cam.properties[lc.properties.PixelArrayActiveAreas][0])
        small_w = max(200, bounds.width // 4)
        small_h = max(160, bounds.height // 4)
        small_rect[0] = lc.Rectangle(bounds.x + (bounds.width - small_w) // 2,
                                     bounds.y + (bounds.height - small_h) // 2,
                                     small_w, small_h)

    def step(n, _):
        if n == 12:
            counts['full_before'] = read_bf_center_count()
            return {C.AfMetering: C.AfMeteringEnum.Windows, C.AfWindows: [small_rect[0]]}
        if n == 24:
            counts['windowed_1'] = read_bf_center_count()
            return {C.AfMetering: C.AfMeteringEnum.Auto}
        if n == 36:
            counts['full_mid'] = read_bf_center_count()
            return {C.AfMetering: C.AfMeteringEnum.Windows, C.AfWindows: [small_rect[0]]}
        if n == 48:
            counts['windowed_2'] = read_bf_center_count()
            return {C.AfMetering: C.AfMeteringEnum.Auto}
        if n == 58:
            counts['full_after'] = read_bf_center_count()
        return {}

    rows = capture(cm, cam, size, 60, {
        C.AeEnable: False, C.ExposureTime: 5000, C.AnalogueGain: 2.0,
        C.AwbEnable: False, C.ColourGains: (1.0, 1.0),
        C.AfMode: C.AfModeEnum.Manual, C.LensPosition: 0.0,
        C.AfMetering: C.AfMeteringEnum.Auto,
    }, step, before_start=setup)
    assert len(rows) == 60
    assert counts['full_before'] > 0 and counts['windowed_1'] > 0 and counts['windowed_2'] > 0, counts
    assert counts['windowed_1'] < counts['full_before'] * 0.25, counts
    assert counts['windowed_2'] < counts['full_before'] * 0.25, counts
    assert abs(counts['full_mid'] - counts['full_before']) <= max(50, counts['full_before'] * 0.05), counts
    assert abs(counts['full_after'] - counts['full_before']) <= max(50, counts['full_before'] * 0.05), counts
    print(json.dumps({'test': 'bf_roi', 'camera': cam.id, 'counts': counts}), flush=True)


def raw_dual(cm, cam, sensor, size):
    pattern(sensor, 0)
    # 1. Standalone RAW stream
    raw_cfg = cam.generate_configuration([lc.StreamRole.Raw])
    assert raw_cfg is not None and raw_cfg.size == 1
    assert raw_cfg.validate() != lc.CameraConfiguration.Status.Invalid
    raw_sc = raw_cfg.at(0)
    assert 'CSI2P' in str(raw_sc.pixel_format), raw_sc.pixel_format
    assert raw_sc.color_space.primaries == lc.ColorSpace.Primaries.Raw, raw_sc.color_space
    cam.configure(raw_cfg)
    raw_stream = raw_sc.stream
    allocator = lc.FrameBufferAllocator(cam)
    assert allocator.allocate(raw_stream) >= 4
    reqs = []
    for buf in allocator.buffers(raw_stream):
        req = cam.create_request()
        req.add_buffer(raw_stream, buf)
        reqs.append(req)
    with ExitStack() as cleanup:
        sel = cleanup.enter_context(selectors.DefaultSelector())
        sel.register(cm.event_fd, selectors.EVENT_READ)
        cam.start({C.AeEnable: False, C.ExposureTime: 5000, C.AnalogueGain: 2.0,
                   **manual_focus_controls(cam, 0.0)})
        cleanup.callback(cm.get_ready_requests)
        cleanup.callback(cam.stop)
        for r in reqs:
            cam.queue_request(r)
        got_raw = 0
        raw_nonzero = 0
        deadline = time.monotonic() + 8
        while got_raw < 16:
            assert time.monotonic() < deadline, ('RAW-only timed out', got_raw)
            if not sel.select(1):
                continue
            for r in cm.get_ready_requests():
                fb = r.buffers[raw_stream]
                assert fb.metadata.status == lc.FrameMetadata.Status.Success
                assert r.metadata.get(C.SensorTimestamp) is not None
                if got_raw == 8:
                    with mapped_planes(fb) as (plane,):
                        raw_nonzero = sum(1 for b in plane[:4096] if b != 0)
                got_raw += 1
                if got_raw < 16:
                    r.reuse()
                    cam.queue_request(r)
    assert raw_nonzero > 0, (cam.id, 'RAW-only buffer is all zero')

    # 2. Concurrent PIX NV12 + RDI RAW10 dual-stream (including mixed NV12-only + NV12+RAW requests)
    dual_cfg = cam.generate_configuration([lc.StreamRole.Viewfinder, lc.StreamRole.Raw])
    assert dual_cfg is not None and dual_cfg.size == 2
    dual_cfg.at(0).size = lc.Size(*size)
    assert dual_cfg.validate() != lc.CameraConfiguration.Status.Invalid
    pix_sc, raw_sc = dual_cfg.at(0), dual_cfg.at(1)
    assert str(pix_sc.pixel_format) == 'NV12' and 'CSI2P' in str(raw_sc.pixel_format)
    cam.configure(dual_cfg)
    pix_stream, raw_stream = pix_sc.stream, raw_sc.stream
    allocator2 = lc.FrameBufferAllocator(cam)
    assert allocator2.allocate(pix_stream) >= 4
    assert allocator2.allocate(raw_stream) >= 4
    pix_bufs = list(allocator2.buffers(pix_stream))
    raw_bufs = list(allocator2.buffers(raw_stream))
    free_raw = list(raw_bufs)
    reqs2 = []
    for pb in pix_bufs:
        req = cam.create_request()
        req.add_buffer(pix_stream, pb)
        req.add_buffer(raw_stream, free_raw.pop(0))
        reqs2.append(req)
    with ExitStack() as cleanup:
        sel = cleanup.enter_context(selectors.DefaultSelector())
        sel.register(cm.event_fd, selectors.EVENT_READ)
        cam.start({C.AeEnable: True, C.AwbEnable: True, **manual_focus_controls(cam, 0.0)})
        cleanup.callback(cm.get_ready_requests)
        cleanup.callback(cam.stop)
        queued = 0
        for r in reqs2:
            cam.queue_request(r)
            queued += 1
        pix_done = 0
        raw_done = 0
        dual_raw_nonzero = 0
        deadline = time.monotonic() + 10
        while pix_done < 24:
            assert time.monotonic() < deadline, ('Dual stream timed out', pix_done, raw_done)
            if not sel.select(1):
                continue
            for r in cm.get_ready_requests():
                pfb = r.buffers[pix_stream]
                assert pfb.metadata.status == lc.FrameMetadata.Status.Success
                assert r.metadata.get(C.SensorTimestamp) is not None
                pix_done += 1
                rfb = r.buffers.get(raw_stream)
                if rfb is not None:
                    assert rfb.metadata.status == lc.FrameMetadata.Status.Success
                    if raw_done == 8:
                        with mapped_planes(rfb) as (plane,):
                            dual_raw_nonzero = sum(1 for b in plane[:4096] if b != 0)
                    raw_done += 1
                    free_raw.append(rfb)
                if queued < 24:
                    next_req = cam.create_request()
                    next_req.add_buffer(pix_stream, pfb)
                    # Mix PIX+RAW and on-demand PIX-only requests after frame 16
                    if (queued < 16 or queued % 2 == 1) and free_raw:
                        next_req.add_buffer(raw_stream, free_raw.pop(0))
                    reqs2.append(next_req)
                    cam.queue_request(next_req)
                    queued += 1
    assert raw_done == 20 and dual_raw_nonzero > 0, (cam.id, pix_done, raw_done, dual_raw_nonzero)
    print(json.dumps({'test': 'raw_dual', 'camera': cam.id,
                      'raw_only_frames': got_raw, 'dual_pix_frames': pix_done,
                      'dual_raw_frames': raw_done,
                      'raw_size': [raw_sc.size.width, raw_sc.size.height],
                      'raw_format': str(raw_sc.pixel_format)}), flush=True)


def dual_camera(cm, size):
    def run_pair(model_a, size_a, model_b, size_b, frames=32):
        cam_a = next(c for c in cm.cameras if model_a in c.id)
        cam_b = next(c for c in cm.cameras if model_b in c.id)
        cam_a.acquire()
        cam_b.acquire()
        try:
            cfg_a = cam_a.generate_configuration([lc.StreamRole.Viewfinder])
            cfg_b = cam_b.generate_configuration([lc.StreamRole.Viewfinder])
            cfg_a.at(0).size = lc.Size(*size_a)
            cfg_b.at(0).size = lc.Size(*size_b)
            assert cfg_a.validate() != lc.CameraConfiguration.Status.Invalid
            assert cfg_b.validate() != lc.CameraConfiguration.Status.Invalid
            cam_a.configure(cfg_a)
            cam_b.configure(cfg_b)
            stream_a, stream_b = cfg_a.at(0).stream, cfg_b.at(0).stream
            alloc_a = lc.FrameBufferAllocator(cam_a)
            alloc_b = lc.FrameBufferAllocator(cam_b)
            assert alloc_a.allocate(stream_a) >= 4
            assert alloc_b.allocate(stream_b) >= 4
            reqs_a = []
            for fb in alloc_a.buffers(stream_a):
                r = cam_a.create_request()
                r.add_buffer(stream_a, fb)
                reqs_a.append(r)
            reqs_b = []
            for fb in alloc_b.buffers(stream_b):
                r = cam_b.create_request()
                r.add_buffer(stream_b, fb)
                reqs_b.append(r)
            with ExitStack() as cleanup:
                sel = cleanup.enter_context(selectors.DefaultSelector())
                sel.register(cm.event_fd, selectors.EVENT_READ)
                cam_a.start({C.AeEnable: True, C.AwbEnable: True,
                             **manual_focus_controls(cam_a, 0.0)})
                cleanup.callback(cm.get_ready_requests)
                cleanup.callback(cam_a.stop)
                cam_b.start({C.AeEnable: True, C.AwbEnable: True,
                             **manual_focus_controls(cam_b, 0.0)})
                cleanup.callback(cam_b.stop)
                for r in reqs_a:
                    cam_a.queue_request(r)
                for r in reqs_b:
                    cam_b.queue_request(r)
                got_a = got_b = 0
                q_a = len(reqs_a)
                q_b = len(reqs_b)
                deadline = time.monotonic() + 12
                while got_a < frames or got_b < frames:
                    assert time.monotonic() < deadline, (model_a, model_b, got_a, got_b)
                    if not sel.select(1):
                        continue
                    for r in cm.get_ready_requests():
                        if stream_a in r.buffers:
                            assert r.buffers[stream_a].metadata.status == lc.FrameMetadata.Status.Success
                            got_a += 1
                            if q_a < frames:
                                r.reuse()
                                cam_a.queue_request(r)
                                q_a += 1
                        elif stream_b in r.buffers:
                            assert r.buffers[stream_b].metadata.status == lc.FrameMetadata.Status.Success
                            got_b += 1
                            if q_b < frames:
                                r.reuse()
                                cam_b.queue_request(r)
                                q_b += 1
            return {'pair': [model_a, model_b], 'frames_a': got_a, 'frames_b': got_b}
        finally:
            cam_b.release()
            cam_a.release()

    pair_results = [
        run_pair('imx363', (1280, 720), 'imx520', (1280, 720), 32),
        run_pair('imx363', (1280, 720), 'imx351', (1280, 720), 32),
    ]

    # Shared CSIPHY2 mux pair (s5k3m5 + imx520) must remain mutually exclusive.
    tele = next(c for c in cm.cameras if 's5k3m5' in c.id)
    front = next(c for c in cm.cameras if 'imx520' in c.id)
    tele.acquire()
    front.acquire()
    conflict_rejected = False
    try:
        cfg_t = tele.generate_configuration([lc.StreamRole.Viewfinder])
        cfg_t.at(0).size = lc.Size(1280, 720)
        cfg_t.validate()
        tele.configure(cfg_t)
        st_t = cfg_t.at(0).stream
        alloc_t = lc.FrameBufferAllocator(tele)
        assert alloc_t.allocate(st_t) >= 4
        reqs_t = []
        for fb in alloc_t.buffers(st_t):
            r = tele.create_request()
            r.add_buffer(st_t, fb)
            reqs_t.append(r)
        with ExitStack() as cleanup:
            sel = cleanup.enter_context(selectors.DefaultSelector())
            sel.register(cm.event_fd, selectors.EVENT_READ)
            tele.start({C.AeEnable: True, C.AwbEnable: True,
                        **manual_focus_controls(tele, 0.0)})
            cleanup.callback(cm.get_ready_requests)
            cleanup.callback(tele.stop)
            cfg_f = front.generate_configuration([lc.StreamRole.Viewfinder])
            cfg_f.at(0).size = lc.Size(1280, 720)
            cfg_f.validate()
            try:
                front.configure(cfg_f)
            except RuntimeError:
                conflict_rejected = True
            for r in reqs_t:
                tele.queue_request(r)
            got = 0
            q = len(reqs_t)
            deadline = time.monotonic() + 8
            while got < 16:
                assert time.monotonic() < deadline, got
                if not sel.select(1):
                    continue
                for r in cm.get_ready_requests():
                    assert r.buffers[st_t].metadata.status == lc.FrameMetadata.Status.Success
                    got += 1
                    if q < 16:
                        r.reuse()
                        tele.queue_request(r)
                        q += 1
    finally:
        front.release()
        tele.release()
    assert conflict_rejected, 'Shared CSIPHY2 mux conflict (s5k3m5 + imx520) was not rejected'
    print(json.dumps({'test': 'dual_camera', 'pairs': pair_results,
                      'csiphy2_conflict_rejected': conflict_rejected}), flush=True)


V4L2_CID_QCOM_CAMSS_LSC = 0x00980900 + 0x1253
V4L2_CID_QCOM_CAMSS_DEMUX = 0x00980900 + 0x1254
V4L2_CID_QCOM_CAMSS_BPC = 0x00980900 + 0x1255
V4L2_CID_QCOM_CAMSS_ABF = 0x00980900 + 0x1256
V4L2_CID_QCOM_CAMSS_GTM = 0x00980900 + 0x1257
VIDIOC_G_EXT_CTRLS = 0xC0205647
VIDIOC_S_EXT_CTRLS = 0xC0205648
V4L2_CTRL_WHICH_CUR_VAL = 0


class _V4L2ExtControl(ctypes.Structure):
    _layout_ = 'ms'
    _pack_ = 1
    _fields_ = [
        ('id', ctypes.c_uint32),
        ('size', ctypes.c_uint32),
        ('reserved2', ctypes.c_uint32),
        ('ptr', ctypes.c_void_p),
    ]


class _V4L2ExtControls(ctypes.Structure):
    _fields_ = [
        ('which', ctypes.c_uint32),
        ('count', ctypes.c_uint32),
        ('error_idx', ctypes.c_uint32),
        ('request_fd', ctypes.c_int32),
        ('reserved', ctypes.c_uint32),
        ('controls', ctypes.POINTER(_V4L2ExtControl)),
    ]


def _vfe_ext_ctrl(subdev_path, cid, payload_bytes, write=False):
    buf = ctypes.create_string_buffer(payload_bytes, len(payload_bytes))
    ctrl = _V4L2ExtControl(id=cid, size=len(payload_bytes), reserved2=0,
                           ptr=ctypes.addressof(buf))
    ctrls = _V4L2ExtControls(which=V4L2_CTRL_WHICH_CUR_VAL, count=1,
                             error_idx=0, request_fd=-1, reserved=0,
                             controls=ctypes.pointer(ctrl))
    fd = os.open(subdev_path, os.O_RDWR)
    try:
        fcntl.ioctl(fd, VIDIOC_S_EXT_CTRLS if write else VIDIOC_G_EXT_CTRLS, ctrls)
    finally:
        os.close(fd)
    return buf.raw


def _sample_corner_center(buffer, config):
    width, height, stride = config.size.width, config.size.height, config.stride
    with mapped_planes(buffer) as (y, _):
        bw, bh = max(16, width // 10), max(16, height // 10)
        cx0, cy0 = width // 2 - bw // 2, height // 2 - bh // 2
        center_vals = [y[r * stride + c]
                       for r in range(cy0, cy0 + bh, 4)
                       for c in range(cx0, cx0 + bw, 4)]
        corners = [
            (8, 8),
            (width - bw - 8, 8),
            (8, height - bh - 8),
            (width - bw - 8, height - bh - 8),
        ]
        corner_vals = [y[r * stride + c]
                       for x0, y0 in corners
                       for r in range(y0, y0 + bh, 4)
                       for c in range(x0, x0 + bw, 4)]
    return {'center_y': statistics.mean(center_vals),
            'corner_y': statistics.mean(corner_vals)}


def _active_vfe_pix_subdev():
    topo = subprocess.check_output(['media-ctl', '-p'], text=True)
    for name in ('msm_vfe0_pix', 'msm_vfe1_pix'):
        m = re.search(rf'entity \d+: {name} .*?(?=\n- entity |\Z)', topo, re.S)
        if m and '<- "msm_csid' in m.group(0) and '[ENABLED]' in m.group(0):
            return subprocess.check_output(['media-ctl', '-e', name], text=True).strip()
    return subprocess.check_output(['media-ctl', '-e', 'msm_vfe0_pix'], text=True).strip()


def lsc_isp(cm, cam, sensor, size):
    pattern(sensor, 0)
    lsc_size = 4 + 4 * 221 * 2
    demux_size = 4 * 2
    bpc_size = 4 + 6 * 2
    abf_size = 172
    gtm_size = 4 + 65 * 4
    snapshot = {}

    def step(n, _):
        if n == 8:
            pix_subdev = _active_vfe_pix_subdev()
            snapshot['pix_subdev'] = pix_subdev
            raw_lsc = _vfe_ext_ctrl(pix_subdev, V4L2_CID_QCOM_CAMSS_LSC, bytes(lsc_size))
            raw_demux = _vfe_ext_ctrl(pix_subdev, V4L2_CID_QCOM_CAMSS_DEMUX, bytes(demux_size))
            raw_bpc = _vfe_ext_ctrl(pix_subdev, V4L2_CID_QCOM_CAMSS_BPC, bytes(bpc_size))
            raw_abf = _vfe_ext_ctrl(pix_subdev, V4L2_CID_QCOM_CAMSS_ABF, bytes(abf_size))
            raw_gtm = _vfe_ext_ctrl(pix_subdev, V4L2_CID_QCOM_CAMSS_GTM, bytes(gtm_size))
            lsc_en = struct.unpack_from('<I', raw_lsc, 0)[0]
            lsc_r = struct.unpack_from('<221H', raw_lsc, 4)
            demux = struct.unpack('<4H', raw_demux)
            bpc = struct.unpack('<I6H', raw_bpc)
            abf_en = struct.unpack_from('<I', raw_abf, 0)[0]
            abf_lut = struct.unpack_from('<65H', raw_abf, 40)
            gtm_en = struct.unpack_from('<I', raw_gtm, 0)[0]
            gtm_lut = struct.unpack_from('<65I', raw_gtm, 4)
            snapshot['lsc_enable'] = lsc_en
            snapshot['lsc_r_corner'] = lsc_r[0]
            snapshot['lsc_r_center'] = lsc_r[6 * 17 + 8]
            snapshot['demux'] = list(demux)
            snapshot['bpc'] = list(bpc)
            snapshot['abf_enable'] = abf_en
            snapshot['abf_lut_ends'] = [abf_lut[0], abf_lut[-1]]
            snapshot['gtm_enable'] = gtm_en
            snapshot['gtm_lut_ends'] = [gtm_lut[0], gtm_lut[-1]]
            snapshot['raw_lsc'] = raw_lsc
            snapshot['raw_abf'] = raw_abf
            snapshot['raw_gtm'] = raw_gtm
        elif n == 12:
            # Disable LSC live mid-stream to verify ping-pong toggle and real shading lift.
            _vfe_ext_ctrl(snapshot['pix_subdev'], V4L2_CID_QCOM_CAMSS_LSC, bytes(lsc_size), write=True)
        elif n == 20:
            # Re-enable LSC live mid-stream (ping-pong bank switch).
            _vfe_ext_ctrl(snapshot['pix_subdev'], V4L2_CID_QCOM_CAMSS_LSC, snapshot['raw_lsc'], write=True)
        elif n == 26:
            # Boost GTM tone curve to 2.0x (8192 Q12) and toggle ABF DMI bank mid-stream.
            boost_gtm = struct.pack('<I65I', 1, *([8192] * 65))
            _vfe_ext_ctrl(snapshot['pix_subdev'], V4L2_CID_QCOM_CAMSS_GTM, boost_gtm, write=True)
            _vfe_ext_ctrl(snapshot['pix_subdev'], V4L2_CID_QCOM_CAMSS_ABF, snapshot['raw_abf'], write=True)
        elif n == 32:
            # Restore normal GTM tone curve (ping-pong bank switch).
            _vfe_ext_ctrl(snapshot['pix_subdev'], V4L2_CID_QCOM_CAMSS_GTM, snapshot['raw_gtm'], write=True)
        return {}

    target_size = (1632, 924) if 'imx520' in cam.id and size == (2016, 1136) else size
    rows = capture(cm, cam, target_size, 36, {
        C.AeEnable: True, C.AwbEnable: True,
        **manual_focus_controls(cam, 0.0),
    }, step, sampler=_sample_corner_center)

    assert snapshot['lsc_enable'] == 1, snapshot
    assert snapshot['lsc_r_corner'] > snapshot['lsc_r_center'] >= 1024, snapshot
    assert snapshot['bpc'][0] == 3, snapshot
    assert snapshot['demux'][0] == 1024 and snapshot['demux'][3] == 1024, snapshot
    assert all(900 <= g <= 1150 for g in snapshot['demux'][1:3]), snapshot
    assert snapshot['abf_enable'] == 5, snapshot
    assert 1 <= snapshot['abf_lut_ends'][0] < snapshot['abf_lut_ends'][1] <= 511, snapshot
    assert snapshot['gtm_enable'] == 1, snapshot
    assert snapshot['gtm_lut_ends'][0] > snapshot['gtm_lut_ends'][1] == 4096, snapshot

    on1_center = statistics.mean(r['center_y'] for r in rows[8:12])
    on1_corner = statistics.mean(r['corner_y'] for r in rows[8:12])
    off_center = statistics.mean(r['center_y'] for r in rows[15:20])
    off_corner = statistics.mean(r['corner_y'] for r in rows[15:20])
    on2_center = statistics.mean(r['center_y'] for r in rows[22:26])
    on2_corner = statistics.mean(r['corner_y'] for r in rows[22:26])
    gtm_boost_center = statistics.mean(r['center_y'] for r in rows[28:32])
    ratio_on1 = on1_corner / max(on1_center, 1.0)
    ratio_off = off_corner / max(off_center, 1.0)
    ratio_on2 = on2_corner / max(on2_center, 1.0)
    if off_center >= 12.0 and on1_corner < 245.0:
        assert ratio_on1 > ratio_off * 1.15 and ratio_on2 > ratio_off * 1.15, (
            cam.id, ratio_on1, ratio_off, ratio_on2)
    if 4.0 <= on2_center < 220.0:
        assert gtm_boost_center > on2_center * 1.08, (
            cam.id, on2_center, gtm_boost_center)

    print(json.dumps({
        'test': 'lsc', 'camera': cam.id, 'frames': len(rows),
        'lsc_r_corner_q10': snapshot['lsc_r_corner'],
        'lsc_r_center_q10': snapshot['lsc_r_center'],
        'demux_q10': snapshot['demux'],
        'bpc_enable': snapshot['bpc'][0],
        'abf_enable': snapshot['abf_enable'],
        'abf_lut_ends': snapshot['abf_lut_ends'],
        'gtm_enable': snapshot['gtm_enable'],
        'gtm_lut_ends_q12': snapshot['gtm_lut_ends'],
        'corner_center_on1': round(ratio_on1, 4),
        'corner_center_off': round(ratio_off, 4),
        'corner_center_on2': round(ratio_on2, 4),
        'center_y_normal': round(on2_center, 2),
        'center_y_gtm_2x': round(gtm_boost_center, 2),
    }), flush=True)


def colorspace_test(cm, cam, sensor, size):
    target_size = (1632, 924) if 'imx520' in cam.id and size == (2016, 1136) else size
    has_bars = C.draft.TestPatternMode in cam.controls
    results = {}

    def run_cs(name, cs):
        cfg = cam.generate_configuration([lc.StreamRole.Viewfinder])
        cfg.at(0).size = lc.Size(*target_size)
        cfg.at(0).color_space = cs
        status = cfg.validate()
        assert status != lc.CameraConfiguration.Status.Invalid
        got_cs = cfg.at(0).color_space
        assert (got_cs.primaries, got_cs.transferFunction,
                got_cs.ycbcrEncoding, got_cs.range) == (
            cs.primaries, cs.transferFunction, cs.ycbcrEncoding, cs.range), (
            name, got_cs, cs)
        cam.configure(cfg)
        stream = cfg.at(0).stream
        alloc = lc.FrameBufferAllocator(cam)
        assert alloc.allocate(stream) >= 4
        reqs = []
        for fb in alloc.buffers(stream):
            r = cam.create_request()
            r.add_buffer(stream, fb)
            reqs.append(r)
        start_ctrls = {
            C.AeEnable: False, C.ExposureTime: 5000, C.AnalogueGain: 1.0,
            C.AwbEnable: False, C.ColourGains: (1.0, 1.0),
            **manual_focus_controls(cam, 0.0),
        }
        if has_bars:
            start_ctrls[C.draft.TestPatternMode] = C.draft.TestPatternModeEnum.ColorBars
        sample = {}
        with ExitStack() as cleanup:
            sel = cleanup.enter_context(selectors.DefaultSelector())
            sel.register(cm.event_fd, selectors.EVENT_READ)
            cam.start(start_ctrls)
            cleanup.callback(cm.get_ready_requests)
            cleanup.callback(cam.stop)
            for r in reqs:
                cam.queue_request(r)
            got = 0
            queued = len(reqs)
            deadline = time.monotonic() + 8
            while got < 8:
                assert time.monotonic() < deadline, (name, got)
                if not sel.select(1):
                    continue
                for r in cm.get_ready_requests():
                    fb = r.buffers[stream]
                    assert fb.metadata.status == lc.FrameMetadata.Status.Success
                    got += 1
                    if got == 8:
                        sc = cfg.at(0)
                        w, h, st = sc.size.width, sc.size.height, sc.stride
                        with mapped_planes(fb) as (y, uv):
                            ys = [y[row * st + col]
                                  for row in range(0, h, 8)
                                  for col in range(0, w, 8)]
                            us = [uv[row * st + col]
                                  for row in range(0, h // 2, 4)
                                  for col in range(0, w, 8)]
                            vs = [uv[row * st + col + 1]
                                  for row in range(0, h // 2, 4)
                                  for col in range(0, w, 8)]
                        sample = {
                            'min_y': min(ys), 'max_y': max(ys),
                            'min_u': min(us), 'max_u': max(us),
                            'min_v': min(vs), 'max_v': max(vs),
                            'bars': sample_bars(fb, sc)['bars'] if has_bars else None,
                        }
                    if queued < 8:
                        r.reuse()
                        cam.queue_request(r)
                        queued += 1
        return sample

    results['sycc'] = run_cs('sycc', lc.ColorSpace.Sycc())
    results['rec709'] = run_cs('rec709', lc.ColorSpace.Rec709())
    results['smpte170m'] = run_cs('smpte170m', lc.ColorSpace.Smpte170m())

    for name in ('rec709', 'smpte170m'):
        s = results[name]
        assert 16 <= s['min_y'] and s['max_y'] <= 235, (cam.id, name, s)
        assert 16 <= s['min_u'] and s['max_u'] <= 240, (cam.id, name, s)
        assert 16 <= s['min_v'] and s['max_v'] <= 240, (cam.id, name, s)
    if has_bars:
        assert results['sycc']['min_y'] < 16 or results['sycc']['max_y'] > 235, results['sycc']
        # BT.709 vs BT.601 limited matrices produce distinct Y/UV on saturated colour bars.
        bar_diff = sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2])
                       for a, b in zip(results['rec709']['bars'], results['smpte170m']['bars']))
        assert bar_diff >= 20, (cam.id, bar_diff, results['rec709']['bars'], results['smpte170m']['bars'])
        results['bar_diff_709_vs_601'] = bar_diff
    print(json.dumps({'test': 'colorspace', 'camera': cam.id,
                      'sycc': {k: v for k, v in results['sycc'].items() if k != 'bars'},
                      'rec709': {k: v for k, v in results['rec709'].items() if k != 'bars'},
                      'smpte170m': {k: v for k, v in results['smpte170m'].items() if k != 'bars'},
                      'bar_diff_709_vs_601': results.get('bar_diff_709_vs_601')}), flush=True)


def _sample_dual_ife_seam(buffer, config):
    width, height, stride = config.size.width, config.size.height, config.stride
    seam_x = (width // 2) & ~0xf
    bw, bh = max(32, width // 10), max(32, height // 10)
    with mapped_planes(buffer) as (y, uv):
        left_vals = [y[r * stride + c]
                     for r in range(64, height - 64, 16)
                     for c in range(64, seam_x - 64, 16)]
        right_vals = [y[r * stride + c]
                      for r in range(64, height - 64, 16)
                      for c in range(seam_x + 64, width - 64, 16)]
        cx0, cy0 = width // 2 - bw // 2, height // 2 - bh // 2
        center_vals = [y[r * stride + c]
                       for r in range(cy0, cy0 + bh, 8)
                       for c in range(cx0, cx0 + bw, 8)]
        left_corner_vals = [y[r * stride + c]
                            for y0 in (8, height - bh - 8)
                            for r in range(y0, y0 + bh, 8)
                            for c in range(8, 8 + bw, 8)]
        right_corner_vals = [y[r * stride + c]
                             for y0 in (8, height - bh - 8)
                             for r in range(y0, y0 + bh, 8)
                             for c in range(width - bw - 8, width - 8, 8)]
        seam_y = [abs(y[r * stride + seam_x] - y[r * stride + seam_x - 1])
                  for r in range(64, height - 64, 8)]
        left_adj_y = [abs(y[r * stride + seam_x - 1] - y[r * stride + seam_x - 2])
                      for r in range(64, height - 64, 8)]
        right_adj_y = [abs(y[r * stride + seam_x + 1] - y[r * stride + seam_x])
                       for r in range(64, height - 64, 8)]
        seam_u = [abs(uv[r * stride + seam_x] - uv[r * stride + seam_x - 2])
                  for r in range(32, height // 2 - 32, 4)]
        seam_v = [abs(uv[r * stride + seam_x + 1] - uv[r * stride + seam_x - 1])
                  for r in range(32, height // 2 - 32, 4)]
    return {
        'left_y': statistics.mean(left_vals),
        'right_y': statistics.mean(right_vals),
        'center_y': statistics.mean(center_vals),
        'left_corner_y': statistics.mean(left_corner_vals),
        'right_corner_y': statistics.mean(right_corner_vals),
        'seam_y_diff': statistics.mean(seam_y),
        'left_adj_y_diff': statistics.mean(left_adj_y),
        'right_adj_y_diff': statistics.mean(right_adj_y),
        'seam_u_diff': statistics.mean(seam_u),
        'seam_v_diff': statistics.mean(seam_v),
    }


def dual_ife_test(cm, cam, sensor, _size):
    assert 'imx351' in cam.id, 'Dual-IFE 16MP validation requires IMX351'
    pattern(sensor, 0)
    lsc_size = 4 + 4 * 221 * 2
    bounds = cam.properties[lc.properties.PixelArrayActiveAreas][0]
    center_zoom = lc.Rectangle(bounds.x + ((bounds.width - 4032) // 2 & ~1),
                               bounds.y + ((bounds.height - 3024) // 2 & ~1),
                               4032, 3024)
    state = {'conflict_rejected': False}

    def step_16mp(n, _):
        if n == 6:
            state['pix_subdev'] = _active_vfe_pix_subdev()
            state['raw_lsc'] = _vfe_ext_ctrl(state['pix_subdev'], V4L2_CID_QCOM_CAMSS_LSC,
                                             bytes(lsc_size))
            # While Dual-IFE is streaming on both VFE0 and VFE1, a second camera must be rejected.
            other = next(c for c in cm.cameras if 'imx363' in c.id)
            other.acquire()
            try:
                ocfg = other.generate_configuration([lc.StreamRole.Viewfinder])
                ocfg.at(0).size = lc.Size(1280, 720)
                ocfg.validate()
                try:
                    other.configure(ocfg)
                except RuntimeError:
                    state['conflict_rejected'] = True
            finally:
                other.release()
        elif n == 10:
            _vfe_ext_ctrl(state['pix_subdev'], V4L2_CID_QCOM_CAMSS_LSC,
                          bytes(lsc_size), write=True)
        elif n == 16:
            _vfe_ext_ctrl(state['pix_subdev'], V4L2_CID_QCOM_CAMSS_LSC,
                          state['raw_lsc'], write=True)
        return {}

    def step_12mp(n, _):
        if n == 4:
            return {C.ScalerCrop: center_zoom}
        if n == 10:
            return {C.ScalerCrop: bounds}
        return {}

    rows_16mp = capture(cm, cam, (4656, 3492), 24, {
        C.AeEnable: True, C.AwbEnable: True,
    }, step_16mp, sampler=_sample_dual_ife_seam)
    rows_12mp = capture(cm, cam, (4032, 3024), 16, {
        C.AeEnable: True, C.AwbEnable: True,
    }, step_12mp, sampler=_sample_dual_ife_seam)

    assert state['conflict_rejected'], 'Second camera was not rejected during active Dual-IFE 16MP stream'
    for label, rows in (('4656x3492', rows_16mp[4:]), ('4032x3024', rows_12mp[4:])):
        for r in rows:
            assert r['left_y'] > 1.0 and r['right_y'] > 1.0, (label, r)
            adj_ref = max(r['left_adj_y_diff'], r['right_adj_y_diff'], 1.0)
            assert r['seam_y_diff'] <= adj_ref * 2.5 + 2.5, (label, r)
            assert r['seam_u_diff'] <= 6.0 and r['seam_v_diff'] <= 6.0, (label, r)

    on_left_ratio = statistics.mean(r['left_corner_y'] / max(r['center_y'], 1.0) for r in rows_16mp[6:10])
    on_right_ratio = statistics.mean(r['right_corner_y'] / max(r['center_y'], 1.0) for r in rows_16mp[6:10])
    off_left_ratio = statistics.mean(r['left_corner_y'] / max(r['center_y'], 1.0) for r in rows_16mp[12:16])
    off_right_ratio = statistics.mean(r['right_corner_y'] / max(r['center_y'], 1.0) for r in rows_16mp[12:16])
    center_y = statistics.mean(r['center_y'] for r in rows_16mp[12:16])
    if 12.0 <= center_y < 235.0:
        assert on_left_ratio > off_left_ratio * 1.12, (on_left_ratio, off_left_ratio)
        assert on_right_ratio > off_right_ratio * 1.12, (on_right_ratio, off_right_ratio)

    assert rows_12mp[7]['scaler_crop'] == center_zoom, rows_12mp[7]
    assert rows_12mp[-1]['scaler_crop'] == bounds, rows_12mp[-1]

    print(json.dumps({
        'test': 'dual_ife',
        'camera': cam.id,
        'frames_4656x3492': len(rows_16mp),
        'frames_4032x3024': len(rows_12mp),
        'left_y': round(statistics.mean(r['left_y'] for r in rows_16mp[18:22]), 2),
        'right_y': round(statistics.mean(r['right_y'] for r in rows_16mp[18:22]), 2),
        'seam_y_diff': round(statistics.mean(r['seam_y_diff'] for r in rows_16mp[18:22]), 3),
        'left_adj_y_diff': round(statistics.mean(r['left_adj_y_diff'] for r in rows_16mp[18:22]), 3),
        'right_adj_y_diff': round(statistics.mean(r['right_adj_y_diff'] for r in rows_16mp[18:22]), 3),
        'seam_u_diff': round(statistics.mean(r['seam_u_diff'] for r in rows_16mp[18:22]), 3),
        'seam_v_diff': round(statistics.mean(r['seam_v_diff'] for r in rows_16mp[18:22]), 3),
        'lsc_left_ratio_on_off': [round(on_left_ratio, 4), round(off_left_ratio, 4)],
        'lsc_right_ratio_on_off': [round(on_right_ratio, 4), round(off_right_ratio, 4)],
        'zoom_seam_y_diff': round(statistics.mean(r['seam_y_diff'] for r in rows_12mp[6:10]), 3),
        'conflict_rejected': state['conflict_rejected'],
    }), flush=True)


def pdaf_test(cm, cam, sensor, _size):
    import ctypes
    is_imx363 = 'imx363' in cam.id
    is_s5k3m5 = 's5k3m5' in cam.id
    assert is_imx363 or is_s5k3m5, 'PDAF validation requires IMX363 or S5K3M5'
    pattern(sensor, 0)

    class QcomCamssPdafBuf(ctypes.Structure):
        _fields_ = [
            ('version', ctypes.c_uint32),
            ('sequence', ctypes.c_uint32),
            ('timestamp', ctypes.c_uint64),
            ('width', ctypes.c_uint32),
            ('height', ctypes.c_uint32),
            ('stride', ctypes.c_uint32),
            ('offset_y', ctypes.c_uint32),
            ('copy_lines', ctypes.c_uint32),
            ('reserved', ctypes.c_uint32),
            ('data', ctypes.c_uint64),
        ]

    # _IOWR('V', BASE_VIDIOC_PRIVATE + 3, sizeof(struct v4l2_qcom_camss_pdaf_buf))
    vidioc_get_pdaf = (3 << 30) | (ctypes.sizeof(QcomCamssPdafBuf) << 16) | (ord('V') << 8) | (192 + 3)
    vfe_nodes = [subprocess.check_output(['media-ctl', '-e', name], text=True).strip()
                 for name in ('msm_vfe0_video3', 'msm_vfe1_video3')]

    def active_vfe_video_node():
        topo = subprocess.check_output(['media-ctl', '-p'], text=True)
        for idx, name in enumerate(('msm_vfe0_pix', 'msm_vfe1_pix')):
            if f'-> "{name}":0 [ENABLED]' in topo:
                return vfe_nodes[idx]
        return vfe_nodes[0]

    def read_pdaf_slice(node, stride, offset_y, copy_lines):
        buf = (ctypes.c_uint8 * (stride * copy_lines))()
        req = QcomCamssPdafBuf(version=1, offset_y=offset_y, copy_lines=copy_lines,
                               data=ctypes.addressof(buf))
        fd = Path(node).open('rb+', buffering=0)
        try:
            fcntl.ioctl(fd.fileno(), vidioc_get_pdaf, req)
        finally:
            fd.close()
        return req, bytes(buf)

    mode_specs = (
        [((4032, 3024), (4032, 756, 5040)), ((2016, 1136), (2016, 284, 2528))]
        if is_imx363 else
        [((4032, 3024), (504, 752, 640)), ((2016, 1136), (504, 560, 640))]
    )
    mode_results = []
    for out_size, (exp_w, exp_h, exp_stride) in mode_specs:
        reads = []

        def probe_step(n, _):
            if n in (10, 16):
                node = active_vfe_video_node()
                req, data = read_pdaf_slice(node, exp_stride, (exp_h // 2 - 16) & ~7, 32)
                nonzero = sum(1 for b in data if b != 0)
                reads.append({
                    'seq': req.sequence,
                    'width': req.width,
                    'height': req.height,
                    'stride': req.stride,
                    'nonzero_ratio': round(nonzero / len(data), 4),
                })
            return {}

        capture(cm, cam, out_size, 20, {
            C.AeEnable: True, C.AwbEnable: True,
            C.AfMode: C.AfModeEnum.Manual, C.LensPosition: 0.0,
        }, probe_step)
        assert len(reads) == 2, reads
        for r in reads:
            assert (r['width'], r['height'], r['stride']) == (exp_w, exp_h, exp_stride), (out_size, r)
            assert r['nonzero_ratio'] > 0.90, (out_size, r)
        assert reads[1]['seq'] > reads[0]['seq'], (out_size, reads)
        mode_results.append({'size': f'{out_size[0]}x{out_size[1]}', 'reads': reads})

    # Verify fast hybrid PDAF + CDAF convergence from both macro and infinity endpoints.
    lens_max = cam.controls[C.LensPosition].max
    af_runs = []
    for label, start_pos in (('from_macro', lens_max), ('from_infinity', 0.0)):
        def af_step(n, _):
            if n == 18:
                return {C.AfMode: C.AfModeEnum.Auto, C.AfTrigger: C.AfTriggerEnum.Start}
            return {}

        rows = capture(cm, cam, (2016, 1136), 80, {
            C.AeEnable: True, C.AwbEnable: True,
            C.AfMode: C.AfModeEnum.Manual, C.LensPosition: float(start_pos),
        }, af_step)
        trig_ts = rows[18]['timestamp']
        first_focus = next((i for i in range(19, len(rows)) if rows[i]['af'] == 2), None)
        assert first_focus is not None, (label, [r['af'] for r in rows[18:]])
        converge_s = (rows[first_focus]['timestamp'] - trig_ts) / 1e9
        assert all(r['af'] == 2 for r in rows[first_focus:]), (label, [r['af'] for r in rows[first_focus:]])
        af_runs.append({
            'start': label,
            'start_lens': round(float(start_pos), 3),
            'final_lens': round(rows[-1]['lens'], 3),
            'converge_frames': first_focus - 18,
            'converge_s': round(converge_s, 3),
        })

    print(json.dumps({
        'test': 'pdaf',
        'camera': cam.id,
        'modes': mode_results,
        'hybrid_af': af_runs,
    }), flush=True)


def tof_af_test(cm, cam, sensor, _size):
    assert 'imx520' in cam.id, 'ToF-assisted AF validation requires IMX520'
    pattern(sensor, 0)
    out_size = (1632, 924)
    lens_max = cam.controls[C.LensPosition].max

    # 1. Verify ToF-assisted convergence from both macro and infinity endpoints.
    af_runs = []
    for label, start_pos in (('from_macro', lens_max), ('from_infinity', 0.0)):
        def af_step(n, _):
            if n == 18:
                return {C.AfMode: C.AfModeEnum.Auto, C.AfTrigger: C.AfTriggerEnum.Start}
            return {}

        rows = capture(cm, cam, out_size, 75, {
            C.AeEnable: True, C.AwbEnable: True,
            C.AfMode: C.AfModeEnum.Manual, C.LensPosition: float(start_pos),
        }, af_step)
        trig_ts = rows[18]['timestamp']
        first_focus = next((i for i in range(19, len(rows)) if rows[i]['af'] == 2), None)
        assert first_focus is not None, (label, [r['af'] for r in rows[18:]])
        converge_s = (rows[first_focus]['timestamp'] - trig_ts) / 1e9
        assert converge_s < 1.8, (label, converge_s, rows[18:first_focus + 1])
        assert all(r['af'] == 2 for r in rows[first_focus:]), (label, [r['af'] for r in rows[first_focus:]])
        final_fom = statistics.median(r['fom'] for r in rows[-8:] if r['fom'] is not None)
        af_runs.append({
            'start': label,
            'start_lens': round(float(start_pos), 3),
            'final_lens': round(rows[-1]['lens'], 3),
            'converge_frames': first_focus - 18,
            'converge_s': round(converge_s, 3),
            'final_fom': round(final_fom, 1),
            'exp': rows[-1]['exposure'],
            'gain': rows[-1]['gain'],
        })

    # 2. Verify converged FocusFoM against an 11-point manual focus sweep peak.
    comp_ctrls = {
        C.AeEnable: False, C.AwbEnable: False, C.AfMode: C.AfModeEnum.Manual,
        C.ExposureTime: af_runs[-1]['exp'], C.AnalogueGain: af_runs[-1]['gain'],
    }
    sweep = []
    for i in range(11):
        pos = i * lens_max / 10
        srows = capture(cm, cam, out_size, 20, {**comp_ctrls, C.LensPosition: pos})
        fom = statistics.median(r['fom'] for r in srows[-8:] if r['fom'] is not None)
        sweep.append({'lens': round(pos, 3), 'fom': round(fom, 1)})
    peak = max(sweep, key=lambda r: r['fom'])
    for run in af_runs:
        assert run['final_fom'] >= peak['fom'] * 0.90, (run, peak, sweep)

    # 3. Verify concurrent imx520 (AfModeAuto) + irs2381c (R16 depth) operation.
    tof_cam = next(c for c in cm.cameras if 'irs2381c' in c.id)
    tof_cam.acquire()
    concurrent_summary = {}
    try:
        cfg_i = cam.generate_configuration([lc.StreamRole.Viewfinder])
        cfg_t = tof_cam.generate_configuration([lc.StreamRole.Viewfinder])
        cfg_i.at(0).size = lc.Size(*out_size)
        cfg_t.at(0).size = lc.Size(224, 172)
        cfg_t.at(0).pixel_format = lc.formats.R16
        assert cfg_i.validate() != lc.CameraConfiguration.Status.Invalid
        assert cfg_t.validate() != lc.CameraConfiguration.Status.Invalid
        cam.configure(cfg_i)
        tof_cam.configure(cfg_t)

        st_i, st_t = cfg_i.at(0).stream, cfg_t.at(0).stream
        alloc_i, alloc_t = lc.FrameBufferAllocator(cam), lc.FrameBufferAllocator(tof_cam)
        assert alloc_i.allocate(st_i) >= 4
        assert alloc_t.allocate(st_t) >= 4
        reqs_i = [cam.create_request() for _ in alloc_i.buffers(st_i)]
        for r, fb in zip(reqs_i, alloc_i.buffers(st_i)):
            r.add_buffer(st_i, fb)
        reqs_t = [tof_cam.create_request() for _ in alloc_t.buffers(st_t)]
        for r, fb in zip(reqs_t, alloc_t.buffers(st_t)):
            r.add_buffer(st_t, fb)

        with ExitStack() as cleanup:
            sel = cleanup.enter_context(selectors.DefaultSelector())
            sel.register(cm.event_fd, selectors.EVENT_READ)
            cam.start({C.AeEnable: True, C.AwbEnable: True,
                       C.AfMode: C.AfModeEnum.Manual, C.LensPosition: float(lens_max)})
            cleanup.callback(cm.get_ready_requests)
            cleanup.callback(cam.stop)
            tof_cam.start()
            cleanup.callback(tof_cam.stop)

            for r in reqs_i:
                cam.queue_request(r)
            for r in reqs_t:
                tof_cam.queue_request(r)

            got_i = got_t = 0
            q_i, q_t = len(reqs_i), len(reqs_t)
            target_i, target_t = 65, 25
            triggered = False
            af_states = []
            deadline = time.monotonic() + 15
            while got_i < target_i or got_t < target_t:
                assert time.monotonic() < deadline, (got_i, got_t, af_states[-10:])
                if not sel.select(1):
                    continue
                for r in cm.get_ready_requests():
                    if st_i in r.buffers:
                        assert r.buffers[st_i].metadata.status == lc.FrameMetadata.Status.Success
                        got_i += 1
                        af_states.append((got_i, r.metadata.get(C.AfState),
                                          round(float(r.metadata.get(C.LensPosition, 0.0)), 3)))
                        if q_i < target_i:
                            r.reuse()
                            if not triggered and got_i == 15:
                                r.set_control(C.AfMode, C.AfModeEnum.Auto)
                                r.set_control(C.AfTrigger, C.AfTriggerEnum.Start)
                                triggered = True
                            cam.queue_request(r)
                            q_i += 1
                    elif st_t in r.buffers:
                        assert r.buffers[st_t].metadata.status == lc.FrameMetadata.Status.Success
                        got_t += 1
                        if q_t < target_t:
                            r.reuse()
                            tof_cam.queue_request(r)
                            q_t += 1
            assert any(s == 2 for _, s, _ in af_states[16:]), af_states[15:]
            concurrent_summary = {
                'imx520_frames': got_i,
                'tof_frames': got_t,
                'final_af_state': af_states[-1][1],
                'final_lens': af_states[-1][2],
            }
    finally:
        tof_cam.release()

    # 4. Verify IRS2381C returns to runtime-suspended after ToF assist + concurrent capture.
    tof_status_path = Path('/sys/bus/i2c/devices/2-003d/power/runtime_status')
    if tof_status_path.exists():
        deadline = time.monotonic() + 3.0
        status = tof_status_path.read_text().strip()
        while time.monotonic() < deadline:
            status = tof_status_path.read_text().strip()
            if status == 'suspended':
                break
            time.sleep(0.05)
        assert status == 'suspended', f'irs2381c runtime_status={status}'

    print(json.dumps({
        'test': 'tof_af',
        'camera': cam.id,
        'runs': af_runs,
        'manual_peak': peak,
        'concurrent': concurrent_summary,
    }), flush=True)


def timestamp_audit(cm, cam, sensor, size):
    target_size = (1632, 924) if 'imx520' in cam.id and size == (2016, 1136) else size
    dur_us = 33333
    exp0_us = 2000
    exp_max_ctrl = cam.controls[C.ExposureTime].max
    exp1_us = min(22000, int(exp_max_ctrl))
    assert exp1_us - exp0_us >= 10000, (cam.id, exp0_us, exp1_us)

    # 1. PIX exposure-step shift check: FrameDuration remains ~33.3 ms while
    # SensorTimestamp shifts earlier by (exp1 - exp0) relative to phase-0 SOF lattice.
    phase_len = 16
    p1_ctrl = {C.AeEnable: False, C.ExposureTime: exp1_us,
               C.AnalogueGain: 2.0, C.FrameDurationLimits: (dur_us, dur_us),
               **manual_focus_controls(cam, 0.0)}
    rows = capture(
        cm, cam, target_size, phase_len * 2,
        {C.AeEnable: False, C.ExposureTime: exp0_us,
         C.AnalogueGain: 2.0, C.FrameDurationLimits: (dur_us, dur_us),
         **manual_focus_controls(cam, 0.0)},
        request_controls=(lambda idx: {} if idx < phase_len else p1_ctrl),
    )
    assert all(b['sensor_timestamp'] > a['sensor_timestamp'] for a, b in zip(rows, rows[1:])), (
        cam.id, [r['sensor_timestamp'] for r in rows])

    p0 = rows[4:phase_len]
    p1 = rows[phase_len + 6:]
    for seg in (p0, p1):
        fd = statistics.median(r['frame_duration'] for r in seg)
        assert abs(fd - dur_us) <= dur_us * 0.02, (cam.id, fd, dur_us)
    # Sum individual steady-state FrameDurations between ref and r rather than multiplying
    # by a short-window median period (which can absorb ~500us Linux IRQ jitter x N frames).
    ref = p0[-1]
    cum_fd_us = {}
    acc = 0
    for prev_r, cur_r in zip(rows[phase_len - 1:], rows[phase_len:]):
        acc += prev_r['frame_duration'] * (cur_r['frame'] - prev_r['frame'])
        cum_fd_us[cur_r['frame']] = acc
    shifts_us = [
        ((r['sensor_timestamp'] - ref['sensor_timestamp']) / 1000.0) - cum_fd_us[r['frame']]
        for r in p1
    ]
    shift_med_us = statistics.median(shifts_us)
    expected_shift_us = -(p1[-1]['exposure'] - p0[-1]['exposure'])
    assert abs(shift_med_us - expected_shift_us) <= 100.0, (
        cam.id, shift_med_us, expected_shift_us, p0[-1]['exposure'], p1[-1]['exposure'])

    # 2. RAW-only exposure+readout compensation check relative to completion time.
    modes_info = frame_duration_modes(cam)
    raw_info = modes_info[0]
    raw_size = raw_info['sensor_size']
    raw_exp_us = 8000
    line_us = raw_info['hts'] * 1e6 / raw_info['pixel_rate']
    expected_readout_us = raw_size[1] * line_us

    raw_cfg = cam.generate_configuration([lc.StreamRole.Raw])
    assert raw_cfg is not None and raw_cfg.validate() != lc.CameraConfiguration.Status.Invalid
    cam.configure(raw_cfg)
    raw_stream = raw_cfg.at(0).stream
    alloc = lc.FrameBufferAllocator(cam)
    assert alloc.allocate(raw_stream) >= 4
    reqs = []
    for fb in alloc.buffers(raw_stream):
        r = cam.create_request()
        r.add_buffer(raw_stream, fb)
        reqs.append(r)

    raw_rows = []
    with ExitStack() as cleanup:
        sel = cleanup.enter_context(selectors.DefaultSelector())
        sel.register(cm.event_fd, selectors.EVENT_READ)
        cam.start({C.AeEnable: False, C.ExposureTime: raw_exp_us, C.AnalogueGain: 2.0,
                   C.FrameDurationLimits: (dur_us, dur_us), **manual_focus_controls(cam, 0.0)})
        cleanup.callback(cm.get_ready_requests)
        cleanup.callback(cam.stop)
        for r in reqs:
            cam.queue_request(r)
        queued = len(reqs)
        deadline = time.monotonic() + 8.0
        while len(raw_rows) < 14:
            assert time.monotonic() < deadline, ('RAW timestamp capture timed out', len(raw_rows))
            if not sel.select(1):
                continue
            for r in cm.get_ready_requests():
                t_done_ns = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
                fb = r.buffers[raw_stream]
                assert fb.metadata.status == lc.FrameMetadata.Status.Success
                ts = r.metadata.get(C.SensorTimestamp)
                assert ts is not None
                raw_rows.append({'frame': fb.metadata.sequence, 'sensor_timestamp': ts,
                                 'done_ns': t_done_ns, 'exposure': r.metadata.get(C.ExposureTime)})
                if queued < 14:
                    r.reuse()
                    cam.queue_request(r)
                    queued += 1

    assert all(b['sensor_timestamp'] > a['sensor_timestamp'] for a, b in zip(raw_rows, raw_rows[1:])), (
        cam.id, raw_rows)
    raw_seg = raw_rows[4:]
    raw_cadence_us = statistics.median(
        (b['sensor_timestamp'] - a['sensor_timestamp']) / 1000.0
        for a, b in zip(raw_seg, raw_seg[1:]) if b['frame'] == a['frame'] + 1
    )
    assert abs(raw_cadence_us - dur_us) <= dur_us * 0.03, (cam.id, raw_cadence_us, dur_us)
    done_lag_us = statistics.median((r['done_ns'] - r['sensor_timestamp']) / 1000.0 for r in raw_seg)
    min_expected_lag_us = expected_readout_us + raw_exp_us - 2500.0
    assert min_expected_lag_us <= done_lag_us < 120000.0, (
        cam.id, done_lag_us, min_expected_lag_us, expected_readout_us)

    print(json.dumps({
        'test': 'timestamp',
        'camera': cam.id,
        'pix_shift_us': round(shift_med_us, 1),
        'expected_shift_us': expected_shift_us,
        'raw_size': raw_size,
        'raw_cadence_us': round(raw_cadence_us, 1),
        'raw_done_minus_ts_us': round(done_lag_us, 1),
        'expected_readout_plus_exp_us': round(expected_readout_us + raw_exp_us, 1),
    }), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', help='AF scene tests require a fixed, textured target',
                        choices=['all', 'modes', 'colours', 'controls', 'exposure',
                                 'ae', 'ae-limit', 'ae-modes', 'ev', 'awb', 'awb-native',
                                 'awb-requests', 'awb-mode', 'lux', 'colour-temperature',
                                 'sensor-requests', 'gain', '3a', 'switch', 'power',
                                 'interleaved-configure', 'frame-duration',
                                 'frame-rate-requests', 'flicker', 'flicker-auto',
                                 'extended-modes', 'timestamp',
                                 'bf-roi', 'raw-dual', 'dual-camera',
                                 'lsc', 'colorspace', 'dual-ife', 'pdaf', 'tof-af',
                                 'lens-positions', 'scaler-crop',
                                 'af', 'af-scene', 'af-controls', 'af-converge',
                                 'af-converge-auto',
                                 'af-travel'], default='all')
    parser.add_argument('--size', help='Output size for control and AF tests')
    parser.add_argument('--sensor', choices=['imx363', 'imx351', 'imx520',
                                             's5k3m5', 'all'],
                        help='Camera sensor model (all for the common suite, S5K3M5 for a selected case)')
    args = parser.parse_args()
    default_size = '640x480' if args.case == 'scaler-crop' else '2016x1136'
    size = tuple(map(int, (args.size or default_size).split('x')))
    cm = lc.CameraManager.singleton()
    if args.case == 'interleaved-configure':
        interleaved_configure(cm)
        return
    if args.case == 'dual-camera':
        dual_camera(cm, size)
        print('PASS: native Qcom pipeline hardware validation', flush=True)
        return
    if args.case in ('switch', 'power'):
        test = camera_switch if args.case == 'switch' else camera_power
        test(cm, size)
        print('PASS: native Qcom pipeline hardware validation', flush=True)
        return
    cameras = ('imx363', 'imx351', 's5k3m5', 'imx520')
    selected = args.sensor or ('all' if args.case in ('all', 'extended-modes', 'timestamp') else
                               'imx351' if args.case == 'dual-ife' else
                               'imx363' if args.case == 'pdaf' else
                               'imx520' if args.case == 'tof-af' else 's5k3m5')
    models = cameras if selected == 'all' else (selected,)
    tests = {
        'modes': modes, 'colours': colours, 'controls': controls, 'exposure': exposure,
        'ae': auto_exposure, 'ae-limit': auto_exposure_limit, 'ev': exposure_value,
        'ae-modes': ae_modes,
        'awb': auto_white_balance, 'awb-native': native_white_balance,
        'awb-requests': white_balance_requests, 'awb-mode': awb_mode,
        'lux': lux_metadata, 'colour-temperature': colour_temperature_requests,
        'sensor-requests': sensor_requests, 'frame-duration': frame_duration_audit,
        'scaler-crop': scaler_crop_requests,
        'frame-rate-requests': frame_rate_requests, 'extended-modes': extended_modes,
        'flicker': flicker_metering,
        'flicker-auto': flicker_auto, 'bf-roi': bf_roi, 'raw-dual': raw_dual, 'timestamp': timestamp_audit,
        'lsc': lsc_isp, 'colorspace': colorspace_test, 'dual-ife': dual_ife_test,
        'pdaf': pdaf_test, 'tof-af': tof_af_test,
        'gain': gain, '3a': three_a, 'lens-positions': lens_positions, 'af': autofocus,
        'af-scene': af_scene, 'af-controls': af_controls, 'af-converge': af_converge,
        'af-converge-auto': af_converge_auto, 'af-travel': af_travel,
    }
    common = ('modes', 'colours', 'controls', 'exposure', 'ae', 'ae-modes', 'awb', 'gain', 'af-controls', 'timestamp')
    chosen = common if args.case == 'all' else (args.case,)
    for model in models:
        cam = next(c for c in cm.cameras if model in c.id)
        sensor = subprocess.check_output(['media-ctl', '-e', cam.id], text=True).strip()
        patterns = C.draft.TestPatternMode in cam.controls
        cam.acquire()
        try:
            for name in chosen:
                skip = (
                    name in ('colours', 'controls', 'awb-requests', 'scaler-crop', 'af-controls') and not patterns
                    or (name.startswith('af') or name == 'bf-roi') and not has_autofocus(cam)
                    or name == 'lens-positions' and not has_autofocus(cam)
                    or name == 'extended-modes' and model == 'imx520'
                    or name == 'awb-native' and model != 'imx520'
                    or name == 'dual-ife' and model != 'imx351'
                    or name == 'pdaf' and model not in ('imx363', 's5k3m5')
                    or name == 'tof-af' and model != 'imx520'
                )
                if skip:
                    if selected != 'all' and args.case != 'all':
                        raise RuntimeError(f"{name} is unsupported by {model}")
                    print(f"SKIP {model} {name}: required camera capability unavailable", flush=True)
                    continue
                test = tests[name]
                if name in ('modes', 'frame-duration'):
                    test(cm, cam, sensor)
                else:
                    test(cm, cam, sensor, size)
        finally:
            pattern(sensor, 0)
            cam.release()
    print('PASS: native Qcom pipeline hardware validation', flush=True)


if __name__ == '__main__':
    main()
