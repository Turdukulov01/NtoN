from __future__ import annotations

import hashlib
import re
from typing import Any

from fastapi import HTTPException

from app.core.settings import BASE58_INDEX, TRON_MAINNET_PREFIX


BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
BECH32M_CONST = 0x2BC830A3
NETWORK_ALIASES = {
    "trx": "tron",
    "tron": "tron",
    "eth": "ethereum",
    "ethereum": "ethereum",
    "btc": "bitcoin",
    "bitcoin": "bitcoin",
}


def normalize_network_key(value: Any) -> str:
    key = str(value or "").strip().lower()
    return NETWORK_ALIASES.get(key, key)


def normalize_external_address(value: Any) -> str:
    return "".join(
        char
        for char in str(value or "").strip()
        if not char.isspace() and char not in {"\u200b", "\u200c", "\u200d", "\ufeff"}
    )


def is_ethereum_address(value: Any) -> bool:
    return bool(re.fullmatch(r"0x[a-fA-F0-9]{40}", normalize_external_address(value)))


def normalize_ethereum_address(value: Any) -> str:
    address = normalize_external_address(value)
    if not is_ethereum_address(address):
        raise HTTPException(status_code=400, detail="Некорректный Ethereum-адрес")
    return address


def is_bitcoin_address(value: Any) -> bool:
    address = normalize_external_address(value)
    if re.fullmatch(r"[13][a-km-zA-HJ-NP-Z1-9]{25,34}", address) and _is_valid_bitcoin_base58check(address):
        return True
    return _is_valid_bitcoin_bech32(address)


def normalize_bitcoin_address(value: Any) -> str:
    address = normalize_external_address(value)
    if not is_bitcoin_address(address):
        raise HTTPException(status_code=400, detail="Некорректный Bitcoin-адрес")
    return address.lower() if address.lower().startswith("bc1") else address


def normalize_wallet_address(network: str, address: Any) -> str:
    network = normalize_network_key(network)
    if network == "tron":
        normalized = normalize_tron_address(address)
        if not is_tron_address(normalized):
            raise HTTPException(status_code=400, detail="Некорректный TRON-адрес")
        return normalized
    if network == "ethereum":
        return normalize_ethereum_address(address)
    if network == "bitcoin":
        return normalize_bitcoin_address(address)
    raise HTTPException(status_code=400, detail="Поддерживаются только сети: tron, ethereum, bitcoin")


def detect_wallet_network(value: Any) -> str | None:
    if is_tron_address(value):
        return "tron"
    if is_ethereum_address(value):
        return "ethereum"
    if is_bitcoin_address(value):
        return "bitcoin"
    return None


def normalize_detected_wallet_address(value: Any) -> tuple[str, str] | None:
    network = detect_wallet_network(value)
    if not network:
        return None
    return network, normalize_wallet_address(network, value)


def normalize_tron_address(value: Any) -> str:
    return "".join(
        char
        for char in str(value or "").strip()
        if not char.isspace() and char not in {"\u200b", "\u200c", "\u200d", "\ufeff"}
    )


def base58_decode(value: str) -> bytes:
    number = 0
    for char in value:
        if char not in BASE58_INDEX:
            raise ValueError("Invalid base58 character")
        number = number * 58 + BASE58_INDEX[char]

    payload = number.to_bytes((number.bit_length() + 7) // 8, byteorder="big") if number else b""
    leading_zeroes = len(value) - len(value.lstrip("1"))
    return b"\x00" * leading_zeroes + payload


def is_tron_address(value: Any) -> bool:
    address = normalize_tron_address(value)
    if len(address) != 34 or not address.startswith("T"):
        return False

    try:
        decoded = base58_decode(address)
    except ValueError:
        return False

    if len(decoded) != 25 or decoded[0] != TRON_MAINNET_PREFIX:
        return False

    payload, checksum = decoded[:-4], decoded[-4:]
    expected_checksum = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    return checksum == expected_checksum


def _is_valid_bitcoin_base58check(address: str) -> bool:
    try:
        decoded = base58_decode(address)
    except ValueError:
        return False

    if len(decoded) != 25 or decoded[0] not in {0x00, 0x05}:
        return False

    payload, checksum = decoded[:-4], decoded[-4:]
    expected_checksum = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    return checksum == expected_checksum


def _bech32_polymod(values: list[int]) -> int:
    generators = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for index, generator in enumerate(generators):
            if (top >> index) & 1:
                checksum ^= generator
    return checksum


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(char) >> 5 for char in hrp] + [0] + [ord(char) & 31 for char in hrp]


def _bech32_decode(address: str) -> tuple[str, list[int], str] | None:
    if not address or any(ord(char) < 33 or ord(char) > 126 for char in address):
        return None
    if address.lower() != address and address.upper() != address:
        return None

    address = address.lower()
    separator = address.rfind("1")
    if separator < 1 or separator + 7 > len(address) or len(address) > 90:
        return None

    hrp = address[:separator]
    data: list[int] = []
    for char in address[separator + 1 :]:
        index = BECH32_CHARSET.find(char)
        if index == -1:
            return None
        data.append(index)

    checksum = _bech32_polymod(_bech32_hrp_expand(hrp) + data)
    if checksum == 1:
        spec = "bech32"
    elif checksum == BECH32M_CONST:
        spec = "bech32m"
    else:
        return None

    return hrp, data[:-6], spec


def _convert_bits(data: list[int], from_bits: int, to_bits: int, pad: bool) -> list[int] | None:
    accumulator = 0
    bits = 0
    result: list[int] = []
    max_value = (1 << to_bits) - 1
    max_accumulator = (1 << (from_bits + to_bits - 1)) - 1

    for value in data:
        if value < 0 or value >> from_bits:
            return None
        accumulator = ((accumulator << from_bits) | value) & max_accumulator
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            result.append((accumulator >> bits) & max_value)

    if pad:
        if bits:
            result.append((accumulator << (to_bits - bits)) & max_value)
    elif bits >= from_bits or ((accumulator << (to_bits - bits)) & max_value):
        return None

    return result


def _is_valid_bitcoin_bech32(address: str) -> bool:
    decoded = _bech32_decode(address)
    if decoded is None:
        return False

    hrp, data, spec = decoded
    if hrp != "bc" or not data:
        return False

    witness_version = data[0]
    if witness_version > 16:
        return False

    witness_program = _convert_bits(data[1:], 5, 8, False)
    if witness_program is None or not 2 <= len(witness_program) <= 40:
        return False

    if witness_version == 0:
        return spec == "bech32" and len(witness_program) in {20, 32}

    return spec == "bech32m"
