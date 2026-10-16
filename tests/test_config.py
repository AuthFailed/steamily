import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from bot.config import ConfigError, load_config, validate_config


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.values = {
            "TELEGRAM_BOT_TOKEN": "123456789:" + "a" * 35,
            "DATABASE_URL": "postgresql://bot:password@db/bot",
            "SECRET_KEY": Fernet.generate_key().decode(),
        }

    def test_defaults_and_optional_location(self):
        settings = validate_config(self.values | {"LOGIN_LOCATION": " Vienna, Wien, AT "})
        self.assertEqual(settings.poll_interval_seconds, 600)
        self.assertEqual(settings.login_location, "Vienna, Wien, AT")
        self.assertNotIn(self.values["SECRET_KEY"], repr(settings))
        self.assertNotIn(self.values["DATABASE_URL"], repr(settings))

    def test_errors_are_aggregated_without_values(self):
        bad = {
            "TELEGRAM_BOT_TOKEN": "private-token",
            "DATABASE_URL": "mysql://private:password@host/db",
            "SECRET_KEY": "private-key",
            "POLL_INTERVAL_SECONDS": "private-interval",
        }
        with self.assertRaises(ConfigError) as caught:
            validate_config(bad)
        self.assertEqual(set(caught.exception.errors), set(bad))
        for value in bad.values():
            self.assertNotIn(value, str(caught.exception))

    def test_missing_settings_list_setup_instruction(self):
        with self.assertRaises(ConfigError) as caught:
            validate_config({})
        self.assertEqual(set(caught.exception.errors), {"TELEGRAM_BOT_TOKEN", "DATABASE_URL", "SECRET_KEY"})
        self.assertIn("python -m bot.setup", str(caught.exception))

    def test_invalid_urls(self):
        for url in (
            "https://host/db",
            "postgresql://host",
            "postgresql://host:invalid/db",
            "postgresql://host:65536/db",
            "postgresql:/db",
            "postgresql://host/db%invalid",
            "postgresql://host/db\x00",
            "postgresql://user:raw`password@host/db",
        ):
            with self.subTest(url=url), self.assertRaises(ConfigError) as caught:
                validate_config(self.values | {"DATABASE_URL": url})
            self.assertIn("DATABASE_URL", caught.exception.errors)
        validate_config(self.values | {"DATABASE_URL": "postgresql:///local_database"})

    def test_poll_interval_boundary(self):
        self.assertEqual(validate_config(self.values | {"POLL_INTERVAL_SECONDS": "60"}).poll_interval_seconds, 60)
        for value in ("59", "", "1.5"):
            with self.subTest(value=value), self.assertRaises(ConfigError) as caught:
                validate_config(self.values | {"POLL_INTERVAL_SECONDS": value})
            self.assertIn("POLL_INTERVAL_SECONDS", caught.exception.errors)

    def test_compose_password_must_be_url_safe(self):
        for value in ("", "has space", "has@sign", "has/slash", "has%escape"):
            with self.subTest(value=value), self.assertRaises(ConfigError) as caught:
                validate_config(self.values | {"POSTGRES_PASSWORD": value}, compose=True)
            self.assertIn("POSTGRES_PASSWORD", caught.exception.errors)
        validate_config(self.values | {"POSTGRES_PASSWORD": "valid-password_123.~"}, compose=True)

    def test_load_only_reads_environment(self):
        with patch.dict("os.environ", self.values, clear=True):
            self.assertEqual(load_config().secret_key, self.values["SECRET_KEY"])
        self.assertEqual(load_config(self.values).database_url, self.values["DATABASE_URL"])

    def test_present_compose_password_is_also_checked_at_startup(self):
        with self.assertRaises(ConfigError) as caught:
            load_config(self.values | {"POSTGRES_PASSWORD": "unsafe@password"})
        self.assertIn("POSTGRES_PASSWORD", caught.exception.errors)

    def test_location_rejects_backticks_and_embedded_newlines(self):
        for location in ("St. John's `place`", "two\nlines"):
            with self.subTest(location=location), self.assertRaises(ConfigError) as caught:
                validate_config(self.values | {"LOGIN_LOCATION": location})
            self.assertIn("LOGIN_LOCATION", caught.exception.errors)
