from typing import Annotated

import phonenumbers
from pydantic import AfterValidator


def normalize_phone_number(value: str) -> str:
    candidate = value.strip()

    try:
        parsed = phonenumbers.parse(candidate, None)
    except phonenumbers.NumberParseException as error:
        raise ValueError("phone number must be a valid international number") from error

    if not phonenumbers.is_valid_number(parsed):
        raise ValueError("phone number must be a valid international number")

    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


PhoneNumber = Annotated[str, AfterValidator(normalize_phone_number)]
