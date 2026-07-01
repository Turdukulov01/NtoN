from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import urllib.request

from sqlalchemy import delete, select

from app.core.database import AsyncSessionLocal
from app.domain.sanctions.normalization import normalize_address, normalize_identifier, normalize_name
from app.domain.sanctions.schemas import SanctionsSource
from app.infrastructure.sanctions.uk_xml import UK_LIST_NAME, UK_SOURCE_URL, parse_uk_sanctions_xml
from app.models import (
    SanctionsAddress,
    SanctionsDocument,
    SanctionsDocumentTypeEnum,
    SanctionsImportRun,
    SanctionsMatch,
    SanctionsName,
    SanctionsNameTypeEnum,
    SanctionsSourceEnum,
    SanctionsSubject,
    SanctionsSubjectTypeEnum,
)


PARSER_VERSION = "uk_xml_v1"


@dataclass(slots=True)
class SanctionsImportResult:
    source: str
    list_name: str
    source_file: str
    source_sha256: str
    publication_date: str | None
    record_count: int
    import_run_id: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_file(url: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as response:
        destination.write_bytes(response.read())
    return destination


def _source_enum(source: SanctionsSource) -> SanctionsSourceEnum:
    return SanctionsSourceEnum(source.value)


def _subject_type_enum(value: str) -> SanctionsSubjectTypeEnum:
    return SanctionsSubjectTypeEnum(value)


def _name_type_enum(value: str) -> SanctionsNameTypeEnum:
    return SanctionsNameTypeEnum(value)


def _document_type_enum(value: str) -> SanctionsDocumentTypeEnum:
    return SanctionsDocumentTypeEnum(value)


async def replace_uk_sanctions_from_xml(xml_path: str | Path, *, source_url: str | None = UK_SOURCE_URL) -> SanctionsImportResult:
    path = Path(xml_path).expanduser().resolve()
    publication_date, records = parse_uk_sanctions_xml(path)
    source_sha256 = sha256_file(path)
    source = SanctionsSource.UK_OFSI

    async with AsyncSessionLocal() as session:
        import_run = SanctionsImportRun(
            source=_source_enum(source),
            list_name=UK_LIST_NAME,
            source_url=source_url,
            source_format="xml",
            source_sha256=source_sha256,
            publication_date=publication_date,
            parser_version=PARSER_VERSION,
            status="running",
            record_count=0,
            metadata_json={"replace_strategy": "replace_source"},
        )
        session.add(import_run)
        await session.flush()

        subject_ids = select(SanctionsSubject.id).where(SanctionsSubject.source == _source_enum(source))
        await session.execute(delete(SanctionsMatch).where(SanctionsMatch.sanctions_subject_id.in_(subject_ids)))
        await session.execute(delete(SanctionsAddress).where(SanctionsAddress.subject_id.in_(subject_ids)))
        await session.execute(delete(SanctionsDocument).where(SanctionsDocument.subject_id.in_(subject_ids)))
        await session.execute(delete(SanctionsName).where(SanctionsName.subject_id.in_(subject_ids)))
        await session.execute(delete(SanctionsSubject).where(SanctionsSubject.source == _source_enum(source)))

        for record in records:
            subject = SanctionsSubject(
                import_run_id=import_run.id,
                source=_source_enum(record.source),
                list_name=record.list_name,
                subject_type=_subject_type_enum(record.subject_type.value),
                primary_name=record.primary_name,
                primary_name_key=normalize_name(record.primary_name),
                program=record.program,
                sanctions_imposed=record.sanctions_imposed,
                designation_source=record.designation_source,
                date_designated=record.date_designated,
                last_updated=record.last_updated,
                unique_id=record.unique_id,
                ofsi_group_id=record.ofsi_group_id,
                un_reference_id=record.un_reference_id,
                raw_text=record.raw_text,
                source_payload=record.source_payload,
            )
            subject.names = [
                SanctionsName(
                    name_type=_name_type_enum(name.name_type.value),
                    value=name.value,
                    normalized_value=normalize_name(name.value),
                    quality=name.quality,
                    script=name.script,
                    language=name.language,
                )
                for name in record.names
                if normalize_name(name.value)
            ]
            subject.documents = [
                SanctionsDocument(
                    document_type=_document_type_enum(document.document_type.value),
                    value=document.value,
                    normalized_value=normalize_identifier(document.value),
                    country=document.country,
                    note=document.note,
                )
                for document in record.documents
                if normalize_identifier(document.value)
            ]
            subject.addresses = [
                SanctionsAddress(
                    full_text=address.full_text,
                    normalized_text=normalize_address(address.full_text),
                    country=address.country,
                    parts=address.parts,
                )
                for address in record.addresses
                if normalize_address(address.full_text)
            ]
            session.add(subject)

        import_run.status = "completed"
        import_run.record_count = len(records)
        import_run.completed_at = datetime.now(timezone.utc)
        await session.commit()

    return SanctionsImportResult(
        source=source.value,
        list_name=UK_LIST_NAME,
        source_file=str(path),
        source_sha256=source_sha256,
        publication_date=publication_date.isoformat() if publication_date else None,
        record_count=len(records),
        import_run_id=str(import_run.id),
    )


async def download_and_replace_uk_sanctions(
    *,
    url: str = UK_SOURCE_URL,
    destination: str | Path = "data/sanctions/uk_official/UK-Sanctions-List.xml",
) -> SanctionsImportResult:
    path = download_file(url, Path(destination))
    return await replace_uk_sanctions_from_xml(path, source_url=url)
