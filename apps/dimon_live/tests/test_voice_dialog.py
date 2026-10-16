import unittest

from apps.dimon_live import voice_dialog


class LiveWakeTests(unittest.TestCase):
    def test_live_wake_accepts_only_bounded_greeting_and_name_variants(self):
        accepted = {
            "Hey Dimon": "",
            "Hi Diamon": "",
            "Hey Diamond send Alicia": "send Alicia",
        }
        for text, command in accepted.items():
            with self.subTest(text=text):
                self.assertEqual(voice_dialog.live_wake(text), (True, command))

        for text in ("Hey", "Dimon", "Hey Demon", "please say Hey Dimon", "the demon in the film"):
            with self.subTest(text=text):
                self.assertEqual(voice_dialog.live_wake(text), (False, ""))


if __name__ == "__main__":
    unittest.main()
