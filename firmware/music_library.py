import json

MAX_MANIFEST_BYTES = 65536
MAX_BANKS = 64
MAX_ID_LENGTH = 48
ID_CHARACTERS = "abcdefghijklmnopqrstuvwxyz0123456789-"


def _positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _text(value: object, limit: int) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= limit
        and bool(value.strip())
        and all(32 <= ord(character) <= 126 for character in value)
    )


def _bank(value: object, index: int, directory: str) -> dict:
    prefix = "Library bank %d" % (index + 1)
    if not isinstance(value, dict):
        raise ValueError(prefix + " must be an object")
    identifier = value.get("id")
    if (
        not isinstance(identifier, str)
        or not 1 <= len(identifier) <= MAX_ID_LENGTH
        or identifier[0] == "-"
        or identifier[-1] == "-"
        or any(character not in ID_CHARACTERS for character in identifier)
    ):
        raise ValueError(prefix + " has an invalid id")
    title = value.get("title")
    if not _text(title, 28):
        raise ValueError(prefix + " title needs 1-28 printable ASCII characters")
    bpm = value.get("bpm")
    if (
        not isinstance(bpm, (int, float))
        or isinstance(bpm, bool)
        or not 0 < bpm < float("inf")
    ):
        raise ValueError(prefix + " bpm must be finite and positive")
    rate = value.get("sample_rate")
    if not _positive_integer(rate) or rate not in (22050, 44100):
        raise ValueError(prefix + " sample_rate must be 22050 or 44100")
    frames = value.get("frames")
    if not _positive_integer(frames):
        raise ValueError(prefix + " frames must be a positive integer")
    result = {
        "id": identifier,
        "title": title,
        "bpm": bpm,
        "sample_rate": rate,
        "frames": frames,
        "audio_root": directory + "/" + identifier,
    }
    if "instruments" in value:
        instruments = value["instruments"]
        if not isinstance(instruments, list) or len(instruments) != 9:
            raise ValueError(prefix + " needs nine instrument label pairs")
        pairs = []
        for pair in instruments:
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or not _text(pair[0], 6)
                or not _text(pair[1], 16)
            ):
                raise ValueError(prefix + " has invalid instrument labels")
            pairs.append((pair[0], pair[1]))
        result["instruments"] = tuple(pairs)
    return result


def load_library(path: str = "/sd/shaggy/library/manifest.json") -> list[dict]:
    with open(path, "rb") as handle:
        payload = handle.read(MAX_MANIFEST_BYTES + 1)
    if len(payload) > MAX_MANIFEST_BYTES:
        raise ValueError("Library manifest exceeds 64 KiB")
    try:
        text = payload.decode("utf-8")
        del payload
        document = json.loads(text)
        del text
    except ValueError:
        raise ValueError("Library manifest is not valid UTF-8 JSON")
    if not isinstance(document, dict):
        raise ValueError("Library manifest must be an object")
    version = document.get("version")
    if not _positive_integer(version) or version != 1:
        raise ValueError("Library manifest version must be 1")
    banks = document.get("banks")
    if not isinstance(banks, list) or not 1 <= len(banks) <= MAX_BANKS:
        raise ValueError("Library manifest needs 1-64 banks")
    directory = path.rsplit("/", 1)[0] if "/" in path else "."
    result = []
    identifiers = set()
    for index, value in enumerate(banks):
        bank = _bank(value, index, directory)
        if bank["id"] in identifiers:
            raise ValueError("Library bank id is repeated: " + bank["id"])
        identifiers.add(bank["id"])
        result.append(bank)
    return result
