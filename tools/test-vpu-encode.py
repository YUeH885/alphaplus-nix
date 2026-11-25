#!/usr/bin/env python3
"""Validate Iris encoding, frame layout, GOP and software-decoded image quality."""

import argparse
import fcntl
import os
import struct
from fractions import Fraction
import json
import math
from pathlib import Path
import re
import subprocess

from vpu_validation import (compare_frames, probe_input, query_iris_device, run, sha256,
                            validate_element)


def validate_stream(stream, frames, codec, width, height, fps, gop):
    if not frames:
        raise ValueError('Expected nonempty encoded frames')
    if (stream['codec_name'], stream['width'], stream['height'], int(stream['nb_read_frames'])) != (
            codec, width, height, len(frames)):
        raise ValueError(f'Unexpected encoded stream: {stream}')
    if Fraction(stream['r_frame_rate']) != fps:
        raise ValueError(f"Unexpected frame rate: {stream['r_frame_rate']}, expected {fps}")
    intra = [index for index, frame in enumerate(frames) if frame['pict_type'] == 'I']
    expected_intra = list(range(0, len(frames), gop))
    if intra != expected_intra or not frames[0]['key_frame']:
        raise ValueError(f'Unexpected intra frame positions: {intra}, expected {expected_intra}')
    timestamps = [Fraction(str(frame['best_effort_timestamp_time'])) for frame in frames]
    if any(abs((actual - timestamps[0]) - Fraction(index, fps)) > Fraction(1, 1000)
           for index, actual in enumerate(timestamps)):
        raise ValueError('Unexpected presentation timestamps')
    return {'intra_frame_indices': intra,
            'key_frame_indices': [i for i, frame in enumerate(frames) if frame['key_frame']],
            'first_timestamp': float(timestamps[0]), 'last_timestamp': float(timestamps[-1])}


def validate_b_frames(frames, requested):
    runs, current = [], 0
    for frame in frames:
        if frame['pict_type'] == 'B':
            current += 1
        elif current:
            runs.append(current)
            current = 0
    if current:
        runs.append(current)
    maximum = max(runs, default=0)
    if maximum != requested:
        raise ValueError(f'Unexpected consecutive B frames: {maximum}, expected {requested}')
    return {'b_frame_count': sum(runs), 'maximum_consecutive_b_frames': maximum}


def query_b_frame_limit(device):
    control = bytearray(68)
    struct.pack_into('I', control, 0, 0x009909ca)  # V4L2_CID_MPEG_VIDEO_B_FRAMES
    fd = os.open(device, os.O_RDWR | os.O_NONBLOCK)
    try:
        fcntl.ioctl(fd, 0xc0445624, control)  # VIDIOC_QUERYCTRL
    finally:
        os.close(fd)
    return struct.unpack_from('ii', control, 40)


def validate_quality(metrics, minimum):
    if metrics['psnr_db'] is not None and metrics['psnr_db'] < minimum:
        raise ValueError(f"PSNR {metrics['psnr_db']:.2f} dB is below {minimum:.2f} dB")


