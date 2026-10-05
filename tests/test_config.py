import pytest

from sticker_bot.config import BOT_TOKEN_PLACEHOLDER, Settings


@pytest.mark.parametrize(
    ("token", "expected"),
    [("", False), (BOT_TOKEN_PLACEHOLDER, False), ("123456:ABC-DEF", True)],
)
def test_placeholder_token_is_not_accepted(token, expected):
    assert Settings(_env_file=None, bot_token=token).has_bot_token is expected
