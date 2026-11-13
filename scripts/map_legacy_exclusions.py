from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.users.exclusion_mapping import apply_legacy_exclusion_mapping, inspect_legacy_exclusions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="기존 ALLERGY / UNABLE 자유 문자열 데이터를 표준 카탈로그 코드로 매핑")
    parser.add_argument("--database-url", default=settings.DATABASE_URL, help="SQLAlchemy 데이터베이스 URL")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="매핑 가능 건수 및 미대응 보존 건수 사전 점검")
    group.add_argument("--apply", action="store_true", help="정확 대응 항목에 대해 카탈로그 코드 매핑 실행")
    group.add_argument("--verify", action="store_true", help="매핑 후 상태 검증")
    parser.add_argument("--maintenance-confirmed", action="store_true", help="--apply 실행 확인 플래그")
    args = parser.parse_args(argv)

    if args.apply and not args.maintenance_confirmed:
        print("--apply 실행 시 --maintenance-confirmed 플래그가 필요합니다.", file=sys.stderr)
        return 2

    engine = create_engine(args.database_url)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    with SessionLocal() as db:
        if args.check:
            report = inspect_legacy_exclusions(db)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if args.apply:
            result = apply_legacy_exclusion_mapping(db)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.verify:
            report = inspect_legacy_exclusions(db)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["mappable"] == 0 else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
