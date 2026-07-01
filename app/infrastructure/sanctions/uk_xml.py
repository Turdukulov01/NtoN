from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

from app.domain.sanctions.normalization import split_pipe
from app.domain.sanctions.schemas import (
    SanctionsAddressRecord,
    SanctionsDocumentRecord,
    SanctionsDocumentType,
    SanctionsNameRecord,
    SanctionsNameType,
    SanctionsSource,
    SanctionsSubjectRecord,
    SanctionsSubjectType,
)


UK_SOURCE_URL = "https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.xml"
UK_LIST_NAME = "UK Sanctions List"


def _text(element: ET.Element | None, tag: str) -> str:
    if element is None:
        return ""
    value = element.findtext(tag)
    return str(value or "").strip()


def _child_texts(element: ET.Element | None, path: str, tag: str) -> list[str]:
    if element is None:
        return []
    parent = element.find(path)
    if parent is None:
        return []
    return [str(child.text or "").strip() for child in parent.findall(tag) if str(child.text or "").strip()]


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    text = value.strip()
    if not text or "dd" in text or "mm" in text:
        return None
    parts = text.split("/")
    if len(parts) != 3:
        return None
    day, month, year = parts
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def _subject_type(value: str | None) -> SanctionsSubjectType:
    key = str(value or "").strip().lower()
    return {
        "individual": SanctionsSubjectType.INDIVIDUAL,
        "entity": SanctionsSubjectType.ENTITY,
        "ship": SanctionsSubjectType.SHIP,
        "aircraft": SanctionsSubjectType.AIRCRAFT,
    }.get(key, SanctionsSubjectType.UNKNOWN)


def _name_value(name: ET.Element) -> str:
    parts = [
        _text(name, "Name1"),
        _text(name, "Name2"),
        _text(name, "Name3"),
        _text(name, "Name4"),
        _text(name, "Name5"),
        _text(name, "Name6"),
    ]
    return " ".join(part for part in parts if part).strip()


def _name_type(value: str | None) -> SanctionsNameType:
    key = str(value or "").strip().lower()
    if key == "primary name":
        return SanctionsNameType.PRIMARY
    if "variation" in key:
        return SanctionsNameType.VARIATION
    if "alias" in key:
        return SanctionsNameType.ALIAS
    return SanctionsNameType.ALIAS


def _address_parts(address: ET.Element) -> dict[str, str]:
    keys = [
        "AddressLine1",
        "AddressLine2",
        "AddressLine3",
        "AddressLine4",
        "AddressLine5",
        "AddressLine6",
        "AddressPostalCode",
        "AddressCountry",
    ]
    return {key: _text(address, key) for key in keys if _text(address, key)}


def _address_text(address: ET.Element) -> str:
    parts = _address_parts(address)
    return ", ".join(parts[key] for key in parts if parts[key])


def _payload(element: ET.Element) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for child in element:
        if list(child):
            value: Any = _payload(child)
        else:
            value = str(child.text or "").strip()
        if child.tag in payload:
            existing = payload[child.tag]
            if isinstance(existing, list):
                existing.append(value)
            else:
                payload[child.tag] = [existing, value]
            continue
        if list(child):
            payload[child.tag] = value
        else:
            payload[child.tag] = value
    return payload


def _raw_text(record: SanctionsSubjectRecord) -> str:
    lines = [
        f"Source: {record.source.value}",
        f"List: {record.list_name}",
        f"Program: {record.program}",
        f"Subject type: {record.subject_type.value}",
        f"Primary name: {record.primary_name}",
        f"Unique ID: {record.unique_id}",
    ]
    if record.ofsi_group_id:
        lines.append(f"OFSI Group ID: {record.ofsi_group_id}")
    if record.un_reference_id:
        lines.append(f"UN Reference ID: {record.un_reference_id}")
    if record.date_designated:
        lines.append(f"Date designated: {record.date_designated.isoformat()}")
    if record.last_updated:
        lines.append(f"Last updated: {record.last_updated.isoformat()}")
    if record.sanctions_imposed:
        lines.append(f"Sanctions imposed: {', '.join(record.sanctions_imposed)}")
    alias_values = [name.value for name in record.names if name.name_type is not SanctionsNameType.PRIMARY]
    if alias_values:
        lines.append(f"Aliases: {' | '.join(alias_values)}")
    if record.documents:
        lines.append("Documents: " + " | ".join(f"{doc.document_type.value}:{doc.value}" for doc in record.documents))
    if record.addresses:
        lines.append("Addresses: " + " | ".join(address.full_text for address in record.addresses))
    return "\n".join(lines)


