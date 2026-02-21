from panikbot import clean_text


def test_clean_email():
    s = "Contact me at alice@example.com"
    assert "[REDACTED_EMAIL]" in clean_text(s)


def test_clean_phone():
    s = "My phone is +1 (555) 123-4567"
    assert "[REDACTED_PHONE]" in clean_text(s)


def test_clean_credit_card():
    s = "Card: 4111 1111 1111 1111"
    assert "[REDACTED_CREDIT_CARD]" in clean_text(s)


def test_passport_label():
    s = "Passport No: A1234567"
    assert "[REDACTED_PASSPORT]" in clean_text(s)
