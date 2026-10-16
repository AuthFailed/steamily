import io
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from bot.config import ConfigError
from bot.setup import read_env_file, setup_env, write_env_file

TOKEN = "123456789:" + "a" * 35


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / ".env"

    def run_setup(self, passwords, answers, **kwargs):
        output = io.StringIO()
        with (
            patch("bot.setup.getpass", side_effect=passwords),
            patch("builtins.input", side_effect=answers),
            patch("sys.stdout", output),
        ):
            setup_env(self.path, **kwargs)
        return output.getvalue()

    def test_first_setup_persists_private_secrets_once(self):
        output = self.run_setup([TOKEN, ""], ["", "Vienna, Wien, AT"])
        values = read_env_file(self.path)
        Fernet(values["SECRET_KEY"].encode())
        self.assertTrue(values["POSTGRES_PASSWORD"])
        self.assertEqual(values["POLL_INTERVAL_SECONDS"], "600")
        self.assertEqual(values["LOGIN_LOCATION"], "Vienna, Wien, AT")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertIn("WARNING", output)
        self.assertIn("Back up", output)
        for secret in (TOKEN, values["SECRET_KEY"], values["POSTGRES_PASSWORD"]):
            self.assertNotIn(secret, output)
        self.run_setup(["", ""], ["", ""])
        self.assertEqual(read_env_file(self.path), values)

    def test_writer_preserves_comments_unknown_values_and_quoted_location(self):
        self.path.write_text("# Keep this comment\nCUSTOM='a value'\nLOGIN_LOCATION=old\n", encoding="utf-8")
        write_env_file(self.path, {"LOGIN_LOCATION": 'St. John\'s "Centre"', "NEW": "added"})
        values = read_env_file(self.path)
        self.assertIn("# Keep this comment\nCUSTOM='a value'\n", self.path.read_text())
        self.assertEqual(values["CUSTOM"], "a value")
        self.assertEqual(values["LOGIN_LOCATION"], 'St. John\'s "Centre"')
        self.assertEqual(values["NEW"], "added")

    def test_existing_invalid_key_is_not_replaced(self):
        original = "SECRET_KEY=invalid-key\nCUSTOM=keep\n"
        self.path.write_text(original)
        with self.assertRaises(ConfigError), patch("builtins.input") as prompt:
            setup_env(self.path)
        prompt.assert_not_called()
        self.assertEqual(self.path.read_text(), original)

    def test_existing_missing_key_requires_explicit_new_install_confirmation(self):
        self.path.write_text("SECRET_KEY=\n")
        with self.assertRaises(ConfigError):
            self.run_setup([], ["n"])
        self.assertEqual(self.path.read_text(), "SECRET_KEY=\n")
        self.run_setup([TOKEN, ""], ["yes", "", ""])
        Fernet(read_env_file(self.path)["SECRET_KEY"].encode())

    def test_external_database_setup(self):
        database_url = "postgresql://local:private@localhost/family"
        output = self.run_setup([TOKEN, database_url], ["60", ""], external_db=True)
        values = read_env_file(self.path)
        self.assertEqual(values["DATABASE_URL"], database_url)
        self.assertNotIn("POSTGRES_PASSWORD", values)
        self.assertNotIn(database_url, output)

    def test_invalid_input_is_explained_and_reprompted(self):
        output = self.run_setup(["not-a-token", TOKEN, "has@sign", "valid-password"], ["59", "60", ""])
        self.assertIn("TELEGRAM_BOT_TOKEN", output)
        self.assertIn("POSTGRES_PASSWORD", output)
        self.assertIn("POLL_INTERVAL_SECONDS", output)
        self.assertNotIn("not-a-token", output)
        self.assertNotIn("has@sign", output)
        self.assertEqual(read_env_file(self.path)["POLL_INTERVAL_SECONDS"], "60")

    def test_env_file_parse_errors_do_not_disclose_values(self):
        self.path.write_text('SECRET_KEY="private-unterminated\n')
        with self.assertRaises(ConfigError) as caught:
            read_env_file(self.path)
        self.assertNotIn("private-unterminated", str(caught.exception))

    def test_symlink_is_not_overwritten(self):
        target = self.path.parent / "target"
        target.write_text("keep\n")
        self.path.symlink_to(target)
        with self.assertRaises(ConfigError):
            write_env_file(self.path, {"SECRET_KEY": "new"})
        self.assertEqual(target.read_text(), "keep\n")

    def test_atomic_write_failure_preserves_original_key(self):
        original = "SECRET_KEY='original'\n"
        self.path.write_text(original)
        with patch("bot.setup.os.replace", side_effect=OSError), self.assertRaises(OSError):
            write_env_file(self.path, {"SECRET_KEY": "replacement"})
        self.assertEqual(self.path.read_text(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_setup_leaves_unknown_complex_values_unchanged(self):
        original = 'CUSTOM="St. John\'s \\`literal\\`"\n'
        key = Fernet.generate_key().decode()
        self.path.write_text(original + f"SECRET_KEY={key}\n")
        self.run_setup([TOKEN, ""], ["", ""])
        self.assertTrue(self.path.read_text().startswith(original))

    def test_quote_roundtrip_with_apostrophe_dollars_and_backslashes(self):
        values = {"LOCATION": 'St. John\'s $District \\$literal "Centre"'}
        write_env_file(self.path, values)
        self.assertEqual(read_env_file(self.path), values)

    def test_unquoted_hashes_and_backslashes_are_preserved(self):
        self.path.write_text("POSTGRES_PASSWORD=foo#bar\nCUSTOM=path\\to\\file # comment\n")
        values = read_env_file(self.path)
        self.assertEqual(values["POSTGRES_PASSWORD"], "foo#bar")
        self.assertEqual(values["CUSTOM"], r"path\to\file")
