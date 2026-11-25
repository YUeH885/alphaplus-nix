#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Qualcomm Spectra 380 (Titan 175) IQM & FC4 AWB Inference Engine
# Pre-processes 64x48 Bayer Grid (BG) hardware statistics, executes neural network
# forward inference, applies FC4 result corrections and reference domain calibrations,
# and integrates with native LG AWB DNN convergence.

import argparse
import json
import math
from pathlib import Path
import struct
import sys
import zipfile


def decode_varint(data, pos):
    """Pure-Python standard Protobuf Varint decoder."""
    result = 0
    shift = 0
    while True:
        b = data[pos]
        pos += 1
        result |= (b & 0x7f) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, pos


# Golden Reference RGB Ratios from vendor calibration (D50, A)
IQM_GOLDEN_REFERENCES = {
    'imx351':  [(0.60429603, 0.55656499), (0.92985100, 0.35342500)],
    'imx363':  [(0.49671701, 0.57841599), (0.77151698, 0.35769400)],
    's5k3m5':  [(0.52828097, 0.54978502), (0.85263002, 0.34289101)],
}

# Sensor Actual Reference Points (D50, A)
SENSOR_CALIB_REFERENCES = {
    'imx351':  [(0.60429603, 0.55656499), (0.92985100, 0.35342500)],
    'imx363':  [(0.49671701, 0.57841599), (0.77151698, 0.35769400)],
    's5k3m5':  [(1.0 / 1.8929, 1.0 / 1.8189), (1.0 / 1.1728, 1.0 / 2.9164)],
}

# Sensor Type ID mapping per vendor stock configuration
SENSOR_TYPE_MAP = {
    'imx351': 16,
    'imx363': 17,
    's5k3m5': 18,
}


def parse_protobuf_records(data, start_pos=8):
    pos = start_pos
    nodes = []
    while pos < len(data):
        tag, new_pos = decode_varint(data, pos)
        field_num = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 2:
            length, new_pos = decode_varint(data, new_pos)
            val = data[new_pos:new_pos + length]
            new_pos += length
            if field_num == 5:
                nodes.append(val)
        elif wire_type == 0:
            _, new_pos = decode_varint(data, new_pos)
        elif wire_type == 1:
            new_pos += 8
        elif wire_type == 5:
            new_pos += 4
        pos = new_pos
    return nodes


def parse_weights_blob(blob):
    pos = 0
    dims = []
    raw_bytes = None
    while pos < len(blob):
        tag, new_pos = decode_varint(blob, pos)
        f = tag >> 3
        w = tag & 0x07
        if w == 0:
            v, new_pos = decode_varint(blob, new_pos)
            if f == 1:
                dims.append(v)
        elif w == 2:
            l, new_pos = decode_varint(blob, new_pos)
            v = blob[new_pos:new_pos + l]
            new_pos += l
            if f == 2:
                raw_bytes = v
        elif w in (1, 5):
            new_pos += 8 if w == 1 else 4
        pos = new_pos

    if raw_bytes is None:
        return dims, []

    count = len(raw_bytes) // 4
    floats = list(struct.unpack(f'<{count}f', raw_bytes[:count * 4]))
    return dims, floats


def parse_node(node_bytes):
    p = 0
    name = ''
    op_type = 0
    out_shape = []
    weights = []
    biases = []

    while p < len(node_bytes):
        tag, new_p = decode_varint(node_bytes, p)
        f = tag >> 3
        w = tag & 0x07
        if w == 0:
            v, new_p = decode_varint(node_bytes, new_p)
            if f == 2:
                op_type = v
            elif f == 3:
                out_shape.append(v)
        elif w == 2:
            l, new_p = decode_varint(node_bytes, new_p)
            v = node_bytes[new_p:new_p + l]
            new_p += l
            if f == 1:
                name = v.decode('ascii', errors='ignore')
            elif f in (20, 30, 70, 71):
                sub_p = 0
                while sub_p < len(v):
                    sub_tag, sub_new_p = decode_varint(v, sub_p)
                    sub_f = sub_tag >> 3
                    sub_w = sub_tag & 0x07
                    if sub_w == 2:
                        sub_l, sub_new_p = decode_varint(v, sub_new_p)
                        sub_v = v[sub_new_p:sub_new_p + sub_l]
                        sub_new_p += sub_l
                        if sub_f in (1, 70):
                            dims, arr = parse_weights_blob(sub_v)
                            if len(arr) > 0:
                                weights.append((dims, arr))
                        elif sub_f in (2, 71):
                            dims, arr = parse_weights_blob(sub_v)
                            if len(arr) > 0:
                                biases.append((dims, arr))
                        elif sub_f == 20:
                            c_p = 0
                            while c_p < len(sub_v):
                                c_tag, c_new_p = decode_varint(sub_v, c_p)
                                c_f = c_tag >> 3
                                c_w = c_tag & 0x07
                                if c_w == 2:
                                    c_l, c_new_p = decode_varint(sub_v, c_new_p)
                                    c_v = sub_v[c_new_p:c_new_p + c_l]
                                    c_new_p += c_l
                                    if c_f == 1:
                                        dims, arr = parse_weights_blob(c_v)
                                        if len(arr) > 0:
                                            weights.append((dims, arr))
                                    elif c_f == 2:
                                        dims, arr = parse_weights_blob(c_v)
                                        if len(arr) > 0:
                                            biases.append((dims, arr))
                                elif c_w == 0:
                                    _, c_new_p = decode_varint(sub_v, c_new_p)
                                elif c_w in (1, 5):
                                    c_new_p += 8 if c_w == 1 else 4
                                c_p = c_new_p
                    elif sub_w == 0:
                        _, sub_new_p = decode_varint(v, sub_new_p)
                    elif sub_w in (1, 5):
                        sub_new_p += 8 if sub_w == 1 else 4
                    sub_p = sub_new_p
        elif w in (1, 5):
            new_p += 8 if w == 1 else 4
        p = new_p

    return {
        'name': name,
        'op_type': op_type,
        'out_shape': out_shape,
        'weights': weights,
        'biases': biases,
    }


