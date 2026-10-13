import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken


class GarthTokenStoreError(Exception):
    """Raised when the encrypted GARTH_TOKEN cannot be processed."""


def _build_fernet(token_salt: str) -> Fernet:
    if not token_salt:
        raise GarthTokenStoreError("GARMIN_TOKEN_SALT is required.")
    key = hashlib.sha256(token_salt.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def has_encrypted_token(token_path: str) -> bool:
    return os.path.exists(token_path)


def read_encrypted_token(token_path: str, token_salt: str) -> str:
    try:
        with open(token_path, "rb") as file:
            encrypted_token = file.read()
        token = _build_fernet(token_salt).decrypt(encrypted_token)
        return token.decode("utf-8")
    except FileNotFoundError as exc:
        raise GarthTokenStoreError(
            f"Encrypted GARTH_TOKEN file is missing: {token_path}"
        ) from exc
    except InvalidToken as exc:
        raise GarthTokenStoreError(
            "Unable to decrypt GARTH_TOKEN with GARMIN_TOKEN_SALT."
        ) from exc


def write_encrypted_token(token_path: str, token_salt: str, garth_token: str) -> None:
    os.makedirs(os.path.dirname(token_path), exist_ok=True)
    encrypted_token = _build_fernet(token_salt).encrypt(
        garth_token.encode("utf-8")
    )
    with open(token_path, "wb") as file:
        file.write(encrypted_token)
