"""Fernet-based credential encryption vault."""

import os
import secrets
import string
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken


class CredentialVault:
    """Encrypts and decrypts credentials using a Fernet symmetric key.

    The key must be provided via the FERNET_KEY environment variable or
    passed directly to the constructor. Never store the key in the database
    or source code.
    """

    def __init__(self, key: Optional[bytes | str] = None) -> None:
        if key is None:
            raw = os.environ.get("FERNET_KEY")
            if not raw:
                raise ValueError(
                    "FERNET_KEY environment variable is not set. "
                    "Generate one with: python -c \"from cryptography.fernet import Fernet; "
                    "print(Fernet.generate_key().decode())\""
                )
            key = raw.encode() if isinstance(raw, str) else raw
        elif isinstance(key, str):
            key = key.encode()

        self._fernet = Fernet(key)

    @staticmethod
    def generate_key() -> str:
        """Generate a new Fernet key. Store this in your .env file."""
        return Fernet.generate_key().decode()

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a string and return a base64-encoded ciphertext string."""
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt a ciphertext string back to plaintext.

        Raises InvalidToken if the ciphertext is corrupted or the wrong key is used.
        """
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("Failed to decrypt credential — wrong key or corrupted data") from exc

    def encrypt_password(self, password: str) -> str:
        """Convenience alias for encrypting passwords."""
        return self.encrypt(password)

    def decrypt_password(self, encrypted_password: str) -> str:
        """Convenience alias for decrypting passwords."""
        return self.decrypt(encrypted_password)

    @staticmethod
    def generate_password(length: int = 24) -> str:
        """Generate a strong random password for auto-created accounts.

        Always meets requirements: uppercase, lowercase, digit, special char.
        """
        if length < 8:
            raise ValueError("Password length must be at least 8")

        alphabet = string.ascii_letters + string.digits + string.punctuation
        # Exclude characters that commonly cause issues in web forms
        alphabet = alphabet.replace('"', '').replace("'", '').replace('\\', '').replace(' ', '')

        while True:
            password = ''.join(secrets.choice(alphabet) for _ in range(length))
            has_upper = any(c.isupper() for c in password)
            has_lower = any(c.islower() for c in password)
            has_digit = any(c.isdigit() for c in password)
            has_special = any(c in string.punctuation for c in password)
            if has_upper and has_lower and has_digit and has_special:
                return password