def relu(x):
    return [max(0.0, v) for v in x]


def softmax(x):
    max_x = max(x)
    exp_x = [math.exp(v - max_x) for v in x]
    sum_exp = sum(exp_x)
    return [v / sum_exp for v in exp_x]


def cal_rb_ratio(r_ratio, b_ratio, golden_points, calib_points):
    """
    lg_iqm_cal_rb_ratio() (0x201a8):
    Converts calibration reference domain to golden reference domain.
    """
    scale_r0 = golden_points[0][0] / calib_points[0][0]
    scale_b0 = golden_points[0][1] / calib_points[0][1]
    scale_r1 = golden_points[1][0] / calib_points[1][0]
    scale_b1 = golden_points[1][1] / calib_points[1][1]

    d0 = math.sqrt((r_ratio - calib_points[0][0]) ** 2 + (b_ratio - calib_points[0][1]) ** 2)
    d1 = math.sqrt((r_ratio - calib_points[1][0]) ** 2 + (b_ratio - calib_points[1][1]) ** 2)
    d_sum = d0 + d1
    if d_sum > 1e-6:
        w0 = d1 / d_sum
        w1 = d0 / d_sum
    else:
        w0 = 0.5
        w1 = 0.5

    scale_r = w0 * scale_r0 + w1 * scale_r1
    scale_b = w0 * scale_b0 + w1 * scale_b1
    return scale_r, scale_b


def golden_to_cal_rb_ratio(r_ratio, b_ratio, golden_points, calib_points):
    """
    lg_iqm_golden_to_cal_rb_ratio() (0x20390):
    Converts golden reference domain to calibration reference domain (reverse).
    """
    scale_r0 = calib_points[0][0] / golden_points[0][0]
    scale_b0 = calib_points[0][1] / golden_points[0][1]
    scale_r1 = calib_points[1][0] / golden_points[1][0]
    scale_b1 = calib_points[1][1] / golden_points[1][1]

    d0 = math.sqrt((r_ratio - golden_points[0][0]) ** 2 + (b_ratio - golden_points[0][1]) ** 2)
    d1 = math.sqrt((r_ratio - golden_points[1][0]) ** 2 + (b_ratio - golden_points[1][1]) ** 2)
    d_sum = d0 + d1
    if d_sum > 1e-6:
        w0 = d1 / d_sum
        w1 = d0 / d_sum
    else:
        w0 = 0.5
        w1 = 0.5

    scale_r = w0 * scale_r0 + w1 * scale_r1
    scale_b = w0 * scale_b0 + w1 * scale_b1
    return scale_r, scale_b


CAMERA_MODELS = Path(__file__).resolve().parents[1] / "nixos/files/camera"
SCENE_MODELS = {
    "imx363": "wb_graph_imx363.dlc",
    "imx351": "wb_graph_imx351w.dlc",
    "s5k3m5": "wb_graph_s5k3m5t.dlc",
}


