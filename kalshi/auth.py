"""
RSA-PSS authentication for the Kalshi API.

Every authenticated request must include headers signed with the user's
RSA private key using PSS padding with SHA-256.

Signing message format: "{timestamp_ms}{HTTP_METHOD}{path_without_query}"
"""

import base64
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from utils.logger import get_logger

log = get_logger("kalshi.auth")


def load_private_key(file_path: str) -> RSAPrivateKey:
    """
    Load an RSA private key from a PEM file, or from the KALSHI_PRIVATE_KEY_B64
    environment variable (base64-encoded PEM, for containerised deployments).

    If KALSHI_PRIVATE_KEY_B64 is set, it takes priority over the file path.
    This allows Railway / Docker deployments to inject the key as an env var
    instead of baking a .pem file into the image.

    Args:
        file_path: Path to the PEM file containing the private key.

    Returns:
        An RSAPrivateKey instance.

    Raises:
        FileNotFoundError: If no env var is set and the PEM file does not exist.
        ValueError: If the key data is not a valid RSA private key.
    """
    import os

    key_b64 = os.getenv("KALSHI_PRIVATE_KEY_B64")

    if key_b64:
        log.debug("Loading RSA private key from KALSHI_PRIVATE_KEY_B64 env var")
        try:
            pem_data = base64.b64decode(key_b64)
        except Exception as exc:
            raise ValueError(f"KALSHI_PRIVATE_KEY_B64 is not valid base64: {exc}") from exc
    else:
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(
                f"Private key file not found: {file_path}\n"
                "Tip: set KALSHI_PRIVATE_KEY_B64 env var for containerised deployments."
            )
        pem_data = path.read_bytes()
        log.debug("Loaded RSA private key from %s", file_path)

    try:
        private_key = serialization.load_pem_private_key(pem_data, password=None)
    except Exception as exc:
        raise ValueError(f"Failed to load RSA private key: {exc}") from exc

    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise ValueError("Loaded key is not an RSA private key")

    return private_key


def sign_request(
    private_key: RSAPrivateKey,
    timestamp_ms: str,
    method: str,
    path: str,
) -> str:
    """
    Create an RSA-PSS signature for a Kalshi API request.

    Args:
        private_key: The RSA private key.
        timestamp_ms: Current timestamp in milliseconds as a string.
        method: HTTP method in uppercase (e.g. "GET", "POST").
        path: Full request path including API prefix (query params stripped).

    Returns:
        Base64-encoded signature string.
    """
    # Strip query parameters
    path_without_query = path.split("?")[0]

    # Build the message to sign
    message = f"{timestamp_ms}{method}{path_without_query}"
    message_bytes = message.encode("utf-8")

    # Sign with RSA-PSS + SHA-256
    signature = private_key.sign(
        message_bytes,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )

    return base64.b64encode(signature).decode("utf-8")


def get_auth_headers(
    private_key: RSAPrivateKey,
    api_key_id: str,
    method: str,
    path: str,
) -> dict[str, str]:
    """
    Generate the full set of authentication headers for a Kalshi API request.

    Args:
        private_key: The RSA private key.
        api_key_id: The Kalshi API key ID.
        method: HTTP method in uppercase.
        path: Full request path (query params will be stripped for signing).

    Returns:
        Dict with all required auth headers.
    """
    timestamp_ms = str(int(time.time() * 1000))
    signature = sign_request(private_key, timestamp_ms, method, path)

    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "Content-Type": "application/json",
    }
