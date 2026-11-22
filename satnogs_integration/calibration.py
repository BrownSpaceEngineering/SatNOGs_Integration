# raw-to-engineering scaling per decoder, read from calibrations/<decoder>.toml
# value = raw * scale + offset

import math
import tomllib
from dataclasses import dataclass
from importlib import resources

from .config import ConfigError

KEYS = {"scale", "offset", "unit"}


@dataclass(frozen=True)
class Calibration:
    field: str
    scale: float = 1.0
    offset: float = 0.0
    unit: str | None = None


def parse_calibrations(text: str, source: str) -> list[Calibration]:
    try:
        entries = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{source}: {exc}") from None
    out = []
    for field, entry in entries.items():
        if not isinstance(entry, dict) or set(entry) - KEYS:
            raise ConfigError(f"{source}: {field} may only set scale, offset and unit")
        scale, offset, unit = (
            entry.get("scale", 1),
            entry.get("offset", 0),
            entry.get("unit"),
        )
        for name, number in (("scale", scale), ("offset", offset)):
            # bool is an int subclass, so it is ruled out by name
            if (
                isinstance(number, bool)
                or not isinstance(number, (int, float))
                or not math.isfinite(number)
            ):
                raise ConfigError(f"{source}: {field} {name} must be a finite number")
        if scale == 0:
            raise ConfigError(f"{source}: {field} scale must not be 0")
        if unit is not None and not isinstance(unit, str):
            raise ConfigError(f"{source}: {field} unit must be text")
        out.append(Calibration(field, float(scale), float(offset), unit))
    return out


def load_calibrations(decoder: str) -> list[Calibration]:
    path = resources.files(__package__) / "calibrations" / f"{decoder}.toml"
    if not path.is_file():
        return []
    return parse_calibrations(path.read_text(encoding="utf-8"), path.name)
