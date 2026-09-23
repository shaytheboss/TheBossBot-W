"""Admin sessions must survive a restart.

They used to live in a module-level set while the cookie lasted a week. Every
deploy emptied the set, the browser kept sending a cookie the server no longer
recognised, and every request came back 401 "Unauthorized" — with nothing to
say that the fix was simply to log in again. That is what broke "Shrink DB"
minutes after a deploy.

Tokens are now signed and carry their own expiry, so any process can verify
one without shared state.
"""
from __future__ import annotations

import time

import pytest
from fastapi import HTTPException

import app.api.admin as admin


@pytest.fixture(autouse=True)
def _secrets(monkeypatch):
    monkeypatch.setattr(admin.settings, "admin_password", "hunter2")
    monkeypatch.setattr(admin.settings, "secret_key", "s3cret")
    admin._ACTIVE_TOKENS.clear()
    admin._REVOKED_TOKENS.clear()
    yield
    admin._ACTIVE_TOKENS.clear()
    admin._REVOKED_TOKENS.clear()


# ── The bug ───────────────────────────────────────────────────────────────

class TestSurvivesRestart:
    def test_a_token_still_works_after_the_process_forgets_everything(self):
        """The whole point. A restart empties both in-memory sets; the cookie
        the browser holds must keep working until it genuinely expires."""
        token = admin._issue_token()
        admin._ACTIVE_TOKENS.clear()
        admin._REVOKED_TOKENS.clear()

        admin._check_admin(token)          # must not raise

    def test_a_restart_used_to_break_this(self):
        """Guards the mechanism, not just the outcome: validity must come from
        the signature, not from membership of the legacy set."""
        token = admin._issue_token()
        assert token not in admin._ACTIVE_TOKENS
        assert admin._token_is_valid(token)


# ── Signature ─────────────────────────────────────────────────────────────

class TestSignature:
    def test_a_forged_token_is_rejected(self):
        far_future = int(time.time()) + 10**6
        assert not admin._token_is_valid(f"{far_future}.deadbeef")

    def test_a_tampered_expiry_is_rejected(self):
        """Extending your own session by editing the cookie must fail, because
        the expiry is what the signature covers."""
        token = admin._issue_token()
        exp, sig = token.split(".", 1)
        assert not admin._token_is_valid(f"{int(exp) + 99999}.{sig}")

    def test_garbage_is_rejected_without_raising(self):
        for junk in ("", "nonsense", "....", "abc.def", "9999999999", None):
            assert not admin._token_is_valid(junk)

    def test_changing_the_admin_password_invalidates_sessions(self, monkeypatch):
        token = admin._issue_token()
        assert admin._token_is_valid(token)
        monkeypatch.setattr(admin.settings, "admin_password", "new-password")
        assert not admin._token_is_valid(token)

    def test_the_key_does_not_rely_on_the_default_secret(self, monkeypatch):
        """`secret_key` ships as "changeme" and may never have been set, so it
        cannot be the only input — otherwise every deployment signs with a
        publicly known key."""
        monkeypatch.setattr(admin.settings, "secret_key", "changeme")
        monkeypatch.setattr(admin.settings, "admin_password", "pw-one")
        key_one = admin._signing_key()
        monkeypatch.setattr(admin.settings, "admin_password", "pw-two")
        assert admin._signing_key() != key_one


# ── Expiry ────────────────────────────────────────────────────────────────

class TestExpiry:
    def test_an_expired_token_is_rejected(self):
        old = admin._issue_token(now=time.time() - admin.ADMIN_SESSION_SECONDS - 10)
        assert not admin._token_is_valid(old)

    def test_a_fresh_token_lasts_the_advertised_week(self):
        token = admin._issue_token()
        assert admin._token_is_valid(token, now=time.time() + admin.ADMIN_SESSION_SECONDS - 60)
        assert not admin._token_is_valid(token, now=time.time() + admin.ADMIN_SESSION_SECONDS + 60)

    def test_the_cookie_lifetime_matches_the_signature_lifetime(self):
        """If the cookie outlived the signature the browser would keep sending
        a token the server always rejects — the original bug in another form."""
        import inspect
        src = inspect.getsource(admin.admin_login)
        assert "max_age=ADMIN_SESSION_SECONDS" in src


# ── Logout and refusal ────────────────────────────────────────────────────

class TestLogoutAndRefusal:
    def test_logout_revokes_the_token(self):
        token = admin._issue_token()
        admin._REVOKED_TOKENS.add(token)
        with pytest.raises(HTTPException) as e:
            admin._check_admin(token)
        assert e.value.status_code == 401

    def test_no_cookie_is_refused(self):
        with pytest.raises(HTTPException) as e:
            admin._check_admin(None)
        assert e.value.status_code == 401

    def test_the_message_says_what_to_do(self):
        """"Unauthorized" sent me hunting for a permissions bug. The message
        should name the cause and the remedy."""
        with pytest.raises(HTTPException) as e:
            admin._check_admin("bogus.token")
        assert "log in again" in e.value.detail.lower()

    def test_an_unconfigured_admin_is_503_not_401(self, monkeypatch):
        monkeypatch.setattr(admin.settings, "admin_password", "")
        with pytest.raises(HTTPException) as e:
            admin._check_admin("anything")
        assert e.value.status_code == 503

    def test_a_legacy_in_memory_token_is_still_accepted(self):
        """Anyone holding a pre-upgrade token stays logged in until it expires
        naturally, instead of being kicked out by the fix for being kicked out."""
        admin._ACTIVE_TOKENS.add("old-style-random-token")
        admin._check_admin("old-style-random-token")


# ── The UI half ───────────────────────────────────────────────────────────

class TestTheScreenReacts:
    @staticmethod
    def _html() -> str:
        from pathlib import Path
        return (Path(__file__).resolve().parents[2]
                / "app" / "static" / "admin.html").read_text(encoding="utf-8")

    def test_a_401_shows_the_login_form(self):
        """Otherwise every button renders its own "Failed: Unauthorized",
        which is what actually happened and explains nothing."""
        html = self._html()
        assert "r.status === 401" in html
        assert "getElementById('login').classList.remove('hidden')" in html

    def test_the_login_call_itself_is_exempt(self):
        """A wrong password must show "wrong password", not bounce you to the
        screen you are already looking at."""
        assert "!path.startsWith('/login')" in self._html()
