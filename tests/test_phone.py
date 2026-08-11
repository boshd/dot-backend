import pytest

from benji_api.schemas.phone import normalize_phone_number


def test_normalizes_phone_number_to_e164() -> None:
    assert normalize_phone_number("+1 (415) 555-2671") == "+14155552671"


def test_rejects_phone_number_without_international_prefix() -> None:
    with pytest.raises(ValueError, match="valid international number"):
        normalize_phone_number("415-555-2671")