def parse_uk_sanctions_xml(path: str | Path) -> tuple[date | None, list[SanctionsSubjectRecord]]:
    tree = ET.parse(path)
    root = tree.getroot()
    publication_date = _parse_date(root.findtext("DateGenerated"))
    records: list[SanctionsSubjectRecord] = []

    for designation in root.findall("Designation"):
        names: list[SanctionsNameRecord] = []
        primary_name = ""
        names_node = designation.find("Names")
        if names_node is not None:
            for name in names_node.findall("Name"):
                value = _name_value(name)
                if not value:
                    continue
                name_type = _name_type(_text(name, "NameType"))
                if name_type is SanctionsNameType.PRIMARY and not primary_name:
                    primary_name = value
                names.append(
                    SanctionsNameRecord(
                        value=value,
                        name_type=name_type,
                        quality=_text(name, "AliasStrength") or None,
                    )
                )

        non_latin_node = designation.find("NonLatinNames")
        if non_latin_node is not None:
            for non_latin in non_latin_node.findall("NonLatinName"):
                value = _text(non_latin, "NameNonLatinScript")
                if value:
                    names.append(
                        SanctionsNameRecord(
                            value=value,
                            name_type=SanctionsNameType.NON_LATIN,
                            script=_text(non_latin, "NonLatinScriptType") or None,
                            language=_text(non_latin, "NonLatinScriptLanguage") or None,
                        )
                    )

        addresses: list[SanctionsAddressRecord] = []
        addresses_node = designation.find("Addresses")
        if addresses_node is not None:
            for address in addresses_node.findall("Address"):
                full_text = _address_text(address)
                if full_text:
                    addresses.append(
                        SanctionsAddressRecord(
                            full_text=full_text,
                            country=_text(address, "AddressCountry") or None,
                            parts=_address_parts(address),
                        )
                    )

        documents: list[SanctionsDocumentRecord] = []
        individual = designation.find("IndividualDetails/Individual")
        if individual is not None:
            for passport in individual.findall("PassportDetails/Passport"):
                number = _text(passport, "PassportNumber")
                if number:
                    documents.append(
                        SanctionsDocumentRecord(
                            value=number,
                            document_type=SanctionsDocumentType.PASSPORT,
                            note=_text(passport, "PassportAdditionalInformation") or None,
                        )
                    )
            for national_id in individual.findall("NationalIdentifierDetails/NationalIdentifier"):
                number = _text(national_id, "NationalIdentifierNumber")
                if number:
                    documents.append(
                        SanctionsDocumentRecord(
                            value=number,
                            document_type=SanctionsDocumentType.NATIONAL_ID,
                            note=_text(national_id, "NationalIdentifierAdditionalInformation") or None,
                        )
                    )

        entity = designation.find("EntityDetails/Entity")
        if entity is not None:
            for value in _child_texts(entity, "BusinessRegistrationNumbers", "BusinessRegistrationNumber"):
                documents.append(
                    SanctionsDocumentRecord(
                        value=value,
                        document_type=SanctionsDocumentType.BUSINESS_REGISTRATION,
                    )
                )

        ship = designation.find("ShipDetails/Ship")
        if ship is not None:
            for value in _child_texts(ship, "IMONumbers", "IMONumber"):
                documents.append(SanctionsDocumentRecord(value=value, document_type=SanctionsDocumentType.IMO))
            for value in _child_texts(ship, "HullIdentificationNumbers", "HullIdentificationNumber"):
                documents.append(SanctionsDocumentRecord(value=value, document_type=SanctionsDocumentType.HIN))

        source_payload = _payload(designation)
        record = SanctionsSubjectRecord(
            source=SanctionsSource.UK_OFSI,
            list_name=UK_LIST_NAME,
            subject_type=_subject_type(_text(designation, "IndividualEntityShip")),
            program=_text(designation, "RegimeName"),
            primary_name=primary_name or (names[0].value if names else _text(designation, "UniqueID")),
            sanctions_imposed=split_pipe(_text(designation, "SanctionsImposed")),
            designation_source=_text(designation, "DesignationSource") or None,
            date_designated=_parse_date(_text(designation, "DateDesignated")),
            last_updated=_parse_date(_text(designation, "LastUpdated")),
            unique_id=_text(designation, "UniqueID"),
            ofsi_group_id=_text(designation, "OFSIGroupID") or None,
            un_reference_id=_text(designation, "UNReferenceNumber") or None,
            source_payload=source_payload,
            names=names,
            addresses=addresses,
            documents=documents,
        )
        record.raw_text = _raw_text(record)
        records.append(record)

    return publication_date, records