class FastSceneModel:
    """Pre-loaded model weights for fast inference."""

    def __init__(self, dlc_path):
        with zipfile.ZipFile(dlc_path, 'r') as zf:
            data = zf.read('model')
        raw_nodes = parse_protobuf_records(data)
        nodes = [parse_node(nb) for nb in raw_nodes]

        self.conv1_w = nodes[2]['weights'][0][1]
        self.conv1_b = nodes[2]['biases'][0][1]
        self.conv2_w = nodes[5]['weights'][0][1]
        self.conv2_b = nodes[5]['biases'][0][1]
        self.conv3_w = nodes[8]['weights'][0][1]
        self.conv3_b = nodes[8]['biases'][0][1]

        self.fc1_w = nodes[14]['weights'][0][1]
        self.fc1_b = nodes[14]['biases'][0][1]
        self.fc2_w = nodes[16]['weights'][0][1]
        self.fc2_b = nodes[16]['biases'][0][1]
        self.fc3_w = nodes[17]['weights'][0][1]
        self.fc3_b = nodes[17]['biases'][0][1]

    def predict(self, input_3072x3):
        """Run forward inference."""
        # Conv1: 48x64x3 -> 48x64x8 -> MaxPool 24x32x8
        c1 = [0.0] * (48 * 64 * 8)
        for oc in range(8):
            b = self.conv1_b[oc]
            for h in range(48):
                for w in range(64):
                    val = b
                    for kh in range(3):
                        ih = h - 1 + kh
                        if 0 <= ih < 48:
                            for kw in range(3):
                                iw = w - 1 + kw
                                if 0 <= iw < 64:
                                    w_base = ((kh * 3 + kw) * 3) * 8 + oc
                                    in_base = (ih * 64 + iw) * 3
                                    val += input_3072x3[in_base] * self.conv1_w[w_base]
                                    val += input_3072x3[in_base + 1] * self.conv1_w[w_base + 8]
                                    val += input_3072x3[in_base + 2] * self.conv1_w[w_base + 16]
                    if val > 0:
                        c1[(h * 64 + w) * 8 + oc] = val

        # MaxPool1: 48x64x8 -> 24x32x8
        mp1 = [0.0] * (24 * 32 * 8)
        for c in range(8):
            for h in range(24):
                for w in range(32):
                    h2 = h * 2
                    w2 = w * 2
                    mp1[(h * 32 + w) * 8 + c] = max(
                        c1[(h2 * 64 + w2) * 8 + c],
                        c1[(h2 * 64 + w2 + 1) * 8 + c],
                        c1[((h2 + 1) * 64 + w2) * 8 + c],
                        c1[((h2 + 1) * 64 + w2 + 1) * 8 + c]
                    )

        # Conv2: 24x32x8 -> 24x32x16 -> MaxPool 12x16x16
        c2 = [0.0] * (24 * 32 * 16)
        for oc in range(16):
            b = self.conv2_b[oc]
            for h in range(24):
                for w in range(32):
                    val = b
                    for kh in range(3):
                        ih = h - 1 + kh
                        if 0 <= ih < 24:
                            for kw in range(3):
                                iw = w - 1 + kw
                                if 0 <= iw < 32:
                                    in_base = (ih * 32 + iw) * 8
                                    w_base = ((kh * 3 + kw) * 8) * 16 + oc
                                    for ic in range(8):
                                        val += mp1[in_base + ic] * self.conv2_w[w_base + ic * 16]
                    if val > 0:
                        c2[(h * 32 + w) * 16 + oc] = val

        # MaxPool2: 24x32x16 -> 12x16x16
        mp2 = [0.0] * (12 * 16 * 16)
        for c in range(16):
            for h in range(12):
                for w in range(16):
                    h2 = h * 2
                    w2 = w * 2
                    mp2[(h * 16 + w) * 16 + c] = max(
                        c2[(h2 * 32 + w2) * 16 + c],
                        c2[(h2 * 32 + w2 + 1) * 16 + c],
                        c2[((h2 + 1) * 32 + w2) * 16 + c],
                        c2[((h2 + 1) * 32 + w2 + 1) * 16 + c]
                    )

        # Conv3: 12x16x16 -> 12x16x32 -> MaxPool 6x8x32
        c3 = [0.0] * (12 * 16 * 32)
        for oc in range(32):
            b = self.conv3_b[oc]
            for h in range(12):
                for w in range(16):
                    val = b
                    for kh in range(3):
                        ih = h - 1 + kh
                        if 0 <= ih < 12:
                            for kw in range(3):
                                iw = w - 1 + kw
                                if 0 <= iw < 16:
                                    in_base = (ih * 16 + iw) * 16
                                    w_base = ((kh * 3 + kw) * 16) * 32 + oc
                                    for ic in range(16):
                                        val += mp2[in_base + ic] * self.conv3_w[w_base + ic * 32]
                    if val > 0:
                        c3[(h * 16 + w) * 32 + oc] = val

        # MaxPool3: 12x16x32 -> 6x8x32
        mp3 = [0.0] * (6 * 8 * 32)
        for c in range(32):
            for h in range(6):
                for w in range(8):
                    h2 = h * 2
                    w2 = w * 2
                    mp3[(h * 8 + w) * 32 + c] = max(
                        c3[(h2 * 16 + w2) * 32 + c],
                        c3[(h2 * 16 + w2 + 1) * 32 + c],
                        c3[((h2 + 1) * 16 + w2) * 32 + c],
                        c3[((h2 + 1) * 16 + w2 + 1) * 32 + c]
                    )

        # Global AvgPool: 6x8x32 -> 32
        gap = [0.0] * 32
        for c in range(32):
            s = 0.0
            for idx in range(48):
                s += mp3[idx * 32 + c]
            gap[c] = max(0.0, s / 48.0)

        # FC1: 32 -> 32 + ReLU
        fc1 = [0.0] * 32
        for o in range(32):
            v = self.fc1_b[o]
            w_row = o * 32
            for i in range(32):
                v += gap[i] * self.fc1_w[w_row + i]
            fc1[o] = max(0.0, v)

        # FC2: 32 -> 32
        fc2 = [0.0] * 32
        for o in range(32):
            v = self.fc2_b[o]
            w_row = o * 32
            for i in range(32):
                v += fc1[i] * self.fc2_w[w_row + i]
            fc2[o] = v

        # FC3: 32 -> 4 + ReLU + Softmax
        fc3 = [0.0] * 4
        for o in range(4):
            v = self.fc3_b[o]
            w_row = o * 32
            for i in range(32):
                v += fc2[i] * self.fc3_w[w_row + i]
            fc3[o] = max(0.0, v)

        return softmax(fc3)


class OnnxSceneModel:
    def __init__(self, model_path):
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        self.session = ort.InferenceSession(str(model_path), options,
                                            providers=["CPUExecutionProvider"])

    def predict(self, input_3072x3):
        import numpy as np

        image = np.asarray(input_3072x3, dtype=np.float32).reshape(1, 3072, 3)
        return self.session.run(["probabilities"], {"input/rgb_stat:0": image})[0][0].tolist()


