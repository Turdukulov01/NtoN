from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.application.sanctions.import_service import (
    download_and_replace_uk_sanctions,
    replace_uk_sanctions_from_xml,
)
from app.infrastructure.sanctions.uk_xml import UK_SOURCE_URL


def main() -> None:
    parser = argparse.ArgumentParser(description="Import official UK Sanctions List XML into PostgreSQL.")
    parser.add_argument("--xml", type=Path, help="Local UK-Sanctions-List.xml path.")
    parser.add_argument("--download", action="store_true", help="Download the official UKSL XML before import.")
    parser.add_argument("--url", default=UK_SOURCE_URL)
    parser.add_argument("--destination", type=Path, default=Path("data/sanctions/uk_official/UK-Sanctions-List.xml"))
    args = parser.parse_args()

    async def run():
        if args.download:
            return await download_and_replace_uk_sanctions(url=args.url, destination=args.destination)
        if not args.xml:
            raise SystemExit("--xml is required unless --download is used")
        return await replace_uk_sanctions_from_xml(args.xml, source_url=args.url)

    result = asyncio.run(run())
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
