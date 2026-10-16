import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from bot.dialogs.menu import begin_login, on_login
from bot.login import LoginRejected
from bot.states import Menu


class LoginFeedbackTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.callback = SimpleNamespace(from_user=SimpleNamespace(id=42), answer=AsyncMock())
        self.logins = SimpleNamespace(start=AsyncMock(), cancel=Mock())
        self.data = {"login": "waiting", "qr": "/old-qr.png", "url": "https://s.team/q/old", "other": 1}
        self.manager = Mock(
            middleware_data={"logins": self.logins, "lang": "en"},
            dialog_data=self.data.copy(),
            switch_to=AsyncMock(),
        )

    async def test_rejections_keep_existing_login_and_show_localized_feedback(self):
        messages = {
            "en": {
                "login_cooldown": "Please wait 12 seconds before trying to log in again.",
                "login_active": "A Steam sign-in is already in progress. Finish or cancel it before starting another.",
                "login_busy": "Too many Steam sign-ins are in progress. Try again in 12 seconds.",
                "login_stopping": "The bot is stopping. Try again after it restarts.",
            },
            "ru": {
                "login_cooldown": "Подождите 12 сек. перед следующей попыткой входа.",
                "login_active": "Вход через Steam уже начат. Завершите или отмените его перед новой попыткой.",
                "login_busy": "Слишком много одновременных входов через Steam. Попробуйте через 12 сек.",
                "login_stopping": "Бот останавливается. Попробуйте войти после его перезапуска.",
            },
        }
        for lang, translations in messages.items():
            for reason, message in translations.items():
                with self.subTest(lang=lang, reason=reason):
                    self.callback.answer.reset_mock()
                    self.manager.middleware_data["lang"] = lang
                    self.logins.start.side_effect = LoginRejected(reason, 12)

                    await begin_login(self.callback, None, self.manager)

                    self.callback.answer.assert_awaited_once_with(message, show_alert=True)
                    self.assertEqual(self.manager.dialog_data, self.data)
                    self.manager.switch_to.assert_not_awaited()

    async def test_success_opens_login_screen(self):
        login = {"login": "waiting", "qr": "/new-qr.png", "url": "https://s.team/q/new"}
        self.logins.start.return_value = login

        await begin_login(self.callback, None, self.manager)

        self.logins.start.assert_awaited_once_with(42, self.manager.bg.return_value)
        self.assertEqual(self.manager.dialog_data, {**self.data, **login})
        self.manager.switch_to.assert_awaited_once_with(Menu.login)
        self.callback.answer.assert_not_awaited()

    async def test_unexpected_failure_keeps_existing_failure_screen_behavior(self):
        self.logins.start.side_effect = RuntimeError("Steam unavailable")

        await begin_login(self.callback, None, self.manager)

        self.assertEqual(self.manager.dialog_data["login"], "failed")
        self.manager.switch_to.assert_awaited_once_with(Menu.login)
        self.callback.answer.assert_not_awaited()

    async def test_intro_does_not_start_or_reserve_a_steam_login(self):
        await on_login(self.callback, None, self.manager)

        self.logins.cancel.assert_called_once_with(42)
        self.logins.start.assert_not_awaited()
        self.assertEqual(self.manager.dialog_data, self.data)
        self.manager.switch_to.assert_awaited_once_with(Menu.login_intro)
        self.callback.answer.assert_not_awaited()
