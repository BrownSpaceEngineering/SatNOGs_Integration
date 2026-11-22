import binascii
import datetime as dt
import enum
import math

import pytest

from satnogs_integration.decode import (
    DecodeError,
    KaitaiDecoder,
    decode_pending,
    normalise,
)
from tests.support import BINARY, NORAD, OBC_BEACON, PAGE, PAGE1_ROWS, UHF_BEACON, UTC

T0 = dt.datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def test_decoder_name_resolution():
    assert KaitaiDecoder("crocube").name == "crocube"
    assert KaitaiDecoder("Cro_Cube").name == "crocube"
    with pytest.raises(DecodeError, match="no struct"):
        KaitaiDecoder("nosuchsatellite")


def test_real_beacons_decode():
    decoder = KaitaiDecoder("crocube")
    obc = decoder.decode(binascii.unhexlify(OBC_BEACON))
    assert obc["src_callsign"].strip() == "9A0CC"  # CroCube's callsign
    assert obc["obc_bat"] == 8205 and obc["obc_uptime"] == 22562609
    uhf = decoder.decode(binascii.unhexlify(UHF_BEACON))
    assert uhf["uhf_uptime"] == 22596632
    assert all(isinstance(v, (int, float, str)) for v in {**obc, **uhf}.values())


def test_bad_bytes_raise_decode_error():
    decoder = KaitaiDecoder("crocube")
    with pytest.raises(DecodeError):
        decoder.decode(b"")
    with pytest.raises(DecodeError):
        decoder.decode(binascii.unhexlify(BINARY))


def test_normalise():
    class Mode(enum.Enum):
        SAFE = 1

    values = {
        "a": True,
        "b": 1,
        "c": 2.5,
        "d": math.nan,
        "e": math.inf,
        "f": None,
        "g": Mode.SAFE,
        "h": b"\x01\xff",
        "i": [1, 2],
        "j": "x",
    }
    assert normalise(values) == {
        "a": 1,
        "b": 1,
        "c": 2.5,
        "g": "SAFE",
        "h": "01ff",
        "j": "x",
    }


def test_decode_pending_marks_every_frame_once(store):
    # an OBC beacon, a UHF beacon, a binary frame
    store.save_page(NORAD, PAGE1_ROWS, next_url=None, fetched_at=T0)
    decoder = KaitaiDecoder("crocube")
    assert decode_pending(store, decoder, NORAD, now=lambda: T0) == {
        "frames": PAGE,
        "decoded": 2,
        "empty": 0,
        "errors": 1,
    }
    assert store.undecoded(NORAD) == []
    assert decode_pending(store, decoder, NORAD, now=lambda: T0)["frames"] == 0


def test_a_crashing_decoder_marks_only_its_frame(store):
    store.save_page(NORAD, PAGE1_ROWS[:2], next_url=None, fetched_at=T0)

    class Exploding:
        name = "boom"

        def decode(self, raw):
            raise RuntimeError("bug")

    counts = decode_pending(store, Exploding(), NORAD, now=lambda: T0)
    assert counts == {"frames": 2, "decoded": 0, "empty": 0, "errors": 2}
    assert store.counts(NORAD)["errors"] == 2
