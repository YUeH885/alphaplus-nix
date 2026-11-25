#!/usr/bin/env python3
"""Validate Iris decoding against software decoding of the same input."""

import argparse
import json
from pathlib import Path
import subprocess

from vpu_validation import (DECODERS, compare_frames, probe_input, query_iris_device, raw_format,
                            run, sha256, validate_device, validate_element)


def compact_planar_frames(source, output, width, height, frames, bits):
    sample_size = 2 if bits == 10 else 1
    rows = (height, height // 2, height // 2)
    widths = (width * sample_size, width // 2 * sample_size, width // 2 * sample_size)
    # GstVideoInfo aligns planar I420 rows to four bytes and height to two lines.
    strides = tuple((row_bytes + 3) & ~3 for row_bytes in widths)
    expected_size = sum(stride * count for stride, count in zip(strides, rows)) * frames
    if source.stat().st_size != expected_size:
        raise ValueError(f'Unexpected GStreamer planar size: {source.stat().st_size}, '
                         f'expected {expected_size}')
    with source.open('rb') as incoming, output.open('xb') as outgoing:
        for _ in range(frames):
            for row_bytes, stride, count in zip(widths, strides, rows):
                plane = incoming.read(stride * count)
                if len(plane) != stride * count:
                    raise ValueError('Incomplete GStreamer planar frame')
                if row_bytes == stride:
                    outgoing.write(plane)
                else:
                    for row in range(count):
                        outgoing.write(plane[row * stride:row * stride + row_bytes])


def decode_gstreamer(args, stream, decoded, hardware_log):
    elements = {'h264': 'v4l2h264dec', 'hevc': 'v4l2h265dec', 'vp9': 'v4l2vp9dec'}
    element = elements[stream['codec_name']]
    description = subprocess.run(['gst-inspect-1.0', element], capture_output=True,
                                 text=True, timeout=args.timeout, check=True)
    validate_element(description.stdout, args.device)
    pixel_format, bits = raw_format(stream)
    capture_format = 'P010_10LE' if bits == 10 else 'NV12'
    planar_format = 'I420_10LE' if bits == 10 else 'I420'
    intermediate = args.output.resolve() / 'hardware.i420'
    compact = args.output.resolve() / 'compact.i420'
    # A format conversion copies GstVideoMeta stride and offsets into a standard planar pool.
    run(['gst-launch-1.0', '-v', 'filesrc', f'location={args.input.resolve()}', '!',
         'parsebin', '!', element, 'capture-io-mode=mmap', '!',
         f'video/x-raw,format={capture_format}', '!', 'videoconvert', 'dither=none', '!',
         f'video/x-raw,format={planar_format}', '!', 'filesink', f'location={intermediate}'],
        hardware_log, args.timeout)
    compact_planar_frames(intermediate, compact, stream['width'], stream['height'],
                          int(stream['nb_read_frames']), bits)
    intermediate.unlink()
    run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
         '-f', 'rawvideo', '-pixel_format', 'yuv420p10le' if bits == 10 else 'yuv420p',
         '-video_size', f"{stream['width']}x{stream['height']}",
         '-framerate', stream['r_frame_rate'], '-i', str(compact),
         '-map', '0:v:0', '-an', '-fps_mode', 'passthrough',
         '-pix_fmt', pixel_format, '-f', 'rawvideo', '-n', str(decoded)],
        args.output / 'normalize.log', args.timeout)
    compact.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path, help='new directory for output, logs and report')
    parser.add_argument('--device', default='/dev/video0',
                        help='required streaming Iris decoder node')
    parser.add_argument('--backend', choices=('ffmpeg', 'ffmpeg-drm', 'gstreamer'), default='ffmpeg')
    parser.add_argument('--dmabuf-helper', type=Path,
                        default=Path(__file__).with_name('test-vpu-dmabuf'),
                        help='compiled DRM PRIME download validator')
    parser.add_argument('--frames', type=int, help='required frame count, checked with ffprobe')
    parser.add_argument('--timeout', type=int, default=120)
    parser.add_argument('--max-difference', type=int, default=0,
                        help='per-sample tolerance; default requires identical visible pixels')
    args = parser.parse_args()
    if (args.timeout <= 0 or not 0 <= args.max_difference <= 1023 or
            (args.frames is not None and args.frames <= 0)):
        parser.error('Timeout and frame count must be positive; tolerance must be between 0 and 1023')
    report = {'status': 'failed', 'device': args.device}
    created_output = False
    try:
        stream, expected_size = probe_input(args.input.resolve(), args.timeout)
        if args.frames is not None and args.frames != int(stream['nb_read_frames']):
            raise ValueError(f"Input frame count is {stream['nb_read_frames']}, expected {args.frames}")
        args.output.mkdir(parents=True)
        created_output = True
        report.update({'input_sha256': sha256(args.input), 'stream': stream,
                       'max_difference': args.max_difference})
        pixel_format, bits = raw_format(stream)
        if args.max_difference >= 1 << bits:
            raise ValueError('Tolerance exceeds the input sample range')
        report.update({'hardware': query_iris_device(args.device, 'Decoder'),
                       'backend': args.backend, 'output_format': pixel_format, 'sample_bits': bits})
        suffix = 'p010' if bits == 10 else 'nv12'
        reference = args.output / f'reference.{suffix}'
        decoded = args.output / f'decoded.{suffix}'
        hardware_log = args.output / 'hardware.log'
        run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
             '-c:v', stream['codec_name'], '-i', str(args.input.resolve()), '-map', '0:v:0',
             '-an', '-fps_mode', 'passthrough', '-pix_fmt', pixel_format,
             '-f', 'rawvideo', '-n', str(reference)], args.output / 'software.log', args.timeout)
        if args.backend == 'gstreamer':
            decode_gstreamer(args, stream, decoded, hardware_log)
        elif args.backend == 'ffmpeg-drm':
            run([str(args.dmabuf_helper.resolve()), str(args.input.resolve()), str(decoded)],
                hardware_log, args.timeout)
        else:
            run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'debug',
                 '-c:v', DECODERS[stream['codec_name']], '-i', str(args.input.resolve()),
                 '-map', '0:v:0', '-an', '-fps_mode', 'passthrough', '-pix_fmt', pixel_format,
                 '-f', 'rawvideo', '-n', str(decoded)], hardware_log, args.timeout)
        if args.backend != 'gstreamer':
            validate_device(hardware_log, args.device, 'Decoder')
        metrics, passed = compare_frames(reference, decoded, expected_size, args.max_difference,
                                         sample_bits=bits)
        report.update(metrics)
        if not passed:
            raise ValueError(f"Pixel difference {metrics['maximum_difference']} exceeds {args.max_difference}")
        report['status'] = 'passed'
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        report['error'] = str(error)
    if created_output:
        (args.output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