class FC4AwbModel:
    """
    Qualcomm Spectra 380 FC4 Neural Network White-Point & Scene Estimation Engine.
    Executes fc4_awb (4.96M float32 parameters) via ONNX Runtime on CPUExecutionProvider.
    Input: Preprocessed 48x64x3 BG statistics (3072 zones, float32 [0.0, 1.0]).
    Output:
      - fc4_est_r: White-point R/G ratio
      - fc4_est_b: White-point B/G ratio
      - top_cls1: Top scene class index (0..3)
      - top_conf1: Confidence score of top class
      - top_cls2: Second scene class index (-1 if confidence < 0.2)
      - top_conf2: Confidence score of second class
      - raw_7: Raw output vector [R_sum, G_sum, B_sum, C0_sum, C1_sum, C2_sum, C3_sum]
    """
    def __init__(self, model_path=None):
        if model_path is None:
            model_path = Path("/etc/camera/iqm/fc4_awb.onnx")
            if not model_path.is_file():
                model_path = CAMERA_MODELS / model_path.name
        else:
            model_path = Path(model_path)

        if not model_path.is_file():
            raise FileNotFoundError(f"FC4 ONNX model file not found: {model_path}")

        import onnxruntime as ort
        sess_opts = ort.SessionOptions()
        sess_opts.intra_op_num_threads = 4
        self.session = ort.InferenceSession(str(model_path), sess_opts, providers=['CPUExecutionProvider'])
        self.backend = "onnxruntime"
        self.provider = "CPUExecutionProvider"
        self.active_model_path = model_path

    def predict(self, input_3072x3):
        """
        Run forward inference.
        Returns: fc4_est_r, fc4_est_b, top_cls1, top_conf1, top_cls2, top_conf2, raw_7
        """
        if len(input_3072x3) != 3072 * 3:
            raise ValueError(f"Expected 9216 floats (3072x3), got {len(input_3072x3)}")

        import numpy as np
        in_arr = np.array(input_3072x3, dtype=np.float32).reshape((1, 48, 64, 3))
        out = self.session.run(None, {"X:0": in_arr})
        raw_7 = [float(v) for v in out[0][0]]

        for idx, val in enumerate(raw_7):
            if not math.isfinite(val):
                raise RuntimeError(f"FC4 inference returned non-finite output tensor at [{idx}]: {raw_7}")

        r_sum = raw_7[0]
        g_sum = raw_7[1]
        b_sum = raw_7[2]
        if g_sum <= 1e-6 or r_sum <= 0.0 or b_sum <= 0.0:
            raise RuntimeError(f"FC4 inference produced invalid white-point sums: R={r_sum}, G={g_sum}, B={b_sum}")

        fc4_est_r = r_sum / g_sum
        fc4_est_b = b_sum / g_sum

        classes = raw_7[3:7]
        probs = softmax(classes)
        sorted_classes = sorted(enumerate(probs), key=lambda x: (x[1], x[0]), reverse=True)
        top_cls1, top_conf1 = sorted_classes[0]
        top_cls2, top_conf2 = sorted_classes[1]
        if top_conf2 < 0.2:
            top_cls2 = -1

        return fc4_est_r, fc4_est_b, top_cls1, top_conf1, top_cls2, top_conf2, raw_7


def preprocess_bg_stats(bg_stats_3072_quads, sensor_name, for_fc4=False):
    """
    Preprocess 64x48 Bayer Grid (BG) hardware statistics according to
    factory lg_iqm_set_input_fc4().
    bg_stats_3072_quads: list of 3072 tuples (R, Gr, Gb, B)
    sensor_name: 'imx363', 'imx351', or 's5k3m5'
    for_fc4: if True, IMX351 and S5K3M5 temporarily use IMX363 golden reference points
    """
    if for_fc4:
        golden = IQM_GOLDEN_REFERENCES['imx363']
    else:
        golden = IQM_GOLDEN_REFERENCES[sensor_name]
    calib = SENSOR_CALIB_REFERENCES[sensor_name]

    out_3072x3 = [0.0] * (3072 * 3)
    HIGH_LUMA_THRESHOLD = 0.8834999799728394

    for idx, (r_raw, gr_raw, gb_raw, b_raw) in enumerate(bg_stats_3072_quads):
        r_norm = r_raw / 255.0
        g_norm = (gr_raw + gb_raw) / 510.0
        b_norm = b_raw / 255.0

        if g_norm > 0.0:
            r_ratio = r_norm / g_norm
            b_ratio = b_norm / g_norm

            scale_r, scale_b = cal_rb_ratio(r_ratio, b_ratio, golden, calib)

            r_scaled = r_norm * scale_r
            b_scaled = b_norm * scale_b

            # High-luma clearing threshold
            if r_scaled > HIGH_LUMA_THRESHOLD or g_norm > HIGH_LUMA_THRESHOLD or b_scaled > HIGH_LUMA_THRESHOLD:
                r_out = 0.0
                g_out = 0.0
                b_out = 0.0
            else:
                r_out = max(0.0, min(1.0, r_scaled))
                g_out = max(0.0, min(1.0, g_norm))
                b_out = max(0.0, min(1.0, b_scaled))
        else:
            r_out = 0.0
            g_out = 0.0
            b_out = 0.0

        out_idx = idx * 3
        out_3072x3[out_idx] = r_out
        out_3072x3[out_idx + 1] = g_out
        out_3072x3[out_idx + 2] = b_out

    return out_3072x3


def load_fc4_tuning(json_path=None):
    """Load extracted FC4 tuning parameters."""
    if json_path and Path(json_path).is_file():
        p = Path(json_path)
    else:
        candidates = [
            CAMERA_MODELS / "fc4_tuning_tables.json",
        ]
        p = next((c for c in candidates if c.is_file()), None)
        if not p:
            raise FileNotFoundError("fc4_tuning_tables.json not found in search paths")
    with open(p, 'r') as f:
        return json.load(f)


def find_nearest_p_locus_pt(r, b, fine_locus, sparse_locus, sparse_indices):
    """
    lg_iqm_find_nearest_p_locus_pt() (0x1b09c).
    Finds nearest Planckian locus point:
    1. Search 51 sparse reference points for minimum Euclidean distance.
    2. Search fine table within range defined by sparse indices.
    """
    min_dist = float('inf')
    best_sparse_idx = 0
    for idx, (sr, sb) in enumerate(sparse_locus):
        d = math.sqrt((r - sr)**2 + (b - sb)**2)
        if d < min_dist:
            min_dist = d
            best_sparse_idx = idx

    start_fine = sparse_indices[best_sparse_idx]
    end_fine = sparse_indices[best_sparse_idx + 1] if best_sparse_idx + 1 < len(sparse_indices) else len(fine_locus)
    if start_fine > end_fine:
        start_fine, end_fine = end_fine, start_fine

    best_r, best_b = fine_locus[start_fine]
    min_fine_dist = float('inf')
    for idx in range(start_fine, end_fine):
        fr, fb = fine_locus[idx]
        d = math.sqrt((r - fr)**2 + (b - fb)**2)
        if d < min_fine_dist:
            min_fine_dist = d
            best_r, best_b = fr, fb

    return best_r, best_b, min_fine_dist


