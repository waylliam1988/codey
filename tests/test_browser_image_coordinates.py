import pytest

from codey.automation.browser_images import image_number


def test_image_coordinate_outside_float_range_is_invalid() -> None:
    with pytest.raises(ValueError, match="invalid image coordinate"):
        image_number(10**400)
