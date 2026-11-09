import struct


def inspect_wav(handle: object) -> tuple[int, int]:
    try:
        handle.seek(0, 2)
        file_length = handle.tell()
        handle.seek(0)
        header = handle.read(12)
        if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE":
            raise ValueError("Invalid WAV header")
        riff_end = 8 + struct.unpack("<I", header[4:8])[0]
        if riff_end < 12 or riff_end > file_length:
            raise ValueError("Invalid or truncated WAV size")
        format_found = False
        sample_rate = None
        frames = None
        while handle.tell() < riff_end:
            if riff_end - handle.tell() < 8:
                raise ValueError("Truncated WAV chunk header")
            chunk_header = handle.read(8)
            if len(chunk_header) != 8:
                raise ValueError("Truncated WAV chunk header")
            chunk_id = chunk_header[:4]
            size = struct.unpack("<I", chunk_header[4:])[0]
            chunk_end = handle.tell() + size + (size & 1)
            if chunk_end > riff_end or chunk_end > file_length:
                raise ValueError("Truncated WAV chunk")
            if chunk_id == b"fmt ":
                if format_found:
                    raise ValueError("WAV has multiple format chunks")
                if size < 16:
                    raise ValueError("Invalid WAV format chunk")
                data = handle.read(16)
                if len(data) != 16:
                    raise ValueError("Truncated WAV format chunk")
                fields = struct.unpack("<HHIIHH", data)
                sample_rate = fields[2]
                if sample_rate not in (22050, 44100) or fields != (
                    1,
                    1,
                    sample_rate,
                    sample_rate * 2,
                    2,
                    16,
                ):
                    raise ValueError("WAV must be mono 16-bit PCM at 22050 or 44100 Hz")
                format_found = True
            elif chunk_id == b"data":
                if frames is not None:
                    raise ValueError("WAV has multiple data chunks")
                if size == 0 or size & 1:
                    raise ValueError("WAV data must contain complete 16-bit samples")
                frames = size // 2
            handle.seek(chunk_end)
        if not format_found or frames is None:
            raise ValueError("WAV needs format and data chunks")
        return sample_rate, frames
    finally:
        handle.seek(0)
