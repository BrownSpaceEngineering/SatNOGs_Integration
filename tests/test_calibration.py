import re

import pytest
from satnogsdecoders.decoder.crocube import Crocube

from satnogs_integration.calibration import (
    Calibration,
    load_calibrations,
    parse_calibrations,
)
from satnogs_integration.config import ConfigError


def test_crocube_file_loads():
    rows = {c.field: c for c in load_calibrations("crocube")}
    assert rows["psu_battery"] == Calibration("psu_battery", 0.001, 0.0, "V")
    assert rows["obc_temp_mcu"] == Calibration("obc_temp_mcu", 0.01, 0.0, "degC")
    assert rows["uhf_act_rssi_raw"] == Calibration(
        "uhf_act_rssi_raw", 0.5, -134.0, "dBm"
    )
    assert rows["obc_uptime"] == Calibration("obc_uptime", 1.0, 0.0, "s")


def test_every_calibrated_field_is_a_decoder_field():
    known = set(re.findall(r":field (\w+):", Crocube.__doc__))
    assert {c.field for c in load_calibrations("crocube")} <= known


def test_a_decoder_without_a_file_has_no_calibrations():
    assert load_calibrations("nosuchsatellite") == []


def test_parse_defaults():
    assert parse_calibrations('a = { unit = "mA" }\nb = { scale = 2 }', "x") == [
        Calibration("a", 1.0, 0.0, "mA"),
        Calibration("b", 2.0, 0.0, None),
    ]


@pytest.mark.parametrize(
    "text",
    [
        "a = { scale = 'big' }",
        "a = { scale = true }",
        "a = { scale = 0 }",
        "a = { offset = inf }",
        "a = { gain = 2 }",
        "a = { unit = 5 }",
        "a = 5",
        "not toml [",
    ],
)
def test_bad_files_are_rejected(text):
    with pytest.raises(ConfigError, match="crocube.toml"):
        parse_calibrations(text, "crocube.toml")
