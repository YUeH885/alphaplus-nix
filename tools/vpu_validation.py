"""Shared validation of Iris video artifacts and hardware selection."""

from array import array
import fcntl
import hashlib
import json
import math
import os
import re
import subprocess
import struct
import sys


DECODERS = {'h264': 'h264_v4l2m2m', 'hevc': 'hevc_v4l2m2m', 'vp9': 'vp9_v4l2m2m'}


def query_iris_device(device, card):
    capability = bytearray(104)
    fd = os.open(device, os.O_RDWR | os.O_NONBLOCK)
    try:
        fcntl.ioctl(fd, 0x80685600, capability)
    finally:
        os.close(fd)
    driver, actual_card, bus, version, caps, device_caps = struct.unpack_from(
        '16s32s32sIII', capability)
    driver, actual_card, bus = (field.split(b'\0', 1)[0].decode()
                               for field in (driver, actual_card, bus))
    if (driver != 'iris_driver' or actual_card != f'Iris {card}' or
            device_caps & 0x04004000 != 0x04004000):
        raise ValueError(f'Expected streaming Iris {card.lower()} at {device}, '
                         f'got {driver}/{actual_card}')
    return {'driver': driver, 'card': actual_card, 'bus': bus, 'version': version,
            'capabilities': caps, 'device_capabilities': device_caps}


def validate_element(description, device):
    match = re.search(r'^\s+device\s+:.*\n\s+flags:.*\n\s+String\. Default: "([^"\n]+)"',
                      description, re.MULTILINE)
    if not match or match[1] != device:
        raise ValueError(f'GStreamer element is not bound to {device}')


def raw_format(stream):
    formats = {'yuv420p': ('nv12', 8), 'yuvj420p': ('nv12', 8),
               'yuv420p10le': ('p010le', 10)}
    try:
        return formats[stream['pix_fmt']]
    except KeyError as error:
        raise ValueError(f"Expected 8-bit or 10-bit 4:2:0 input, got {stream['pix_fmt']}") from error


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def run(command, log, timeout):
    with log.open('wb') as output:
        try:
            result = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT,
                                    timeout=timeout, check=False)
        except subprocess.TimeoutExpired as error:
            raise ValueError(f'Command timed out after {timeout}s; see {log}') from error
    if result.returncode:
        raise ValueError(f'Command exited with {result.returncode}; see {log}')


def probe_input(path, timeout):
    result = subprocess.run([
        'ffprobe', '-v', 'error', '-select_streams', 'v:0', '-count_frames',
        '-show_entries', 'stream=codec_name,profile,level,width,height,pix_fmt,r_frame_rate,nb_read_frames',
        '-of', 'json', str(path),
    ], capture_output=True, text=True, timeout=timeout, check=True)
    streams = json.loads(result.stdout)['streams']
    if len(streams) != 1:
        raise ValueError('Expected one video stream')
    stream = streams[0]
    codec = stream['codec_name']
    width, height = stream['width'], stream['height']
    frames = int(stream['nb_read_frames'])
    if codec not in DECODERS:
        raise ValueError(f'Unsupported codec: {codec}')
    _, bits = raw_format(stream)
    if width <= 0 or height <= 0 or width % 2 or height % 2 or frames <= 0:
        raise ValueError('Expected nonempty video with even dimensions')
    return stream, width * height * 3 // 2 * frames * (2 if bits == 10 else 1)


def compare_frames(reference, decoded, expected_size, max_difference=0, *, sample_bits=8):
    if sample_bits not in (8, 10) or (sample_bits == 10 and expected_size % 2):
        raise ValueError('Expected 8-bit samples or complete P010 samples')
    for path in (reference, decoded):
        size = path.stat().st_size
        if size != expected_size or not size:
            raise ValueError(f'Unexpected decoded size: {path}: {size}, expected {expected_size}')
    reference_hash, decoded_hash = sha256(reference), sha256(decoded)
    maximum, squared_error, changed = 0, 0, 0
    matching = reference_hash == decoded_hash
    if not matching or sample_bits == 10:
        valid_p010_low_bytes = bytes(range(0, 256, 64))
        with reference.open('rb') as left, decoded.open('rb') as right:
            while chunk := left.read(1024 * 1024):
                actual_chunk = chunk if matching else right.read(len(chunk))
                if sample_bits == 10:
                    # Each little-endian low byte has two sample bits and six padding bits.
                    if chunk[::2].translate(None, valid_p010_low_bytes) or (
                            not matching and actual_chunk[::2].translate(None, valid_p010_low_bytes)):
                        raise ValueError('P010 samples must have zero low six bits')
                    if matching:
                        continue
                    original_samples, actual_samples = array('H'), array('H')
                    original_samples.frombytes(chunk)
                    actual_samples.frombytes(actual_chunk)
                    if sys.byteorder != 'little':
                        original_samples.byteswap()
                        actual_samples.byteswap()
                    originals = (sample >> 6 for sample in original_samples)
                    actuals = (sample >> 6 for sample in actual_samples)
                else:
                    originals, actuals = chunk, actual_chunk
                for original, actual in zip(originals, actuals, strict=True):
                    delta = abs(original - actual)
                    maximum = max(maximum, delta)
                    squared_error += delta * delta
                    changed += bool(delta)
    mse = squared_error / (expected_size // (2 if sample_bits == 10 else 1))
    metrics = {
        'reference_sha256': reference_hash,
        'decoded_sha256': decoded_hash,
        'bytes': expected_size,
        'changed_samples': changed,
        'maximum_difference': maximum,
        'mse': mse,
        'psnr_db': 10 * math.log10(((1 << sample_bits) - 1) ** 2 / mse) if mse else None,
    }
    return metrics, maximum <= max_difference


def validate_device(log, expected, card):
    text = log.read_text(errors='replace')
    devices = re.findall(r'Using device (/dev/\S+)', text)
    if devices != [expected] or f"driver 'iris_driver' on card 'Iris {card}'" not in text:
        raise ValueError(f'Expected Iris {card.lower()} {expected}, got {devices}; see {log}')