def get_hist2d(preprocessed_3072x3, planckian_cells_set):
    """
    lg_iqm_get_hist2d() (0x1b820).
    75x75 ratio histogram.
    """
    hist = [[0] * 75 for _ in range(75)]
    occupied = [[0] * 75 for _ in range(75)]
    total_occupied_cells = 0
    total_samples = 0
    locus_occupied = 0
    locus_samples = 0
    non_locus_occupied = 0
    non_locus_samples = 0

    max_count = 0
    peak_x, peak_y = 0, 0

    for idx in range(3072):
        base = idx * 3
        r = preprocessed_3072x3[base]
        g = preprocessed_3072x3[base + 1]
        b = preprocessed_3072x3[base + 2]

        if r < 0.949999988079071 and g < 0.949999988079071 and b < 0.949999988079071 and g > 1e-6:
            r_ratio = r / g
            b_ratio = b / g
            x = int(math.floor(100.0 * r_ratio / 2.0 + 0.5)) - 1
            y = int(math.floor(100.0 * b_ratio / 2.0 + 0.5)) - 1
            if 0 <= x < 75 and 0 <= y < 75:
                if occupied[y][x] == 0:
                    occupied[y][x] = 1
                    total_occupied_cells += 1
                    if (x, y) in planckian_cells_set:
                        locus_occupied += 1
                    else:
                        non_locus_occupied += 1
                hist[y][x] += 1
                total_samples += 1
                if (x, y) in planckian_cells_set:
                    locus_samples += 1
                else:
                    non_locus_samples += 1

                if hist[y][x] > max_count:
                    max_count = hist[y][x]
                    peak_x, peak_y = x, y

    locus_coverage = locus_occupied / 232.0
    non_locus_occ_ratio = (non_locus_occupied / total_occupied_cells) if total_occupied_cells > 0 else 0.0
    non_locus_samp_ratio = (non_locus_samples / total_samples) if total_samples > 0 else 0.0

    return {
        'hist': hist,
        'occupied': occupied,
        'total_occupied': total_occupied_cells,
        'total_samples': total_samples,
        'locus_coverage': locus_coverage,
        'non_locus_occ_ratio': non_locus_occ_ratio,
        'non_locus_samp_ratio': non_locus_samp_ratio,
        'peak': (peak_x, peak_y, max_count),
    }


def decide_single_color(occupied, single_color_tables):
    """
    lg_iqm_decide_single_color_from_hist2d() (0x1b40c) & lg_iqm_decide_line_or_point() (0x1a944).
    """
    slots_x = single_color_tables['slots_x']
    slots_y = single_color_tables['slots_y']
    line_threshold = single_color_tables['line_ratio_threshold']
    shape_weight_span = single_color_tables['shape_weight_span']

    total_occ = sum(sum(row) for row in occupied)
    if total_occ == 0:
        return 0.0, 0, 0.0

    # Stock mapping index 8
    offset = 8 * 5625
    bins_x = [0] * 75
    bins_y = [0] * 75
    for idx in range(5625):
        y = idx // 75
        x = idx % 75
        if occupied[y][x]:
            bx = slots_x[offset + idx]
            by = slots_y[offset + idx]
            if 0 <= bx < 75:
                bins_x[bx] += 1
            if 0 <= by < 75:
                bins_y[by] += 1

    max_3_x = max(bins_x[i] + bins_x[i+1] + bins_x[i+2] for i in range(73)) if len(bins_x) >= 3 else 0
    max_3_y = max(bins_y[i] + bins_y[i+1] + bins_y[i+2] for i in range(73)) if len(bins_y) >= 3 else 0

    score_x = max_3_x / total_occ
    score_y = max_3_y / total_occ
    single_color_score = max(score_x, score_y)

    min_s = min(score_x, score_y)
    max_s = max(score_x, score_y)
    axis_ratio = (min_s / max_s) if max_s > 1e-6 else 1.0
    if axis_ratio < line_threshold:
        shape_type = 1  # line
        shape_weight = min(1.0, (1.0 - axis_ratio) / shape_weight_span)
    else:
        shape_type = 0  # point
        shape_weight = 0.0

    return single_color_score, shape_type, shape_weight


def scene_class_decision(total_occupied, single_color_score, non_locus_occ_ratio, non_locus_samp_ratio, locus_coverage):
    """
    lg_iqm_scene_class_decision() (0x1c2d0).
    """
    if total_occupied <= 25:
        return 1, 0.0, 0.0
    elif total_occupied <= 29:
        conf = (total_occupied - 25) * 0.2
        return 4, conf, conf
    else:
        if single_color_score <= 0.85:
            if non_locus_samp_ratio >= 0.7:
                return 6, 1.0, 0.0
            else:
                return 5, 1.0, 1.0
        else:
            if locus_coverage > 0.2:
                return 4, 1.0, 1.0
            else:
                if non_locus_occ_ratio < 0.001:
                    return 1, 1.0, 0.0
                else:
                    return 3, 1.0, 1.0


def detect_grass_scene(base_r_ratio, base_b_ratio, hist_info, grass_cells_set, upper_locus_mask):
    """
    lg_iqm_detect_grass_scene() (0x1aa94) & lg_iqm_detect_grass_scene2() (0x1ac0c).
    """
    hist = hist_info['hist']
    sum_weight = 0
    weighted_x = -1.0
    weighted_y = -1.0
    total_grass_samples = 0

    for y in range(75):
        for x in range(75):
            idx = y * 75 + x
            if upper_locus_mask[idx] != 0:
                count = hist[y][x]
                if count > 0:
                    sum_weight += count
                    weighted_x += x * count
                    weighted_y += y * count
            if (x, y) in grass_cells_set:
                total_grass_samples += hist[y][x]

    sample_fraction = (total_grass_samples / hist_info['total_samples']) if hist_info['total_samples'] > 0 else 0.0

    if sample_fraction <= 0.1:
        ramp_weight = 0.0
    elif sample_fraction >= 0.2:
        ramp_weight = 1.0
    else:
        ramp_weight = (sample_fraction - 0.1) / 0.1

    is_grass = False
    if sum_weight > 0:
        cx = int(round(weighted_x / sum_weight))
        cy = int(round(weighted_y / sum_weight))
        if (cx, cy) in grass_cells_set and ramp_weight > 0.1:
            is_grass = True

    return is_grass, ramp_weight


