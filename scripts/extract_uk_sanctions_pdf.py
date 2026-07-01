from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


RECORD_TYPES = {"INDIVIDUAL", "ENTITY", "SHIP", "AIRCRAFT"}

FIELD_LABELS = [
    "Primary Name Variations",
    "Primary Name Varia ons",
    "Primary Name",
    "Alias - Good Quality",
    "Alias - Low Quality",
    "Alias - A.k.a",
    "Alias",
    "Non Latin Names",
    "Non La n Names",
    "Position",
    "Posi on",
    "Address",
    "Phone Number",
    "Email address",
    "Website",
    "D.O.B",
    "Place of Birth",
    "Nationality(/ies)",
    "Na onality(/ies)",
    "Passport number",
    "Passport Additional Information",
    "Passport Addi onal Informa on",
    "National Identifier Number",
    "Na onal Iden ﬁer Number",
    "National Identifier Additional Information",
    "Na onal Iden ﬁer Addi onal Informa on",
    "Gender",
    "IMO Number",
    "IMO number",
    "Type of Ship",
    "Flag",
    "Parent Company",
    "Business Reg Number",
    "Business registration number(s)",
    "Business registra on number(s)",
    "Sanctions Imposed",
    "Sanc ons Imposed",
    "UK Statement of Reasons",
    "Other Information",
    "Other Informa on",
    "Designation Source",
    "Designa on Source",
    "Date Designated",
    "Last Updated",
    "Unique Id",
    "OFSI Group ID",
    "UN Reference ID",
]

LABEL_NORMALISATION = {
    "Primary Name Varia ons": "Primary Name Variations",
    "Non La n Names": "Non Latin Names",
    "Posi on": "Position",
    "Na onality(/ies)": "Nationality(/ies)",
    "Passport Addi onal Informa on": "Passport Additional Information",
    "Na onal Iden ﬁer Number": "National Identifier Number",
    "Na onal Iden ﬁer Addi onal Informa on": "National Identifier Additional Information",
    "Sanc ons Imposed": "Sanctions Imposed",
    "Other Informa on": "Other Information",
    "Designa on Source": "Designation Source",
    "IMO number": "IMO Number",
    "Business registra on number(s)": "Business registration number(s)",
}

TEXT_NORMALISATION = {
    "Sanc ons": "Sanctions",
    "sanc ons": "sanctions",
    "Sanc on": "Sanction",
    "sanc on": "sanction",
    "Informa on": "Information",
    "informa on": "information",
    "Designa on": "Designation",
    "designa on": "designation",
    "Na onality": "Nationality",
    "na onality": "nationality",
    "Na onal": "National",
    "na onal": "national",
    "Addi onal": "Additional",
    "addi onal": "additional",
    "Varia ons": "Variations",
    "varia ons": "variations",
    "Posi on": "Position",
    "posi on": "position",
    "La n": "Latin",
    "la n": "latin",
    "Iden ﬁer": "Identifier",
    "iden ﬁer": "identifier",
    "Disqualiﬁca on": "Disqualification",
    "disqualiﬁca on": "disqualification",
}


@dataclass
class SanctionsRecord:
    source_file: str
    list_publication_last_updated: str | None
    regime: str | None
    record_type: str | None
    list_number: int
    sequence: int
    raw_text: str
    fields: dict[str, str | list[str]]


def run_pdftotext(pdf_path: Path, txt_path: Path) -> None:
    txt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["pdftotext", "-layout", str(pdf_path), str(txt_path)],
        check=True,
    )


def clean_line(line: str) -> str:
    return line.replace("\f", "").strip()


def is_page_footer(line: str) -> bool:
    return bool(re.fullmatch(r"Page \d+ of \d+", clean_line(line)))


def next_content_line(lines: list[str], index: int) -> str:
    cursor = index + 1
    while cursor < len(lines):
        line = clean_line(lines[cursor])
        if line and not is_page_footer(line):
            return line
        cursor += 1
    return ""


def is_entry_start(lines: list[str], index: int) -> bool:
    line = clean_line(lines[index])
    if not re.fullmatch(r"\d+\.", line):
        return False
    return bool(re.match(r"Primary\s+Name:", next_content_line(lines, index)))


