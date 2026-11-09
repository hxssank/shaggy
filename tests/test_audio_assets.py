import io
import struct

import pytest

from firmware.audio_assets import inspect_wav

PCM_FORMAT = struct.pack("<HHIIHH", 1, 1, 44100, 88200, 2, 16)


def chunk(kind: bytes, data: bytes) -> bytes:
    return kind + struct.pack("<I", len(data)) + data + b"\0" * (len(data) & 1)


def riff(*chunks: bytes) -> bytes:
    payload = b"WAVE" + b"".join(chunks)
    return b"RIFF" + struct.pack("<I", len(payload)) + payload


@pytest.mark.parametrize("data_first", [False, True])
def test_reads_frame_count_with_either_chunk_order_and_odd_metadata(
    data_first: bool,
) -> None:
    format_chunk = chunk(b"fmt ", PCM_FORMAT + b"\0")
    data_chunk = chunk(b"data", b"\0\0" * 85355)
    ordered = (data_chunk, format_chunk) if data_first else (format_chunk, data_chunk)
    handle = io.BytesIO(
        riff(chunk(b"JUNK", b"abc"), *ordered, chunk(b"LIST", b"metadata!"))
    )
    handle.seek(7)

    assert inspect_wav(handle) == (44100, 85355)

    assert handle.tell() == 0
    assert not handle.closed


def test_skips_audio_payload_without_reading_it_into_memory() -> None:
    class BoundedReads(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            assert 0 <= size <= 16
            return super().read(size)

    handle = BoundedReads(
        riff(chunk(b"fmt ", PCM_FORMAT), chunk(b"data", b"\0\0" * 100000))
    )
    assert inspect_wav(handle) == (44100, 100000)
    assert handle.tell() == 0


@pytest.mark.parametrize("sample_rate", [22050, 44100])
def test_returns_the_supported_sample_rate(sample_rate: int) -> None:
    format_data = struct.pack("<HHIIHH", 1, 1, sample_rate, sample_rate * 2, 2, 16)
    handle = io.BytesIO(
        riff(chunk(b"fmt ", format_data), chunk(b"data", b"\0\0" * 75600))
    )
    assert inspect_wav(handle) == (sample_rate, 75600)
    assert handle.tell() == 0


@pytest.mark.parametrize(
    "format_fields",
    [
        (3, 1, 44100, 88200, 2, 16),
        (1, 2, 44100, 176400, 4, 16),
        (1, 1, 48000, 96000, 2, 16),
        (1, 1, 44100, 44100, 1, 8),
        (1, 1, 44100, 88200, 4, 16),
        (1, 1, 44100, 44100, 2, 16),
        (1, 1, 22050, 88200, 2, 16),
        (1, 1, 22050, 44100, 4, 16),
        (1, 2, 22050, 88200, 4, 16),
    ],
)
def test_rejects_unsupported_or_inconsistent_audio_format(
    format_fields: tuple[int, ...],
) -> None:
    handle = io.BytesIO(
        riff(
            chunk(b"fmt ", struct.pack("<HHIIHH", *format_fields)),
            chunk(b"data", b"\0\0"),
        )
    )
    with pytest.raises(ValueError, match="mono 16-bit PCM at 22050 or 44100 Hz"):
        inspect_wav(handle)
    assert handle.tell() == 0
    assert not handle.closed


@pytest.mark.parametrize("data", [b"", b"\0", b"\0\0\0"])
def test_rejects_empty_or_partial_sample_data(data: bytes) -> None:
    handle = io.BytesIO(riff(chunk(b"fmt ", PCM_FORMAT), chunk(b"data", data)))
    with pytest.raises(ValueError, match="complete 16-bit samples"):
        inspect_wav(handle)
    assert handle.tell() == 0


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"RIFF\x04\x00\x00\x00",
        b"RIFX\x04\x00\x00\x00WAVE",
        b"RIFF\x04\x00\x00\x00AVI ",
        b"RIFF\x00\x00\x00\x00WAVE",
        b"RIFF\xff\xff\xff\xffWAVE",
        riff(b"JUNK\x01"),
        riff(chunk(b"fmt ", PCM_FORMAT), b"data\x04\x00\x00\x00\0\0"),
        riff(chunk(b"fmt ", PCM_FORMAT), chunk(b"data", b"\0\0"))[:-1],
        riff(chunk(b"fmt ", PCM_FORMAT), chunk(b"data", b"\0\0"), b"JUNK\x01\0\0\0x"),
        riff(chunk(b"fmt ", PCM_FORMAT), b"data\xff\xff\xff\xff"),
        riff(chunk(b"fmt ", PCM_FORMAT[:14]), chunk(b"data", b"\0\0")),
        riff(chunk(b"data", b"\0\0")),
        riff(chunk(b"fmt ", PCM_FORMAT)),
    ],
)
def test_rejects_malformed_or_truncated_files_and_rewinds(data: bytes) -> None:
    handle = io.BytesIO(data)
    with pytest.raises(ValueError):
        inspect_wav(handle)
    assert handle.tell() == 0
    assert not handle.closed


@pytest.mark.parametrize("duplicate", [b"fmt ", b"data"])
def test_rejects_ambiguous_duplicate_chunks(duplicate: bytes) -> None:
    content = PCM_FORMAT if duplicate == b"fmt " else b"\0\0"
    handle = io.BytesIO(
        riff(
            chunk(b"fmt ", PCM_FORMAT),
            chunk(b"data", b"\0\0"),
            chunk(duplicate, content),
        )
    )
    with pytest.raises(ValueError, match="multiple"):
        inspect_wav(handle)
    assert handle.tell() == 0
