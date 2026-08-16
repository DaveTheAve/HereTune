from __future__ import annotations

import struct

from heretune.analyzer import CorrectionProfile
from heretune.hsp import (
    PACKAGE_FILE_TAG,
    build,
    compress_proxy,
    decompress_proxy,
    inspect,
    version,
)
from heretune.server import parse_byte_range
from heretune.vrformat import detect


def test_mkx200_detection():
    value = detect("VR_Astro_MKX200.mp4", 3840, 1920)
    assert value["lens"] == "MKX200"
    assert value["projection"] == "fisheye"
    assert value["stereo"] == "sbs"


def test_current_build_hsp_defaults_and_round_trip():
    correction = CorrectionProfile(
        r_gain=1.03,
        g_gain=0.99,
        b_gain=0.98,
        contrast=1.04,
        brightness=0.01,
        gamma=0.99,
        saturation=1.02,
    )
    blob = build("abc", "Movie", 120.0, correction)
    assert struct.unpack_from("<q", blob, 0)[0] == PACKAGE_FILE_TAG
    assert version(blob) == 7
    parsed = inspect(blob)
    image = parsed.image_keyframes[0]
    assert image.sharpen == 2.0
    assert image.white_temp == 6500.0
    assert image.white_tint == 0.0
    assert image.shadows == 0.0
    assert image.midtones == 1.0
    assert image.highlights == 1.0
    assert all(abs(a - b) < 1e-6 for a, b in zip(image.gain[:3], (correction.r_gain, correction.g_gain, correction.b_gain)))
    assert abs(image.gain[3] - 1.0) < 1e-6
    assert all(abs(value - 1.0) < 1e-6 for value in image.saturation[:3])
    assert abs(image.saturation[3] - correction.saturation) < 1e-6
    assert all(abs(value - 1.0) < 1e-6 for value in image.contrast[:3])
    assert abs(image.contrast[3] - correction.contrast) < 1e-6
    assert all(abs(value - 1.0) < 1e-6 for value in image.gamma[:3])
    assert abs(image.gamma[3] - correction.gamma) < 1e-6
    assert all(abs(value - 0.0) < 1e-6 for value in image.offset[:3])
    assert abs(image.offset[3] - correction.brightness) < 1e-6


def test_concatenated_proxy_round_trip():
    raw = bytes(range(251)) * 1600
    blob = compress_proxy(raw)
    assert decompress_proxy(blob) == raw


def test_ranges():
    assert parse_byte_range(None, 1000) is None
    assert parse_byte_range("bytes=0-99", 1000) == (0, 99)
    assert parse_byte_range("bytes=900-", 1000) == (900, 999)
    assert parse_byte_range("bytes=-100", 1000) == (900, 999)
