from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any


class SanctionsSource(str, Enum):
    UK_OFSI = "UK_OFSI"
    UN = "UN"
    US_OFAC = "US_OFAC"
    EU = "EU"


class SanctionsSubjectType(str, Enum):
    INDIVIDUAL = "individual"
    ENTITY = "entity"
    SHIP = "ship"
    AIRCRAFT = "aircraft"
    UNKNOWN = "unknown"


class SanctionsNameType(str, Enum):
    PRIMARY = "primary"
    ALIAS = "alias"
    VARIATION = "variation"
    NON_LATIN = "non_latin"


class SanctionsDocumentType(str, Enum):
    PASSPORT = "passport"
    NATIONAL_ID = "national_id"
    BUSINESS_REGISTRATION = "business_registration"
    IMO = "imo"
    HIN = "hin"
    OTHER = "other"


class SanctionsMatchLevel(str, Enum):
    EXACT = "exact"
    STRONG = "strong"
    WEAK = "weak"


class SanctionsReviewStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    FALSE_POSITIVE = "false_positive"


@dataclass(slots=True)
class SanctionsNameRecord:
    value: str
    name_type: SanctionsNameType
    quality: str | None = None
    script: str | None = None
    language: str | None = None


@dataclass(slots=True)
class SanctionsDocumentRecord:
    value: str
    document_type: SanctionsDocumentType
    country: str | None = None
    note: str | None = None


@dataclass(slots=True)
class SanctionsAddressRecord:
    full_text: str
    country: str | None = None
    parts: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SanctionsSubjectRecord:
    source: SanctionsSource
    list_name: str
    subject_type: SanctionsSubjectType
    program: str
    primary_name: str
    sanctions_imposed: list[str]
    designation_source: str | None
    date_designated: date | None
    last_updated: date | None
    unique_id: str
    ofsi_group_id: str | None = None
    un_reference_id: str | None = None
    raw_text: str = ""
    source_payload: dict[str, Any] = field(default_factory=dict)
    names: list[SanctionsNameRecord] = field(default_factory=list)
    addresses: list[SanctionsAddressRecord] = field(default_factory=list)
    documents: list[SanctionsDocumentRecord] = field(default_factory=list)


@dataclass(slots=True)
class SanctionsScreeningInput:
    checked_subject_type: str
    name: str | None = None
    aliases: list[str] = field(default_factory=list)
    date_of_birth: str | None = None
    nationality: str | None = None
    address: str | None = None
    passport_numbers: list[str] = field(default_factory=list)
    national_identifiers: list[str] = field(default_factory=list)
    business_registration_numbers: list[str] = field(default_factory=list)
    imo_numbers: list[str] = field(default_factory=list)
    unique_id: str | None = None
    ofsi_group_id: str | None = None
    un_reference_id: str | None = None


@dataclass(slots=True)
class SanctionsScreeningMatch:
    subject_unique_id: str
    subject_id: str | None
    primary_name: str
    source: str
    list_name: str
    program: str
    match_score: float
    match_level: SanctionsMatchLevel
    matched_fields: list[str]
    override_hit: bool
    review_status: SanctionsReviewStatus
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SanctionsScreeningResult:
    override_hit: bool
    risk_tag: str
    score: int
    risk_zone: str
    matches: list[SanctionsScreeningMatch]
    manual_review: bool
