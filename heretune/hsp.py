from __future__ import annotations

import io
import math
import struct
import zlib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable

PACKAGE_FILE_TAG = 0x9E2A83C1
PROXY_CHUNK_SIZE = 131_072
TICKS_PER_SECOND = 10_000_000
SUPPORTED_HSP_VERSION = 7
# Native standalone .hsp exports observed from the current HereSphere player
# prepend this 32-bit marker before the serialized UMediaPreset v7 payload. Embedded
# media presets inside Library.hsl do not carry this outer marker.
STANDALONE_HSP_PREFIX = 1024
MAX_REASONABLE_COUNT = 100_000
# Sparse scene grades are useful, but importing a very large native array while
# playback is active is an unnecessary player-side stressor. Full evidence stays
# in SQLite; only the player-facing HSP is capped.
MAX_SAFE_IMAGE_KEYFRAMES = 64
MAX_SAFE_HSP_RAW_BYTES = 8 * 1024 * 1024


class HSPError(RuntimeError):
    pass


@dataclass(slots=True)
class ImageKeyFrame:
    time_ticks: int = 0
    sharpen: float = 2.0
    exposure_bias: float = 0.0
    white_temp: float = 6500.0
    white_tint: float = 0.0
    saturation: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)
    contrast: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)
    gamma: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)
    gain: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)
    offset: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    shadows: float = 0.0
    midtones: float = 1.0
    highlights: float = 1.0


@dataclass(slots=True)
class FormatKeyFrame:
    time_ticks: int = 0
    projection_mode: int = 0
    stereo_mode: int = 0
    eye_swap_enabled: bool = False
    force_mono_enabled: bool = False
    flip_front_back: bool = False
    flip_left_right: bool = False
    flip_up_down: bool = False
    aspect_ratio_type: int = 0
    aspect_ratio: float = 16.0 / 9.0
    zoom: tuple[float, float] = (1.0, 1.0)
    pan: tuple[float, float] = (0.0, 0.0)
    orientation_type: int = 0


@dataclass(slots=True)
class LensKeyFrame:
    time_ticks: int = 0
    true_lens_name: str = "Linear"
    export_lens_name: str = "Linear"
    true_lens_calibration: tuple[float, float, float, float] = (0.6366199851036072, 0.0, 0.0, 0.0)
    export_lens_calibration: tuple[float, float, float, float] = (0.6366199851036072, 0.0, 0.0, 0.0)
    true_fov: float = 180.0
    export_fov: float = 180.0


@dataclass(slots=True)
class AlignmentKeyFrame:
    time_ticks: int = 0
    position: tuple[float, float, float] = (0.0, 6.5, 0.0)
    rotation: tuple[float, float, float] = (0.0, 0.0, 0.0)


@dataclass(slots=True)
class StitchKeyFrame:
    time_ticks: int = 0
    shift: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    scale: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)
    shear: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    flare: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    slant: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)


@dataclass(slots=True)
class OrientationKeyFrame:
    time_ticks: int = 0
    orientation: tuple[float, float, float] = (0.0, 0.0, 0.0)


@dataclass(slots=True)
class AutoFocusKeyFrame:
    time_ticks: int = 0
    rotation: tuple[float, float, float] = (0.0, 0.0, 0.0)
    focal_distance_override: float = 0.0
    min_focus_distance: float = 10.0
    max_focus_distance: float = 1000.0
    focus_transition_speed: float = 1.0


@dataclass(slots=True)
class SyncKeyFrame:
    time_ticks: int = 0
    starting_ticks_offset: int = 0
    ticks_per_hour_offset: int = 0


@dataclass(slots=True)
class AudioKeyFrame:
    time_ticks: int = 0
    time_offset: float = 0.0
    volume_multiplier: float = 1.0


@dataclass(slots=True)
class HSPInspection:
    version: int
    unique_id: str
    title: str
    description: str
    format_keyframes: list[FormatKeyFrame]
    lens_keyframes: list[LensKeyFrame]
    stitch_keyframes: list[StitchKeyFrame]
    alignment_keyframes: list[AlignmentKeyFrame]
    orientation_keyframes: list[OrientationKeyFrame]
    autofocus_keyframes: list[AutoFocusKeyFrame]
    sync_keyframes: list[SyncKeyFrame]
    image_keyframes: list[ImageKeyFrame]
    audio_keyframes: list[AudioKeyFrame]
    array_counts: dict[str, int]
    raw_size: int
    compressed: bool
    trailing_bytes: int = 0
    container_prefix: int | None = None

    def dict(self) -> dict[str, Any]:
        return asdict(self)


