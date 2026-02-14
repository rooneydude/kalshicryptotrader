"""
Tests for kalshi/auth.py — RSA-PSS signing and header generation.
"""

import base64
import time
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)

from kalshi.auth import get_auth_headers, load_private_key, sign_request


@pytest.fixture
def rsa_key_pair(tmp_path):
    """Generate a temporary RSA key pair for testing."""
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

    # Write PEM to temp file
    pem_path = tmp_path / "test-key.pem"
    pem_bytes = private_key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    )
    pem_path.write_bytes(pem_bytes)

    return private_key, str(pem_path)


class TestLoadPrivateKey:
    def test_load_valid_key(self, rsa_key_pair):
        """Test loading a valid RSA private key from PEM file."""
        _, pem_path = rsa_key_pair
        key = load_private_key(pem_path)
        assert key is not None
        assert hasattr(key, "sign")

    def test_file_not_found(self):
        """Test that FileNotFoundError is raised for missing file."""
        with pytest.raises(FileNotFoundError):
            load_private_key("/nonexistent/path/key.pem")

    def test_invalid_pem(self, tmp_path):
        """Test that ValueError is raised for invalid PEM content."""
        bad_pem = tmp_path / "bad-key.pem"
        bad_pem.write_text("not a valid pem file")
        with pytest.raises(ValueError):
            load_private_key(str(bad_pem))


class TestSignRequest:
    def test_sign_produces_valid_base64(self, rsa_key_pair):
        """Test that sign_request returns a valid base64 string."""
        private_key, _ = rsa_key_pair
        signature = sign_request(private_key, "1234567890000", "GET", "/trade-api/v2/markets")
        # Should be valid base64
        decoded = base64.b64decode(signature)
        assert len(decoded) > 0

    def test_sign_strips_query_params(self, rsa_key_pair):
        """Test that query parameters are stripped before signing.
        
        RSA-PSS uses random padding, so two calls produce different
        signatures even for the same message. Instead, we verify that
        both signatures validate against the same stripped message.
        """
        private_key, _ = rsa_key_pair
        timestamp = "1234567890000"
        path_with_query = "/trade-api/v2/markets?limit=100&cursor=abc"
        path_without_query = "/trade-api/v2/markets"

        sig_with_query = sign_request(private_key, timestamp, "GET", path_with_query)
        sig_without_query = sign_request(private_key, timestamp, "GET", path_without_query)

        # Both signatures should verify against the message WITHOUT query params
        message = f"{timestamp}GET{path_without_query}".encode("utf-8")
        public_key = private_key.public_key()

        for sig_b64 in [sig_with_query, sig_without_query]:
            sig_bytes = base64.b64decode(sig_b64)
            # Should not raise
            public_key.verify(
                sig_bytes,
                message,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH,
                ),
                hashes.SHA256(),
            )

    def test_different_timestamps_produce_different_sigs(self, rsa_key_pair):
        """Test that different timestamps produce different signatures."""
        private_key, _ = rsa_key_pair
        sig1 = sign_request(private_key, "1000000000000", "GET", "/trade-api/v2/markets")
        sig2 = sign_request(private_key, "2000000000000", "GET", "/trade-api/v2/markets")
        assert sig1 != sig2

    def test_different_methods_produce_different_sigs(self, rsa_key_pair):
        """Test that different HTTP methods produce different signatures."""
        private_key, _ = rsa_key_pair
        sig_get = sign_request(private_key, "1234567890000", "GET", "/trade-api/v2/markets")
        sig_post = sign_request(private_key, "1234567890000", "POST", "/trade-api/v2/markets")
        assert sig_get != sig_post

    def test_signature_can_be_verified(self, rsa_key_pair):
        """Test that the signature can be verified with the public key."""
        private_key, _ = rsa_key_pair
        timestamp = "1234567890000"
        method = "GET"
        path = "/trade-api/v2/markets"

        signature_b64 = sign_request(private_key, timestamp, method, path)
        signature_bytes = base64.b64decode(signature_b64)

        message = f"{timestamp}{method}{path}".encode("utf-8")
        public_key = private_key.public_key()

        # Should not raise
        public_key.verify(
            signature_bytes,
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )


class TestGetAuthHeaders:
    def test_returns_required_headers(self, rsa_key_pair):
        """Test that all required headers are present."""
        private_key, _ = rsa_key_pair
        headers = get_auth_headers(
            private_key, "test-api-key-id", "GET", "/trade-api/v2/markets"
        )

        assert "KALSHI-ACCESS-KEY" in headers
        assert "KALSHI-ACCESS-SIGNATURE" in headers
        assert "KALSHI-ACCESS-TIMESTAMP" in headers
        assert "Content-Type" in headers

        assert headers["KALSHI-ACCESS-KEY"] == "test-api-key-id"
        assert headers["Content-Type"] == "application/json"

    def test_timestamp_is_current(self, rsa_key_pair):
        """Test that the timestamp is approximately current."""
        private_key, _ = rsa_key_pair
        before = int(time.time() * 1000)
        headers = get_auth_headers(
            private_key, "test-key", "GET", "/trade-api/v2/markets"
        )
        after = int(time.time() * 1000)

        ts = int(headers["KALSHI-ACCESS-TIMESTAMP"])
        assert before <= ts <= after

    def test_signature_is_valid_base64(self, rsa_key_pair):
        """Test that the signature header is valid base64."""
        private_key, _ = rsa_key_pair
        headers = get_auth_headers(
            private_key, "test-key", "POST", "/trade-api/v2/portfolio/orders"
        )
        decoded = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
        assert len(decoded) > 0