def is_upperish(line: str) -> bool:
    letters = [char for char in line if char.isalpha()]
    if not letters:
        return False
    upper_count = sum(1 for char in letters if char.upper() == char)
    return upper_count / len(letters) > 0.85


def is_regime_header_start(line: str) -> bool:
    return "REGULATIONS" in line and ":" not in line and is_upperish(line)


def is_regime_header_prefix(line: str) -> bool:
    return line.startswith("THE ") and ":" not in line and is_upperish(line)


def normalise_value_text(value: str) -> str:
    text = value
    for source, target in TEXT_NORMALISATION.items():
        text = text.replace(source, target)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_fields(raw_text: str) -> dict[str, str | list[str]]:
    text = normalise_value_text(raw_text)
    labels = sorted(FIELD_LABELS, key=len, reverse=True)
    label_patterns = [re.escape(label).replace(r"\ ", r"\s+") for label in labels]
    pattern = re.compile(r"(?<![A-Za-z])(" + "|".join(label_patterns) + r")\s*:")

    matches = list(pattern.finditer(text))
    fields: dict[str, str | list[str]] = {}
    for index, match in enumerate(matches):
        raw_label = re.sub(r"\s+", " ", match.group(1)).strip()
        label = LABEL_NORMALISATION.get(raw_label, raw_label)
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = text[start:end].strip()
        if not value:
            continue
        existing = fields.get(label)
        if existing is None:
            fields[label] = value
        elif isinstance(existing, list):
            existing.append(value)
        else:
            fields[label] = [existing, value]
    return fields


def finalise_record(
    records: list[SanctionsRecord],
    *,
    source_file: str,
    publication_last_updated: str | None,
    regime: str | None,
    record_type: str | None,
    list_number: int,
    sequence: int,
    lines: list[str],
) -> None:
    raw_lines = [line.rstrip() for line in lines if clean_line(line) and not is_page_footer(line)]
    raw_text = "\n".join(raw_lines).strip()
    if not raw_text:
        return
    records.append(
        SanctionsRecord(
            source_file=source_file,
            list_publication_last_updated=publication_last_updated,
            regime=regime,
            record_type=record_type,
            list_number=list_number,
            sequence=sequence,
            raw_text=raw_text,
            fields=parse_fields(raw_text),
        )
    )


def parse_records(text: str, source_file: str) -> list[SanctionsRecord]:
    lines = text.splitlines()
    publication_last_updated: str | None = None
    current_regime: str | None = None
    current_type: str | None = None
    header_buffer: list[str] = []
    records: list[SanctionsRecord] = []
    active_lines: list[str] = []
    active_number = 0

    def finish_active() -> None:
        nonlocal active_lines, active_number
        if not active_lines:
            return
        finalise_record(
            records,
            source_file=source_file,
            publication_last_updated=publication_last_updated,
            regime=current_regime,
            record_type=current_type,
            list_number=active_number,
            sequence=len(records) + 1,
            lines=active_lines,
        )
        active_lines = []
        active_number = 0

    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = clean_line(line)

        if not stripped or is_page_footer(line):
            index += 1
            continue

        if stripped.startswith("Last Updated:") and publication_last_updated is None:
            publication_last_updated = stripped.removeprefix("Last Updated:").strip()
            index += 1
            continue

        if stripped in RECORD_TYPES:
            finish_active()
            if header_buffer:
                current_regime = " ".join(header_buffer)
                header_buffer = []
            current_type = stripped
            index += 1
            continue

        if header_buffer and ":" not in stripped and (is_upperish(stripped) or re.fullmatch(r"\d{4}", stripped)):
            header_buffer.append(stripped)
            index += 1
            continue

        if is_regime_header_start(stripped) or (
            is_regime_header_prefix(stripped) and "REGULATIONS" in next_content_line(lines, index)
        ):
            finish_active()
            header_buffer = [stripped]
            index += 1
            continue

        if is_entry_start(lines, index):
            finish_active()
            active_number = int(stripped.rstrip("."))
            index += 1
            continue

        if active_number:
            active_lines.append(line)

        index += 1

    finish_active()
    return records


