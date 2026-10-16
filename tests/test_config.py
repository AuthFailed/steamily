import os
import unittest
from unittest.mock import patch

from bot.config import poll_seconds


class PollIntervalTest(unittest.TestCase):
    def test_default_and_explicit_slow_polling(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(poll_seconds(), 1800)
        with patch.dict(os.environ, {"POLL_INTERVAL_SECONDS": "300"}, clear=True):
            self.assertEqual(poll_seconds(), 300)

    def test_fast_polling_is_rejected(self):
        with patch.dict(os.environ, {"POLL_INTERVAL_SECONDS": "299"}, clear=True):
            with self.assertRaises(RuntimeError):
                poll_seconds()