def clipping_low_cct(r, b, scene_class, conf, est_locus_b, base_r, base_b):
    """
    lg_iqm_clipping_low_cct() (0x1f024).
    """
    if scene_class not in (3, 4, 5) or est_locus_b >= 0.42391061782836914:
        return r, b, scene_class, conf

    low_b = 0.35769400000572205
    high_b = 0.42391061782836914
    w = (est_locus_b - low_b) / (high_b - low_b)
    if w < 0.01:
        return r, b, 6, conf
    else:
        new_conf = 0.4 * w
        new_r = base_r + new_conf * (r - base_r)
        new_b = base_b + new_conf * (b - base_b)
        return new_r, new_b, 5, new_conf


def comp_multiple_res(scene_class, model_cls1, model_conf1, model_cls2,
                      base_r, base_b, est_r, est_b,
                      base_locus_r, base_locus_b, est_locus_r, est_locus_b,
                      sensor_type, is_grass, grass_weight,
                      red_ratio, yellow_accept_weight, tuning_res):
    """
    lg_iqm_comp_multiple_res() (0x1c884).
    """
    final_r = est_r
    final_b = est_b
    final_class = scene_class
    final_weight = 0.0
    diag_status = 0

    is_sensor_17 = (sensor_type == 17)

    # 1. Grass path
    if (scene_class in (5, 6)) and is_grass and (est_locus_r < base_locus_r):
        scale = tuning_res['grass_blend_scale']['sensor_17'] if is_sensor_17 else tuning_res['grass_blend_scale']['other_sensors']
        w = grass_weight * scale
        if model_cls1 == 0:
            w *= tuning_res['grass_class_zero_penalty']
            if model_cls2 == -1:
                w *= tuning_res['grass_class_zero_penalty']

        min_w = tuning_res['grass_minimum_weight'] if is_sensor_17 else tuning_res['grass_other_sensor_minimum_weight']
        if w >= min_w:
            final_class = 7
            final_weight = w
            final_r = base_r + (est_r - base_r) * w
            final_b = base_b + (est_b - base_b) * w
            bias = tuning_res['grass_rgb_ratio_bias']['sensor_17'] if is_sensor_17 else tuning_res['grass_rgb_ratio_bias']['other_sensors']
            final_r += bias[0]
            final_b += bias[1]
            return final_r, final_b, final_class, final_weight, 0
        else:
            final_class = 8
            diag_status = 4
            final_weight = 0.0
            return est_r, est_b, final_class, final_weight, diag_status

    # 2. Yellow path
    if (scene_class in (5, 6)) and (base_r > tuning_res['yellow_base_red_threshold_exclusive']) and (model_cls1 == 2) and (est_locus_r < base_locus_r):
        if yellow_accept_weight > tuning_res['yellow_minimum_acceptance_exclusive']:
            final_class = 2
            w = yellow_accept_weight * 0.4
            mult = tuning_res['yellow_sensor_weight_multipliers']['sensor_17'] if is_sensor_17 else tuning_res['yellow_sensor_weight_multipliers']['other_sensors']
            final_weight = w * mult
            final_r = base_r + (est_r - base_r) * final_weight
            final_b = base_b + (est_b - base_b) * final_weight
            bias = tuning_res['yellow_rgb_ratio_bias']['sensor_17'] if is_sensor_17 else tuning_res['yellow_rgb_ratio_bias']['other_sensors']
            final_r += bias[0]
            final_b += bias[1]
            return final_r, final_b, final_class, final_weight, 0

    # 3. Class 3 path
    if scene_class == 3:
        w = model_conf1 * 0.4
        final_weight = w
        final_r = base_r + (est_r - base_r) * w
        final_b = base_b + (est_b - base_b) * w
        return final_r, final_b, 3, final_weight, 0

    # 4. Class 5 non-grass
    if scene_class == 5:
        if est_locus_b < base_locus_b and base_locus_b > tuning_res['projected_blue_rejection_threshold']:
            return est_r, est_b, 6, 0.0, 0

        if red_ratio <= tuning_res['red_fraction_maximum']:
            slope = tuning_res['red_fraction_weight_slope']
            w_red = 1.0 + red_ratio * slope
        else:
            w_red = 0.0

        if w_red > tuning_res['red_minimum_weight_exclusive']:
            w = w_red * 0.4
            final_weight = w
            final_r = base_r + (est_r - base_r) * w
            final_b = base_b + (est_b - base_b) * w
            return final_r, final_b, 5, final_weight, 0
        else:
            return base_r, base_b, 6, 0.0, 1

    # Class 4 path
    if scene_class == 4:
        final_weight = 0.2
        final_r = base_r + (est_r - base_r) * final_weight
        final_b = base_b + (est_b - base_b) * final_weight
        return final_r, final_b, 4, final_weight, 0

    return est_r, est_b, scene_class, 0.0, 0


