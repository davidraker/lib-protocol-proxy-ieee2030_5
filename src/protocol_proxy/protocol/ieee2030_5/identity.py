"""Device identifiers of IEEE 2030.5 §6.3.4: LFDI and SFDI from the client certificate, and check digits.

The LFDI is the SHA-256 fingerprint of the DER-encoded certificate left-truncated to 160 bits (40 hex digits). The
SFDI is the fingerprint left-truncated to 36 bits, written as 11 decimal digits, followed by a check digit chosen so
the sum of all digits is a multiple of ten. Registration PINs (``Registration.pIN``) use the same check-digit rule
over six digits.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization

LFDI_HEX_DIGITS = 40
SFDI_DIGITS = 12
PIN_DIGITS = 6


def load_certificate_der(path: str | Path) -> bytes:
    """The DER encoding of a certificate stored as PEM or DER."""
    data = Path(path).read_bytes()
    if b'-----BEGIN' in data:
        return x509.load_pem_x509_certificate(data).public_bytes(serialization.Encoding.DER)
    return x509.load_der_x509_certificate(data).public_bytes(serialization.Encoding.DER)


def fingerprint(cert_der: bytes) -> bytes:
    return hashlib.sha256(cert_der).digest()


def lfdi_from_der(cert_der: bytes) -> str:
    """Upper-case 40 hex digit LFDI of a DER certificate."""
    return fingerprint(cert_der)[:20].hex().upper()


def lfdi_from_cert(path: str | Path) -> str:
    return lfdi_from_der(load_certificate_der(path))


def normalize_lfdi(lfdi: str | bytes) -> str:
    """Accept hex with or without colons, or raw bytes; return upper-case 40 hex digits."""
    if isinstance(lfdi, (bytes, bytearray)):
        text = bytes(lfdi).hex()
    else:
        text = str(lfdi).replace(':', '').replace(' ', '')
    text = text.upper()
    if len(text) != LFDI_HEX_DIGITS or any(c not in '0123456789ABCDEF' for c in text):
        raise ValueError(f'An LFDI is {LFDI_HEX_DIGITS} hex digits, not {lfdi!r}')
    return text


def lfdi_bytes(lfdi: str | bytes) -> bytes:
    return bytes.fromhex(normalize_lfdi(lfdi))


def check_digit(digits: str) -> int:
    """The digit that makes the sum of ``digits`` plus itself a multiple of ten."""
    return (10 - sum(int(d) for d in digits) % 10) % 10


def has_valid_check_digit(number: int | str, length: int) -> bool:
    text = str(number)
    return len(text) == length and text.isdigit() and sum(int(d) for d in text) % 10 == 0


def sfdi_from_lfdi(lfdi: str | bytes) -> int:
    """SFDI (12 digits, check digit included) of an LFDI."""
    bits36 = int(normalize_lfdi(lfdi)[:9], 16)
    digits = f'{bits36:011d}'
    return int(digits + str(check_digit(digits)))


def sfdi_is_valid(sfdi: int | str) -> bool:
    return has_valid_check_digit(sfdi, SFDI_DIGITS)


def pin_is_valid(pin: int | str) -> bool:
    return has_valid_check_digit(f'{int(pin):0{PIN_DIGITS}d}', PIN_DIGITS)


def format_sfdi(sfdi: int) -> str:
    """Human form ``167-261-211-391``."""
    text = f'{int(sfdi):0{SFDI_DIGITS}d}'
    return '-'.join(text[i:i + 3] for i in range(0, SFDI_DIGITS, 3))
