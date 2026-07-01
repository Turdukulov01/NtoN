from __future__ import annotations

import re
import unicodedata
from typing import Any


def normalize_name(value: Any) -> str:
    text = str(value or "").strip().lower().replace("ё", "е")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = "".join(char if char.isalnum() else " " for char in text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_identifier(value: Any) -> str:
    text = str(value or "").strip().upper()
    return re.sub(r"[^0-9A-ZА-Я]+", "", text)


def normalize_address(value: Any) -> str:
    text = normalize_name(value)
    return re.sub(r"\b(street|str|road|rd|avenue|ave|office|suite|floor)\b", "", text).strip()


def split_pipe(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in str(value).split("|") if part and part.strip()]
