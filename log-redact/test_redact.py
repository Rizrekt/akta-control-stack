#!/usr/bin/env python3
"""Tes untuk log-redact. Semua nilai secret di sini PALSU, dibuat untuk tes.

Jalankan: python3 test_redact.py
"""
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import redact as R

FAKE_PRIV = "0x" + "ab" * 32                       # 64 hex dengan 0x
FAKE_PRIV_RAW = "cd" * 32                          # 64 hex tanpa 0x
FAKE_BOT = "123456789:AAFakeBotTokenForTestsOnly_0123456789"
FAKE_DISCORD = "MTFakeUserIdForTestsOnlyAA.BcDeFg.FakeTokenSegmentForTests_0123456789abcd"
FAKE_MFA = "MfFakeMfaTokenSegmentForTests.AbcDef.FakeMfaTailSegmentForTests01234"
FAKE_JWT = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
            ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
            ".FakeSignatureSegmentForTests0123456789")
FAKE_SESSION = "1" + "B" * 300                     # StringSession Telethon (palsu)
FAKE_PROXY = "http://proxyuser:proxypass123@203.0.113.7:8080"
FAKE_MNEMONIC = ("ridge layer broom apple ocean canyon "
                 "table velvet maple river stone arrow")
FAKE_MNEMONIC_18 = ("alpha bravo charlie delta echo foxtrot golf hotel india "
                    "juliet kilo lima mango nectar ocean piano queen river")
WALLET = "0x000000000000000000000000000000000000dEaD"
CLEAN = ("INFO wallet 0x000000000000000000000000000000000000dEaD balance ok; "
         "vault path ~/.vault/keys.json dimuat; tidak ada yang aneh di baris ini.")


class TestPatterns(unittest.TestCase):
    def test_private_key_0x(self):
        out = R.redact(f"key={FAKE_PRIV}")
        self.assertNotIn(FAKE_PRIV, out)
        self.assertIn("0x…ababab", out)

    def test_private_key_raw(self):
        out = R.redact(f"key={FAKE_PRIV_RAW}")
        self.assertNotIn(FAKE_PRIV_RAW, out)
        self.assertIn("0x…cdcdcd", out)

    def test_telegram_bot_token(self):
        out = R.redact(f"bot {FAKE_BOT} siap")
        self.assertNotIn(FAKE_BOT, out)
        self.assertIn("bot…", out)

    def test_discord_token(self):
        out = R.redact(f"discord {FAKE_DISCORD}")
        self.assertNotIn(FAKE_DISCORD, out)
        self.assertIn("token…", out)

    def test_discord_mfa_token(self):
        out = R.redact(f"mfa {FAKE_MFA}")
        self.assertNotIn(FAKE_MFA, out)
        self.assertIn("discord…", out)

    def test_jwt(self):
        out = R.redact(f"auth {FAKE_JWT}")
        self.assertNotIn(FAKE_JWT, out)
        self.assertIn("token…", out)

    def test_telethon_session(self):
        out = R.redact(f"session={FAKE_SESSION}")
        self.assertNotIn(FAKE_SESSION, out)
        self.assertIn("session…", out)

    def test_proxy_url_password(self):
        out = R.redact(f"proxy {FAKE_PROXY}")
        self.assertNotIn("proxypass123", out)
        self.assertIn("http://proxyuser:***@203.0.113.7:8080", out)

    def test_mnemonic_with_hint(self):
        out = R.redact(f"seed phrase: {FAKE_MNEMONIC}")
        self.assertNotIn("ridge layer broom", out)
        self.assertIn("mnemonic…[redacted]", out)

    def test_mnemonic_18_no_hint(self):
        out = R.redact(f"catatan {FAKE_MNEMONIC_18} selesai")
        self.assertNotIn("alpha bravo charlie", out)
        self.assertIn("mnemonic…[redacted]", out)

    def test_plain_sentence_not_mnemonic(self):
        sentence = ("the quick brown fox jumps over the lazy dog "
                    "near river bank today")
        self.assertEqual(R.redact(sentence), sentence)


class TestContextLayer(unittest.TestCase):
    def test_wallet_address_stays_visible(self):
        out = R.redact(f"dari {WALLET} kirim")
        self.assertIn(WALLET, out)

    def test_clean_text_untouched(self):
        self.assertEqual(R.redact(CLEAN), CLEAN)

    def test_empty(self):
        self.assertEqual(R.redact(""), "")


class TestEnvLiterals(unittest.TestCase):
    NAME = "LOG_REDACT_TEST_TOKEN"
    VALUE = "fake-secret-value-987654321"

    def setUp(self):
        os.environ[self.NAME] = self.VALUE

    def tearDown(self):
        os.environ.pop(self.NAME, None)

    def test_literal_masked_when_named(self):
        out = R.redact(f"token dipakai: {self.VALUE}", sensitive_names=[self.NAME])
        self.assertNotIn(self.VALUE, out)
        self.assertIn("fak…321", out)

    def test_literal_untouched_when_not_named(self):
        out = R.redact(f"token dipakai: {self.VALUE}")
        self.assertIn(self.VALUE, out)

    def test_short_literal_masked_fully(self):
        os.environ["LOG_REDACT_SHORT"] = "shortval9"
        out = R.redact("x shortval9 y", sensitive_names=["LOG_REDACT_SHORT"])
        self.assertNotIn("shortval9", out)
        self.assertIn("***", out)
        os.environ.pop("LOG_REDACT_SHORT", None)

    def test_auto_mode_by_name(self):
        os.environ["SOME_API_SECRET"] = self.VALUE
        out = R.redact(f"v={self.VALUE}", sensitive_auto=True)
        self.assertNotIn(self.VALUE, out)
        os.environ.pop("SOME_API_SECRET", None)


class TestIdempotency(unittest.TestCase):
    def test_idempotent_on_dirty_text(self):
        dirty = (f"{FAKE_PRIV} {FAKE_BOT} {FAKE_DISCORD} {FAKE_JWT} "
                 f"{FAKE_SESSION} {FAKE_PROXY} seed phrase: {FAKE_MNEMONIC} {WALLET}")
        once = R.redact(dirty)
        twice = R.redact(once)
        self.assertEqual(once, twice)
        for secret in (FAKE_PRIV, FAKE_BOT, FAKE_DISCORD, FAKE_JWT, FAKE_SESSION):
            self.assertNotIn(secret, once)


class TestCli(unittest.TestCase):
    def test_cli_stdin_with_sensitive_names(self):
        env = dict(os.environ)
        env["LOG_REDACT_TEST_TOKEN"] = TestEnvLiterals.VALUE
        proc = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "redact.py"),
             "--sensitive-names", "LOG_REDACT_TEST_TOKEN"],
            input=f"key {FAKE_PRIV} token {TestEnvLiterals.VALUE} addr {WALLET}",
            capture_output=True, text=True, env=env, timeout=30,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn(FAKE_PRIV, proc.stdout)
        self.assertNotIn(TestEnvLiterals.VALUE, proc.stdout)
        self.assertIn(WALLET, proc.stdout)
        # nilai env tidak pernah bocor ke stderr juga
        self.assertNotIn(TestEnvLiterals.VALUE, proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
