import math
from typing import Any


def parse_coordinates(latitude: Any, longitude: Any) -> tuple[float, float]:
    """Validate a latitude/longitude pair and return it as canonical text.

    Text such as ``"1e1"``, ``"+30.5"`` or Arabic-Indic digits would otherwise
    reach the URL as typed.
    """
    try:
        if isinstance(latitude, bool) or isinstance(longitude, bool):
            raise TypeError
        lat_value, lon_value = float(latitude), float(longitude)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("invalid geo coordinates") from None
    in_range = (
        math.isfinite(lat_value)
        and math.isfinite(lon_value)
        and -90 <= lat_value <= 90
        and -180 <= lon_value <= 180
    )
    if not in_range:
        raise ValueError("invalid geo coordinates")
    return lat_value, lon_value
