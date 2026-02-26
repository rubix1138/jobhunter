"""Tests for the credential vault (Fernet encryption)."""

import os
import pytest
from cryptography.fernet import Fernet

from jobhunter.crypto.vault import CredentialVault


@pytest.fixture
def key():
    return Fernet.generate_key().decode()


@pytest.fixture
def vault(key):
    return CredentialVault(key=key)


# ── Key management ────────────────────────────────────────────────────────────

class TestKeyManagement:
    def test_generate_key_returns_string(self):
        key = CredentialVault.generate_key()
        assert isinstance(key, str)
        assert len(key) == 44  # Fernet keys are 44 base64 chars

    def test_key_from_string(self, key):
        vault = CredentialVault(key=key)
        assert vault is not None

    def test_key_from_bytes(self, key):
        vault = CredentialVault(key=key.encode())
        assert vault is not None

    def test_missing_key_raises(self, monkeypatch):
        monkeypatch.delenv("FERNET_KEY", raising=False)
        with pytest.raises(ValueError, match="FERNET_KEY"):
            CredentialVault()

    def test_key_from_env(self, monkeypatch):
        key = Fernet.generate_key().decode()
        monkeypatch.setenv("FERNET_KEY", key)
        vault = CredentialVault()
        assert vault is not None


# ── Encrypt / Decrypt ─────────────────────────────────────────────────────────

class TestEncryptDecrypt:
    def test_encrypt_returns_string(self, vault):
        ciphertext = vault.encrypt("my-secret-password")
        assert isinstance(ciphertext, str)
        assert ciphertext != "my-secret-password"

    def test_decrypt_roundtrip(self, vault):
        original = "super$ecret123!"
        ciphertext = vault.encrypt(original)
        decrypted = vault.decrypt(ciphertext)
        assert decrypted == original

    def test_different_encryptions_of_same_plaintext(self, vault):
        # Fernet uses random IV so same plaintext -> different ciphertext each time
        ct1 = vault.encrypt("password")
        ct2 = vault.encrypt("password")
        assert ct1 != ct2
        assert vault.decrypt(ct1) == vault.decrypt(ct2) == "password"

    def test_encrypt_empty_string(self, vault):
        ciphertext = vault.encrypt("")
        assert vault.decrypt(ciphertext) == ""

    def test_encrypt_unicode(self, vault):
        original = "p@$$w0rd-你好-🔒"
        assert vault.decrypt(vault.encrypt(original)) == original

    def test_wrong_key_raises(self, key):
        vault1 = CredentialVault(key=key)
        vault2 = CredentialVault(key=Fernet.generate_key())
        ciphertext = vault1.encrypt("secret")
        with pytest.raises(ValueError, match="Failed to decrypt"):
            vault2.decrypt(ciphertext)

    def test_corrupted_ciphertext_raises(self, vault):
        with pytest.raises(ValueError, match="Failed to decrypt"):
            vault.decrypt("notvalidbase64==")

    def test_password_aliases(self, vault):
        enc = vault.encrypt_password("mypassword")
        assert vault.decrypt_password(enc) == "mypassword"


# ── Password Generation ───────────────────────────────────────────────────────

class TestGeneratePassword:
    def test_default_length(self):
        pwd = CredentialVault.generate_password()
        assert len(pwd) == 24

    def test_custom_length(self):
        pwd = CredentialVault.generate_password(length=32)
        assert len(pwd) == 32

    def test_too_short_raises(self):
        with pytest.raises(ValueError):
            CredentialVault.generate_password(length=4)

    def test_has_uppercase(self):
        for _ in range(20):
            pwd = CredentialVault.generate_password()
            assert any(c.isupper() for c in pwd), f"No uppercase in: {pwd}"

    def test_has_lowercase(self):
        for _ in range(20):
            pwd = CredentialVault.generate_password()
            assert any(c.islower() for c in pwd), f"No lowercase in: {pwd}"

    def test_has_digit(self):
        for _ in range(20):
            pwd = CredentialVault.generate_password()
            assert any(c.isdigit() for c in pwd), f"No digit in: {pwd}"

    def test_has_special_char(self):
        import string
        for _ in range(20):
            pwd = CredentialVault.generate_password()
            assert any(c in string.punctuation for c in pwd), f"No special char in: {pwd}"

    def test_no_problematic_chars(self):
        for _ in range(50):
            pwd = CredentialVault.generate_password()
            assert '"' not in pwd
            assert "'" not in pwd
            assert '\\' not in pwd
            assert ' ' not in pwd

    def test_passwords_are_unique(self):
        passwords = {CredentialVault.generate_password() for _ in range(20)}
        assert len(passwords) == 20
