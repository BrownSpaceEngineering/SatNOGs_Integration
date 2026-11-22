# decode stored frames with satnogs-decoders, the same way satnogs db does

import datetime as dt
import enum
import logging
import math
from collections.abc import Callable, Mapping
from typing import Any

from .store import Store
from .timeutil import utcnow

log = logging.getLogger(__name__)

Fields = dict[str, float | int | str]


class DecodeError(ValueError):
    """Bytes the decoder cannot parse."""


def normalise(values: Mapping[str, Any]) -> Fields:
    # keep only json-safe values
    out: Fields = {}
    for name, value in values.items():
        if value is None:
            continue
        if isinstance(value, bool):  # before int: bool is an int subclass
            out[name] = int(value)
        elif isinstance(value, enum.Enum):
            out[name] = value.name
        elif isinstance(value, (bytes, bytearray)):
            out[name] = bytes(value).hex()
        elif isinstance(value, int):
            out[name] = value
        elif isinstance(value, float):
            if math.isfinite(value):
                out[name] = value
        elif isinstance(value, str):
            out[name] = value
        else:
            log.debug("dropping non-scalar field %s (%s)", name, type(value).__name__)
    return out


class KaitaiDecoder:
    def __init__(self, struct_name: str) -> None:
        import satnogsdecoders.decoder as sd  # slow import

        wanted = struct_name.strip().lower().replace("_", "").replace("-", "")
        classes = {
            name.lower().replace("_", ""): name
            for name in dir(sd)
            if name[:1].isupper() and isinstance(getattr(sd, name), type)
        }
        if wanted not in classes:
            raise DecodeError(
                f"satnogs-decoders has no struct named {struct_name!r}; "
                f"some known names: {sorted(classes)[:12]}"
            )
        self._struct = getattr(sd, classes[wanted])
        self._get_fields = sd.get_fields
        self.name = wanted

    def decode(self, raw: bytes) -> Fields:
        if not raw:
            raise DecodeError("empty frame")
        try:
            parsed = self._struct.from_bytes(raw)
            fields = self._get_fields(parsed)
        except DecodeError:
            raise
        except Exception as exc:
            raise DecodeError(f"{self.name}: {type(exc).__name__}: {exc}") from exc
        return normalise(fields)


def decode_pending(
    store: Store,
    decoder: KaitaiDecoder,
    norad_cat_id: int,
    *,
    now: Callable[[], dt.datetime] = utcnow,
) -> dict[str, int]:
    counts = {"frames": 0, "decoded": 0, "empty": 0, "errors": 0}
    for frame in store.undecoded(norad_cat_id):
        counts["frames"] += 1
        try:
            fields = decoder.decode(frame.raw)
        except DecodeError as exc:
            store.save_decode(
                frame.id, decoder=decoder.name, decoded_at=now(), error=str(exc)
            )
            counts["errors"] += 1
            continue
        except Exception as exc:  # a decoder bug: record it and move on
            log.exception("decoder crashed on frame %d", frame.id)
            store.save_decode(
                frame.id,
                decoder=decoder.name,
                decoded_at=now(),
                error=f"unexpected {type(exc).__name__}: {exc}",
            )
            counts["errors"] += 1
            continue
        store.save_decode(
            frame.id, decoder=decoder.name, decoded_at=now(), fields=fields
        )
        counts["decoded" if fields else "empty"] += 1
    log.info(
        "decode: %(frames)d frame(s), %(decoded)d decoded, %(empty)d empty, "
        "%(errors)d not telemetry/undecodable",
        counts,
    )
    return counts