def validate_h264_timing(log, fps):
    text = log.read_text()
    ticks = re.findall(r'num_units_in_tick\s+\S+ = (\d+)', text)
    scales = re.findall(r'\btime_scale\s+\S+ = (\d+)', text)
    if not ticks or len(ticks) != len(scales):
        raise ValueError('Missing H.264 VUI timing fields')
    if any(not int(tick) or Fraction(int(scale), 2 * int(tick)) != fps
           for tick, scale in zip(ticks, scales, strict=True)):
        raise ValueError(f'Unexpected H.264 VUI timing: scale={scales}, ticks={ticks}, expected {fps}')
    return {'h264_time_scale': int(scales[0]), 'h264_num_units_in_tick': int(ticks[0])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path, help='new directory for artifacts and report')
    parser.add_argument('--pixel-format', choices=('nv12', 'p010le'), default='nv12')
    parser.add_argument('--b-frames', type=int, default=0)
    parser.add_argument('--codec', choices=('h264', 'hevc'), default='h264')
    parser.add_argument('--width', type=int, default=320)
    parser.add_argument('--height', type=int, default=240)
    parser.add_argument('--frames', type=int, default=60)
    parser.add_argument('--fps', type=int, default=30)
    parser.add_argument('--bitrate', type=int, default=2000000)
    parser.add_argument('--bitrate-mode', choices=('vbr', 'cbr'), default='vbr')
    parser.add_argument('--gop', type=int, default=30)
    parser.add_argument('--profile', help='GStreamer codec profile, e.g. high or main')
    parser.add_argument('--level', help='explicit codec level, e.g. 3.1')
    parser.add_argument('--device', default='/dev/video1')
    parser.add_argument('--timeout', type=int, default=120)
    parser.add_argument('--min-psnr', type=float, default=30)
    args = parser.parse_args()
    if min(args.width, args.height, args.frames, args.fps, args.bitrate, args.gop, args.timeout) <= 0:
        parser.error('Dimensions, frame count, frame rate, bitrate, GOP and timeout must be positive')
    if args.width % 2 or args.height % 2 or not math.isfinite(args.min_psnr) or args.min_psnr < 0:
        parser.error('Dimensions must be even and minimum PSNR must be finite and nonnegative')
    if args.b_frames < 0:
        parser.error('B-frame count must be nonnegative')
    if args.b_frames and (args.gop - 1) % (args.b_frames + 1):
        parser.error('GOP minus one must be a multiple of B-frame count plus one')
    if args.pixel_format == 'p010le' and args.codec != 'hevc':
        parser.error('P010 input requires HEVC')
    report = {'status': 'failed', 'parameters': vars(args) | {'output': str(args.output)}}
    created_output = False
    try:
        report['hardware'] = query_iris_device(args.device, 'Encoder')
        minimum_b, maximum_b = query_b_frame_limit(args.device)
        report['b_frame_limits'] = [minimum_b, maximum_b]
        if not minimum_b <= args.b_frames <= maximum_b:
            raise ValueError(f'B-frame count must be between {minimum_b} and {maximum_b}')
        element = 'v4l2h264enc' if args.codec == 'h264' else 'v4l2h265enc'
        description = subprocess.run(['gst-inspect-1.0', element], capture_output=True,
                                     text=True, timeout=args.timeout, check=True)
        validate_element(description.stdout, args.device)
        args.output.mkdir(parents=True)
        created_output = True
        reference = args.output.resolve() / ('reference.' + args.pixel_format)
        encoded = args.output.resolve() / 'encoded.mkv'
        decoded = args.output.resolve() / ('decoded.' + args.pixel_format)
        mode = int(args.bitrate_mode == 'cbr')
        caps = 'video/x-h264' if args.codec == 'h264' else 'video/x-h265'
        if args.profile:
            caps += f',profile=(string){args.profile}'
        if args.level:
            caps += f',level=(string){args.level}'
        commands = [
            ['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
             f'testsrc2=size={args.width}x{args.height}:rate={args.fps}', '-frames:v',
             str(args.frames), '-pix_fmt', args.pixel_format, '-f', 'rawvideo', '-n', str(reference)],
            ['gst-launch-1.0', '-e', '-v', 'filesrc', f'location={reference}', '!',
             'rawvideoparse', ('format=p010-10le' if args.pixel_format == 'p010le' else 'format=nv12'), f'width={args.width}', f'height={args.height}',
             f'framerate={args.fps}/1', '!', element,
             f'extra-controls=controls,video_bitrate={args.bitrate},video_bitrate_mode={mode},'
             f'video_gop_size={args.gop},video_b_frames={args.b_frames}', '!', caps, '!',
             *([('h264timestamper' if args.codec == 'h264' else 'h265timestamper'), '!']
              if args.b_frames else []),
             'h264parse' if args.codec == 'h264' else 'h265parse', '!', 'matroskamux', '!',
             'filesink', f'location={encoded}'],
            ['ffmpeg', '-nostdin', '-v', 'error', '-c:v', args.codec, '-i', str(encoded),
             '-fps_mode', 'passthrough', '-pix_fmt', args.pixel_format, '-f', 'rawvideo', '-n', str(decoded)],
        ]
        report['commands'] = commands
        names = ['reference', 'hardware', 'software']
        if args.codec == 'h264':
            commands.append(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'verbose',
                             '-i', str(encoded), '-c:v', 'copy', '-bsf:v', 'trace_headers',
                             '-frames:v', '1', '-f', 'null', '-'])
            names.append('timing')
        for command, name in zip(commands, names, strict=True):
            run(command, args.output / f'{name}.log', args.timeout)
        if args.codec == 'h264':
            report.update(validate_h264_timing(args.output / 'timing.log', args.fps))
        stream, expected_size = probe_input(encoded, args.timeout)
        report['stream'] = stream
        if int(stream['nb_read_frames']) != args.frames:
            raise ValueError(f"Encoded {stream['nb_read_frames']} frames, expected {args.frames}")
        probe = subprocess.run([
            'ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_frames', '-show_entries',
            'frame=key_frame,pict_type,best_effort_timestamp_time', '-of', 'json', str(encoded),
        ], capture_output=True, text=True, timeout=args.timeout, check=True)
        frames = json.loads(probe.stdout)['frames']
        report.update(validate_stream(stream, frames, args.codec,
                                      args.width, args.height, args.fps, args.gop))
        report.update(validate_b_frames(frames, args.b_frames))
        if args.profile:
            profiles = {'baseline': 'Baseline', 'constrained-baseline': 'Constrained Baseline',
                        'main': 'Main', 'high': 'High', 'main-still-picture': 'Main Still Picture',
                        'main-10': 'Main 10'}
            if stream['profile'] != profiles[args.profile]:
                raise ValueError(f"Unexpected profile: {stream['profile']}, expected {args.profile}")
        if args.level and stream['level'] != int(Fraction(args.level) * (10 if args.codec == 'h264' else 30)):
            raise ValueError(f"Unexpected level: {stream['level']}, expected {args.level}")
        metrics, _ = compare_frames(reference, decoded, expected_size,
                                    1023 if args.pixel_format == 'p010le' else 255,
                                    sample_bits=10 if args.pixel_format == 'p010le' else 8)
        report.update(metrics)
        report['encoded_sha256'] = sha256(encoded)
        report['encoded_bytes'] = encoded.stat().st_size
        report['container_bitrate'] = encoded.stat().st_size * 8 * args.fps / args.frames
        validate_quality(metrics, args.min_psnr)
        report['status'] = 'passed'
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        report['error'] = str(error)
    if created_output:
        (args.output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