class _Writer:
    def __init__(self) -> None:
        self._buffer = io.BytesIO()

    def raw(self, value: bytes) -> None:
        self._buffer.write(value)

    def i32(self, value: int) -> None:
        self.raw(struct.pack("<i", int(value)))

    def i64(self, value: int) -> None:
        self.raw(struct.pack("<q", int(value)))

    def u8(self, value: int) -> None:
        self.raw(struct.pack("<B", int(value)))

    def f32(self, value: float) -> None:
        self.raw(struct.pack("<f", float(value)))

    def bool32(self, value: bool) -> None:
        # UE4's non-editor FArchive serializes bool as a uint32.
        self.raw(struct.pack("<I", 1 if value else 0))

    def vec2(self, values) -> None:
        values = tuple(values)
        if len(values) != 2:
            raise HSPError("FVector2D requires exactly two values")
        for value in values:
            self.f32(value)

    def vec3(self, values) -> None:
        values = tuple(values)
        if len(values) != 3:
            raise HSPError("FVector/FRotator requires exactly three values")
        for value in values:
            self.f32(value)

    def vec4(self, values) -> None:
        values = tuple(values)
        if len(values) != 4:
            raise HSPError("FVector4/FLinearColor requires exactly four values")
        for value in values:
            self.f32(value)

    def fstring(self, value: str) -> None:
        value = value or ""
        # UE's canonical serialized empty FString is a zero length. Earlier
        # HereTune builds emitted a one-byte NUL string, which our parser could
        # read but did not match HereSphere's own exported presets byte-for-byte.
        if not value:
            self.i32(0)
            return
        try:
            encoded = value.encode("ascii")
        except UnicodeEncodeError:
            encoded = value.encode("utf-16-le")
            self.i32(-(len(encoded) // 2 + 1))
            self.raw(encoded + b"\0\0")
        else:
            self.i32(len(encoded) + 1)
            self.raw(encoded + b"\0")

    def value(self) -> bytes:
        return self._buffer.getvalue()


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def raw(self, size: int) -> bytes:
        if size < 0 or self.pos + size > len(self.data):
            raise HSPError(
                f"Unexpected end of HSP at byte {self.pos}; needed {size} more bytes"
            )
        value = self.data[self.pos:self.pos + size]
        self.pos += size
        return value

    def i32(self) -> int:
        return struct.unpack("<i", self.raw(4))[0]

    def i64(self) -> int:
        return struct.unpack("<q", self.raw(8))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.raw(4))[0]

    def u8(self) -> int:
        return self.raw(1)[0]

    def f32(self) -> float:
        return struct.unpack("<f", self.raw(4))[0]

    def bool32(self) -> bool:
        return bool(self.u32())

    def vec2(self) -> tuple[float, float]:
        return self.f32(), self.f32()

    def vec3(self) -> tuple[float, float, float]:
        return self.f32(), self.f32(), self.f32()

    def vec4(self) -> tuple[float, float, float, float]:
        return self.f32(), self.f32(), self.f32(), self.f32()

    def fstring(self) -> str:
        count = self.i32()
        if count == 0:
            return ""
        if count > 0:
            raw = self.raw(count)
            if raw.endswith(b"\0"):
                raw = raw[:-1]
            return raw.decode("utf-8", errors="replace")
        units = -count
        raw = self.raw(units * 2)
        if raw.endswith(b"\0\0"):
            raw = raw[:-2]
        return raw.decode("utf-16-le", errors="replace")


def datetime_ticks(value: datetime | None = None) -> int:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    epoch = datetime(1, 1, 1, tzinfo=timezone.utc)
    delta = value - epoch
    return (
        delta.days * 86_400 * TICKS_PER_SECOND
        + delta.seconds * TICKS_PER_SECOND
        + delta.microseconds * 10
    )


def _compress_segment(raw: bytes, chunk_size: int = PROXY_CHUNK_SIZE) -> bytes:
    if not raw:
        raise HSPError("UE compressed archives cannot contain an empty segment")
    chunks = [raw[i:i + chunk_size] for i in range(0, len(raw), chunk_size)]
    compressed = [zlib.compress(chunk, level=6) for chunk in chunks]
    out = io.BytesIO()
    out.write(struct.pack("<qq", PACKAGE_FILE_TAG, chunk_size))
    out.write(struct.pack("<qq", sum(map(len, compressed)), len(raw)))
    for zipped, original in zip(compressed, chunks):
        out.write(struct.pack("<qq", len(zipped), len(original)))
    for zipped in compressed:
        out.write(zipped)
    return out.getvalue()


def compress_proxy(raw: bytes, proxy_chunk_size: int = PROXY_CHUNK_SIZE) -> bytes:
    """Match FArchiveSaveCompressedProxy, including concatenated outer segments."""
    if not raw:
        raise HSPError("Cannot compress an empty HSP")
    return b"".join(
        _compress_segment(raw[i:i + proxy_chunk_size], proxy_chunk_size)
        for i in range(0, len(raw), proxy_chunk_size)
    )


def _decompress_segment(blob: bytes, offset: int) -> tuple[bytes, int]:
    if offset + 32 > len(blob):
        raise HSPError(f"Truncated UE compressed header at byte {offset}")
    tag, chunk_size = struct.unpack_from("<qq", blob, offset)
    if tag != PACKAGE_FILE_TAG:
        raise HSPError(f"Invalid UE compressed archive tag at byte {offset}")
    if chunk_size <= 0 or chunk_size > 64 * 1024 * 1024:
        raise HSPError(f"Unreasonable compression chunk size: {chunk_size}")

    total_compressed, total_uncompressed = struct.unpack_from("<qq", blob, offset + 16)
    if total_compressed < 0 or total_uncompressed <= 0:
        raise HSPError("Invalid UE compressed archive summary")
    if total_uncompressed > MAX_SAFE_HSP_RAW_BYTES:
        raise HSPError(
            f"HSP decompressed payload exceeds the {MAX_SAFE_HSP_RAW_BYTES // (1024 * 1024)} MiB safety limit"
        )
    chunk_count = max(1, math.ceil(total_uncompressed / chunk_size))
    table_pos = offset + 32
    data_pos = table_pos + chunk_count * 16
    if data_pos > len(blob):
        raise HSPError("Truncated UE compressed chunk table")

    chunks: list[tuple[int, int]] = []
    for index in range(chunk_count):
        compressed_size, uncompressed_size = struct.unpack_from(
            "<qq", blob, table_pos + index * 16
        )
        if compressed_size <= 0 or uncompressed_size <= 0:
            raise HSPError("Invalid compressed chunk size")
        chunks.append((compressed_size, uncompressed_size))

    if sum(size for size, _ in chunks) != total_compressed:
        raise HSPError("Compressed-size summary does not match the chunk table")

    out: list[bytes] = []
    pos = data_pos
    for compressed_size, uncompressed_size in chunks:
        end = pos + compressed_size
        if end > len(blob):
            raise HSPError("Truncated compressed chunk data")
        try:
            raw = zlib.decompress(blob[pos:end])
        except zlib.error as exc:
            raise HSPError(f"HSP zlib decompression failed: {exc}") from exc
        if len(raw) != uncompressed_size:
            raise HSPError("Uncompressed chunk size mismatch")
        out.append(raw)
        pos = end

    value = b"".join(out)
    if len(value) != total_uncompressed:
        raise HSPError("Uncompressed-size summary mismatch")
    return value, pos


def decompress_proxy(blob: bytes) -> bytes:
    """Decompress one or more concatenated FArchiveSaveCompressedProxy segments."""
    if len(blob) < 8:
        raise HSPError("HSP is too small")
    out: list[bytes] = []
    pos = 0
    total_raw = 0
    while pos < len(blob):
        segment, next_pos = _decompress_segment(blob, pos)
        if next_pos <= pos:
            raise HSPError("Compressed archive parser made no progress")
        total_raw += len(segment)
        if total_raw > MAX_SAFE_HSP_RAW_BYTES:
            raise HSPError(
                f"HSP decompressed payload exceeds the {MAX_SAFE_HSP_RAW_BYTES // (1024 * 1024)} MiB safety limit"
            )
        out.append(segment)
        pos = next_pos
    return b"".join(out)



def _raw_bytes(blob: bytes) -> tuple[bytes, bool]:
    if len(blob) >= 8 and struct.unpack_from("<q", blob, 0)[0] == PACKAGE_FILE_TAG:
        return decompress_proxy(blob), True
    return blob, False


def _payload_raw(blob: bytes) -> tuple[bytes, bool, int]:
    """Return the payload from a native standalone HereSphere HSP.

    Public HereTune accepts the standalone file form observed from HereSphere:
    the Unreal compressed envelope decompresses to ``int32 1024`` followed by
    the HSP/UMediaPreset version and payload. Embedded ``Library.hsl`` records
    are a different container and are documented separately; they are not valid
    inputs to HereTune's standalone-HSP runtime paths.
    """
    raw, compressed = _raw_bytes(blob)
    if len(raw) < 8:
        raise HSPError("Standalone HSP is too small")
    prefix, found_version = struct.unpack_from("<ii", raw, 0)
    if prefix != STANDALONE_HSP_PREFIX:
        raise HSPError("Standalone HSP wrapper is missing")
    if not 0 <= found_version <= 10_000:
        raise HSPError(f"Unreasonable HSP version {found_version}")
    return raw[4:], compressed, prefix


def version(blob: bytes) -> int | None:
    try:
        raw, _, _ = _payload_raw(blob)
        value = struct.unpack_from("<i", raw, 0)[0]
        return value if 0 <= value <= 10_000 else None
    except (HSPError, struct.error):
        return None


def _array(reader: _Reader, item_reader: Callable[[], Any]) -> list[Any]:
    count = reader.i32()
    if count < 0 or count > MAX_REASONABLE_COUNT:
        raise HSPError(f"Unreasonable HSP array count {count} at byte {reader.pos - 4}")
    return [item_reader() for _ in range(count)]


def _read_tag(reader: _Reader, hsp_version: int) -> None:
    reader.fstring()
    reader.f32()
    reader.i64()
    reader.i64()
    if hsp_version >= 3:
        reader.i32()


def _read_format(reader: _Reader, hsp_version: int) -> FormatKeyFrame:
    value = FormatKeyFrame(
        time_ticks=reader.i64(),
        projection_mode=reader.u8(),
        stereo_mode=reader.u8(),
        eye_swap_enabled=reader.bool32(),
    )
    if hsp_version >= 6:
        value.force_mono_enabled = reader.bool32()
    if hsp_version >= 7:
        value.flip_front_back = reader.bool32()
        value.flip_left_right = reader.bool32()
        value.flip_up_down = reader.bool32()
    value.aspect_ratio_type = reader.u8()
    value.aspect_ratio = reader.f32()
    value.zoom = reader.vec2()
    value.pan = reader.vec2()
    if hsp_version >= 1:
        value.orientation_type = reader.u8()
    return value


def _read_lens(reader: _Reader, _: int) -> LensKeyFrame:
    return LensKeyFrame(
        time_ticks=reader.i64(),
        true_lens_name=reader.fstring(),
        export_lens_name=reader.fstring(),
        true_lens_calibration=reader.vec4(),
        export_lens_calibration=reader.vec4(),
        true_fov=reader.f32(),
        export_fov=reader.f32(),
    )


def _read_stitch(reader: _Reader, _: int) -> StitchKeyFrame:
    return StitchKeyFrame(
        time_ticks=reader.i64(),
        shift=reader.vec4(),
        scale=reader.vec4(),
        shear=reader.vec4(),
        flare=reader.vec4(),
        slant=reader.vec4(),
    )


def _read_alignment(reader: _Reader, _: int) -> AlignmentKeyFrame:
    return AlignmentKeyFrame(
        time_ticks=reader.i64(),
        position=reader.vec3(),
        rotation=reader.vec3(),
    )


def _read_orientation(reader: _Reader, _: int) -> OrientationKeyFrame:
    return OrientationKeyFrame(time_ticks=reader.i64(), orientation=reader.vec3())


def _read_origin(reader: _Reader, _: int) -> None:
    reader.i64(); reader.vec3()


def _read_motion(reader: _Reader, _: int) -> None:
    reader.i64(); reader.f32()


def _read_autofocus(reader: _Reader, hsp_version: int) -> AutoFocusKeyFrame:
    value = AutoFocusKeyFrame(
        time_ticks=reader.i64(),
        rotation=reader.vec3(),
        focal_distance_override=reader.f32(),
    )
    if hsp_version >= 2:
        value.min_focus_distance = reader.f32()
        value.max_focus_distance = reader.f32()
        value.focus_transition_speed = reader.f32()
    return value


def _read_sync(reader: _Reader, _: int) -> SyncKeyFrame:
    return SyncKeyFrame(
        time_ticks=reader.i64(),
        starting_ticks_offset=reader.i64(),
        ticks_per_hour_offset=reader.i64(),
    )


def _read_transition(reader: _Reader, _: int) -> None:
    reader.i64(); reader.u8(); reader.i64()


def _read_image(reader: _Reader, hsp_version: int) -> ImageKeyFrame:
    keyframe = ImageKeyFrame(
        time_ticks=reader.i64(),
        sharpen=reader.f32(),
        exposure_bias=reader.f32(),
        white_temp=reader.f32(),
        white_tint=reader.f32(),
        saturation=reader.vec4(),
        contrast=reader.vec4(),
        gamma=reader.vec4(),
        gain=reader.vec4(),
        offset=reader.vec4(),
    )
    if hsp_version >= 4:
        keyframe.shadows = reader.f32()
        keyframe.midtones = reader.f32()
        keyframe.highlights = reader.f32()
    return keyframe


def _read_audio(reader: _Reader, _: int) -> AudioKeyFrame:
    return AudioKeyFrame(
        time_ticks=reader.i64(),
        time_offset=reader.f32(),
        volume_multiplier=reader.f32(),
    )


def _read_environment(reader: _Reader, hsp_version: int) -> None:
    if hsp_version < 7:
        return
    reader.i64(); reader.u8(); reader.vec4(); reader.fstring(); reader.u8(); reader.f32()
    for _ in range(10):
        reader.vec4()


def inspect(blob: bytes, *, require_complete: bool = True) -> HSPInspection:
    raw, was_compressed, container_prefix = _payload_raw(blob)
    reader = _Reader(raw)
    hsp_version = reader.i32()
    if not 0 <= hsp_version <= SUPPORTED_HSP_VERSION:
        raise HSPError(
            f"HSP version {hsp_version} is newer than the parser's supported version "
            f"{SUPPORTED_HSP_VERSION}"
        )

    unique_id = reader.fstring()
    title = reader.fstring()
    description = reader.fstring()
    date_added = reader.i64()
    if hsp_version >= 5:
        reader.i64()  # DateReleased
    _ = date_added
    reader.i64()  # DateLastPlayed
    reader.i64()  # DateEdited
    reader.i64()  # Duration
    reader.i64()  # ResumeTime
    if hsp_version >= 7:
        reader.i64(); reader.i64()  # AB loop
    reader.i32(); reader.i32(); reader.i32()
    reader.bool32(); reader.f32()
    if hsp_version >= 4:
        reader.i32()  # AudioTrack

    tags = _array(reader, lambda: _read_tag(reader, hsp_version))
    formats = _array(reader, lambda: _read_format(reader, hsp_version))
    lenses = _array(reader, lambda: _read_lens(reader, hsp_version))
    stitches = _array(reader, lambda: _read_stitch(reader, hsp_version))
    alignments = _array(reader, lambda: _read_alignment(reader, hsp_version))
    orientations = _array(reader, lambda: _read_orientation(reader, hsp_version))
    origins = _array(reader, lambda: _read_origin(reader, hsp_version))
    motions = _array(reader, lambda: _read_motion(reader, hsp_version))
    autofocus = _array(reader, lambda: _read_autofocus(reader, hsp_version))
    syncs = _array(reader, lambda: _read_sync(reader, hsp_version))
    transitions = _array(reader, lambda: _read_transition(reader, hsp_version))
    images = _array(reader, lambda: _read_image(reader, hsp_version))
    audios = _array(reader, lambda: _read_audio(reader, hsp_version)) if hsp_version >= 3 else []
    environments = _array(reader, lambda: _read_environment(reader, hsp_version)) if hsp_version >= 7 else []

    array_counts = {
        "tags": len(tags),
        "format": len(formats),
        "lens": len(lenses),
        "stitch": len(stitches),
        "alignment": len(alignments),
        "orientation": len(orientations),
        "origin": len(origins),
        "motion": len(motions),
        "autofocus": len(autofocus),
        "sync": len(syncs),
        "transition": len(transitions),
        "image": len(images),
        "audio": len(audios),
        "environment": len(environments),
    }

    trailing = len(raw) - reader.pos
    if require_complete and trailing:
        raise HSPError(f"HSP contains {trailing} unexpected trailing raw bytes")
    return HSPInspection(
        version=hsp_version,
        unique_id=unique_id,
        title=title,
        description=description,
        format_keyframes=formats,
        lens_keyframes=lenses,
        stitch_keyframes=stitches,
        alignment_keyframes=alignments,
        orientation_keyframes=orientations,
        autofocus_keyframes=autofocus,
        sync_keyframes=syncs,
        image_keyframes=images,
        audio_keyframes=audios,
        array_counts=array_counts,
        raw_size=len(raw),
        compressed=was_compressed,
        trailing_bytes=trailing,
        container_prefix=container_prefix,
    )


def validate(blob: bytes, *, expected_version: int | None = None) -> HSPInspection | None:
    raw, _, _ = _payload_raw(blob)
    if len(raw) < 4:
        raise HSPError("Standalone HSP payload is too small")
    found = struct.unpack_from("<i", raw, 0)[0]
    if not 0 <= found <= 10_000:
        raise HSPError(f"Unreasonable HSP version {found}")
    if expected_version is not None and found != expected_version:
        raise HSPError(f"Expected HSP v{expected_version}, got v{found}")
    # Preserve future write-backs byte-for-byte even when their extended layout is
    # not understood. Current/generated v7 profiles get strict structural parsing.
    if found > SUPPORTED_HSP_VERSION:
        return None
    return inspect(blob)


def validate_playback_safe(
    blob: bytes,
    *,
    expected_filename: str | None = None,
    duration_seconds: float | None = None,
) -> HSPInspection:
    """Strict semantic validation for bytes that may be loaded by HereSphere.

    Structural round-tripping alone is not enough for a live player. This rejects
    missing standalone wrapping, incomplete fixed groups, duplicate/out-of-order
    Image times, non-finite values, and obviously out-of-duration keyframes.
    """
    parsed = validate(blob, expected_version=SUPPORTED_HSP_VERSION)
    if parsed is None:
        raise HSPError("Future HSP versions cannot be playback-validated by this build")
    if parsed.container_prefix != STANDALONE_HSP_PREFIX:
        raise HSPError("Standalone HSP wrapper is missing")
    if parsed.trailing_bytes:
        raise HSPError(f"HSP contains {parsed.trailing_bytes} trailing bytes")
    fixed = (
        "format", "lens", "stitch", "alignment", "orientation", "origin",
        "motion", "autofocus", "sync", "transition", "audio", "environment",
    )
    for name in fixed:
        if parsed.array_counts.get(name) != 1:
            raise HSPError(f"Playback-safe HSP requires exactly one {name} keyframe")
    count = parsed.array_counts.get("image", 0)
    if count < 1 or count > MAX_SAFE_IMAGE_KEYFRAMES:
        raise HSPError(f"Playback-safe HSP requires 1-{MAX_SAFE_IMAGE_KEYFRAMES} Image keyframes")
    if expected_filename:
        expected = str(expected_filename)
        if parsed.unique_id != expected or parsed.title != expected:
            raise HSPError("HSP identity does not match the source filename")

    previous = -1
    max_ticks = None
    try:
        seconds = float(duration_seconds or 0.0)
        if seconds > 0 and math.isfinite(seconds):
            max_ticks = round(seconds * TICKS_PER_SECOND) + TICKS_PER_SECOND
    except (TypeError, ValueError):
        max_ticks = None
    for index, image in enumerate(parsed.image_keyframes):
        if index == 0 and image.time_ticks != 0:
            raise HSPError("The first Image keyframe must start at time zero")
        if image.time_ticks <= previous:
            raise HSPError("Image keyframe times must be strictly increasing")
        if image.time_ticks < 0 or (max_ticks is not None and image.time_ticks > max_ticks):
            raise HSPError("Image keyframe time falls outside the media duration")
        previous = image.time_ticks
        numeric_values = (
            image.sharpen, image.exposure_bias, image.white_temp, image.white_tint,
            image.shadows, image.midtones, image.highlights,
            *image.saturation, *image.contrast, *image.gamma, *image.gain, *image.offset,
        )
        if not all(math.isfinite(float(value)) for value in numeric_values):
            raise HSPError("Image keyframe contains a non-finite value")

        # Corrupt-but-finite floats can be just as dangerous as NaN/Inf to a
        # native renderer. These are intentionally broad envelopes around all
        # values HereTune and current HereSphere exports are expected to use.
        if not -1.0 <= float(image.sharpen) <= 10.0:
            raise HSPError("Image sharpen value is outside a playback-safe range")
        if not -10.0 <= float(image.exposure_bias) <= 10.0:
            raise HSPError("Image exposure value is outside a playback-safe range")
        if not 500.0 <= float(image.white_temp) <= 50_000.0:
            raise HSPError("Image white temperature is outside a playback-safe range")
        if not -10.0 <= float(image.white_tint) <= 10.0:
            raise HSPError("Image white tint is outside a playback-safe range")
        if not all(0.001 <= float(value) <= 16.0 for value in image.gamma):
            raise HSPError("Image gamma vector is outside a playback-safe range")
        if not all(0.0 <= float(value) <= 16.0 for value in (*image.saturation, *image.contrast, *image.gain)):
            raise HSPError("Image color vector is outside a playback-safe range")
        if not all(-16.0 <= float(value) <= 16.0 for value in image.offset):
            raise HSPError("Image offset vector is outside a playback-safe range")
        if not -4.0 <= float(image.shadows) <= 4.0:
            raise HSPError("Image Shadows value is outside a playback-safe range")
        if not 0.0 <= float(image.midtones) <= 8.0:
            raise HSPError("Image Midtones value is outside a playback-safe range")
        if not 0.0 <= float(image.highlights) <= 8.0:
            raise HSPError("Image Highlights value is outside a playback-safe range")
    return parsed


def _finite(value: float, name: str) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise HSPError(f"{name} must be finite")
    return value


def _bounded(value: float, lo: float, hi: float, name: str) -> float:
    return max(lo, min(hi, _finite(value, name)))


def _image_from_native(native: dict[str, object], *, time_ticks: int = 0) -> ImageKeyFrame:
    """Validate one complete native image-keyframe mapping."""
    def vec(values, lo, hi, name):
        values = tuple(values)
        if len(values) != 4:
            raise HSPError(f"{name} must contain four values")
        return tuple(_bounded(v, lo, hi, name) for v in values)

    return ImageKeyFrame(
        time_ticks=int(time_ticks),
        sharpen=_bounded(native.get("sharpen", 2.0), 0.0, 10.0, "sharpen"),
        exposure_bias=_bounded(native.get("exposure_bias", 0.0), -2.0, 2.0, "exposure"),
        white_temp=_bounded(native.get("white_temp", 6500.0), 1000.0, 15000.0, "temperature"),
        white_tint=_bounded(native.get("white_tint", 0.0), -1.0, 1.0, "tint"),
        saturation=vec(native["saturation"], 0.5, 1.5, "saturation"),
        contrast=vec(native["contrast"], 0.5, 1.5, "contrast"),
        gamma=vec(native["gamma"], 0.5, 1.5, "gamma"),
        gain=vec(native["gain"], 0.5, 1.5, "gain"),
        offset=vec(native["offset"], -0.25, 0.25, "offset"),
        shadows=_bounded(native.get("shadows", 0.0), -1.0, 1.0, "shadows"),
        midtones=_bounded(native.get("midtones", 1.0), 0.0, 2.0, "midtones"),
        highlights=_bounded(native.get("highlights", 1.0), 0.0, 2.0, "highlights"),
    )


def image_from_correction(correction, *, time_ticks: int = 0) -> ImageKeyFrame:
    if hasattr(correction, "native_image_settings"):
        native = correction.native_image_settings()
    else:
        native = {
            "sharpen": 2.0,
            "exposure_bias": 0.0,
            "white_temp": 6500.0,
            "white_tint": 0.0,
            "saturation": (1.0, 1.0, 1.0, getattr(correction, "saturation", 1.0)),
            "contrast": (1.0, 1.0, 1.0, getattr(correction, "contrast", 1.0)),
            "gamma": (1.0, 1.0, 1.0, getattr(correction, "gamma", 1.0)),
            "gain": (
                getattr(correction, "r_gain", 1.0),
                getattr(correction, "g_gain", 1.0),
                getattr(correction, "b_gain", 1.0),
                1.0,
            ),
            "offset": (0.0, 0.0, 0.0, getattr(correction, "brightness", 0.0)),
            "shadows": 0.0,
            "midtones": 1.0,
            "highlights": 1.0,
        }
    return _image_from_native(native, time_ticks=time_ticks)


def image_keyframes_from_correction(
    correction,
    *,
    duration_seconds: float | None = None,
    max_keyframes: int = MAX_SAFE_IMAGE_KEYFRAMES,
) -> list[ImageKeyFrame]:
    """Return a canonical, playback-safe sparse Image sequence.

    HereSphere is much happier when keyframe times are finite, unique, strictly
    increasing, start at zero, and remain inside the media duration. Analysis
    evidence can contain boundaries that round to the same tick; those are
    collapsed here rather than handed to the active player.
    """
    scene_values = getattr(correction, "scene_keyframes", None)
    if not isinstance(scene_values, list) or not scene_values:
        return [image_from_correction(correction)]

    duration_ticks = 0
    try:
        duration_ticks = max(0, round(float(duration_seconds or 0.0) * TICKS_PER_SECOND))
    except (TypeError, ValueError):
        duration_ticks = 0
    max_time = max(0, duration_ticks - 1) if duration_ticks else None

    by_time: dict[int, ImageKeyFrame] = {}
    for item in scene_values:
        if not isinstance(item, dict):
            continue
        native = item.get("native")
        if not isinstance(native, dict):
            continue
        try:
            seconds = max(0.0, float(item.get("time_seconds") or 0.0))
            ticks = round(seconds * TICKS_PER_SECOND)
            if max_time is not None:
                ticks = min(ticks, max_time)
            # Keep the last complete grade when two boundaries round to one tick.
            by_time[int(ticks)] = _image_from_native(native, time_ticks=int(ticks))
        except (TypeError, ValueError, HSPError, OverflowError):
            continue
    if not by_time:
        return [image_from_correction(correction)]

    result = [by_time[key] for key in sorted(by_time)]
    result[0].time_ticks = 0
    # Promoting the first key to zero can collide only if malformed input had a
    # later duplicate zero; collapse once more defensively.
    canonical: dict[int, ImageKeyFrame] = {int(item.time_ticks): item for item in result}
    result = [canonical[key] for key in sorted(canonical)]
    if result[0].time_ticks != 0:
        result[0].time_ticks = 0

    safe_limit = max(1, int(max_keyframes or MAX_SAFE_IMAGE_KEYFRAMES))
    if len(result) > safe_limit:
        # Preserve first/last and evenly sample the interior. The full evidence
        # stays in SQLite; only the player-facing HSP receives the safety cap.
        if safe_limit == 1:
            result = [result[0]]
        else:
            indices = {
                round(index * (len(result) - 1) / (safe_limit - 1))
                for index in range(safe_limit)
            }
            result = [result[index] for index in sorted(indices)]
            result[0].time_ticks = 0
    return result



_PROJECTION_MODES = {
    "perspective": 0,
    "equirectangular": 1,
    "fisheye": 2,
    "equirectangular360": 3,
    "cubemap": 4,
    "equiangularcubemap": 5,
}
_STEREO_MODES = {"mono": 0, "sbs": 1, "tb": 2}
_ASPECT_RATIO_TYPES = {
    "autodetect": 0,
    "auto": 0,
    "standard": 1,
    "highdefinition": 2,
    "high_definition": 2,
    "hd": 2,
    "widescreen": 3,
    "wide_screen": 3,
    "custom": 4,
}
_ORIENTATION_TYPES = {"up": 0, "left": 1, "right": 2, "down": 3}
_LINEAR_CALIBRATION = (0.6366199851036072, 0.0, 0.0, 0.0)
_MKX200_CALIBRATION = (
    0.58980000019,
    -0.01329999976,
    0.01930000074,
    -0.009899999946,
)


def _projection_mode(metadata: dict[str, Any]) -> int:
    value = str(metadata.get("projection") or "perspective").replace("_", "").replace("-", "").casefold()
    return _PROJECTION_MODES.get(value, 0)


def _stereo_mode(metadata: dict[str, Any]) -> int:
    stereo = str(metadata.get("stereo") or "mono").casefold()
    if stereo == "sbs" and bool(metadata.get("full_side_by_side")):
        return 3
    return _STEREO_MODES.get(stereo, 0)


def _source_aspect(metadata: dict[str, Any]) -> float:
    try:
        value = float(metadata.get("source_aspect_ratio") or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    if value > 0:
        return value
    try:
        width = float(metadata.get("width") or 0.0)
        height = float(metadata.get("height") or 0.0)
    except (TypeError, ValueError):
        width = height = 0.0
    return width / height if width > 0 and height > 0 else 16.0 / 9.0


def _enum_value(value: object, mapping: dict[str, int], fallback: int) -> int:
    if value is None:
        return fallback
    if isinstance(value, str):
        key = value.strip().replace("-", "_").replace(" ", "_").casefold()
        compact = key.replace("_", "")
        if key in mapping:
            return mapping[key]
        if compact in mapping:
            return mapping[compact]
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if 0 <= parsed <= 255 else fallback


def _vec2(value: object, fallback: tuple[float, float]) -> tuple[float, float]:
    try:
        parts = tuple(float(part) for part in value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    if len(parts) != 2 or not all(math.isfinite(part) for part in parts):
        return fallback
    return parts


def _vec3(value: object, fallback: tuple[float, float, float]) -> tuple[float, float, float]:
    try:
        parts = tuple(float(part) for part in value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    if len(parts) != 3 or not all(math.isfinite(part) for part in parts):
        return fallback
    return parts


def _vec4(value: object, fallback: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    try:
        parts = tuple(float(part) for part in value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    if len(parts) != 4 or not all(math.isfinite(part) for part in parts):
        return fallback
    return parts


def format_from_metadata(metadata: dict[str, Any] | None = None) -> FormatKeyFrame:
    """Create the native projection keyframe HereSphere stores in current v7 presets.

    The April-2025/current-library corpus used to validate HereTune contains one
    Format keyframe in every one of 2,832 saved media presets.  Generated HSPs
    therefore carry the projection/stereo state rather than relying on an empty
    array and hoping the player merges JSON state into a sparse preset.
    """
    metadata = dict(metadata or {})
    projection = _projection_mode(metadata)
    stereo = _stereo_mode(metadata)

    # Current saved VR presets overwhelmingly use Custom aspect 2.0 for SBS and
    # 1.0 for TB/mono spherical content. Perspective mono files use Autodetect.
    if stereo in {1, 3}:  # SBS / Full SBS
        aspect_type, aspect = 4, 2.0
    elif stereo == 2:  # Top/Bottom
        aspect_type, aspect = 4, 1.0
    elif projection != 0:
        aspect_type, aspect = 4, 1.0
    else:
        aspect_type, aspect = 0, _source_aspect(metadata)

    return FormatKeyFrame(
        projection_mode=projection,
        stereo_mode=stereo,
        eye_swap_enabled=bool(metadata.get("isEyeSwapped", False)),
        force_mono_enabled=bool(metadata.get("force_mono", False)),
        flip_front_back=bool(metadata.get("flip_front_back", False)),
        flip_left_right=bool(metadata.get("flip_left_right", False)),
        flip_up_down=bool(metadata.get("flip_up_down", False)),
        aspect_ratio_type=_enum_value(
            metadata.get("aspect_ratio_type"), _ASPECT_RATIO_TYPES, aspect_type
        ),
        aspect_ratio=float(metadata.get("aspect_ratio", aspect)),
        zoom=_vec2(metadata.get("zoom"), (1.0, 1.0)),
        pan=_vec2(metadata.get("pan"), (0.0, 0.0)),
        orientation_type=_enum_value(
            metadata.get("orientation_type"), _ORIENTATION_TYPES, 0
        ),
    )


def lens_from_metadata(metadata: dict[str, Any] | None = None) -> LensKeyFrame:
    """Create the native lens keyframe using values observed in real v7 presets."""
    metadata = dict(metadata or {})
    lens = str(metadata.get("lens") or "Linear").strip()
    key = lens.casefold().replace(" ", "")
    try:
        fov = float(metadata.get("fov") or 0.0)
    except (TypeError, ValueError):
        fov = 0.0

    if "mkx200" in key:
        return LensKeyFrame(
            true_lens_name="iZugar MKX200",
            export_lens_name="Linear",
            true_lens_calibration=_MKX200_CALIBRATION,
            export_lens_calibration=_LINEAR_CALIBRATION,
            true_fov=fov if fov > 0 else 200.0,
            export_fov=180.0,
        )

    # The supplied current saved library contains exact calibration evidence for
    # Linear and MKX200. For other named 220-degree lenses we retain the name/FOV
    # while using the neutral linear calibration rather than inventing proprietary
    # coefficients. The filename-preserving media URL gives HereSphere its normal
    # filename-recognition path as an additional source of truth.
    true_name = lens or "Linear"
    if "mkx220" in key:
        true_name = "iZugar MKX22"
    elif "vrca220" in key:
        true_name = "DZO VRCA 220"
    return LensKeyFrame(
        true_lens_name=true_name,
        export_lens_name="Linear",
        true_lens_calibration=_LINEAR_CALIBRATION,
        export_lens_calibration=_LINEAR_CALIBRATION,
        true_fov=fov if fov > 0 else 180.0,
        export_fov=180.0,
    )


def alignment_from_metadata(metadata: dict[str, Any] | None = None) -> AlignmentKeyFrame:
    metadata = dict(metadata or {})
    position = metadata.get("alignment_position") or (0.0, 6.5, 0.0)
    rotation = metadata.get("alignment_rotation") or (0.0, 0.0, 0.0)

    def triple(values, fallback):
        try:
            result = tuple(float(value) for value in values)
        except (TypeError, ValueError):
            return fallback
        if len(result) != 3 or not all(math.isfinite(value) for value in result):
            return fallback
        return result

    position_value = triple(position, (0.0, 6.5, 0.0))
    rotation_value = triple(rotation, (0.0, 0.0, 0.0))
    # Never let automatic calibration rewrite the camera IPD or produce a
    # dramatic world rotation. The visual verifier is intentionally capped, and
    # the serializer enforces a second safety boundary.
    position_value = (
        max(-2.0, min(2.0, position_value[0])),
        max(5.0, min(8.0, position_value[1])),
        max(-2.0, min(2.0, position_value[2])),
    )
    rotation_value = tuple(max(-2.0, min(2.0, value)) for value in rotation_value)
    return AlignmentKeyFrame(position=position_value, rotation=rotation_value)


def stitch_from_metadata(metadata: dict[str, Any] | None = None) -> StitchKeyFrame:
    """Create a conservative stitch keyframe.

    Automatic stitch suggestions are bounded very tightly because the four
    components are player-native calibration axes rather than generic pixels.
    Explicit per-video overrides use the same bounds, preventing a typo from
    turning the projection into modern art.
    """
    metadata = dict(metadata or {})
    shift = _vec4(metadata.get("stitch_shift"), (0.0, 0.0, 0.0, 0.0))
    scale = _vec4(metadata.get("stitch_scale"), (1.0, 1.0, 1.0, 1.0))
    shear = _vec4(metadata.get("stitch_shear"), (0.0, 0.0, 0.0, 0.0))
    flare = _vec4(metadata.get("stitch_flare"), (0.0, 0.0, 0.0, 0.0))
    slant = _vec4(metadata.get("stitch_slant"), (0.0, 0.0, 0.0, 0.0))
    return StitchKeyFrame(
        shift=tuple(max(-0.15, min(0.15, value)) for value in shift),
        scale=tuple(max(0.85, min(1.15, value)) for value in scale),
        shear=tuple(max(-0.10, min(0.10, value)) for value in shear),
        flare=tuple(max(-0.10, min(0.10, value)) for value in flare),
        slant=tuple(max(-0.10, min(0.10, value)) for value in slant),
    )


def orientation_from_metadata(metadata: dict[str, Any] | None = None) -> OrientationKeyFrame:
    metadata = dict(metadata or {})
    value = _vec3(metadata.get("orientation_rotation"), (0.0, 0.0, 0.0))
    # Horizon leveling should remain subtle. Cardinal 90/180-degree mistakes use
    # the Format orientation/flip flags instead of a giant free rotation.
    value = tuple(max(-8.0, min(8.0, part)) for part in value)
    return OrientationKeyFrame(orientation=value)


def autofocus_from_metadata(metadata: dict[str, Any] | None = None) -> AutoFocusKeyFrame:
    metadata = dict(metadata or {})
    rotation = _vec3(metadata.get("autofocus_rotation"), (0.0, 0.0, 0.0))

    def number(key: str, fallback: float) -> float:
        try:
            result = float(metadata.get(key, fallback))
        except (TypeError, ValueError):
            return fallback
        return result if math.isfinite(result) else fallback

    minimum = max(1.0, min(1000.0, number("autofocus_min_distance", 10.0)))
    maximum = max(minimum + 1.0, min(5000.0, number("autofocus_max_distance", 1000.0)))
    return AutoFocusKeyFrame(
        rotation=tuple(max(-8.0, min(8.0, part)) for part in rotation),
        focal_distance_override=max(0.0, min(5000.0, number(
            "autofocus_focal_override",
            number("autofocus_focal_distance", 0.0),
        ))),
        min_focus_distance=minimum,
        max_focus_distance=maximum,
        focus_transition_speed=max(0.05, min(10.0, number("autofocus_transition_speed", 1.0))),
    )


def sync_from_metadata(metadata: dict[str, Any] | None = None) -> SyncKeyFrame:
    metadata = dict(metadata or {})
    try:
        start_seconds = float(metadata.get("sync_start_offset_seconds") or 0.0)
    except (TypeError, ValueError):
        start_seconds = 0.0
    try:
        drift_seconds_per_hour = float(metadata.get("sync_drift_seconds_per_hour") or 0.0)
    except (TypeError, ValueError):
        drift_seconds_per_hour = 0.0
    start_seconds = max(-5.0, min(5.0, start_seconds))
    drift_seconds_per_hour = max(-2.0, min(2.0, drift_seconds_per_hour))
    return SyncKeyFrame(
        starting_ticks_offset=round(start_seconds * TICKS_PER_SECOND),
        ticks_per_hour_offset=round(drift_seconds_per_hour * TICKS_PER_SECOND),
    )


def audio_from_metadata(metadata: dict[str, Any] | None = None) -> AudioKeyFrame:
    metadata = dict(metadata or {})
    try:
        offset = float(metadata.get("audio_time_offset") or 0.0)
    except (TypeError, ValueError):
        offset = 0.0
    try:
        volume = float(metadata.get("audio_volume_multiplier") or 1.0)
    except (TypeError, ValueError):
        volume = 1.0
    return AudioKeyFrame(
        time_offset=max(-2.0, min(2.0, offset)),
        volume_multiplier=max(0.25, min(4.0, volume)),
    )


def _write_format(writer: _Writer, value: FormatKeyFrame) -> None:
    writer.i64(value.time_ticks)
    writer.u8(value.projection_mode)
    writer.u8(value.stereo_mode)
    writer.bool32(value.eye_swap_enabled)
    writer.bool32(value.force_mono_enabled)
    writer.bool32(value.flip_front_back)
    writer.bool32(value.flip_left_right)
    writer.bool32(value.flip_up_down)
    writer.u8(value.aspect_ratio_type)
    writer.f32(value.aspect_ratio)
    writer.vec2(value.zoom)
    writer.vec2(value.pan)
    writer.u8(value.orientation_type)


def _write_lens(writer: _Writer, value: LensKeyFrame) -> None:
    writer.i64(value.time_ticks)
    writer.fstring(value.true_lens_name)
    writer.fstring(value.export_lens_name)
    writer.vec4(value.true_lens_calibration)
    writer.vec4(value.export_lens_calibration)
    writer.f32(value.true_fov)
    writer.f32(value.export_fov)


def _write_alignment(writer: _Writer, value: AlignmentKeyFrame) -> None:
    writer.i64(value.time_ticks)
    writer.vec3(value.position)
    writer.vec3(value.rotation)


def _write_stitch(writer: _Writer, value: StitchKeyFrame) -> None:
    writer.i64(value.time_ticks)
    writer.vec4(value.shift)
    writer.vec4(value.scale)
    writer.vec4(value.shear)
    writer.vec4(value.flare)
    writer.vec4(value.slant)


def _write_orientation(writer: _Writer, value: OrientationKeyFrame) -> None:
    writer.i64(value.time_ticks)
    writer.vec3(value.orientation)


def _write_autofocus(writer: _Writer, value: AutoFocusKeyFrame) -> None:
    writer.i64(value.time_ticks)
    writer.vec3(value.rotation)
    writer.f32(value.focal_distance_override)
    writer.f32(value.min_focus_distance)
    writer.f32(value.max_focus_distance)
    writer.f32(value.focus_transition_speed)


def _write_sync(writer: _Writer, value: SyncKeyFrame) -> None:
    writer.i64(value.time_ticks)
    writer.i64(value.starting_ticks_offset)
    writer.i64(value.ticks_per_hour_offset)


def _write_image(writer: _Writer, image: ImageKeyFrame) -> None:
    writer.i64(image.time_ticks)
    writer.f32(image.sharpen)
    writer.f32(image.exposure_bias)
    writer.f32(image.white_temp)
    writer.f32(image.white_tint)
    writer.vec4(image.saturation)
    writer.vec4(image.contrast)
    writer.vec4(image.gamma)
    writer.vec4(image.gain)
    writer.vec4(image.offset)
    writer.f32(image.shadows)
    writer.f32(image.midtones)
    writer.f32(image.highlights)


def _write_audio(writer: _Writer, value: AudioKeyFrame) -> None:
    writer.i64(value.time_ticks)
    writer.f32(value.time_offset)
    writer.f32(value.volume_multiplier)


def build(
    video_id: str,
    title: str,
    duration_seconds: float,
    correction,
    *,
    metadata: dict[str, Any] | None = None,
    source_filename: str | None = None,
    hsp_version: int = SUPPORTED_HSP_VERSION,
    timestamp_ticks: int | None = None,
    timestamp: datetime | None = None,
    profile_label: str = "Auto",
) -> bytes:
    """Build a complete native HereSphere v7 media preset.

    Earlier HereTune releases emitted an image-only HSP with zero-count Format,
    Lens and other keyframe arrays. Inspection of the user's 2,832 real current
    HereSphere presets showed that every preset carries one keyframe in every
    projection/image/audio/environment group. A complete preset is important
    when HereSphere's "Always Load HSP" option is enabled: the HSP itself must
    preserve stereo/projection state while applying the image correction.
    """
    if hsp_version != SUPPORTED_HSP_VERSION:
        raise HSPError(
            f"HereTune only generates validated HSP v{SUPPORTED_HSP_VERSION} profiles"
        )
    metadata = dict(metadata or {})
    image_keys = image_keyframes_from_correction(correction, duration_seconds=duration_seconds)
    format_key = format_from_metadata(metadata)
    lens_key = lens_from_metadata(metadata)
    stitch_key = stitch_from_metadata(metadata)
    alignment_key = alignment_from_metadata(metadata)
    orientation_key = orientation_from_metadata(metadata)
    autofocus_key = autofocus_from_metadata(metadata)
    sync_key = sync_from_metadata(metadata)
    audio_key = audio_from_metadata(metadata)
    writer = _Writer()
    now = int(timestamp_ticks) if timestamp_ticks is not None else datetime_ticks(timestamp)

    writer.i32(hsp_version)
    # HereSphere's own exported presets use the media filename as both UniqueID
    # and Title (2,832/2,832 records in the supplied current saved library).
    # Mirroring that identity avoids importing a preset under an unrelated ID.
    preset_identity = str(source_filename or title or f"HereTune:{video_id}")
    writer.fstring(preset_identity)
    writer.fstring(preset_identity)
    writer.fstring("")
    writer.i64(now)  # DateAdded
    writer.i64(now)  # DateReleased
    writer.i64(0)    # DateLastPlayed
    writer.i64(now)  # DateEdited
    writer.i64(round(max(0.0, float(duration_seconds)) * TICKS_PER_SECOND))
    writer.i64(0)    # ResumeTime
    writer.i64(0)    # ABLoopStartTime
    writer.i64(0)    # ABLoopEndTime
    writer.i32(0)    # PlayCount
    writer.i32(0)    # Comments
    writer.i32(0)    # Favorites
    writer.bool32(False)
    writer.f32(0.0)  # AverageRating
    writer.i32(0)    # AudioTrack
    writer.i32(0)    # Tags

    # Format
    writer.i32(1)
    _write_format(writer, format_key)

    # Lens
    writer.i32(1)
    _write_lens(writer, lens_key)

    # Stitch: neutral by default, or a tightly bounded experimental calibration
    # supplied by the visual verifier/manual per-video override.
    writer.i32(1)
    _write_stitch(writer, stitch_key)

    # Alignment
    writer.i32(1)
    _write_alignment(writer, alignment_key)

    # Orientation
    writer.i32(1)
    _write_orientation(writer, orientation_key)

    # Origin
    writer.i32(1)
    writer.i64(0)
    writer.vec3((0.0, 0.0, 0.0))

    # Motion
    writer.i32(1)
    writer.i64(0)
    writer.f32(200.0)

    # Autofocus
    writer.i32(1)
    _write_autofocus(writer, autofocus_key)

    # Sync
    writer.i32(1)
    _write_sync(writer, sync_key)

    # Transition
    writer.i32(1)
    writer.i64(0)
    writer.u8(0)
    writer.i64(0)

    # Image
    writer.i32(len(image_keys))
    for image in image_keys:
        _write_image(writer, image)

    # Audio
    writer.i32(1)
    _write_audio(writer, audio_key)

    # Environment (v7)
    writer.i32(1)
    writer.i64(0)
    writer.u8(0)  # GlobalSetting
    writer.vec4((0.0, 0.0, 0.0, 1.0))
    writer.fstring("")
    writer.u8(0)  # No mask
    writer.f32(1.0)
    writer.vec4((0.0, 0.0, 0.0, 1.0))  # ChromaKey1Color
    writer.vec4((1.0, 0.1, 0.0, 1.0))  # ChromaKey1Settings
    writer.vec4((0.0, 0.0, 0.0, 1.0))  # ChromaKey2Color
    writer.vec4((1.0, 0.1, 0.0, 1.0))  # ChromaKey2Settings
    writer.vec4((0.0, 0.0, 0.0, 1.0))  # ChromaKey3Color
    writer.vec4((1.0, 0.1, 0.0, 1.0))  # ChromaKey3Settings
    writer.vec4((0.0, 0.0, 0.0, 1.0))  # DespillColor
    writer.vec4((0.0, 0.2, 0.2, 1.0))  # DespillSettings
    writer.vec4((0.5, 0.5, 0.5, 1.0))  # LightColor
    writer.vec4((0.25, -0.5, 0.4, 0.4)) # AlphaCoordinates

    # Standalone/importable HereSphere HSPs carry a 32-bit 1024 marker before
    # the UMediaPreset v7 payload. Library.hsl embeds the same payload without it.
    standalone_raw = struct.pack("<i", STANDALONE_HSP_PREFIX) + writer.value()
    blob = compress_proxy(standalone_raw)
    parsed = validate_playback_safe(
        blob,
        expected_filename=preset_identity,
        duration_seconds=duration_seconds,
    )
    fixed_groups = (
        "format", "lens", "stitch", "alignment", "orientation", "origin",
        "motion", "autofocus", "sync", "transition", "audio", "environment",
    )
    if (
        parsed is None
        or any(parsed.array_counts.get(name) != 1 for name in fixed_groups)
        or parsed.array_counts.get("image", 0) < 1
        or parsed.array_counts.get("image", 0) != len(image_keys)
    ):
        raise HSPError("Generated HSP did not round-trip as a complete v7 preset")
    return blob
