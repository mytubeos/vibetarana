from bot.utils.formatting import format_ms


def test_format_ms_under_a_minute():
    assert format_ms(45_000) == "0:45"


def test_format_ms_minutes_and_seconds():
    assert format_ms(3 * 60_000 + 7_000) == "3:07"


def test_format_ms_over_an_hour():
    assert format_ms(90 * 60_000 + 5_000) == "1:30:05"


def test_format_ms_zero():
    assert format_ms(0) == "0:00"


def test_format_ms_negative_clamped_to_zero():
    assert format_ms(-5000) == "0:00"