def post_process_fc4_output(r_ratio, b_ratio, sensor_name):
    """
    lg_iqm_post_processing() (0x206f0):
    Converts corrected (R/G, B/G) ratio to output gains.
    Calls golden_to_cal_rb_ratio() to convert from golden domain to sensor calib domain.
    Clips gains to [1.0, 3.9000000953674316].
    """
    if r_ratio <= 1e-6 or b_ratio <= 1e-6:
        return 1.0, 1.0

    raw_gain_r = 1.0 / r_ratio
    raw_gain_b = 1.0 / b_ratio

    golden = IQM_GOLDEN_REFERENCES['imx363']
    calib = SENSOR_CALIB_REFERENCES[sensor_name]

    scale_r, scale_b = golden_to_cal_rb_ratio(r_ratio, b_ratio, golden, calib)
    calib_gain_r = raw_gain_r / scale_r
    calib_gain_b = raw_gain_b / scale_b

    final_gain_r = max(1.0, min(3.9000000953674316, calib_gain_r))
    final_gain_b = max(1.0, min(3.9000000953674316, calib_gain_b))
    return final_gain_r, final_gain_b


class IQMStateManager:
    """
    IQM processing state manager and temporal filter:
    - Temporal filtering: 0.6 * new + 0.4 * history (initialized on first sample)
    - State transition: Initial state 5 transitions to 2 upon first update
    """
    def __init__(self, sensor_name):
        self.sensor_name = sensor_name
        self.history_slots = None
        self.state = 5
        self.stable_counter = 0

    def update(self, new_slots):
        if self.history_slots is None:
            filtered_slots = list(new_slots)
        else:
            filtered_slots = [0.0] * 7
            for i in range(7):
                filtered_slots[i] = 0.6 * new_slots[i] + 0.4 * self.history_slots[i]
        # Slot 0 encodes class and confidence directly without temporal smoothing
        filtered_slots[0] = new_slots[0]
        self.history_slots = list(filtered_slots)

        self.stable_counter += 1
        if self.state == 5:
            self.state = 2

        # Extract 4-tuple for LgAwbDnn matching stock LGAECAlgoGetTagIQM and LgAwbDnn:
        # [conf_scene0 (outdoor), conf_scene1 (indoor), gain_r, gain_b]
        iqm_dnn_tuple = [
            filtered_slots[1],  # outdoor confidence (scene 0)
            filtered_slots[4],  # indoor confidence (scene 1)
            filtered_slots[2],  # calibrated R gain
            filtered_slots[3],  # calibrated B gain
        ]
        return filtered_slots, iqm_dnn_tuple


class FC4Corrector:
    """FC4 results corrector and output calibration processor."""

    def __init__(self, tuning_path=None):
        self.tuning = load_fc4_tuning(tuning_path)
        self.planckian_cells = set(tuple(p) for p in self.tuning['histogram']['planckian_locus_cells'])
        self.grass_cells = set(tuple(p) for p in self.tuning['grass']['cells'])
        self.upper_locus_mask = self.tuning['grass']['upper_locus_region']
        self.fine_locus = self.tuning['white_point_locus']['fine_rgb_ratios']
        self.sparse_locus = self.tuning['white_point_locus']['sparse_rgb_ratios']
        self.sparse_indices = self.tuning['white_point_locus']['sparse_indices']

    def correct(self, bg_stats_3072_quads, fc4_est_r, fc4_est_b,
                fc4_cls1, fc4_conf1, fc4_cls2, sensor_name, scene_probabilities, lux=250):
        """
        Runs complete FC4 correction pipeline on preprocessed statistics.
        Returns the seven raw IQM slots.
        """
        sensor_type = SENSOR_TYPE_MAP[sensor_name]

        # 1. Preprocess with FC4 model domain calibration
        preprocessed = preprocess_bg_stats(bg_stats_3072_quads, sensor_name, for_fc4=True)

        # 2. Simple Gray World base ratio
        sum_r, sum_g, sum_b = 0.0, 0.0, 0.0
        count = 0
        for i in range(3072):
            base = i * 3
            r, g, b = preprocessed[base], preprocessed[base + 1], preprocessed[base + 2]
            if g > 1e-6:
                sum_r += r
                sum_g += g
                sum_b += b
                count += 1
        if count > 0 and sum_g > 1e-6:
            base_r = sum_r / sum_g
            base_b = sum_b / sum_g
        else:
            base_r = 0.6
            base_b = 0.5

        # 3. 2D Histogram & Single Color
        hist_info = get_hist2d(preprocessed, self.planckian_cells)
        sc_score, sc_type, sc_weight = decide_single_color(hist_info['occupied'], self.tuning['single_color'])

        # 4. Scene classification decision
        scene_cls, conf, weight = scene_class_decision(
            hist_info['total_occupied'], sc_score,
            hist_info['non_locus_occ_ratio'], hist_info['non_locus_samp_ratio'],
            hist_info['locus_coverage']
        )

        # 5. Grass & Locus projections
        is_grass, grass_weight = detect_grass_scene(base_r, base_b, hist_info, self.grass_cells, self.upper_locus_mask)

        base_lr, base_lb, _ = find_nearest_p_locus_pt(base_r, base_b, self.fine_locus, self.sparse_locus, self.sparse_indices)
        est_lr, est_lb, _ = find_nearest_p_locus_pt(fc4_est_r, fc4_est_b, self.fine_locus, self.sparse_locus, self.sparse_indices)

        # 6. Color statistics (red & yellow fractions)
        red_numerator = 0
        red_denominator = 0
        yellow_numerator = 0
        yellow_denominator = 0
        for i in range(3072):
            base = i * 3
            r, g, b = preprocessed[base], preprocessed[base + 1], preprocessed[base + 2]
            if g > 1e-6:
                pr_r, pr_b, dist = find_nearest_p_locus_pt(r/g, b/g, self.fine_locus, self.sparse_locus, self.sparse_indices)
                if pr_r > 0.0:
                    if dist < 0.5:
                        red_denominator += 1
                        if pr_r > 0.7715169787406921:
                            red_numerator += 1
                    if dist < 0.1:
                        yellow_denominator += 1
                        if pr_r < fc4_est_r:
                            yellow_numerator += 1

        red_ratio = (red_numerator / red_denominator) if red_denominator > 0 else 0.0
        yellow_accept = (yellow_numerator / yellow_denominator) if yellow_denominator > 0 else 0.0

        # 7. Multiple results correction
        corr_r, corr_b, final_cls, final_w, diag = comp_multiple_res(
            scene_cls, fc4_cls1, fc4_conf1, fc4_cls2,
            base_r, base_b, fc4_est_r, fc4_est_b,
            base_lr, base_lb, est_lr, est_lb,
            sensor_type, is_grass, grass_weight,
            red_ratio, yellow_accept, self.tuning['result_correction']
        )

        # 8. Low-CCT Clipping
        corr_r, corr_b, final_cls, final_conf = clipping_low_cct(
            corr_r, corr_b, final_cls, final_w, est_lb, base_r, base_b
        )

        # 9. Output Calibration & Reverse Domain Scaling
        gain_r, gain_b = post_process_fc4_output(corr_r, corr_b, sensor_name)

        # 10. Encode FC4 class & confidence
        enc_cls = final_cls
        if (sensor_type == 16 and enc_cls != 3) or enc_cls == 5:
            enc_cls += 10
        iqm_value_0 = enc_cls * 10000.0 + fc4_conf1

        slots = [
            iqm_value_0,                   # Slot 0: Class encoding + confidence
            float(scene_probabilities[1]),  # Slot 1: Outdoor probability
            gain_r,                        # Slot 2: Calibrated R gain
            gain_b,                        # Slot 3: Calibrated B gain
            float(scene_probabilities[2]),  # Slot 4: Indoor probability
            float(scene_probabilities[3]),  # Slot 5: Scene outcome 3
            0.0                            # Slot 6
        ]

        return slots


