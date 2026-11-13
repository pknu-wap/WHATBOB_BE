from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.recommend.personal_model import (
    collect_training_samples_from_db,
    publish_personal_model_artifact,
    train_personal_logistic_model,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WhatBap 개인화 로지스틱 회귀 모델 오프라인 학습 및 아티팩트 배포")
    parser.add_argument("--database-url", default=settings.DATABASE_URL, help="SQLAlchemy 데이터베이스 URL")
    parser.add_argument("--version", default="v1", help="생성할 모델 버전 문자열")
    parser.add_argument("--output", default=settings.PERSONAL_MODEL_ARTIFACT_PATH, help="활성 아티팩트 파일 경로")
    parser.add_argument("--min-samples", type=int, default=10, help="최소 학습 표본 수")
    parser.add_argument("--dry-run", action="store_true", help="배포하지 않고 검증 결과만 출력")
    args = parser.parse_args(argv)

    engine = create_engine(args.database_url)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    with SessionLocal() as db:
        samples = collect_training_samples_from_db(db)
        payload = train_personal_logistic_model(
            samples,
            version=args.version,
            min_samples=args.min_samples,
        )

    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if payload.get("validation_passed") else 1

    published = publish_personal_model_artifact(payload, active_path=Path(args.output))
    print(f"Published personal model artifact to {published}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
