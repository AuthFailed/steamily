import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("check", Path(__file__).parent.parent / "scripts" / "check_commit_msg.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


class CommitMessageTest(unittest.TestCase):
    def test_valid(self):
        for message in [
            "feat: add inline search",
            "fix(login): keep the qr code in place\n\nSteam rotates it every 20 seconds.\n",
            "refactor!: drop the legacy importer",
            "ci(docker): build arm64 images\n# Please enter the commit message\n",
            "Merge branch 'main' into feature",
        ]:
            self.assertEqual(check.problems(message), [], message)

    def test_invalid(self):
        cases = {
            "Add inline search": "type(scope): subject",
            "feature: add search": "type(scope): subject",
            "feat: добавить поиск": "English",
            "feat: add search.": "period",
            "feat: " + "x" * 80: "limit is 72",
            "feat: add search\nbody without a blank line": "blank line",
            "": "empty",
        }
        for message, problem in cases.items():
            self.assertTrue(any(problem in p for p in check.problems(message)), (message, check.problems(message)))