def main():
    parser = argparse.ArgumentParser(description="Qualcomm Spectra 380 IQM Preprocessing and FC4 Inference Engine")
    parser.add_argument("--model", type=Path,
                        help="Override the FC4 ONNX model or the scene classifier DLC for the selected mode")
    parser.add_argument("--sensor", choices=["imx363", "imx351", "s5k3m5"], default="imx363",
                        help="Sensor calibration profile")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--fc4", action="store_true", help="Run full FC4 neural network inference and correction pipeline")
    mode.add_argument("--test", action="store_true", help="Run scene classification self-test with sample grid")
    args = parser.parse_args()

    if args.fc4:
        if args.model and args.model.suffix != ".onnx":
            parser.error("--fc4 requires an ONNX model")
        print(f"Loading and executing full FC4 neural network inference for {args.sensor} (sensor_type={SENSOR_TYPE_MAP[args.sensor]})...")
        fc4_model = FC4AwbModel(args.model)
        print(f"FC4 Model active: backend={fc4_model.backend}, provider={fc4_model.provider}, path={fc4_model.active_model_path}")

        corrector = FC4Corrector()
        state_mgr = IQMStateManager(args.sensor)

        sample_grid = [(128, 128, 128, 128)] * 3072
        preprocessed = preprocess_bg_stats(sample_grid, args.sensor, for_fc4=True)

        fc4_est_r, fc4_est_b, top_cls1, top_conf1, top_cls2, top_conf2, raw_7 = fc4_model.predict(preprocessed)
        print(f"FC4 Model Raw Output: {[round(v, 4) for v in raw_7]}")
        print(f"FC4 Estimated White Point: R/G={fc4_est_r:.4f}, B/G={fc4_est_b:.4f}")
        print(f"FC4 Scene Classification: Top1=class {top_cls1} (conf={top_conf1:.4f}), Top2=class {top_cls2} (conf={top_conf2:.4f})")

        scene_name = Path(SCENE_MODELS[args.sensor]).with_suffix(".onnx")
        scene_path = next(
            (directory / scene_name
             for directory in (fc4_model.active_model_path.parent, Path("/etc/camera/iqm"), CAMERA_MODELS)
             if (directory / scene_name).is_file()),
            fc4_model.active_model_path.parent / scene_name,
        )
        slots = corrector.correct(
            sample_grid,
            fc4_est_r=fc4_est_r, fc4_est_b=fc4_est_b,
            fc4_cls1=top_cls1, fc4_conf1=top_conf1, fc4_cls2=top_cls2,
            sensor_name=args.sensor, lux=250,
            scene_probabilities=OnnxSceneModel(scene_path).predict(
                preprocess_bg_stats(sample_grid, args.sensor)
            )
        )
        filtered_slots, dnn_tuple = state_mgr.update(slots)

        print(f"Raw Output Slots: {[round(s, 4) for s in slots]}")
        print(f"Filtered Slots (0.6 * new + 0.4 * hist): {[round(s, 4) for s in filtered_slots]}")
        print(f"LgAwbDnn Tuple [outdoor_conf, indoor_conf, gain_r, gain_b]: {[round(v, 4) for v in dnn_tuple]}")
        print(f"State Machine Transitioned: {state_mgr.state} (counter={state_mgr.stable_counter})")
        return 0

    if args.test:
        model_path = args.model or CAMERA_MODELS / SCENE_MODELS[args.sensor]
        if model_path.suffix != ".dlc":
            parser.error("--test requires a DLC scene classifier")
        if not model_path.is_file():
            raise FileNotFoundError(f"DLC model not found: {model_path}")

        print(f"Loading model: {model_path} (sensor profile: {args.sensor})")
        model = FastSceneModel(model_path)

        sample_grid = [(128, 128, 128, 128)] * 3072
        preprocessed = preprocess_bg_stats(sample_grid, args.sensor)
        probs = model.predict(preprocessed)

        print(f"Preprocessed 3072 BG zones (min={min(preprocessed):.3f}, max={max(preprocessed):.3f})")
        print(f"Scene Softmax Probabilities: {[round(p, 5) for p in probs]}")
        print(f"Sum: {sum(probs):.6f}")
        return 0

    parser.print_help()
    return 0


if __name__ == '__main__':
    sys.exit(main())