def write_jsonl(records: Iterable[SanctionsRecord], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(asdict(record), ensure_ascii=False, sort_keys=True))
            handle.write("\n")


def scalar_field(fields: dict[str, str | list[str]], name: str) -> str:
    value = fields.get(name)
    if isinstance(value, list):
        return " | ".join(value)
    return value or ""


def write_csv(records: list[SanctionsRecord], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "sequence",
        "regime",
        "record_type",
        "list_number",
        "primary_name",
        "primary_name_variations",
        "alias_good_quality",
        "alias_low_quality",
        "alias_aka",
        "non_latin_names",
        "nationality",
        "date_of_birth",
        "place_of_birth",
        "sanctions_imposed",
        "designation_source",
        "date_designated",
        "last_updated",
        "unique_id",
        "ofsi_group_id",
        "un_reference_id",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for record in records:
            fields = record.fields
            writer.writerow(
                {
                    "sequence": record.sequence,
                    "regime": record.regime or "",
                    "record_type": record.record_type or "",
                    "list_number": record.list_number,
                    "primary_name": scalar_field(fields, "Primary Name"),
                    "primary_name_variations": scalar_field(fields, "Primary Name Variations"),
                    "alias_good_quality": scalar_field(fields, "Alias - Good Quality"),
                    "alias_low_quality": scalar_field(fields, "Alias - Low Quality"),
                    "alias_aka": scalar_field(fields, "Alias - A.k.a"),
                    "non_latin_names": scalar_field(fields, "Non Latin Names"),
                    "nationality": scalar_field(fields, "Nationality(/ies)"),
                    "date_of_birth": scalar_field(fields, "D.O.B"),
                    "place_of_birth": scalar_field(fields, "Place of Birth"),
                    "sanctions_imposed": scalar_field(fields, "Sanctions Imposed"),
                    "designation_source": scalar_field(fields, "Designation Source"),
                    "date_designated": scalar_field(fields, "Date Designated"),
                    "last_updated": scalar_field(fields, "Last Updated"),
                    "unique_id": scalar_field(fields, "Unique Id"),
                    "ofsi_group_id": scalar_field(fields, "OFSI Group ID"),
                    "un_reference_id": scalar_field(fields, "UN Reference ID"),
                }
            )


def build_summary(records: list[SanctionsRecord]) -> dict[str, object]:
    by_type: dict[str, int] = {}
    by_regime: dict[str, int] = {}
    missing_unique_id = 0
    for record in records:
        by_type[record.record_type or "UNKNOWN"] = by_type.get(record.record_type or "UNKNOWN", 0) + 1
        by_regime[record.regime or "UNKNOWN"] = by_regime.get(record.regime or "UNKNOWN", 0) + 1
        if not scalar_field(record.fields, "Unique Id"):
            missing_unique_id += 1
    return {
        "record_count": len(records),
        "publication_last_updated": records[0].list_publication_last_updated if records else None,
        "records_by_type": dict(sorted(by_type.items())),
        "records_by_regime": dict(sorted(by_regime.items())),
        "missing_unique_id_count": missing_unique_id,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract UK sanctions PDF into text, JSONL and CSV.")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--out-dir", type=Path, default=Path("data/sanctions/uk"))
    parser.add_argument("--skip-text-extract", action="store_true")
    args = parser.parse_args()

    pdf_path = args.pdf.expanduser().resolve()
    out_dir = args.out_dir
    txt_path = out_dir / "uk_sanctions_list.txt"
    jsonl_path = out_dir / "uk_sanctions_list_records.jsonl"
    csv_path = out_dir / "uk_sanctions_list_records.csv"
    summary_path = out_dir / "uk_sanctions_list_summary.json"

    if not args.skip_text_extract or not txt_path.exists():
        run_pdftotext(pdf_path, txt_path)

    text = txt_path.read_text(encoding="utf-8", errors="replace")
    records = parse_records(text, str(pdf_path))
    write_jsonl(records, jsonl_path)
    write_csv(records, csv_path)
    summary = build_summary(records)
    summary.update(
        {
            "source_pdf": str(pdf_path),
            "text_output": str(txt_path),
            "jsonl_output": str(jsonl_path),
            "csv_output": str(csv_path),
        }
    )
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
