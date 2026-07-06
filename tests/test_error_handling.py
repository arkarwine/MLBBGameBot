import unittest
from types import SimpleNamespace
from unittest.mock import patch

from telegram.error import NetworkError

from bot import error_handler


class ErrorHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_polling_network_error_is_logged_as_retryable_warning(self) -> None:
        context = SimpleNamespace(error=NetworkError("connection reset"))

        with (
            patch("bot.LOGGER.warning") as warning,
            patch("bot.LOGGER.exception") as exception,
        ):
            await error_handler(None, context)

        warning.assert_called_once()
        exception.assert_not_called()


if __name__ == "__main__":
    unittest.main()
