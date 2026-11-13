# WhatBap Backend Development Log

WhatBap 백엔드의 주요 개발 및 변경 사항을 기록한다.
최신 작업을 위에 추가하며, 기록 방법은 같은 디렉터리의 `AGENTS.md`를 따른다.

## 2026-11-13

### #39 T02~T09 개인 데이터·방 유예/취소·프로필·계정 전환/병합/로그아웃·MySQL 마이그레이션 및 동시성 검증 구현

#### 변경 이유 및 내용

- `docs/plans/39-design-baseline.md`와 확정된 공통 선택(`R1A`~`R5A`, `M1A`~`M4A`)을 기준으로 기존 T01 및 사용자 변경을 보존하면서 T02~T09를 의존 순서대로 구현했다.
- **T02 (개인 데이터·추천·피드백·개인화 모델)**:
  - 추천·질문·피드백 API에 Bearer JWT 인증을 필수로 적용하고 요청 본문 `user_id`를 제거했다.
  - 식약처 19대 알레르기 및 8대 섭취불가 재료군(`M2A`) 표준 카탈로그 조회·유형별 전체 교체 API(`GET/PUT /api/users/me/allergies`, `GET/PUT /api/users/me/unables`)와 미매핑 레거시 제외 항목 보호(`409 EXCLUSION_MAPPING_REQUIRED`)·배치 매핑 스크립트(`scripts/map_legacy_exclusions.py`)를 구현했다.
  - 추천 생성 시점 특징 스냅샷(`RecommendationItemSnapshot`)과 추천·메뉴별 최신 유효 1표 상태(`RecommendationFeedbackState`)를 분리해 피드백 수정 시 원본 이력(`RecommendationFeedback`)을 보존하면서 유효 상태와 전역 질문 통계만 단일 트랜잭션으로 재계산하도록 변경했다.
  - 다중 신호 개인화 로지스틱 추론(`±5%` 보정 상한, 90일 반감기, 수축 `k=5`, 최근 3일 동일 식사 특징 반영, 근접 후보 가중 추첨 `beta=0.5`, 아티팩트 검증 실패 시 자동 폴백)과 오프라인 학습 CLI(`scripts/train_personal_model.py`)를 구현했다.
- **T03 (방 60초 재접속 유예·재시작 복구·방 작업 취소)**:
  - 다중 WebSocket 연결 중 마지막 연결 종료 시에만 `disconnected_at`을 기록하고, 60초 경과 시 일반 멤버 자동 퇴장(주문서 삭제·인원 재계산·`member_left` 브로드캐스트) 및 호스트 방 종료(`HOST_DISCONNECTED`)를 수행하도록 구현했다.
  - 앱 시작 시 `recover_rooms_after_restart`로 인메모리 연결이 없는 온라인 멤버들을 유예 상태로 전환하도록 구성하고, `Idempotency-Key` 기반 방 생성/참여 및 방 작업 상태 조회·취소 API(`GET /api/room-operations/{operation_key}`, `POST /api/room-operations/{operation_key}/cancellation`)를 추가했다.
- **T04 & T05 (닉네임 편집 및 프로필 사진 업로드/삭제/서빙/고아 정리)**:
  - `PATCH /api/users/me/nickname`(앞뒤 공백 제거 후 2~20자 한글/영문/숫자/공백 및 `nickname_key` 전역 유일성 검증, 출처 플래그 기록)을 구현했다.
  - `PUT /api/users/me/profile-image`, `DELETE /api/users/me/profile-image`, `GET /uploads/profile-images/{filename}`(`R5A`)을 구현했다. 5MB 스트리밍 제한, `BoundedSemaphore(1)` 기반 동시 디코딩 제한, `JPEG`/`PNG`/`WebP` 정지 이미지·1,200만 픽셀 상한 검증, EXIF 회전 반영 및 메타데이터 제거(`Image.new` + `paste`), 긴 변 512px 리사이즈, 임시 파일 원자적 교체, 참조 카운트 기반 삭제 및 고아 파일 정리(`cleanup_orphan_profile_images`)를 적용했다.
- **T06, T07, T08 (계정 전환 2단계 Prepare/Commit·무손실 이관/병합·로그아웃 왕복·응답 유실 복구)**:
  - `POST /api/auth/transitions`, `POST /api/auth/transitions/{transition_id}/commit`, `GET /api/auth/transitions/{transition_id}`, `POST /api/auth/transitions/{transition_id}/recovery`를 구현했다.
  - `KAKAO_LINK` 확정 시 `KakaoLinkGuard(kakao_id) -> User(id ASC) -> AuthSession(id ASC) -> Room/Task` 고정 잠금 순서 아래 방 참여·유예 여부를 검증하고, 제외 조건 합집합(100개 초과 보존)·식사 기록·추천/피드백/스냅샷/유효 상태·질문 선택 상태·멱등 작업 기록·필드별 프로필 우선순위(`is_legacy_kakao`/`*_edited` 최우선, 이후 최초 생성 UUID 출처 오름차순)를 무손실 병합한 뒤 원본 UUID 사용자의 세션과 `User` 행을 물리 삭제(`R3A`)하도록 구현했다.
  - MySQL InnoDB `REPEATABLE READ`에서 잠금 대기 후 상대 트랜잭션의 커밋 결과를 즉시 관측하도록 `commit_account_transition`, `lock_user_and_verify_session`, `has_active_or_pending_room_activity` 내부 조회를 잠금 읽기(`FOR UPDATE`)로 통일했다.
  - `LOGOUT` 확정 시 카카오 계정 데이터와 타 기기 세션은 보존하고 현재 브라우저 세션만 폐기한 뒤 새 UUID 사용자와 정식 세션을 원자적으로 발급하도록 구현했다.
- **T09 (명시적·재개 가능 MySQL 마이그레이션 및 동시성/E2E 검증)**:
  - `scripts/migrate_issue39.py`(`--check`, `--apply --maintenance-confirmed`, `--verify`)를 구현해 레거시 스키마에서 T01~T08 전체 테이블·컬럼·인덱스 전환과 유효 피드백 상태 백필 및 부분 실패 후 재개·원본 데이터 지문 검증을 지원했다.

#### 주요 변경 파일

- `app/core/config.py`, `app/main.py`
- `app/auth/models.py`, `app/auth/schemas.py`, `app/auth/dependencies.py`, `app/auth/sessions.py`, `app/auth/operations.py`, `app/auth/cookie_router.py`, `app/auth/transitions.py`, `app/auth/transition_router.py`
- `app/users/models.py`, `app/users/schemas.py`, `app/users/repository.py`, `app/users/service.py`, `app/users/router.py`, `app/users/exclusion_mapping.py`, `app/users/profile_image.py`
- `app/menus/models.py`, `app/recommend/common.py`, `app/recommend/schemas.py`, `app/recommend/order_sheet.py`, `app/recommend/variety.py`, `app/recommend/personal_model.py`, `app/recommend/router.py`
- `app/rooms/schemas.py`, `app/rooms/service.py`, `app/rooms/router.py`, `app/rooms/websocket.py`, `app/rooms/cleanup.py`
- `scripts/map_legacy_exclusions.py`, `scripts/train_personal_model.py`, `scripts/migrate_issue39.py`
- `tests/test_t02_personal_data.py`, `tests/test_t03_rooms.py`, `tests/test_t04_t05_profile.py`, `tests/test_t06_t07_t08_transitions.py`, `tests/test_issue39_mysql_concurrency.py`
- `docs/USER_EXCLUSIONS_API.md`, `docs/UUID_AUTH_API.md`, `LOG.md`

#### API 변경

- 추가:
  - `GET /api/recommendations/unable-ingredients`
  - `GET /api/users/me/allergies`, `PUT /api/users/me/allergies`
  - `GET /api/users/me/unables`, `PUT /api/users/me/unables`
  - `PATCH /api/users/me/nickname`
  - `PUT /api/users/me/profile-image`, `DELETE /api/users/me/profile-image`, `GET /uploads/profile-images/{filename}`
  - `GET /api/room-operations/{operation_key}`, `POST /api/room-operations/{operation_key}/cancellation`
  - `POST /api/auth/transitions`, `POST /api/auth/transitions/{transition_id}/commit`, `GET /api/auth/transitions/{transition_id}`, `POST /api/auth/transitions/{transition_id}/recovery`
- 수정:
  - `GET /api/recommendations/random`, `GET /api/recommendations/order-sheet/questions`, `POST /api/recommendations/order-sheet`, `POST /api/recommendations/{recommendation_id}/feedback`: Bearer JWT 필수화 및 요청 본문·쿼리 `user_id` 거절, 피드백 `Idempotency-Key` 지원 (`GET /api/recommendations/random` 쿼리·최소 메뉴 응답·`no-store` 헤더 유지)
  - `POST /api/users/me/meals`, `POST /api/rooms`, `POST /api/rooms/{room_id}/join`: 선택 헤더 `Idempotency-Key` 지원
  - `POST /api/auth/tokens`: 선택 헤더 `X-Whatbob-Auth-Context` 검증 지원

#### DB 변경

- `users`: 프로필 편집/출처 컬럼 추가 (`nickname_edited`, `nickname_source_edited`, `nickname_source_uuid`, `nickname_source_id`, `nickname_source_created_at`, `profile_image_edited`, `profile_image_source_edited`, `profile_image_source_uuid`, `profile_image_source_id`, `profile_image_source_created_at`)
- `user_food_exclusions`: `catalog_code`, `catalog_version` 컬럼 및 `ix_user_food_exclusions_catalog_code` 인덱스 추가
- `recommendation_feedbacks`: `menu_key`, `menu_base_name`, `menu_main_ingredient`, `operation_key` 컬럼 및 인덱스 추가
- 신규 테이블: `request_operations`, `account_transitions`, `kakao_link_guards`, `recommendation_feedback_states`, `recommendation_item_snapshots`
- 마이그레이션 스크립트: `scripts/migrate_issue39.py`

#### 검증

- 실행 완료:
  - 기능별 및 신규 통합 테스트(SQLite): `tests/test_t02_personal_data.py`(11 passed), `tests/test_t03_rooms.py`(6 passed), `tests/test_t04_t05_profile.py`(6 passed), `tests/test_t06_t07_t08_transitions.py`(5 passed), 추천/방/인증 회귀 스위트 전체 통과.
  - 실제 MySQL 8.4.13 동시성·마이그레이션·E2E 검증: `MYSQL_TEST_DATABASE_URL` 기준 `tests/test_issue39_mysql_concurrency.py`, `tests/test_uuid_auth_mysql.py`, `tests/test_rooms_mysql.py` 실행 → `13 passed` (준비 쿠키 동시 사용자 생성, 동시 동일 닉네임 저장, 동시 제외 교체, 동시 피드백 수정, 서로 다른 2개 UUID의 동일 신규 `kakao_id` 동시 연결, 방 생성/참여와 계정 전환 동시 경합, 원본 이관과 식사 저장/토큰 갱신/닉네임 선점 4자 동시 경합, 마지막 연결 종료·재접속·만료 정리 동시 경합, 레거시 스키마 마이그레이션·부분 실패 재개·무손실 검증, T01~T08 전체 사용자 여정 E2E).
  - 전체 테스트 스위트(`.tools\venv\Scripts\python.exe -m pytest -q -rs`, `MYSQL_TEST_DATABASE_URL` 포함): 총 344개 테스트 중 342개 전부 통과 (`342 passed, 2 failed`; 실패 2건은 기존 `app/menus/display.py` 변경으로 인한 `tests/test_user_meals.py`의 `'파스타 (토마토)' != '토마토파스타'` 기존 기준선 실패로 보존됨).
- 미실행 및 사유:
  - 실제 교차 사이트 HTTPS 브라우저 쿠키 수용/차단 검증: 로컬 백엔드 전용 작업 환경이며 배포된 프론트엔드-백엔드 HTTPS 교차 도메인 브라우저 환경이 연결되어 있지 않음.
  - 실제 1GB RAM 운영 서버 환경의 부하·메모리 실측 검증: 로컬 Windows 개발 환경에서 검증했으며 운영 1GB 리눅스 인스턴스 및 운영 DB에는 접근하지 않음.

#### 남은 작업 및 주의 사항

- 스테이징/운영 배포 시 점검 모드에서 DB 백업 후 `python -m scripts.migrate_issue39 --check`, `--apply --maintenance-confirmed`, `--verify` 순서로 스키마 전환을 수행하고, 필요 시 `python -m scripts.map_legacy_exclusions --apply --maintenance-confirmed`로 레거시 제외 키워드를 매핑한다.
- 프론트엔드 통합 후 실제 HTTPS 환경에서 교차 사이트 쿠키(`SameSite=None; Secure`) 동작과 1GB 운영 인스턴스 메모리 사용량을 확인한다.

### #39 주요 아키텍처 선택과 통합 설계의 구현 인계 정리

#### 변경 이유 및 내용

- 사용자가 처리 시나리오를 이해했음을 확인한 뒤 T02~T09 주요 선택·통합 설계·검증 계획의 읽기 순서와 다음 구현 기준을 한 문서로 정리했다. 이미 선택한 R/M 항목을 재질문하지 않는다.
- 설계에서 정한 규칙과 실제 기존 값 대응·메뉴 재료 연결·학습 데이터/품질·MySQL/브라우저/1GB 검증을 구분했다. 세부 기술 초안 전체를 사용자가 개별 승인했거나 구현 완료한 것으로 기록하지 않았다.
- 다음 작업 예정일과 순서를 기록했으며 자동 실행 예약·코드 구현·운영 변경은 수행하지 않았다.

#### 주요 변경 파일

- `docs/plans/39-design-baseline.md`, `docs/plans/39-design-completion-plan.md`, `docs/plans/39-handoff-2026-11-13.md`, `LOG.md`

#### API 변경

- 없음.

#### DB 변경

- 없음.

#### 검증

- 인계 문서 로컬 링크·T02~T09 범위/선택 요약·공백 및 `git diff --check` 검사 통과. 앱·DB·브라우저·학습·1GB 부하 테스트 미실행: 설계 인계 문서만 정리했다.

#### 남은 작업 및 주의 사항

- 2026-11-14 구현 및 실제 검증. 기존 사용자 변경·T01 구현을 보존하고 기준선 테스트부터 확인한다. 설계 문서 작성 완료를 운영 안정성/추천 품질 검증으로 보고하지 않는다.

### #39 M1A·M2A·M3A·M4A 설계 선택 확정과 처리 시나리오 설명

#### 변경 이유 및 내용

- 사용자가 M1A·M2A·M3A·M4A를 선택했다. 개인 보정 최대 ±5점·90일 반감기·k=5·근접 추첨 beta=0.5, 섭취 불가 8재료군, 별도 기존 환경 주 1회 점검/수동 학습, 사진 5MB·1,200만 픽셀 이하/긴 변 512px 정지 이미지 정규화를 설계 기준으로 기록했다.
- 기존 최신 불만족 0.35 감점 교체·모델 불가 시 개인 보정 없는 기본 추천도 M1A 범위로 기록했다. 작은 평가 증거의 완화, 이력 보존, 현재 개인 입력 갱신과 모델 전체 재학습을 구분했다.
- 선택 문서·통합 설계·T02 D15·진행표·인계·ADR의 현재 상태를 갱신했다. 사용자가 구체화한 핵심의 의미를 질문해 평가 수정/누적, 추천 당시 기록, 계정 이관/응답 유실, 방 마감 시각, 테스트 계획을 사용자 사례로 설명한다.

#### 주요 변경 파일

- `docs/plans/39-final-design-choices.md`, `docs/plans/39-implementation-architecture.md`, `docs/plans/39-design-completion-plan.md`, `docs/plans/39-t02-personal-data-architecture.md`, `docs/plans/39-handoff-2026-11-13.md`, `docs/plans/39-cross-feature-architecture-checklist.md`, `docs/plans/39-validation-and-handoff.md`, `docs/adr/0003-account-transition-and-runtime-decisions.md`, `LOG.md`

#### API 변경

- 실행 API 변경 없음. 평가/사진 정책은 구현 전 설계 확정이다.

#### DB 변경

- 없음.

#### 검증

- M1A·M2A·M3A·M4A 선택 상태, 8개 문서 로컬 링크·코드 블록 짝·공백 및 `git diff --check` 검사 통과. 앱·DB·학습·사진 처리·브라우저·1GB 부하 검증 미실행: 설계 문서만 갱신했다.

#### 남은 작업 및 주의 사항

- 기술 초안의 처리 순서·실패 시나리오 설명과 최종 검토. 실제 기존 값 대응·재료 매핑·모델 품질·환경 용량은 내일 구현 후 검증한다. 선택을 구현 완료로 기록하지 않는다.

### #39 T02~T09 통합 기술 아키텍처와 T09 검증 계획 초안 작성

#### 변경 이유 및 내용

- 승인된 T02 10항목과 R1A·R2A·R3A·R4A·R5A를 바탕으로 DB 확장·API·트랜잭션/잠금·멱등 작업·추천 시점 학습 입력·모델 교체/대체·방 복구·프로필/파일 실패·계정 전환/원본 삭제의 기술 초안을 통합했다. 선택 정책은 ADR-0003에 따로 기록했다.
- 실제 코드는 평가/질문 통계/인증의 중간 commit과 인메모리 방 연결을 사용한다. 이를 구현 완료로 간주하지 않고 Service 최상위 원자성·일관된 잠금·복구 작업·고아 파일 정리의 후속 변경으로 명시했다.
- 로컬 메뉴 JSON/CSV 7,503행의 스키마·대표 재료 분류 7종·other 2,166행·빈 태그 736행을 확인했다. 전체 원재료표가 없으므로 코드 catalog와 실제 재료 매핑의 검증을 분리했다. 운영 DB는 조회하지 않았다.
- T09에 명세 대응·실제 MySQL 경쟁 11종·응답 유실·마이그레이션 재실행·브라우저·1GB 용량 검증과 내일 구현 순서를 작성했다. 새 테스트 통과나 실제 추천 품질을 주장하지 않았다.
- 초기 보정 강도/최근성·재료 범위·학습 위치·사진 픽셀/정규화의 M1~M4를 모두 미선택으로 제시한다. 기존 5MB 제한에 새 픽셀 제한을 사용자 선택 없이 추가하지 않았다. T02/공통 선택/인계/설계 진행표에 최신 링크를 연결했다.
- 기존 10% 근접 후보 균등 추첨은 작은 학습 보정만으로 실제 선택 확률이 바뀌지 않을 수 있어, M1 안에 상한이 있는 가중 추첨 전환을 명시적 추가 선택으로 포함했다. 기존 종류별 중복 방지·근접 범위·최근 노출 규칙은 보존하는 제안이며 실행·승인으로 간주하지 않았다.

#### 주요 변경 파일

- `docs/adr/0003-account-transition-and-runtime-decisions.md`
- `docs/plans/39-implementation-architecture.md`, `docs/plans/39-final-design-choices.md`, `docs/plans/39-validation-and-handoff.md`
- `docs/plans/39-design-completion-plan.md`, `docs/plans/39-t02-personal-data-architecture.md`, `docs/plans/39-handoff-2026-11-13.md`, `docs/plans/39-cross-feature-architecture-checklist.md`, `LOG.md`

#### API 변경

- 실행 API 변경 없음. 경로/DTO/오류/작업 키는 통합 기술 초안에 기록했다.

#### DB 변경

- 없음. 스키마 표·guard·상태/스냅샷·작업 이력과 마이그레이션 절차는 설계 제안이며 적용하지 않았다.

#### 검증

- 실제 코드·명세·로컬 메뉴 스키마와 Google·scikit-learn·MySQL·Pillow 공식 자료를 대조했다. 8개 문서 링크·코드 블록 짝/공백, R 5개 선택·M 4묶음/8개 미선택, 점수/가중치 예시·로컬 데이터 수와 `git diff --check` 검사 통과. 앱·DB·브라우저·모델·1GB 부하 테스트 미실행: 설계 문서만 작성했다.

#### 남은 작업 및 주의 사항

- M1~M4 선택·질문과 통합 기술 초안 검토. 실제 기존 값 대응·재료 매핑·모델 품질·MySQL/브라우저/1GB 용량 검증은 내일 구현 후 수행한다. GitHub 이슈 게시·커밋·배포·운영 DB 변경은 수행하지 않았다.

### #39 R5A 사진 URL 선택으로 공통 아키텍처 5묶음 결정 완료

#### 변경 이유 및 내용

- 사용자가 R5A를 선택해 사진 조회 URL에는 별도 인증·방 참여 검사·서명 만료를 적용하지 않는 정책을 기록했다. 본인 화면·방 참여자 표시만 제공하고 클릭·확대·공유 기능을 만들지 않는 UI 요구는 유지한다.
- 업로드·교체·삭제의 본인 인증·소유권 검사와 안전한 파일 검증·참조 보존은 그대로 유지한다. R5B·R5C는 비교 제안으로 남기고 채택하지 않았다. 주소 자체의 비노출이나 외부 접근 차단을 보장하는 것으로 기록하지 않았다.
- 최종 공통 선택 R1A·R2A·R3A·R4A·R5A를 정리했다. 정책 선택 완료와 세부 기술 계약·구현 완료를 구분한다.

#### 주요 변경 파일

- `docs/plans/39-cross-feature-architecture-checklist.md`, `docs/plans/39-design-completion-plan.md`, `LOG.md`

#### API 변경

- 실행 API 변경 없음. 사진 조회 정책만 설계 문서에 확정했다.

#### DB 변경

- 없음.

#### 검증

- R1A·R2A·R3A·R4A·R5A의 5개 선택 상태, 로컬 링크 및 `git diff --check` 통과. 앱·DB 테스트 미실행: 설계 문서만 변경했다.

#### 남은 작업 및 주의 사항

- T02~T09 데이터·API·상태 전이·실패/동시성·자원 예산과 검증 계획 구체화. 오늘 설계·내일 구현 일정을 유지한다.

### #39 단일 API 프로세스 선택과 비공개 프로필 이미지 접근 대안 보완

#### 변경 이유 및 내용

- 사용자가 R1A를 선택해 단일 API 프로세스를 설계 기준으로 기록했다. RAM 1GB·MySQL 동시 실행 제약을 유지하며 운영 설정 변경이나 처리 용량 검증은 수행하지 않았다.
- 프로필 사진 주소를 외부에 배포하는 기능을 의도하지 않는다는 설명을 반영했다. 사진은 본인·방 참여자 화면에 표시하고 클릭·확대·공유 기능을 만들지 않는다.
- 앞선 A/B 선택지가 요청마다 조회 권한을 검사하는 방식을 빠뜨린 점을 보완해 R5C를 추가 제안했다. 주소 비노출의 절대 보장과 주소만으로 접근할 수 없게 하는 인가를 구분했다. R5C는 사용자 선택 전이며 코드 구현은 하지 않았다.

#### 주요 변경 파일

- `docs/plans/39-cross-feature-architecture-checklist.md`, `docs/plans/39-design-completion-plan.md`, `LOG.md`

#### API 변경

- 없음. 보호 이미지 API는 추가 설계 제안이다.

#### DB 변경

- 없음.

#### 검증

- MDN Fetch·Blob URL 공식 자료 확인. R1A·R2A·R3A·R4A 선택과 R5C 미선택 상태, 로컬 링크 및 `git diff --check` 통과. 앱·DB 테스트 미실행: 설계 문서만 변경했다.

#### 남은 작업 및 주의 사항

- R5 접근 정책 선택과 전체 기술 계약·검증 계획 통합. 사진을 이미 수신한 사람의 저장·재배포 방지를 보장하는 설계로 표현하지 않는다.

### #39 1GB 백엔드·DB 동시 운영 제약과 프로필 사진 표시 범위 확인

#### 변경 이유 및 내용

- 사용자가 백엔드와 MySQL이 같은 1GB 서버에서 실행됨을 확인했다. 다중 API 프로세스·동일 호스트 메시지 시스템이 메모리·연결 풀 비용을 늘릴 수 있음을 설계 제약으로 기록했다. R1A를 추천하지만 사용자의 선택이나 실제 용량 검증으로 기록하지 않았다.
- 프로필 사진은 본인 화면과 방 참여자 표시만 제공하고 클릭·확대·상세 보기는 제공하지 않는 요구를 기록했다. 비클릭 이미지도 표시용 요청이 필요하므로 R5 링크 접근 정책과 구분했다. R5A/B는 아직 미선택이다.
- 모델 학습을 요청 밖에서 실행하는 것과 1GB 호스트 자원 분리는 별개임을 명시했다. 학습 실행 자원과 운영 메모리는 미측정이며 실행 코드는 변경하지 않았다.

#### 주요 변경 파일

- `docs/plans/39-cross-feature-architecture-checklist.md`, `docs/plans/39-design-completion-plan.md`, `LOG.md`

#### API 변경

- 없음.

#### DB 변경

- 없음.

#### 검증

- 저장소 실행 관련 설정·의존성과 FastAPI·MDN 공식 설명을 확인했다. 문서 제약·기존 3개 선택 유지·로컬 링크 및 `git diff --check` 검사 통과. 실제 운영 worker 수·메모리·부하 테스트는 미실행: 운영 접근 없이 설계 제약만 기록했다.

#### 남은 작업 및 주의 사항

- R1·R5 선택과 1GB 자원 예산·부하 검증 계획 구체화. 코드 구현·운영 설정 변경은 수행하지 않았다.

### #39 두 단계 계정 전환·원본 UUID 물리 삭제·탭 동기화 설계 선택

#### 변경 이유 및 내용

- 사용자가 R2A·R3A·R4A를 선택했다. 전환 전 복구용 준비 비밀값을 전달하고 확정하는 방식, 기록 이관·참조·인증 정리 후 원본 사용자 행 삭제, 탭 알림과 서버 인증 재확인을 설계 정책으로 기록했다.
- 원본 삭제 후에도 성공 작업·대상 인증의 복구 근거가 유지돼야 한다는 R2A/R3A 결합 조건을 명시했다. 세부 API·작업 수명·테이블과 실제 삭제는 구현하지 않았다.
- R1은 단일/복수 프로세스의 전달 흐름·장애 범위·전체 연결 확인·유실 복구·운영 비용을 보충했다. R5는 사진 조회 권한과 만료 링크 예시·다운로드 후 한계를 보충했다. 두 항목은 사용자 선택 대기로 유지한다.

#### 주요 변경 파일

- `docs/plans/39-cross-feature-architecture-checklist.md`, `docs/plans/39-account-transition-contract.md`, `docs/plans/39-design-completion-plan.md`, `LOG.md`

#### API 변경

- 실행 API 변경 없음. R2A·R4A의 정책 선택만 기록했다.

#### DB 변경

- 없음. R3A의 물리 삭제와 복구 참조 정책은 설계이며 아직 실행하지 않았다.

#### 검증

- 실제 연결 관리자와 FastAPI·Redis·AWS 공식 자료를 확인했다. R2A·R3A·R4A만 선택된 상태와 3개 문서의 로컬 링크 검사 및 `git diff --check` 통과. 앱·DB 테스트 미실행: 설계 문서만 변경했다.

#### 남은 작업 및 주의 사항

- R1·R5 선택 후 구체 기술 계약과 T09 검증 계획을 통합한다. 오늘 설계·내일 구현 일정을 유지한다.

### #39 T03~T08 공통 미결정 아키텍처의 일괄 검토안 작성

#### 변경 이유 및 내용

- T02 주요 선택을 반복하지 않고 남은 서버 연결 구조·전환 복구·원본 UUID 폐기·현재 웹 탭 대응·업로드 사진 조회의 5묶음을 R1~R5 체크리스트로 작성했다. 각 대안의 장점·비용과 기존 정책에 미치는 영향을 구분했다.
- 원본 사용자 비활성화, 두 단계 전환, 단일 프로세스, 탭 알림·서명 이미지 URL은 아직 미승인 제안이다. 앱 형태·운영 클라우드·테스트 통과나 구현 완료로 해석하지 않았다.
- 전환 경계 문서에 남아 있던 단일 질문 지시를 최신 체크리스트 방식으로 정정했다. 오늘 설계·내일 구현 일정을 유지하며 실행 코드는 변경하지 않았다.

#### 주요 변경 파일

- `docs/plans/39-cross-feature-architecture-checklist.md`, `docs/plans/39-account-transition-contract.md`, `docs/plans/39-design-completion-plan.md`, `LOG.md`

#### API 변경

- 실행 코드 변경 없음. API·조회 권한의 대안은 사용자 선택 전이다.

#### DB 변경

- 없음.

#### 검증

- 실제 연결 관리자·ADR·전환 문서와 공식 자료를 대조했다. `git diff --check`와 3개 문서 로컬 링크, 5묶음·10개 미선택 대안·본문 공백 검사 통과. 앱·DB 테스트 미실행: 설계 검토안만 변경했다.

#### 남은 작업 및 주의 사항

- 사용자의 선택·보류·질문에 따라 R1~R5를 정리한 뒤 T02~T09의 구체 데이터·API·상태 전이·실패/경합·검증 계획을 통합한다. 보류를 추천대로 확정하지 않는다.

### #39 T02 마지막 선택과 설계·구현 일정 정정

#### 변경 이유 및 내용

- 사용자가 9B·10A를 선택해 T02 주요 체크리스트 10항목을 마쳤다. 기존 목록 밖 값은 정확히 정식 항목과 대응한 뒤 편집하며 불명확한 원본은 보존한다. 주문서·피드백 본문 `user_id`는 제거하고 JWT 검증 결과로 본인을 확인하도록 설계한다.
- 사용자의 2시간 제한을 구현까지로 잘못 해석한 것을 정정했다. 오늘은 T02~T09 아키텍처 설계, 내일(2026-11-14)은 구현·실제 검증이라는 일정을 기록했다.
- 병렬 구현을 즉시 중단했다. 두 에이전트 모두 파일 수정·테스트 실행이 없었음을 보고했다. 루트가 추가한 User 필드 4개·의존성 2줄·`.data/` ignore 1줄만 정밀 제거했고 기존 T01·사용자 변경은 유지했다. 의존성 설치 명령은 실행 도구 경로를 찾지 못해 설치를 시작하지 않았고 DB 변경도 없었다.

#### 주요 변경 파일

- `docs/plans/39-t02-architecture-checklist.md`, `docs/plans/39-t02-personal-data-architecture.md`, `docs/plans/39-design-completion-plan.md`, `docs/plans/39-uuid-user-tickets.md`, `docs/specs/39-uuid-users-and-kakao-migration.md`, `docs/plans/39-handoff-2026-11-13.md`, `LOG.md`

#### API 변경

- 실행 코드 변경 없음. 10A의 본문 사용자 번호 제거는 설계에만 반영했다.

#### DB 변경

- 없음. 기존 제외 항목 대응·스키마 변경을 실행하지 않았다.

#### 검증

- `requirements.txt`에 순변경이 없고 추가 코드 부분만 제거했는지 확인했다. 문서 `git diff --check`와 6개 문서의 로컬 링크, 정확한 10개 선택·일정·본문 공백·T01 유지 검사 통과. 앱·DB·모델 학습 테스트 미실행: 오늘은 설계만 진행한다.

#### 남은 작업 및 주의 사항

- T02의 구체적인 데이터·학습·API 계약, T03~T08 상태 전이·실패/경합 처리, T09 검증 계획을 오늘 설계로 정리한다. 체크리스트 선택 완료를 전체 설계나 구현 완료로 안내하지 않는다.

### #39 T02 학습형 추천 체크리스트 1~8번 선택과 9·10번 보류

#### 변경 이유 및 내용

- 사용자의 명시 응답 `1A,2A,3A,4A,5B,6A,7A,8B`를 체크하고 D13에 기록했다. 오늘 점수의 제한 재정렬, 명시적 평가를 학습하는 공용 로지스틱 모델, 주기 학습과 개인 입력 갱신, 현재 백엔드 예측·별도 학습, 기존 추천 대체 경로와 최근 평가 중시를 선택했다.
- 9번 기존 목록 밖 값의 취급과 10번 요청 본문 사용자 번호 호환은 사용자가 설명을 요청했으므로 보류했다. 추천안이나 질문을 승인으로 간주하지 않았다.
- 실제 특징·계수·시간 가중·학습 주기·검증·모델 교체·트랜잭션 계약은 후속 초안으로 남겼다. 코드·DB·학습 모델·라이브러리·사용자 기록은 변경하지 않았다.

#### 주요 변경 파일

- `docs/plans/39-t02-architecture-checklist.md`, `docs/plans/39-t02-personal-data-architecture.md`, `docs/plans/39-uuid-user-tickets.md`, `docs/specs/39-uuid-users-and-kakao-migration.md`, `docs/plans/39-handoff-2026-11-13.md`, `LOG.md`

#### API 변경

- 실행 코드 변경 없음. 요청 본문 `user_id`의 처리 방식은 아직 보류다.

#### DB 변경

- 없음. 기존 목록 밖 제외 항목을 삭제·변환하지 않았다.

#### 검증

- 변경 문서 `git diff --check`와 5개 문서의 로컬 링크, 8개 선택 코드의 정확한 일치·9/10 미선택·본문 공백 검사 통과. 앱·DB·모델 학습 테스트 미실행: 설계 선택 기록만 변경했다.

#### 남은 작업 및 주의 사항

- 9·10번을 기존 데이터와 JWT 본인 확인 예시로 설명한다. 답변 후 전체 구현 계약·검증 계획·작업 분할을 정리하며 체크리스트 선택을 구현 완료로 안내하지 않는다.

### #39 T02 여러 신호 학습형 추천 방향과 체크 응답 설계 방식 합의

#### 변경 이유 및 내용

- 사용자가 여러 신호를 학습하는 추천 모델을 사용하고 실제 사용자 피드백으로 조정하는 방향을 선택해 D11로 기록했다. 기존 단순 누적 보정 외에 학습 데이터·학습·검증·예측 연결이 필요한 범위 확장을 명시했다.
- 사용자는 개별 질문이 오래 걸려 체크리스트와 trade-off를 한 번에 보고 선택·보류·질문하는 방식으로 바꾸도록 요청했다. D12와 10개 미선택 항목을 담은 체크리스트를 추가하고 기존 단일 질문 지시를 최신 문서에서 대체했다.
- 기존 인증·소유권·이력·제외 설정은 유지한다. D10의 영향 제한을 자동 폐기하거나 수동 예시 수식을 새 모델에 강제하지 않으며, 모델과의 연결을 별도 선택하도록 했다. 모든 추천안·계수·학습 주기·인프라·배포는 아직 미승인이다.
- 현재 라이브러리와 추천 코드, 공식 모델·이벤트 반영 문서를 확인했다. 실행 코드·DB·모델 파일·라이브러리·외부 서비스·원격 이슈는 변경하지 않았다.

#### 주요 변경 파일

- `docs/plans/39-t02-architecture-checklist.md`, `docs/plans/39-t02-personal-data-architecture.md`, `docs/plans/39-uuid-user-tickets.md`, `docs/specs/39-uuid-users-and-kakao-migration.md`
- `docs/plans/39-architecture-status.md`, `docs/plans/39-handoff-2026-11-13.md`, `docs/plans/39-t02-recommendation-industry-research.md`, `LOG.md`

#### API 변경

- 없음. 체크리스트의 호환 처리·학습·예측 계약은 아직 선택되지 않았다.

#### DB 변경

- 없음.

#### 검증

- 변경 문서 `git diff --check`와 7개 문서의 로컬 링크, 신규 체크리스트 10묶음·22개 미선택 옵션·본문 공백 검사 통과. 최신 진행표·인계·T02 문서에서 구 단일 질문 지시를 대체했는지 확인했다. 앱·DB·모델 학습 테스트 미실행: 설계와 검토 방식만 변경했다.

#### 남은 작업 및 주의 사항

- 체크리스트의 응답·보류를 바탕으로 구체적인 설계와 검증 계획·구현 작업 범위를 정리한다. 사용자가 건너뛴 항목을 추천대로 확정하거나 실제 피드백 없이 성능이 검증됐다고 안내하지 않는다.

### #39 T02 개인화 가중치에 관한 빅테크 공식 사례 조사

#### 변경 이유 및 내용

- 사용자의 실제 업계 방식 질문에 따라 공식 도움말·기업 연구·AWS 제품 문서를 교차 확인하고 조사 노트를 추가했다. YouTube의 화면별 주요 신호, Netflix의 최근 행동 반영, Google의 후보 생성·순위화·다중 목표와 실제 실험, Amazon Personalize 이벤트 가중치·실시간 기능의 조건을 구분했다.
- 2016·2017 공개 사례를 현행 전체 구현으로 일반화하지 않고 2026 공개 YouTube Music 실험도 참고했다. 서비스의 현행 전체 계수·모델은 확인한 자료만으로 재현할 수 없다는 한계를 명시했다.
- 기존 제안 5%·10%·20%와 기준 건수 4는 빅테크 공통 공식이나 WhatBap에서 검증된 값이 아니다. D10의 방식 승인만 유지하고 실제 강도·실험·학습 모델·신규 측정 정책을 승인으로 기록하지 않았다.

#### 주요 변경 파일

- `docs/plans/39-t02-recommendation-industry-research.md`, `docs/plans/39-t02-personal-data-architecture.md`, `LOG.md`

#### API 변경

- 없음.

#### DB 변경

- 없음.

#### 검증

- 공식 자료의 본문·발행 연도와 주장을 대조했다. 변경 문서 `git diff --check`와 조사·설계 문서의 로컬 링크·행 끝 공백·새 정책 미승인 상태 검사 통과. 앱·DB 테스트 미실행: 조사 문서만 변경했다.

#### 남은 작업 및 주의 사항

- 실제 반영 강도는 미결정이다. WhatBap의 목표·데이터·비교 검증을 바탕으로 초기 가설과 효과 검증을 구분해 설계한다. 조사 결과를 딥러닝·외부 추천 서비스·운영 실험 도입 승인으로 해석하지 않는다.

### #39 T02 누적 개인 선호의 제한된 보조 점수 방식 합의

#### 변경 이유 및 내용

- 사용자가 ②를 선택해 오늘 주문서 점수를 기본으로 하고 개인 평가의 유효 건수·만족 비율을 참고하는 보조 점수에 영향 한도를 두는 D10을 기록했다.
- 실제 반영 강도·수식은 같은 T02에서 구현 전에 설계한다. 예시의 100점·±10점·기준 건수 4와 계산식을 확정값으로 사용하지 않으며 고정 90:10 비율이나 오늘 답변의 무조건 우선을 승인한 것으로 해석하지 않았다.
- 평가가 적을 때 약하게 반영하고 강제 제외를 먼저 적용하는 방향을 명시했다. 최근 추천·다양성·공통 질문 통계·랜덤 적용 변경은 별도이며 실행 코드·DB·기존 평가를 변경하지 않았다.

#### 주요 변경 파일

- `docs/plans/39-t02-personal-data-architecture.md`, `docs/plans/39-uuid-user-tickets.md`, `docs/specs/39-uuid-users-and-kakao-migration.md`, `LOG.md`

#### API 변경

- 실행 코드 변경 없음. 주문서 추천 개인 보정 방식의 설계 승인만 기록했다.

#### DB 변경

- 없음.

#### 검증

- 변경 문서 `git diff --check`와 T02·작업 계획·명세의 로컬 링크, T02 승인 상태·행 끝 공백 검사 통과. 앱·DB 테스트 미실행: 설계 기록만 변경했다.

#### 남은 작업 및 주의 사항

- D10 실제 반영 강도를 구현 전 정한다. 후보 점수 차이·평가 건수·수정·상한·제외 조건 보존을 검증하고 같은 2번 묶음에서 적용 범위와 중복 처리 등을 정리한다.

### #39 T02 섭취 불가 선택 단위의 재료 중심 방식 합의

#### 변경 이유 및 내용

- 사용자가 섭취 불가를 개별 메뉴 대신 재료 중심으로 선택하는 D09를 승인했다. 선택한 재료를 메뉴 분류·태그·키워드와 연결해 제외하며 서버 목록 제한과 마지막 저장 우선을 유지하도록 기록했다.
- 제안한 초기 8개 재료는 실제 매핑을 점검할 초안으로 구분했다. 현재 대표 주재료가 한 값이고 태그가 추정값이라는 한계, 넓은 재료 선택이 후보를 많이 줄일 수 있다는 비용을 명시했다.
- 실제 재료 목록·매핑·필터와 기존 사용자 데이터는 변경하지 않았다. 비선호 입력 변경이나 Amazon Personalize 도입으로 해석하지 않았다.

#### 주요 변경 파일

- `docs/plans/39-t02-personal-data-architecture.md`, `docs/plans/39-uuid-user-tickets.md`, `docs/specs/39-uuid-users-and-kakao-migration.md`, `LOG.md`

#### API 변경

- 실행 코드 변경 없음. 섭취 불가 선택 단위를 재료 중심으로 설계했다.

#### DB 변경

- 없음. 기존 메뉴와 사용자 설정을 수정하지 않았다.

#### 검증

- 변경 문서 `git diff --check`와 T02·작업 계획·명세의 로컬 링크, T02 승인 상태·행 끝 공백 검사 통과. 앱·DB 테스트 미실행: 설계 기록만 변경했다.

#### 남은 작업 및 주의 사항

- 재료 목록·고정 코드 매핑과 나머지 제외 조건 계약, 기존 데이터 호환, 피드백 계산·재전송과 기록·연동 검증을 기존 5묶음 안에서 정리한다.

### #39 T02 섭취 불가 신규 등록의 서버 선택 목록 제한 합의

#### 변경 이유 및 내용

- 사용자가 ①을 선택해 섭취 불가도 서버가 제공하는 음식·재료 선택 목록에서만 신규 등록하고 자유 입력을 허용하지 않는 D08을 기록했다.
- 실제 선택 항목·범위·고정 코드 매핑은 다음 검토 대상으로 남겼다. 알레르기 목록을 그대로 쓰거나 기존 비선호 입력까지 제한하는 것으로 해석하지 않았다.
- 서버 검증과 유효하지 않은 저장의 전체 거절, D06의 마지막 저장 우선을 유지한다. 기존 목록 밖 섭취 불가 데이터는 임의 삭제·변환하지 않으며 실제 전용 선택 목록·API 구현은 아직 없다.

#### 주요 변경 파일

- `docs/plans/39-t02-personal-data-architecture.md`, `docs/plans/39-uuid-user-tickets.md`, `docs/specs/39-uuid-users-and-kakao-migration.md`, `LOG.md`

#### API 변경

- 실행 코드 변경 없음. 섭취 불가 신규 저장의 입력 범위를 서버 선택 목록으로 제한하는 설계만 기록했다.

#### DB 변경

- 없음. 기존 사용자 제외 설정과 메뉴 데이터는 변경하지 않았다.

#### 검증

- 변경 문서 `git diff --check`와 T02·작업 계획·명세의 로컬 링크, T02 승인 상태·행 끝 공백 검사 통과. 앱·DB 테스트 미실행: 설계 기록만 변경했다.

#### 남은 작업 및 주의 사항

- 섭취 불가 목록 구성, 기존 목록 밖 값의 호환과 제외 조건의 나머지 계약을 정리한다. 이번 선택을 실제 목록·API·필터 구현 완료로 안내하지 않는다.

### #39 T02 알레르기 신규 등록의 서버 선택 목록 제한 합의

#### 변경 이유 및 내용

- 사용자가 ①을 선택해 알레르기 신규 등록은 서버의 선택 목록에 있는 항목만 받고 자유 입력을 허용하지 않는 D07을 기록했다. 고정 코드와 표시 이름을 구분하고 서버에서 허용 항목을 검증해 기존 제외 조건으로 연결하도록 정리했다.
- 유효하지 않은 항목이 든 저장 요청은 일부 항목만 버린 성공으로 처리하지 않는다. D06의 마지막 저장 우선은 유지하며 기존 목록 밖 알레르기 데이터의 삭제·변환과 섭취 불가·비선호 입력 범위는 이번 승인에 포함하지 않았다.
- 코드 선택과 메뉴 재료 데이터의 완전성은 별개임을 기록했다. 코드·필터 매핑, 기존 데이터 호환과 실제 API 구현은 아직 수행하지 않았다.

#### 주요 변경 파일

- `docs/plans/39-t02-personal-data-architecture.md`, `docs/plans/39-uuid-user-tickets.md`, `docs/specs/39-uuid-users-and-kakao-migration.md`, `LOG.md`

#### API 변경

- 실행 코드 변경 없음. 알레르기 신규 저장은 서버 선택 항목만 허용하도록 설계했다.

#### DB 변경

- 없음. 기존 알레르기·메뉴 데이터를 변경하지 않았다.

#### 검증

- 변경 문서 `git diff --check`와 T02·작업 계획·명세의 로컬 링크, T02 승인 상태·행 끝 공백 검사 통과. 앱·DB 테스트 미실행: 설계 기록만 변경했다.

#### 남은 작업 및 주의 사항

- 기존 목록 밖 알레르기의 호환, 섭취 불가 입력과 나머지 제외 조건 계약을 정리한다. 선택 목록 제한을 실제 재료 식별의 정확성이나 알레르기 API 구현 완료로 안내하지 않는다.

### #39 T02 제외 조건 전체 목록의 마지막 저장 우선 합의

#### 변경 이유 및 내용

- 사용자가 목록 버전 검증 대신 마지막 저장 우선을 선택해 D06으로 기록했다. 알레르기·섭취 불가·비선호의 유형별 목록은 기존 비선호의 전체 교체 방식과 사용자 행 잠금 직렬화를 따르며 마지막 성공 저장이 최종 상태를 결정한다.
- 오래된 화면의 저장이 앞선 항목 추가·삭제를 덮어쓸 수 있는 비용을 기록했다. 자동 합집합이나 버전 불일치 거절을 적용하지 않으며 다른 유형·사용자의 목록은 유지한다.
- 실제 DB 저장 순서와 클라이언트 클릭·응답 순서를 구분하고 실패한 요청은 이전 성공 상태를 변경하지 않는다는 조건을 명시했다. 기존 비선호 구현은 재사용 대상이며 이번에는 실행 코드를 수정하지 않았다.

#### 주요 변경 파일

- `docs/plans/39-t02-personal-data-architecture.md`, `docs/plans/39-uuid-user-tickets.md`, `docs/specs/39-uuid-users-and-kakao-migration.md`, `LOG.md`

#### API 변경

- 실행 코드 변경 없음. 제외 조건 유형별 전체 목록 교체·마지막 성공 저장 우선의 설계 결정이다. 기존 비선호에 없는 버전 요구를 추가하지 않는다.

#### DB 변경

- 없음. 버전 컬럼이나 데이터 수정은 수행하지 않았다.

#### 검증

- 변경 문서 `git diff --check`와 T02·작업 계획·명세의 로컬 링크, T02 승인 상태·행 끝 공백 검사 통과. 앱·DB 테스트 미실행: 설계 기록만 변경했다.

#### 남은 작업 및 주의 사항

- 제외 조건 관리의 나머지 계약과 피드백 계산·중복 처리, 기록 저장·연동 검증을 이어간다. D06을 알레르기·섭취 불가 API 구현 완료로 안내하지 않는다.

### #39 T02 수정 평가의 최신 상태와 다른 추천 평가의 누적 단위 합의

#### 변경 이유 및 내용

- 사용자가 D05 누적 단위 제안을 승인했다. 같은 사용자·추천·메뉴의 수정 이력은 보존하되 개인 선호에는 최신 유효 상태 한 건만 기여하고, 서로 다른 추천에서 받은 유효 평가를 누적한다.
- 통신 재전송은 새 평가로 추가하지 않는 원칙을 명시했다. 작업 키·보관·동시 수정과 점수 공식·공통 통계·적용 범위는 아직 미결정이며 임의 확정하지 않았다.

#### 주요 변경 파일

- `docs/plans/39-t02-personal-data-architecture.md`, `docs/plans/39-uuid-user-tickets.md`, `docs/specs/39-uuid-users-and-kakao-migration.md`, `LOG.md`

#### API 변경

- 실행 코드 변경 없음. 개인 선호 집계 단위의 설계만 기록했다.

#### DB 변경

- 없음. 기존 피드백을 수정·삭제·재집계하지 않았다.

#### 검증

- 변경 문서 `git diff --check`와 T02·작업 계획·명세의 로컬 링크, T02 행 끝 공백·다음 결정 미확정 상태 검사 통과. 앱·DB 테스트 미실행: 설계 기록만 변경했다.

#### 남은 작업 및 주의 사항

- 제외 조건 관리 설계를 이어간다. 피드백의 남은 계산·통계·재전송 세부는 기존 2번 묶음에 유지하며 완료 처리하지 않는다. T02 실행 코드는 아직 변경하지 않았다.

### #39 T02 평가 변경·이력 보존 선택과 개인 메뉴 선호 누적 요구 반영

#### 변경 이유 및 내용

- 사용자가 평가 변경 허용·이력 보존 방향을 선택하여 D04로 기록했다. 개인 메뉴 선호를 누적 반영하겠다는 추가 요구는 D05로 기록하고 기존 T02의 알고리즘 유지 범위에 해당 예외를 명시했다.
- 실제 주문서 추천 코드는 사용자·메뉴별 최신 평가의 불만족 감점을 사용하며 다음 한 번의 추천 뒤 평가를 소비하지 않는다. 공통 질문 가중치는 이미 누적 피드백을 사용하고 랜덤 추천은 위 개인 평가 감점을 사용하지 않는다는 차이를 확인했다.
- 같은 평가의 수정은 최신 상태만 기여하고 서로 다른 추천의 평가를 누적하는 방식은 에이전트 미승인 제안으로 구분했다. 계산식·수정 집계·재전송·공통 통계·랜덤 적용은 확정하지 않았고 실행 코드·DB·기존 평가 데이터는 변경하지 않았다.

#### 주요 변경 파일

- `docs/plans/39-t02-personal-data-architecture.md`, `docs/plans/39-uuid-user-tickets.md`, `docs/specs/39-uuid-users-and-kakao-migration.md`, `LOG.md`

#### API 변경

- 없음. 평가 변경·누적 반영은 설계 중이며 세부 계약 미확정이다.

#### DB 변경

- 없음.

#### 검증

- 실제 주문서·랜덤 추천과 개인 선호 코드를 정적으로 확인했다. 변경 문서 `git diff --check`와 T02·작업 계획·명세의 로컬 링크, T02 승인 상태·행 끝 공백 검사 통과. 앱·DB 테스트 미실행: 설계 기록만 변경했다.

#### 남은 작업 및 주의 사항

- 수정 이력의 보존과 집계 기여를 구분하여 사용자와 누적 단위를 검토한다. 고정 5묶음을 유지하며 피드백 묶음 안에서 다룬다. D05의 방향 승인을 특정 알고리즘·통계 정책 승인으로 해석하지 않는다.

### #39 T02 설계 진행표와 남은 네 묶음 정리

#### 변경 이유 및 내용

- 사용자의 남은 분량 질문에 답하기 위해 기존 T02 설계를 5묶음으로 정리했다. 인증·소유권은 D01~D03 승인, 피드백 반복·중복 처리 / 제외 조건 관리 / 기록·기존 데이터 / 연동 계약·검증 계획의 4묶음은 남은 상태로 표시했다.
- 묶음 수를 구현 완료율·같은 작업량·질문 개수로 해석하지 않으며 세부 구현 필드마다 새 승인 항목을 늘리지 않도록 명시했다. 피드백 반복·중복 처리의 정책은 아직 승인되지 않았다.

#### 주요 변경 파일

- `docs/plans/39-t02-personal-data-architecture.md`, `LOG.md`

#### API 변경

- 없음.

#### DB 변경

- 없음.

#### 검증

- `git diff --check`와 T02 문서의 로컬 링크·행 끝 공백·D04 미확정 상태 검사 통과. 앱·DB 테스트 미실행: 진행표만 추가했다.

#### 남은 작업 및 주의 사항

- T02 구현은 아직 시작하지 않았다. 이번에는 피드백 변경과 재전송의 차이를 설명하고 사용자 결정을 받는다. 기존 비선호·식사·추천 구현의 존재를 T02 전체 검증 완료로 기록하지 않는다.

### #39 T02 피드백 JWT 인증·추천 소유권 검증 합의

#### 변경 이유 및 내용

- 사용자가 피드백의 JWT 인증 필수·현재 본인 소유 추천만 평가하는 제안을 승인했다. 인증된 사용자와 추천 소유자 일치, 평가 메뉴의 추천 포함 여부를 저장 전에 검사하도록 설계에 반영했다.
- 거절된 요청은 피드백·통계를 변경하지 않는다. 사용자 없는 기존 추천은 보존하되 일반 사용자 피드백을 받지 않고 추천 번호만으로 소유권을 부여하지 않는다.
- JWT 갱신 후에도 같은 사용자면 본인 추천을 평가할 수 있다. 반복 평가·수정·중복 전송·트랜잭션·오류 계약은 별도 검토하며 기존 추천 알고리즘 변경이나 T02 구현 시작으로 해석하지 않았다.

#### 주요 변경 파일

- `docs/plans/39-t02-personal-data-architecture.md`, `docs/plans/39-uuid-user-tickets.md`, `docs/specs/39-uuid-users-and-kakao-migration.md`, `LOG.md`

#### API 변경

- 실행 코드 변경 없음. 설계: `POST /api/recommendations/{recommendation_id}/feedback`에 JWT 인증·추천 소유권·메뉴 포함 검증을 필수로 적용 예정. 기존 비인증 피드백 호출·관련 테스트 변경 필요.

#### DB 변경

- 없음. 기존 추천·피드백의 삭제·귀속 변경은 수행하지 않았다.

#### 검증

- 변경 문서 `git diff --check`와 T02 문서의 승인 상태·로컬 링크·행 끝 공백 검사 통과. 앱·DB 테스트 미실행: 설계 결정 기록만 변경했다.

#### 남은 작업 및 주의 사항

- 피드백 반복·수정·중복 전송, 요청·오류 계약, 기존 비인증 질문 상태의 취급과 제외 조건·기록 저장 설계를 이어간다. 현재 실행 코드는 아직 이 결정을 반영하지 않았다.

### #39 T02 주문서 질문 조회의 JWT 필수 정책(①) 합의

#### 변경 이유 및 내용

- 사용자가 질문 단계부터 인증하는 ①을 선택했다. 주문서 질문과 메뉴 추천이 같은 사용자 기준으로 질문 선택 이력·추천 기록을 이어 쓰도록 설계에 반영했다.
- 유효한 JWT를 질문·추천 요청에 재사용하며 매번 새로 발급하지 않는다. 질문 조회는 인증 없는 `question_session` 경로로 우회하지 않도록 설계했다.
- 현재 상위 7개·탐색 2개의 질문 선택 방식은 유지한다. 모든 질문의 점수를 낮추는 방식이 아니라 탐색 단계에서 직전 주제·사용자별 누적 횟수·가중 추첨을 반영한다는 설명을 남겼다.
- 인증 준비가 질문 조회의 선행 조건이 되는 비용과 기존 비인증 호출의 변경을 기록했다. 기존 비인증 이력의 삭제·이관, 피드백 세부 정책은 승인으로 간주하지 않았다. 실행 코드·DB·원격 이슈는 변경하지 않았다.

#### 주요 변경 파일

- `docs/plans/39-t02-personal-data-architecture.md`, `docs/plans/39-uuid-user-tickets.md`, `docs/specs/39-uuid-users-and-kakao-migration.md`, `LOG.md`

#### API 변경

- 실행 코드 변경 없음. 설계: `GET /api/recommendations/order-sheet/questions`의 인증을 필수로 변경 예정. 신규 요청은 인증된 사용자로 질문 상태를 관리하고 기존 비인증 경로로 우회하지 않는다.

#### DB 변경

- 없음. 기존 비인증 질문 이력의 삭제·귀속 변경은 수행하지 않았다.

#### 검증

- 변경 문서 `git diff --check`와 T02 문서의 상태 표기·로컬 링크·행 끝 공백 검사 통과. 앱·DB 테스트 미실행: 설계 기록만 변경했다.

#### 남은 작업 및 주의 사항

- 피드백 소유권·인증 계약, 기존 비인증 이력의 취급, 제외 조건과 기록 저장 설계를 이어간다. 현재 코드의 질문 조회는 여전히 선택적 인증이며 이번 결정을 구현 완료로 안내하지 않는다.

### #39 T02 메뉴 추천 전 JWT 인증 필수 정책(B) 합의

#### 변경 이유 및 내용

- 사용자와 인증 선택·필수 방식의 차이를 검토한 뒤 사용자가 B를 선택했다. 랜덤·주문서 메뉴 추천 전에 요청의 서비스 JWT와 T01 DB 인증 세션을 검증하도록 설계를 기록했다.
- 카카오 로그인은 필수가 아니며 UUID 사용자도 동일하게 이용한다. 기존 JWT 없는 추천 호출의 변경, 인증 준비 실패 시 추천 불가라는 비용과 비인증 추천으로 자동 우회하지 않는 흐름을 명시했다.
- T02 설계부터 함께 진행한다는 요청을 유지한다. 이 선택을 나머지 정책 승인이나 구현 시작으로 해석하지 않았고 실행 코드·DB·원격 이슈는 변경하지 않았다.

#### 주요 변경 파일

- `docs/plans/39-t02-personal-data-architecture.md`, `docs/plans/39-uuid-user-tickets.md`, `docs/specs/39-uuid-users-and-kakao-migration.md`, `LOG.md`

#### API 변경

- 실행 코드 변경 없음. 설계: `POST /api/recommendations/random`, `POST /api/recommendations/order-sheet`의 선택적 인증을 필수 인증으로 변경 예정. 프론트 호출 순서·기존 테스트 갱신 필요.

#### DB 변경

- 없음.

#### 검증

- 실제 사용자·추천·피드백 코드와 기존 기록을 정적으로 대조했다. 변경 문서 `git diff --check`와 신규 T02 문서의 로컬 링크·행 끝 공백 검사 통과.
- 앱·DB 테스트 미실행: 이번 변경은 설계 결정 기록이며 실행 코드를 수정하지 않았다.

#### 남은 작업 및 주의 사항

- 질문·피드백 경로와 소유권, 제외 조건 관리·동시 저장, 기록 저장과 통합 검증을 한 번에 하나씩 검토한다. 현재 추천 API에는 선택적 인증 경로가 남아 있으므로 B가 이미 적용됐다고 안내하지 않는다.

### #39 T01 UUID 사용자 생성·복원과 세션 인증 구현 및 격리 MySQL 검증

#### 변경 이유 및 내용

- 사용자의 T01 시작 지시에 따라 준비 쿠키 발급 → UUID 사용자 생성 → 같은 인증 복원의 세 API를 구현했다. 중복 생성 요청은 인증 행 잠금으로 한 사용자에 수렴시키고, 닉네임·UUID 충돌 재시도와 사용자·인증 연결의 원자적 롤백을 적용했다.
- 복원 비밀값은 HttpOnly·Secure 쿠키로만 전달하고 서버에는 SHA-256 32바이트 해시만 저장한다. 준비 5분·정식 30일·JWT 1시간, 발급 성공 시 30일 연장, Origin·추가 헤더 검사, 상태별 오류·캐시 금지를 연결했다.
- 일반 API와 WebSocket의 최초·재인증에서 JWT의 서명·필수 클레임·사용자·DB 세션을 함께 검증한다. WebSocket 동일 인증 교체·중복 성공 확인·이전 상태 비퇴행·만료 종료와 보호 데이터 전송 검사를 추가했다.
- 기존 카카오 사용자 ID·프로필·기록은 보존하고 로그인 때 기존 프로필을 덮어쓰지 않는다. 단독 카카오 경로는 새 세션 JWT를 발급하되 복원 쿠키·기록 이관을 제공하지 않는다. 복원 쿠키를 가진 브라우저의 단독 카카오 로그인은 T06 전까지 거절해 UUID 기록 이관이 된 것처럼 처리하지 않는다.
- 기존 PK·기록을 보존하는 명시적 MySQL 사전 점검·적용·보존 검증, 중간 단계 재실행과 종료·만료 인증 총 100건 정리 명령을 추가했다. 600초 정리 실행 주체·요청 제한·운영 배포 정책은 확정하지 않았다.
- JWT_SECRET의 코드 기본값을 제거하고 최소 32자를 요구한다. 격리 Python·MySQL 도구는 Git에서 제외한 `.tools/`에 마련했으며 일반 테스트는 테스트 DB·키를 사용한다. 외부 라이브러리 요구 목록은 변경하지 않았다.
- 초기 점검에서 이전 T02 로그와 현재 코드의 차이를 재확인했다. 현재 알레르기·섭취 불가·통합 API 및 비인증 user_id 보완은 코드에 없고 관련 신규 문서·테스트 파일은 비어 있다. 이전 이력·사용자 변경을 삭제하거나 완료로 재해석하지 않았다.

#### 주요 변경 파일

- `app/auth/models.py`, `repository.py`, `sessions.py`, `errors.py`, `cookie_router.py`, `websocket_auth.py`
- `app/auth/router.py`, `service.py`, `dependencies.py`, `schemas.py`
- `app/core/config.py`, `app/core/security.py`, `app/main.py`
- `app/users/models.py`, `app/users/schemas.py`, `app/users/router.py`, `app/static/default-profile.svg`
- `app/rooms/websocket.py`
- `scripts/migrate_uuid_auth.py`, `scripts/cleanup_auth_sessions.py`
- `tests/test_uuid_auth.py`, `tests/test_uuid_websocket_auth.py`, `tests/test_uuid_auth_mysql.py`, `tests/auth_helpers.py`, `tests/conftest.py` 및 기존 인증·개인 기능·방 테스트
- `docs/UUID_AUTH_API.md`, `docs/plans/39-architecture-status.md`, T01 계약·인계·작업 계획 문서, `.gitignore`, `LOG.md`

#### API 변경

- 추가: `POST /api/auth/preparations` — 200, `CREATE_USER`/`ISSUE_TOKEN`과 선택적 이전 인증 복원 불가 안내.
- 추가: `POST /api/auth/anonymous-users` — 신규 201·같은 쿠키 복구 200. 별도 UUID나 사용자 ID를 입력 인증으로 받지 않는다.
- 추가: `POST /api/auth/tokens` — 200, 유효 정식 쿠키로 동일 사용자 JWT 발급·30일 연장. 준비 상태는 409.
- 수정: 사용자 응답에 `uuid` 추가·`kakao_id` nullable 확장, 토큰 응답에 `expires_in=3600` 추가. `GET /api/users/me` 캐시 금지.
- 수정: Bearer 인증은 새 `sid` 기반 세션 검증과 구조화한 오류 코드를 사용한다. 인식 가능한 서명 유효 구형 JWT는 `REAUTHENTICATION_REQUIRED`로 재로그인한다.
- 수정: 기존 카카오 경로는 유지하되 기존 프로필 보존·세션 JWT를 적용하고 복원 쿠키 존재 시 409 `ACCOUNT_LINKING_NOT_AVAILABLE`로 거절한다. 단독 로그인에는 쿠키 복원 기능을 추가하지 않았다.
- 수정: `/ws/rooms/{room_id}`에 Origin 필수 검사·DB 세션 검증, `auth.refresh`/`auth.refreshed`/`auth.error`와 JWT 만료 시 4408 종료를 추가했다. 프론트 갱신·재연결 시간 한도 구현은 별도다.
- 추가: 기본 이미지 `GET /static/default-profile.svg`. CORS 허용 헤더에 `X-Whatbob-Request` 추가.

#### DB 변경

- 모델·마이그레이션 `scripts/migrate_uuid_auth.py`: 기존 정수 `users.id`·FK·프로필·기록 보존. `users.uuid` CHAR(36) NOT NULL·유일 인덱스, `nickname_key` VARBINARY(400) nullable·유일 인덱스, `is_legacy_kakao` BOOLEAN 추가, `kakao_id` NULL 허용.
- `auth_sessions`: BIGINT PK, nullable INTEGER 사용자 FK `RESTRICT`, 유일 BINARY(32) 해시, UTC DATETIME(6) 생성·만료·종료 시각과 사용자·만료/ID·종료/ID 인덱스 추가.
- 격리 MySQL에서만 적용·보존·재실행을 검증했다. 운영 DB 변경·실제 데이터 사전 점검은 미실행이다. MySQL DDL 전체 롤백을 보장하지 않으며 별도 백업·쓰기 차단이 필요하다.

#### 검증

- 수정 전 기준선: `.tools/venv/Scripts/python.exe -m pytest -q` — **225 passed, 1 skipped, 2 failed**. 두 실패는 기존 `tests/test_user_meals.py`의 `파스타 (토마토)`/`토마토파스타` 표시명 차이였다.
- 실제 Python 3.11.15, requirements.txt 환경과 격리 MySQL 8.4.13에서 `.tools/venv/Scripts/python.exe -m pytest -q tests/test_uuid_auth.py tests/test_uuid_websocket_auth.py tests/test_uuid_auth_mysql.py tests/test_rooms_mysql.py --tb=short` — **80 passed**.
- HTTP 쿠키 상태·CSRF·생성 응답 유실 복구·사용자 격리·만료·종료·DB 장애·저장 실패 롤백·닉네임 충돌·정리 100건 상한, WebSocket 재인증·중복·구형 응답·인증 불일치·만료 종료를 확인했다.
- 실제 MySQL에서 기존 데이터·FK·시간 보존, check/apply/verify CLI·점검 확인 강제, 중간 UUID 채움·누락 인증 인덱스 재실행, 닉네임 충돌·잘못된 스키마 중단, 동시 사용자 생성·갱신/종료·정리/연장 경합을 검증했다.
- 전체 회귀 `.tools/venv/Scripts/python.exe -m pytest -q --tb=short` (`MYSQL_TEST_DATABASE_URL`은 위 격리 MySQL) — **305 passed, 2 failed**. 실패 두 건은 기준선과 같으며 새 실패는 없었다.
- `python -m compileall -q app scripts tests`, `git diff --check`, 변경 문서 로컬 링크 39개·신규 파일 공백·기본 SVG XML 검사 통과. 짧은 JWT 키 거부 시 설정 오류에 입력 비밀값이 노출되지 않는 것도 확인했다.
- 검증용 MySQL은 정상 종료했다. 다운로드·임시 데이터는 Git 제외 `.tools/` 아래에만 남겼으며 Windows 서비스나 자동 실행 작업은 등록하지 않았다.
- 미실행: 프론트 실제 구현, 서로 다른 사이트의 HTTPS 브라우저 쿠키 수용·차단, 운영 DB·배포 환경 검증.

#### 남은 작업 및 주의 사항

- T01은 백엔드 구현·격리 검증 상태다. 실제 프론트 연동·브라우저 검증과 미결정 여러 탭·전환·운영·배포 설계는 남아 있다. 사용자 지시를 새 플랫폼·탭 정책 승인으로 기록하지 않는다.
- 배포 전 환경의 JWT_SECRET·발급자·대상, 기존 사용자 재로그인·프론트 오류 처리, 명시적 DB 전환·백업·복구를 맞춘다. 현재 구현을 운영에 자동 배포하지 않았다.
- 기존 메뉴 표시명 테스트 2건과 T02 로그·코드 차이를 후속 작업에서 확인한다. T03의 유예 후 일반 참여자 자동 퇴장, T06~T08 이관·계정 전환·카카오 쿠키 복원은 이번 작업의 완료 범위가 아니다.

### #39 아키텍처 결정 권한과 미승인 제안 표시 정정

#### 변경 이유 및 내용

- 사용자가 아키텍처를 직접 선택해야 한다고 지적하여 앱 형태 미정 답변을 진행 방향의 승인으로 해석한 설명을 정정했다. 공통 서버 우선·플랫폼 선택 유보·클라이언트별 보관 분리는 사용자 미승인 제안으로 표시했다.
- 기존 동일 인증 복원 계약과 새 탭·추가 실행 환경의 사용자 정책을 구분했다. 후자는 미결정이며 에이전트가 특정 방향으로 바꾸지 않겠다고 한 설명을 사용자 선택으로 기록하지 않는다.
- 선택지·trade-off·현업 근거를 설명하고 한 번에 하나의 결정만 받으며 채택·변경·유보를 임의로 확정하지 않도록 명시했다. 기존 승인 정책과 진행표의 남은 3묶음은 유지한다.

#### 주요 변경 파일

- `docs/plans/39-account-transition-contract.md`
- `docs/plans/39-architecture-status.md`
- `docs/plans/39-handoff-2026-11-13.md`
- `LOG.md`

#### API 변경

- 없음.

#### DB 변경

- 없음.

#### 검증

- 설계 문서 3개의 공백·로컬 링크 19개 검사와 `git diff --check` 통과.
- 앱·DB·브라우저 테스트 미실행: 결정 권한·제안 상태를 정정했으며 이번 작업에서는 실행 코드를 변경하지 않았다.

#### 남은 작업 및 주의 사항

- 새 탭·추가 실행 환경의 사용자 정책을 선택지와 영향으로 비교해 사용자 결정을 받는다. 기존 병행 작업과 이력은 보존한다.

### #39 앱 형태 미정과 웹 탭 검토의 적용 범위 명시

#### 변경 이유 및 내용

- 사용자가 앱 형태를 아직 정하지 않았다고 재확인하여 현재 웹 쿠키·여러 탭 검토를 모든 앱의 필수 구조로 해석하지 않도록 명시했다. 서버의 공통 사용자·인증 규칙과 향후 클라이언트별 보관·전달을 구분했다.
- 실행·화면 추가 자체로 새 사용자를 만들거나 모든 탭의 사용자 일치를 보장하는 정책은 채택하지 않았다. 기존 동일 인증 복원·기록 보존·방 참여 중 전환 금지는 유지했다.
- 설명은 선택지·trade-off·현업 근거를 포함하되 결정 질문은 한 번에 하나만 제시하도록 최신 사용자 요구를 기록했다. 탭 전환 대응과 두 단계 전환은 미승인 검토 상태이며 남은 설계는 기존 3묶음을 유지한다.

#### 주요 변경 파일

- `docs/plans/39-account-transition-contract.md`
- `docs/plans/39-architecture-status.md`
- `docs/plans/39-handoff-2026-11-13.md`
- `LOG.md`

#### API 변경

- 없음. 앱 전용 인증 전달 API를 추가하거나 승인하지 않았다.

#### DB 변경

- 없음.

#### 검증

- 설계 문서 3개의 공백·로컬 링크 19개 검사와 `git diff --check` 통과.
- 앱·DB·브라우저 테스트 미실행: 적용 범위·검토 방식만 문서화했으며 이번 작업에서는 실행 코드를 변경하지 않았다.

#### 남은 작업 및 주의 사항

- 여러 탭·계정 전환의 미승인 정책을 하나씩 검토하고 운영·배포 복구 설계를 이어간다. 앱별 보관·전달은 앱 형태 확정 후 구체화하며 새 T01 선행 묶음을 추가하지 않는다. 병행 작업의 T02 구현 변경과 기록은 유지한다.

### #39 T02 음식 제외 조건(알레르기·섭취 불가·통합 조회) API 추가 및 개인 추천 소유자 검증 보완

#### 변경 이유 및 내용

- T01(UUID 사용자 생성·인증 세션) 구현 전 선행 가능한 T02 범위를 먼저 구현했다. 기존에는 `user_food_exclusions` 테이블과 추천 제외 로직이 `ALLERGY`, `UNABLE`, `DISLIKE` 3종류를 모두 처리하지만 REST API는 `/api/users/me/dislikes`만 존재했다.
- `app/users/`의 비선호 조회·교체·삭제 로직을 제외 유형(`ALLERGY`, `UNABLE`, `DISLIKE`)별 공통 헬퍼로 통합해 중복을 제거하고, 알레르기(`/api/users/me/allergies`), 섭취 불가(`/api/users/me/unables`), 전체 제외 요약 조회(`/api/users/me/exclusions`) API를 추가했다.
- `POST /api/recommendations/order-sheet`와 `POST /api/recommendations/{recommendation_id}/feedback`에서 비인증 요청이 요청 본문 `user_id`를 전달해 타인의 제외 조건·이력을 조회하거나 저장할 수 있던 허점(`_bind_user`)을 차단(`401`)하고, 사용자 소유 추천(`rec.user_id != null`)에 비인증 호출자가 피드백을 남기지 못하도록 소유자 검증을 보완했다.

#### 주요 변경 파일

- `app/users/schemas.py`
- `app/users/repository.py`
- `app/users/service.py`
- `app/users/router.py`
- `app/recommend/router.py`
- `app/recommend/order_sheet.py`
- `docs/USER_EXCLUSIONS_API.md`
- `docs/USER_DISLIKES_API.md`
- `tests/test_user_exclusions_and_t02.py`
- `LOG.md`

#### API 변경

- 추가: `GET /api/users/me/exclusions` (인증 사용자의 `allergies`, `unables`, `dislikes` 전체 요약 조회, `Cache-Control: no-store`)
- 추가: `GET /api/users/me/allergies`, `PUT /api/users/me/allergies`, `DELETE /api/users/me/allergies/{exclusion_id}` (`ALLERGY` 전용, `DISLIKE`와 동일한 입력·중복 제거·ID 유지·404 격리 규칙 적용)
- 추가: `GET /api/users/me/unables`, `PUT /api/users/me/unables`, `DELETE /api/users/me/unables/{exclusion_id}` (`UNABLE` 전용, `DISLIKE`와 동일한 규칙 적용)
- 수정: `POST /api/recommendations/order-sheet` 및 `POST /api/recommendations/{recommendation_id}/feedback`에서 비인증 HTTP 요청의 `user_id` 지정 시 `401 Unauthorized`, 사용자 소유 추천에 대한 비인증 피드백 시 `400` 거절

#### DB 변경

- 없음 (기존 `user_food_exclusions`, `user_meals`, `recommendations`, `recommendation_feedbacks`, `question_selection_states` 스키마 유지).

#### 검증

- `.venv` 가상환경(`Python 3.11.9`, `requirements.txt` 설치)에서 `KAKAO_REST_API_KEY=test-kakao-key` 환경으로 인메모리 SQLite 테스트 실행:
  - T02·비선호·추천 관련 묶음(`tests/test_user_exclusions_and_t02.py`, `test_user_dislikes.py`, `test_recommend.py`, `test_recommend_variety.py`, `test_random_pick.py`, `test_question_selection.py`, `test_binary_questions.py`, `test_order_sheet_contract.py`): **139 passed**
  - 전체 테스트(`pytest -q`): **274 passed, 1 skipped(MySQL 전용), 2 failed** — 2건 실패는 작업 전 기준선부터 존재하던 `tests/test_user_meals.py`의 `파스타 (토마토)` vs `토마토파스타` 표시명 차이 때문이며 본 작업과 무관하다.
- `git diff --check -- app/ tests/ docs/USER_DISLIKES_API.md docs/USER_EXCLUSIONS_API.md` 통과.
- 미실행: T01 기반 실제 UUID 생성·재방문 쿠키 복원 E2E 통합 테스트(T01 미구현), 실제 MySQL 환경 검증.

#### 남은 작업 및 주의 사항

- T01(`users.uuid`, `kakao_id` nullable, `auth_sessions`, 익명 준비/생성/토큰 API, `sid` 기반 JWT 검증) 구현이 완료되면 실제 UUID 생성·재방문 복원 경계를 통과하는 T02 E2E 검증을 연결해 T02를 최종 완료 처리한다.
- 기존 `tests/test_user_meals.py`의 2건 실패는 별도 확인 대상이며 이번 작업에서 임의 수정하지 않았다.

### #39 여러 탭·계정 전환과 응답 유실 복구의 묶음 검토안 작성

#### 변경 이유 및 내용

- 자세한 설명과 진행 요청에 따라 탭별 메모리 JWT와 공유 쿠키의 불일치, 전환 중 요청·늦은 응답, 방 참여·전환 서버 경계와 응답 유실 복구를 한 문서로 정리했다.
- BroadcastChannel 힌트와 서버 재검증·응답 세대, 기대 인증 문맥, 사용자 잠금, 같은 작업 키와 참여 요청 종결, 별도 보호 준비 쿠키 후 계정 전환 확정을 제시했다. 최초 UUID 다중 생성 허용·방 참여 중 전환 금지·기록 보존은 유지한다.
- 보호 요청 보류·사용자 확인과 두 단계 전환은 검토할 사용자 경험임을 명시했다. 새 헤더·전환 및 작업 확인 인터페이스는 사용자 최종 승인이나 구현 완료로 기록하지 않았다. 후속 표·API 구현은 T03/T06~T08 범위를 유지한다.

#### 주요 변경 파일

- `docs/plans/39-account-transition-contract.md`
- `docs/plans/39-architecture-status.md`
- `docs/plans/39-handoff-2026-11-13.md`
- `LOG.md`

#### API 변경

- 실행 API 변경 없음. 기대 인증 문맥 헤더·`AUTH_CONTEXT_CHANGED`와 작업 키·결과 조회·취소·전환 준비/확정의 인터페이스는 검토안이다.

#### DB 변경

- 실제 변경 없음. 참여 작업·전환 전용 준비와 결과 보관은 후속 기능 설계안이며 마이그레이션 파일을 추가하지 않았다.

#### 검증

- 기존 방 API·인증 서비스·ADR·HTTP/DB 계약을 읽고 대조했다. 문서 로컬 링크·공백·JSON 구문과 `git diff --check` 검사 통과.
- 앱·DB·브라우저 테스트 미실행: 검토안 작성이며 실제 쿠키·탭·잠금·복구의 동작은 확인하지 않았다.

#### 남은 작업 및 주의 사항

- 진행표의 3번 검토 후 운영 설정·배포 복구를 정리한다. 복구 기록 보관과 인증 증명은 구현 시 함께 검증하며 작업 번호만으로 인증하지 않는다. 코드·DB 변경·커밋·푸시·배포는 수행하지 않았다.

### #39 DB 구조·기존 사용자 보존 전환 계약 정리 완료

#### 변경 이유 및 내용

- 실제 사용자·메뉴 기록·방 Entity와 기존 마이그레이션을 확인하고, 기존 정수 PK·FK·프로필·기록을 유지하는 UUID·별도 인증 표 구조와 전환 단계를 기술 설계로 정리했다.
- 문자형 UUIDv4, NULL 허용 카카오 ID, 닉네임 중복 키·기존 카카오 구분, `auth_sessions` 자료형·FK RESTRICT·해시와 만료 인덱스·UTC 시간을 명시했다. 준비 생성 원자성·중복 생성·갱신과 종료 경합·정리 순서를 구체화했다.
- check/apply/verify는 구현 예정 명령임을 명시하고 사전 점검·단계 재실행·기존 데이터 보존·실제 충돌 시 중단 기준을 정리했다. 기존 닉네임 자동 수정·사용자 삭제는 승인하지 않았다.
- 고정 진행표에서 DB를 설계 정리 완료·구현 전으로 표시해 남은 설계를 3묶음으로 줄였다. 다음은 여러 탭·계정 전환이다.

#### 주요 변경 파일

- `docs/plans/39-t01-database-contract.md`
- `docs/plans/39-architecture-status.md`
- `docs/plans/39-t01-auth-architecture.md`
- `docs/plans/39-handoff-2026-11-13.md`
- `LOG.md`

#### API 변경

- 실행 API 변경 없음. 구현 시 사용자 DTO에 UUID를 추가하고 `kakao_id`를 nullable로 확장할 연결 지점을 명시했다.

#### DB 변경

- 실제 변경 없음. `users.uuid`·`nickname_key`·`is_legacy_kakao` 추가, `kakao_id` NULL 허용, `auth_sessions`와 인덱스·FK 추가를 설계했다. 마이그레이션 파일은 아직 작성하지 않았다.

#### 검증

- 실제 Entity·FK·프로필 DTO·마이그레이션 선례를 읽고 대조했다. 설계 문서 로컬 링크·공백·JSON 예시 구문과 `git diff --check` 검사 통과.
- MySQL·앱·브라우저 테스트 미실행: 실제 DB 버전·데이터 충돌·DDL·잠금·성능은 확인하지 않았고 운영 DB에 접속하지 않았다.

#### 남은 작업 및 주의 사항

- 고정 진행표의 여러 탭·전환, 운영 설정, 배포·복구 3묶음 정리 후 T01 구현·격리 검증을 진행한다. 충돌 데이터는 실제 점검 시 별도 확인하며 임의 보정하지 않는다. 코드 변경·커밋·푸시·배포는 수행하지 않았다.

### #39 인증 통신 계약을 묶음으로 마무리하고 고정 설계 진행표 추가

#### 변경 이유 및 내용

- 사용자 요청에 따라 세부 항목마다 승인 질문을 반복하는 대신 기존 합의 안의 기술 세부를 한 번에 정리했다. 같은 화면의 HTTP·WebSocket 갱신은 하나의 토큰 발급을 공유하고 연결별 성공 확인을 분리하며, 늦은 응답과 이전 JWT가 최신 인증을 덮어쓰지 않게 계약을 구체화했다.
- 요청 번호·날짜 형식, `auth.error`·오류 코드·연결 유지와 종료, Origin·인증 전 데이터·만료 타이머 경계를 명시했다. 사용자 승인된 정책과 에이전트가 구체화한 구현 세부를 구분했다.
- 고정된 5묶음 진행표를 만들고 인증 통신을 설계 정리 완료·구현 전으로 표시했다. 남은 4묶음은 DB·여러 탭과 전환·설정 운영·배포 복구이며 다음은 DB다. 과거 미정 요약을 최신 계약과 맞췄다.

#### 주요 변경 파일

- `docs/plans/39-t01-websocket-auth-contract.md`
- `docs/plans/39-architecture-status.md`
- `docs/plans/39-t01-auth-architecture.md`
- `docs/plans/39-handoff-2026-11-13.md`
- `LOG.md`

#### API 변경

- 실행 API 변경 없음. 설계상 `auth.error`, 요청 번호·UTC 만료 형식, 중복·동시 갱신과 오류별 WebSocket 종료 처리를 구체화했다. 기존 HTTP 쿠키 상태·오류·사용자 데이터 보존 정책은 유지한다.

#### DB 변경

- 없음.

#### 검증

- 기존 WebSocket 종료 코드와 HTTP 오류 계약을 대조했다. 문서 로컬 링크·공백과 JSON 예시 구문 검사, `git diff --check` 통과.
- 애플리케이션·DB·브라우저 테스트 미실행: 설계 계약만 정리했으며 동작 검증 결과로 보고하지 않는다.

#### 남은 작업 및 주의 사항

- 고정 진행표의 4묶음을 정리한 후 T01 구현·격리 테스트로 이동한다. 다른 탭 전환과 예외적 운영 폐기는 담당 묶음에서 다룬다. 실행 코드 변경·운영 DB 접속·배포·커밋·푸시는 수행하지 않았다.

### #39 방 재접속 유예를 60초로 변경하고 자동 재연결을 3회·10초로 제한

#### 변경 이유 및 내용

- 사용자 결정에 따라 최신 ADR·명세·작업 계획·설계·인계의 방 재접속 유예를 30초에서 60초로 변경했다. 유예 중 계정 전환 금지와 만료 후 일반 참여자 자동 퇴장·방장 종료 정책은 유지한다. 과거 LOG 기록은 보존했다.
- 자동 재연결은 최초 포함 최대 3회·각 시도 최대 3초·전체 최대 10초와 기존 지터를 적용하기로 확정했다. 연결 열기부터 최초 인증·준비 완료 확인까지 한 시도이며, 성공 후 방 상태를 재조회한다. 실패하면 자동 반복을 중단하고 수동 재시도를 제공한다.
- 재연결로 기존 유예를 초기화하거나 갱신 단계부터 무한 반복하지 않도록 명시했다. 복구 불가 오류는 즉시 중단한다.

#### 주요 변경 파일

- `docs/UUID_USER_DESIGN.md`
- `docs/adr/0002-room-reconnect-and-account-switching.md`
- `docs/specs/39-uuid-users-and-kakao-migration.md`
- `docs/plans/39-uuid-user-tickets.md`
- `docs/plans/39-t01-auth-architecture.md`
- `docs/plans/39-t01-websocket-auth-contract.md`
- `docs/plans/39-handoff-2026-11-13.md`
- `LOG.md`

#### API 변경

- 실행 API 변경 없음. 설계상 재접속 유예를 60초로 바꾸고 자동 재연결 횟수·시간 한도 및 실패 후 처리를 확정했다.

#### DB 변경

- 없음.

#### 검증

- 관련 문서의 유예 시간·재연결 한도를 대조하고 `git diff --check` 및 신규 계약 파일의 줄 끝 공백 검사 통과.
- 현재 `app/core/config.py`에서 방장·일반 참여자 기본 유예가 모두 60초임을 확인했다. 일반 참여자는 유예 후 오프라인으로 유지하는 기존 코드이므로 새 자동 퇴장 정책은 미구현이다.
- 애플리케이션·DB·브라우저 테스트 미실행: 설계 문서만 변경했고 실행 코드·설정은 수정하지 않았다.

#### 남은 작업 및 주의 사항

- 오류·종료 코드·필드 형식·갱신 경합과 실패한 임시 연결의 유예 비연장 처리를 구체화하고 구현한다. 배포·커밋·푸시는 수행하지 않았다.

### #39 T01 토큰 발급과 WebSocket 재인증에 공유 10초 한도 적용

#### 변경 이유 및 내용

- 사용자 승인에 따라 토큰 API 최초 요청부터 WebSocket 성공 확인까지 전체 최대 10초를 공유하도록 설계했다. 각 단계는 최초 포함 최대 3회이며 각 대기는 최대 3초와 전체 남은 시간 중 작은 값을 적용한다.
- WebSocket 재전송에 같은 JWT·요청 번호와 1초·2초 기준 ±50% 지터를 사용하며 대기 중 성공하면 재전송을 취소한다. 시간이 먼저 소진되면 남은 시도를 하지 않고 WebSocket 재전송에서 토큰 API를 반복 호출하지 않는다.
- 기존 실패 후 재연결 원칙은 유지하되 예산 초기화를 통한 무한 갱신 반복을 금지했다. 재연결 자체의 횟수·시간 한도와 오류 계약은 미확정으로 구분했다.

#### 주요 변경 파일

- `docs/plans/39-t01-websocket-auth-contract.md`
- `docs/plans/39-t01-auth-architecture.md`
- `docs/plans/39-handoff-2026-11-13.md`
- `LOG.md`

#### API 변경

- 실행 API 변경 없음. WebSocket 갱신의 확인 대기·재전송·HTTP 토큰 발급과의 공유 시간 한도를 설계상 확정했다.

#### DB 변경

- 없음.

#### 검증

- 문서의 단계별 횟수·응답 대기·공유 한도와 미확정 항목을 대조했다. `git diff --check` 및 신규 계약 파일의 줄 끝 공백 검사 통과.
- 애플리케이션·DB·브라우저 테스트 미실행: 실행 코드 변경 없는 설계 문서 작업이다.

#### 남은 작업 및 주의 사항

- 재연결 한도·오류와 종료 코드·필드 형식·서로 다른 갱신의 경합을 구체화한다. 구현·배포·커밋·푸시는 수행하지 않았다.

### #39 T01 WebSocket 재인증 성공 확인과 중복 재전송·재연결 계약 확정

#### 변경 이유 및 내용

- 사용자 승인에 따라 `auth.refresh`와 `auth.refreshed`를 요청 번호로 연결하고 서버의 인증 교체 완료 후 성공을 확인하는 계약을 추가했다.
- 확인 유실 시 같은 JWT·요청 번호의 제한적 재전송, 현재 인증 검증 후 중복 성공 확인, 오래된 요청·응답의 상태 비퇴행을 명시했다. 끝내 확인하지 못하면 연결을 정리하고 유효 JWT로 재연결한 뒤 방 상태를 재조회한다.
- 횟수·대기·전체 시간 예산과 오류·종료 코드는 미확정으로 분리했다. 기존 2분 전 갱신·만료 종료·방 참여 중 전환 금지 정책은 유지한다.

#### 주요 변경 파일

- `docs/plans/39-t01-websocket-auth-contract.md`
- `docs/plans/39-t01-auth-architecture.md`
- `docs/plans/39-handoff-2026-11-13.md`
- `LOG.md`

#### API 변경

- 실행 API 변경 없음. WebSocket 재인증 요청 `auth.refresh`의 `request_id`·`access_token`, 성공 응답 `auth.refreshed`의 `request_id`·`expires_at` 및 복구 동작을 설계상 확정했다.

#### DB 변경

- 없음.

#### 검증

- 관련 문서의 승인 상태·메시지·중복 및 만료 처리와 링크를 대조하고 `git diff --check` 통과.
- 애플리케이션·DB·브라우저 테스트 미실행: 실행 코드 변경 없는 설계 문서 작업이다.

#### 남은 작업 및 주의 사항

- 확인 시간·재전송 예산·오류와 종료 코드·세부 필드 형식·다른 갱신과의 경합을 구체화한다. 구현·배포·커밋·푸시는 수행하지 않았다.

### #39 방 참여 요청 시작부터 계정 전환을 차단하도록 설계 명확화

#### 변경 이유 및 내용

- 사용자 확인에 따라 방 참여 완료 후뿐 아니라 요청 시작·응답 대기부터 로그인·로그아웃·계정 전환을 차단하도록 ADR·명세·아키텍처·인계를 맞췄다.
- 참여 성공 시 기존 참여·30초 유예 제한을 유지하고, 실패 확정 시 요청 중 차단을 해제한다. 응답 유실·시간 초과 시 서버의 참여 여부와 처리 결과를 확인하며 지연 성공 가능성을 남긴 단순 미참여 조회만으로 해제하지 않는다.
- 프론트 차단과 서버 검증·기존 직렬화 요구를 유지했다. 방 이용 중 전환을 허용하는 흐름이나 모든 WebSocket 전송 전 DB 인증 조회는 채택하지 않았다.

#### 주요 변경 파일

- `docs/adr/0002-room-reconnect-and-account-switching.md`
- `docs/specs/39-uuid-users-and-kakao-migration.md`
- `docs/plans/39-t01-auth-architecture.md`
- `docs/plans/39-handoff-2026-11-13.md`
- `LOG.md`

#### API 변경

- 실행 API 변경 없음. 요청 대기 중 계정 전환 제한과 결과 불명 시 복구 원칙의 설계 보충이다. 확인 경로·추적 방식·오류 코드는 미확정이다.

#### DB 변경

- 없음.

#### 검증

- 관련 문서의 차단 시작·해제 조건과 응답 유실 처리를 대조하고 `git diff --check` 통과.
- 애플리케이션·DB·브라우저 테스트 미실행: 문서만 변경했다.

#### 남은 작업 및 주의 사항

- 재인증 메시지·응답 계약, 참여 결과 확인 경로 및 서버 직렬화의 세부 구현을 설계한다. 코드 구현·배포는 수행하지 않았다.

### #39 T01 활성 WebSocket의 JWT 갱신을 만료 2분 전에 시작

#### 변경 이유 및 내용

- 사용자 선택에 따라 활성 WebSocket이 있는 브라우저가 JWT 만료 120초 전에 갱신을 시작하도록 설계를 확정했다. 1시간 JWT 수명을 유지하며 새 JWT 만료 시각으로 다음 예약을 계산한다.
- HTTP만 이용할 때의 만료 후 갱신과 적용 범위를 명확히 하고, 브라우저 지연·갱신 실패 시 서버의 만료 종료 및 2분 여유의 trade-off를 기록했다.

#### 주요 변경 파일

- `docs/plans/39-t01-auth-architecture.md`
- `docs/plans/39-handoff-2026-11-13.md`
- `LOG.md`

#### API 변경

- 실행 API 변경 없음. WebSocket 사전 갱신 시점의 설계 확정이다.

#### DB 변경

- 없음.

#### 검증

- 문서의 120초 정책과 HTTP·WebSocket 적용 범위 대조 및 `git diff --check` 통과.
- 애플리케이션·DB·브라우저 테스트 미실행: 문서만 변경했다.

#### 남은 작업 및 주의 사항

- WebSocket 인증 종료 반영·재인증 메시지·경합 처리, DB 상세·계정 전환 동시성·운영 설정과 배포 절차를 설계한다. 코드 구현은 아직 하지 않았다.

### #39 T01 WebSocket 연결 유지 중 JWT 재인증 방식 선택

#### 변경 이유 및 내용

- 사용자 선택에 따라 JWT 만료 전 새 JWT를 발급받고 현재 WebSocket에서 재인증하는 2번 방식을 채택했다. HTTP의 만료 응답 후 갱신 원칙과 활성 WebSocket의 사전 갱신 흐름을 구분했다.
- 서버 검증과 성공 확인 후 같은 사용자·인증 기록의 연결 인증을 교체하며, 기존 JWT 만료까지 완료하지 못하면 종료하는 흐름을 기록했다. 연결 공백을 줄이는 이점과 타이머·재인증·경합 처리 비용, 공식 제품 사례 및 미확정 상세를 명시했다.
- 인계 문서의 미승인 상태와 이전 추천안을 최신 결정으로 갱신했다. 이후 설명은 쉬운 개념·단계별 과정·trade-off·근거 있는 현업 사례·프로젝트 추천을 포함한다.

#### 주요 변경 파일

- `docs/plans/39-t01-auth-architecture.md`
- `docs/plans/39-handoff-2026-11-13.md`
- `LOG.md`

#### API 변경

- 실행 API 변경 없음. WebSocket 재인증 방향만 설계상 선택했으며 메시지·응답·오류·종료 코드는 미확정이다.

#### DB 변경

- 없음.

#### 검증

- 설계·인계 문서의 선택 상태와 HTTP 갱신 원칙의 적용 범위를 대조했다. `git diff --check` 통과.
- 애플리케이션·DB·브라우저 테스트 미실행: 실행 코드 변경 없는 설계 문서 작업이다.

#### 남은 작업 및 주의 사항

- 갱신 시점·주체, 메시지 상세, 동시 갱신·응답 유실·순서 역전, 인증 종료의 기존 연결 반영을 설계한다. 구현·운영 적용은 수행하지 않았다.

### #39 다른 컴퓨터에서 이어가기 위한 설계 인계 문서 정리

#### 변경 이유 및 내용

- 사용자 요청에 따라 지금까지 합의한 인증·쿠키·DB 변경·배포 방향과 남은 항목을 저장소 인계 문서로 정리했다. TEMP 문서에 의존하지 않고 저장소 파일로 작업을 이어가도록 구성했다.
- 마지막 WebSocket JWT 만료 처리 선택지는 아직 미승인임을 명시했다. 실행 코드·DB·운영 배포는 미변경 상태를 유지한다.
- 관련 없는 로컬 식사 기록 테스트 수정과 설치 스킬 파일은 설계 인계 커밋에서 제외하고 작업 상태를 보존한다. 인계 브랜치는 `docs/39-auth-architecture`를 사용한다.

#### 주요 변경 파일

- `docs/plans/39-handoff-2026-11-13.md`
- `LOG.md`

#### API 변경

- 없음.

#### DB 변경

- 없음.

#### 검증

- 애플리케이션·DB 테스트 미실행: 설계 문서 인계 작업이다. 문서 및 커밋 범위 검사를 수행하고 Git 전송 결과는 별도로 확인한다.

#### 남은 작업 및 주의 사항

- 다른 컴퓨터에서는 인계 브랜치의 문서와 최근 LOG를 읽고 미승인 WebSocket 설계부터 이어간다. Git에 문서를 올리는 것은 main 병합·기능 구현·운영 배포가 아니다.

## 2026-11-12

### #39 T01 점검 중 서비스 중단 후 DB 변경·배포 방식 합의

#### 변경 이유 및 내용

- 사용자 선택에 따라 서비스 요청을 잠시 중단하고 DB 변경·새 백엔드/프론트 배포·기존 데이터 및 인증 검증 후 재개하는 방식으로 설계를 확정했다. 무중단 다단계 전환이나 온라인 변경 도구는 이번에 추가하지 않는다.
- 사전 충돌 점검과 격리 DB 검증, 진행 중 요청·WebSocket·주기 작업 정리, 일관된 백업 및 복구 준비의 적용 순서를 기록했다. 기존 사용자·프로필·이용 기록 보존과 구형 JWT 재로그인 정책을 유지한다.
- MySQL 구조 변경 전체의 자동 롤백을 가정하지 않고 단계별 재실행·복구 및 이전 코드 호환 확인 후 재개하도록 명시했다. 실제 중단 시간과 명령은 확인 전 보장하지 않는다.

#### 주요 변경 파일

- `docs/plans/39-t01-auth-architecture.md`
- `LOG.md`

#### API 변경

- 실행 API 변경 없음. 향후 배포 점검 중 API 접근 차단 방식은 운영 상세에서 정한다.

#### DB 변경

- 실제 변경 없음. 백업·마이그레이션·복구는 실행하지 않았으며 변경 방식과 순서만 설계했다.

#### 검증

- 애플리케이션·DB·배포 테스트 미실행: 설계 문서만 변경했다.

#### 남은 작업 및 주의 사항

- 점검 차단 위치·진행 중 방 처리·공지, 제한된 브라우저 검증 접근, 마이그레이션 명령과 백업/복구 및 서비스 재개 기준을 구체화한다. 현재 서비스는 중단하거나 배포하지 않았다.

### #39 T01 기존 users 확장과 SHA-256 32바이트 저장 합의

#### 변경 이유 및 내용

- 사용자 결정에 따라 기존 `users` 표와 내부 PK·기록 연결을 유지하며 UUID 컬럼 추가와 `kakao_id` NULL 허용으로 확장하는 설계를 반영했다. 기존 사용자·프로필·이용 기록은 보존한다.
- 인증 기록은 별도 표로 유지하고 SHA-256 해시는 64글자 16진수 문자열 대신 `BINARY(32)`에 바이트 결과로 저장한다. 해시 유일 인덱스와 저장·조회 길이 및 인코딩 일관성 원칙을 기록했다.
- 저장 표현을 줄이는 장점과 DB에서 사람이 읽기 불편한 trade-off를 명시했다. 데이터 충돌 사전 점검, 기존 닉네임 임의 변경 금지 및 구현 후 격리 DB 검증 계획을 추가했다.

#### 주요 변경 파일

- `docs/plans/39-t01-auth-architecture.md`
- `LOG.md`

#### API 변경

- 실행 API 변경 없음.

#### DB 변경

- 실제 스키마·데이터 변경 없음. `users` 확장과 별도 인증 표의 `BINARY(32)` 해시 저장 설계만 확정했다. 마이그레이션 파일은 아직 추가하지 않았다.

#### 검증

- 애플리케이션·DB 테스트 미실행: 설계 문서만 변경했다.

#### 남은 작업 및 주의 사항

- UUID 형식·기존 데이터 채움, 인증 표의 나머지 자료형·FK·인덱스, 닉네임 충돌·기본 사진·프로필 출처·마이그레이션 순서를 구체화한다. 현재 운영 DB에는 적용하지 않았다.

### #39 T01 현재 운영 배포 환경에서 인증·쿠키 검증하기로 합의

#### 변경 이유 및 내용

- 사용자 결정에 따라 별도 테스트 환경을 추가하지 않고 현재 Vercel 프론트와 `whatbob.kro.kr` API의 HTTPS 배포 환경에서 실제 브라우저 인증·쿠키를 검증하도록 설계에 기록했다.
- 운영 쿠키 보안 설정과 기존 사이트 관계를 유지하고, 테스트 사용자·기록도 운영 DB에 저장되며 배포 영향이 실제 이용자에게 적용된다는 점을 명시했다. 자동 단위·통합 테스트의 격리 DB 원칙은 유지한다.
- 브라우저별 쿠키 전달·새로고침 복원·CORS·종료 거절 등의 검증 계획을 추가했다. 이번에는 환경 선택만 문서화했으며 운영 배포·마이그레이션·테스트 데이터 생성은 하지 않았다.

#### 주요 변경 파일

- `docs/plans/39-t01-auth-architecture.md`
- `LOG.md`

#### API 변경

- 없음.

#### DB 변경

- 없음. 운영 데이터 생성·변경·삭제는 수행하지 않았다.

#### 검증

- 애플리케이션·실제 브라우저 테스트 미실행: 설계 문서만 변경했다. 환경 구성이나 배포 완료를 의미하지 않는다.

#### 남은 작업 및 주의 사항

- 운영 쿠키 이름과 실제 설정 연결, DB·마이그레이션, 계정 전환·동시성·WebSocket, 운영 제한·배포 절차를 구체화한다. 현재 운영 서버와 DB를 사용하는 실제 검증은 구현 후 수행한다.

### #39 T01 HTTP 상세 계약의 사용자 승인 반영

#### 변경 이유 및 내용

- 사용자 승인에 따라 세 인증 API의 HTTP 상세 검토안을 합의된 설계 계약으로 전환했다. 준비 200, 신규 사용자 생성 201·기존 복구 200, 토큰 200 및 쿠키 상태별 오류·프론트 처리 방향을 구현 기준으로 정했다.
- 새 준비 전환 시 `notice` 안내, 정식 만료와 명시적 종료·DB 장애의 구분, 쿠키 미전달 시 무한 반복 금지, 인증 응답 캐시 방지 계약을 유지한다. 기존 사용자 데이터와 카카오 호환 원칙은 그대로다.
- 본문과 주 아키텍처 문서에서 미승인 제안 표기를 해소했다. 기존 링크 호환을 위해 HTTP 문서 파일명은 유지했으며 실행 API가 변경됐다는 의미는 아니다.

#### 주요 변경 파일

- `docs/plans/39-t01-http-contract-proposal.md`
- `docs/plans/39-t01-auth-architecture.md`
- `LOG.md`

#### API 변경

- 실행 API 변경 없음. 세 인증 API와 새 JWT 경계의 상세 HTTP 계약을 설계상 확정했다.

#### DB 변경

- 없음.

#### 검증

- 애플리케이션 테스트 미실행: 설계 문서만 변경했다.

#### 남은 작업 및 주의 사항

- 쿠키·개발 환경, DB·마이그레이션, 계정 전환·동시성·WebSocket, 운영 제한·배포의 네 묶음이 남아 있다. 코드 구현과 실제 브라우저·API 검증은 아직 하지 않았다.

### #39 T01 세 인증 API의 HTTP 상세 계약 검토안 작성

#### 변경 이유 및 내용

- 남은 설계 중 API 상세를 한 묶음으로 검토할 수 있도록 세 API 성공 상태·쿠키 상태별 분기·고정 오류 코드·프론트 행동의 제안 문서를 작성했다. 사용자 신규 생성 201/기존 복구 200, 준비 응답 안내 필드, 나머지 무효 쿠키 계약은 기존 합의와 구분한 신규 제안이다.
- 기존 인증 종료·기간 만료·DB 장애를 구분하고, 준비 API 외 경로에서 사용자 자동 생성을 하지 않으며 이전 사용자의 변경 요청을 새 사용자로 자동 재생하지 않는 기준을 명시했다.
- 실제 카카오 라우터·인증 의존성·JWT 검증 코드와 공개 HTTP/Bearer 표준을 확인했다. 애플리케이션이나 기존 API 계약을 실제 변경한 것은 아니다.

#### 주요 변경 파일

- `docs/plans/39-t01-http-contract-proposal.md`
- `docs/plans/39-t01-auth-architecture.md`
- `LOG.md`

#### API 변경

- 실행 API 변경 없음. 세 인증 API와 새 JWT 인증 경계의 HTTP 상세 계약 제안만 작성했다. 신규 제안은 사용자 확정으로 기록하지 않는다.

#### DB 변경

- 없음.

#### 검증

- 애플리케이션 테스트 미실행: 설계 문서만 변경했다. 상태 조합·기존 데이터 보존·동시 요청·오류 구분의 구현 후 검증 항목을 작성했다.

#### 남은 작업 및 주의 사항

- API 제안 검토 후 계약 확정이 필요하다. 쿠키·개발 환경, DB·마이그레이션, 계정 전환·동시성·WebSocket, 운영 제한·배포 묶음은 별도 설계 대상으로 남는다.

### #39 T01 웹 접근 JWT의 프론트 메모리 보관 합의

#### 변경 이유 및 내용

- 사용자 선택에 따라 접근 JWT는 프론트 메모리에만 보관하고 브라우저 지속 저장소에 남기지 않는 설계를 기록했다. 복원 비밀값은 기존 HttpOnly 쿠키에 유지한다.
- 새로고침·새 탭 등 JWT 메모리가 없는 경우 유효한 정식 쿠키로 같은 사용자·인증 기록의 JWT를 재발급받는다. JWT 부재만으로 사용자를 생성하지 않고 기존 준비·생성 분기와 장애 구분을 유지한다.
- 저장소에 남은 토큰의 노출을 줄이는 대신 초기 복원 요청·DB 조회·대기가 추가되는 trade-off와 실행 중 악성 JavaScript 방어의 한계를 명시했다. 동시 복원 합치기, 보호 요청 보류, 로그아웃 시 메모리·화면 상태 정리의 후속 검증을 추가했다.

#### 주요 변경 파일

- `docs/plans/39-t01-auth-architecture.md`
- `LOG.md`

#### API 변경

- 실행 API 변경 없음. 기존 설계의 토큰 API와 Bearer 인증을 사용하는 웹 프론트 저장·복원 정책이다.

#### DB 변경

- 없음.

#### 검증

- 애플리케이션 테스트 미실행: 설계 문서만 변경했다. 실제 브라우저의 저장·복원 동작은 후속 구현 후 확인한다.

#### 남은 작업 및 주의 사항

- 프론트 저장 모듈·초기화 흐름, 계정 전환 응답 경합·탭 동기화 및 타사 쿠키 제한의 브라우저 검증이 남아 있다. 네이티브 앱 저장 방식은 이번 선택에 포함하지 않는다.

### #39 T01 인증 기록 부재 시 자동 새 준비 시작 합의

#### 변경 이유 및 내용

- 사용자 선택에 따라 준비 API에서 DB 조회는 성공했지만 쿠키 해시에 대응하는 인증 기록이 없으면 새 비밀값·준비 인증을 발급하고 쿠키를 교체한 뒤 `CREATE_USER`를 안내하도록 설계했다.
- 준비 API 자체에서는 사용자·JWT를 만들지 않고 후속 생성 요청에서 새 UUID 사용자를 만든다. 기존 사용자·기록은 보존하며 이전 사용자로 자동 복원하거나 병합하지 않는다.
- DB 장애와 준비 저장 실패를 기록 부재로 처리하지 않고, 기존 유효 인증 유지 및 확인 가능한 종료 인증 처리는 보존하도록 명시했다. 토큰·사용자 생성·일반 인증 경로에 자동 생성을 추가하는 결정은 아니다.

#### 주요 변경 파일

- `docs/plans/39-t01-auth-architecture.md`
- `LOG.md`

#### API 변경

- 실행 API 변경 없음. `POST /api/auth/preparations`에서 인증 기록 부재 시 새 준비 쿠키와 `{"next_action":"CREATE_USER"}`를 반환하는 설계를 추가했다.

#### DB 변경

- 실제 변경 없음. 새 준비 인증 기록 저장은 후속 구현 대상이며 기존 사용자·데이터는 삭제하지 않는다.

#### 검증

- 애플리케이션 테스트 미실행: 설계 문서만 변경했다. 기록 부재·DB 장애·저장 실패·기존 사용자 보존 등의 검증 계획을 추가했다.

#### 남은 작업 및 주의 사항

- 새 사용자 시작 안내를 위한 응답 구분, 다른 인증 API의 기록 부재 오류 코드와 나머지 무효 쿠키 계약은 후속 설계 대상이다. 삭제된 기록의 과거 종료 사유는 쿠키만으로 복구할 수 없다.

### #39 T01 준비 API의 next_action 안내 방식 합의

#### 변경 이유 및 내용

- 사용자 선택에 따라 준비 API는 상태 해석을 프론트에 맡기지 않고 `next_action`으로 다음 API 호출을 직접 안내하도록 설계했다.
- 신규·기존 유효 준비는 `CREATE_USER`, 기존 유효 정식 인증은 `ISSUE_TOKEN`을 반환한다. 준비 API의 사용자·JWT 미발급, 유효 쿠키 유지 및 준비 요청만으로 만료를 연장하지 않는 기존 정책을 유지한다.
- 안내 이후 실제 호출에서도 쿠키·만료·종료를 다시 확인하고, 알 수 없는 값·장애를 새 사용자 생성으로 추정하지 않도록 기록했다. 준비 응답 본문에는 비밀값·해시·JWT를 포함하지 않는다.

#### 주요 변경 파일

- `docs/plans/39-t01-auth-architecture.md`
- `LOG.md`

#### API 변경

- 실행 API 변경 없음. `POST /api/auth/preparations`의 성공 응답을 `{"next_action":"CREATE_USER"}` 또는 `{"next_action":"ISSUE_TOKEN"}`으로 설계했다.

#### DB 변경

- 없음.

#### 검증

- 애플리케이션 테스트 미실행: 설계 문서만 변경했다.

#### 남은 작업 및 주의 사항

- 성공 HTTP 상태와 나머지 무효 쿠키 처리, 프론트 분기 및 다음 API에서의 유효성 재검증은 후속 설계·구현 대상이다.

### #39 T01 JWT와 사용자 정보의 공통 성공 응답 합의

#### 변경 이유 및 내용

- 사용자 선택에 따라 사용자 생성·생성 결과 복구·복원·재발급에서 JWT와 사용자 정보를 함께 반환하는 설계를 기록했다. 추가 사용자 조회 없이 초기 화면을 표시하고 기존 카카오 `TokenResponse` 패턴을 이어가기 위한 결정이다.
- 공통 응답은 `access_token`, `token_type`, `expires_in`, `user`, `is_new_user`로 구성한다. 사용자 정보는 JWT 내부가 아닌 별도 DTO로 반환하며 비밀값·해시는 본문에 노출하지 않는다.
- 준비 API는 별도 응답을 유지하고, `is_new_user`는 이번 요청의 신규 생성 여부로 정의했다. 생성 응답 유실 후 복구 요청에서는 값이 달라질 수 있으므로 영구 신규 여부나 부작용 재실행 기준으로 사용하지 않는다.

#### 주요 변경 파일

- `docs/plans/39-t01-auth-architecture.md`
- `LOG.md`

#### API 변경

- 실행 API 변경 없음. `POST /api/auth/anonymous-users`와 `POST /api/auth/tokens`의 공통 성공 응답 설계만 추가했다.

#### DB 변경

- 없음.

#### 검증

- 애플리케이션 테스트 미실행: 설계 문서만 변경했다.

#### 남은 작업 및 주의 사항

- 준비 API 응답, 성공 HTTP 상태, 사용자 DTO의 UUID·기본 사진 표현과 기존 카카오 필드 호환, 프론트 연동 검증을 구체화한다. 실행 응답은 아직 변경하지 않았다.

### #39 T01 최소 JWT 클레임 구성 합의

#### 변경 이유 및 내용

- 사용자 선택에 따라 프로필·권한 스냅샷 대신 인증에 필요한 최소 JWT 구성을 설계에 반영했다. 필수 클레임은 `sub`, `sid`, `iat`, `exp`, `iss`, `aud`이며 비밀 쿠키 원본·해시는 포함하지 않는다.
- 내부 사용자·인증 기록 번호는 십진 문자열로 표현하고, 재발급은 같은 인증 기록을 유지하며 DB의 연결 사용자와 JWT 사용자 일치를 검증하도록 기록했다. 발급자·대상 검증과 시간 정보 계약을 추가했다.
- 사용자 번호는 인증 기록의 FK에서 유도할 수도 있지만 이번에는 두 번호를 대조한다. 인증 기록 번호를 생략하면 현재 설계의 개별 인증 종료 확인이 불가능하다는 차이를 명시했다.

#### 주요 변경 파일

- `docs/plans/39-t01-auth-architecture.md`
- `LOG.md`

#### API 변경

- 실행 API 변경 없음. 새 접근 JWT 필수 내용의 설계만 확정했다.

#### DB 변경

- 없음.

#### 검증

- `git diff --check`와 설계 문서의 줄 끝 공백 검사 통과. 애플리케이션 테스트 미실행: 설계 문서만 변경했다.

#### 남은 작업 및 주의 사항

- `iss`·`aud`의 실제 설정값, 시간 오차 허용, 필수 클레임 누락·타입 불일치·서명 변조의 오류 계약과 발급·검증 연동 테스트는 후속 구현 대상이다. 실행 JWT 형식은 아직 변경하지 않았다.

## 2026-11-11

### #39 T01 새 인증 배포 시 기존 카카오 JWT 재로그인 전환 합의

#### 변경 이유 및 내용

- 사용자 결정에 따라 새 인증 구조 배포 시 세션 번호 없는 기존 JWT를 거절하고 기존 카카오 사용자에게 한 번 재로그인을 요구한다. 유예 허용·자동 교환 경로는 추가하지 않는다.
- 기존 카카오 사용자와 기록은 보존하며 재로그인으로 새 인증 기록·쿠키·JWT를 발급하는 설계를 기록했다. 이전 형식에는 401과 `REAUTHENTICATION_REQUIRED`를 사용하고 무한 갱신이나 새 UUID 복원으로 처리하지 않는다.
- 발급·검증·프론트가 함께 새 구조를 지원하도록 배포 순서를 후속 계획에 포함했다. 세션 번호 없는 토큰 발급과 새 검증만 혼재하는 배포를 피한다.

#### 주요 변경 파일

- `docs/plans/39-t01-auth-architecture.md`
- `LOG.md`

#### API 변경

- 실행 API 변경 없음. 설계상 이전 형식 토큰의 거절과 재로그인 오류 계약을 추가했다.

#### DB 변경

- 없음. 기존 사용자·관련 기록 삭제나 계정 재생성은 하지 않는 설계다.

#### 검증

- 문서 공백 검사와 `git diff --check` 통과.
- 애플리케이션 테스트 미실행: 설계 문서만 변경했다. 기존 미만료 토큰 거절·새 세션 검증·카카오 기록 보존의 검증 계획을 추가했다.

#### 남은 작업 및 주의 사항

- JWT 필수 클레임, 카카오 발급 경로·프론트 호환과 배포 순서, 기존 WebSocket 반영을 구체화한다. 지금 실행 중인 토큰을 실제로 차단한 것은 아니다.

### #39 T01 인증 정리 작업의 실행당 최대 100건 확정

#### 변경 이유 및 내용

- 사용자 결정에 따라 10분 주기 인증 정리 작업의 실행당 삭제 상한을 총 100건으로 확정했다. 준비·정식 만료 및 명시적 종료 대상을 합산한 상한이며 같은 실행에서 100건 배치를 반복해 초과하지 않는다.
- 초과 대상은 다음 주기로 넘기고 유효한 인증과 사용자·연관 기록은 보존한다. 삭제 건수와 남은 대상 수를 확인하여 누적 시 한도를 재검토하는 방향을 기록했다.
- 정상 실행 기준 시간당 최대 600건은 설정의 상한이며 서버 성능 측정값이 아님을 명시했다.

#### 주요 변경 파일

- `docs/plans/39-t01-auth-architecture.md`
- `LOG.md`

#### API 변경

- 없음.

#### DB 변경

- 실제 스키마·데이터 변경 없음. 정리 실행당 최대 100건 삭제로 설계를 확정했다.

#### 검증

- 문서 공백 검사와 `git diff --check` 통과.
- 애플리케이션 테스트 미실행: 설계 문서만 변경했다. 0건·100건 경계·초과·대상 종류 혼합과 데이터 보존의 검증 계획을 추가했다.

#### 남은 작업 및 주의 사항

- 삭제 순서·DB 작업 시간 제한·정리 실행 주체와 누적 확인 방법을 구현 설계에서 구체화한다. 실행 코드는 아직 없다.

### #39 T01 만료·종료 인증 기록의 서버 내부 10분 주기 정리 합의

#### 변경 이유 및 내용

- 사용자 결정에 따라 서버 내부 주기적 정리 방식을 채택하고 실행 간격을 10분(600초)으로 확정했다.
- 만료된 준비·정식 인증과 명시적 종료 인증을 삭제 대상으로 삼고 사용자·취향·식사 등 연관 데이터는 보존한다. 인증 거절은 만료·종료 즉시 적용하며 DB 삭제를 기다리지 않는다.
- 삭제 시 최신 만료·종료 조건 재검증, DB 실패 후 다음 실행 복구, 서버 중단에 따른 지연 및 여러 프로세스의 중복 실행 관리 필요성을 기록했다.
- 최대 100건은 앞선 제안이며 사용자 확정값으로 기록하지 않았다. 삭제 후에는 과거 만료·종료 이유를 항상 구분할 수 없는 점도 후속 오류 계약 대상으로 남겼다.

#### 주요 변경 파일

- `docs/plans/39-t01-auth-architecture.md`
- `LOG.md`

#### API 변경

- 없음. 실행 코드 변경 없는 인증 기록 정리 설계다.

#### DB 변경

- 실제 변경 없음. 만료·종료 인증 기록만 주기적으로 삭제하도록 설계했다. 마이그레이션·삭제 작업은 미구현이다.

#### 검증

- 문서 공백 검사와 `git diff --check` 통과.
- 애플리케이션 테스트 미실행: 실행 코드 변경 없음. 600초 설정·대상 한정 삭제·데이터 보존·동시성·실패 복구의 검증 계획을 기록했다.

#### 남은 작업 및 주의 사항

- 한 번의 처리 한도·반복 배치 예산, 정리 실행 주체와 삭제 후 기록 부재 오류를 구체화한다. 정리 주기는 삭제 완료 시한이 아니다.

### #39 T01 사용자 FK 유무로 준비·정식 인증 구분 합의

#### 변경 이유 및 내용

- 사용자 선택에 따라 별도 PENDING·ACTIVE 상태 컬럼 없이 인증 기록의 사용자 FK 유무로 준비·정식 단계를 구분하는 설계를 채택했다.
- 준비 단계는 사용자 FK가 없고, 생성 성공 트랜잭션에서 같은 인증 기록에 내부 정수 `users.id`를 연결한다. 사용자 UUID와 내부 번호의 역할을 구분했다.
- 만료·명시적 종료 여부는 별도 시각 정보로 먼저 검사하고, 사용자 FK가 있다는 이유만으로 인증하지 않는다. FK 제거·사용자 삭제로 정식 기록을 준비 기록처럼 재사용하지 않는 제약과 검증 계획을 기록했다.

#### 주요 변경 파일

- `docs/plans/39-t01-auth-architecture.md`
- `LOG.md`

#### API 변경

- 없음. 실행 코드 변경 없는 DB 상태 표현 설계다.

#### DB 변경

- 실제 스키마 변경 없음. 설계상 준비·정식 구분용 상태 컬럼을 두지 않고 nullable 사용자 FK를 사용한다. 테이블·제약·마이그레이션은 미구현이다.

#### 검증

- 문서 공백 검사와 `git diff --check` 통과.
- 애플리케이션 테스트 미실행: 설계 문서만 변경했다.

#### 남은 작업 및 주의 사항

- DB 컬럼·인덱스·FK 참조 정책·이관 및 사용자 삭제 처리·만료 기록 정리와 마이그레이션을 구체화한다.

### #39 T01 프론트 복구 행동을 구분하는 고정 인증 오류 코드 합의

#### 변경 이유 및 내용

- 사용자 승인에 따라 오류 문장 대신 `detail.code`로 프론트 행동을 구분하는 설계를 채택했다. 설명 문구는 `detail.message`에 둔다.
- JWT·준비·정식 인증 만료, 인증 종료, CSRF 거절, DB 일시 장애의 여섯 코드를 기록하고 HTTP 401·403·503을 기본 설계로 연결했다.
- 종료된 인증의 무조건 재발급·새 사용자 생성과 DB 장애의 인증 실패 오분류를 방지하는 프론트 처리 기준을 명시했다. 기존 API의 문자열 오류를 일괄 변경하지 않는다.

#### 주요 변경 파일

- `docs/plans/39-t01-auth-architecture.md`
- `LOG.md`

#### API 변경

- 실행 API 변경 없음. 새 인증 오류의 `detail.code`·`detail.message` 계약과 여섯 코드의 복구 동작을 설계했다.

#### DB 변경

- 없음.

#### 검증

- 현재 인증 DTO·라우터와 구현 명세를 확인했다. 문서 공백 검사와 `git diff --check` 통과.
- 애플리케이션 테스트 미실행: 실행 코드 변경 없음.

#### 남은 작업 및 주의 사항

- 나머지 오류·성공 DTO, 기존 JWT 의존성 호출부 호환 및 DB 상세 계약을 확정한다. 프론트 복구·재시도 구현은 아직 없다.

### #39 T01 웹 인증 명령의 Origin·필수 헤더 CSRF 방어 합의

#### 변경 이유 및 내용

- 사용자 선택에 따라 준비·UUID 생성·토큰 발급과 후속 쿠키 기반 인증 변경을 정확한 Origin 허용 목록과 `X-Whatbob-Request: 1` 검사로 보호하는 설계를 채택했다.
- 실제 작업 전에 출처와 필수 헤더를 검사하며 실패 시 403과 상태 비변경을 설계했다. 사용자 생성·JWT 발급에는 각각 준비·정식 쿠키 검증도 계속 필요하다.
- OPTIONS는 헤더가 아니라 브라우저의 사전 확인 요청 방식이며 작업을 실행하지 않는다. 서버는 OPTIONS 통과 이력에 의존하지 않고 실제 요청을 검사한다. 공개 조회·일반 Bearer API·OAuth 콜백의 적용 범위도 구분했다.
- 비브라우저 헤더 위조·탈취 쿠키·XSS 방어와 혼동하지 않도록 한계를 기록했다. 앱 지원을 위해 웹 Origin 검사를 우회하지 않는다.

#### 주요 변경 파일

- `docs/plans/39-t01-auth-architecture.md`
- `LOG.md`

#### API 변경

- 실행 API 변경 없음. 설계상 웹 보호 경로의 출처·필수 헤더 요구 및 거절 규칙을 확정했다. CORS 허용 헤더 추가와 보호 검사는 아직 미구현이다.

#### DB 변경

- 없음. 별도 CSRF 토큰 저장을 추가하지 않는 설계다.

#### 검증

- 문서 공백 검사와 `git diff --check` 통과.
- 현재 CORS 허용 헤더 목록에는 새 필수 헤더가 없음을 확인했다.
- 애플리케이션·브라우저 테스트 미실행: 설계 문서만 변경했다. 정상·누락·위조 출처·OPTIONS·상태 비변경 검증 계획을 추가했다.

#### 남은 작업 및 주의 사항

- 쿠키·오류·DB 상세 계약 후 T01 구현 시 보호 검사와 CORS 설정을 반영하고 실제 브라우저 동작을 검증한다.

### #39 T01 프론트·API의 서로 다른 사이트 배포 유지

#### 변경 이유 및 내용

- 앱 형태가 미정인 상태에서 사용자가 선택한 ③ 현재의 서로 다른 사이트 배포를 유지하는 방식을 설계에 반영했다. 프론트 중계나 도메인 이전은 추가하지 않는다.
- 운영 웹 쿠키의 HttpOnly·Secure·SameSite=None, API 호스트 한정·인증 경로 범위 및 프론트 자격 증명 전송과 명시적 CORS Origin을 기본 설계로 기록했다.
- 타사 쿠키 차단은 해당 설정으로 해제되지 않는 제약, 쿠키 부재 시 생성·준비 무한 반복 방지와 후속 CSRF 설계를 기록했다. 앱의 보관·전송 방식은 앱 형태 확정 후 설계한다.

#### 주요 변경 파일

- `docs/plans/39-t01-auth-architecture.md`
- `LOG.md`

#### API 변경

- 실행 API 변경 없음. 웹 쿠키 전달 기본 설계만 기록했다.

#### DB 변경

- 없음.

#### 검증

- 현재 `app/main.py`의 명시적 Origin 목록과 `allow_credentials=True`를 확인했다.
- MDN 쿠키 문서와 OWASP CSRF 지침을 확인했다. 문서 공백 검사와 `git diff --check` 통과.
- 애플리케이션 테스트 미실행: 실행 코드 변경 없음. 실제 브라우저 쿠키 저장·전송은 아직 검증하지 않았다.

#### 남은 작업 및 주의 사항

- CSRF 방어 조합, 개발 환경·쿠키 이름·쿠키 부재 오류와 브라우저 검증을 진행한다. 이 배포 관계에서 모든 브라우저의 인증 유지 성공을 보장하지 않는다.

### #39 T01 정식 인증을 30일·만료 전 이용 시 연장으로 변경

#### 변경 이유 및 내용

- 사용자 최종 결정에 따라 기존 14일 고정 수명을 30일로 변경하고, 만료 전 이용 시 그 시점부터 30일 연장하는 설계로 정리했다. 만료 후 추가 복구 기간 제안은 채택하지 않았다.
- 설계상 연장 지점은 유효한 정식 쿠키를 통한 JWT 발급·재발급 성공으로 정해 일반 API 요청마다 만료일을 쓰지 않도록 했다. 쿠키 만료와 DB 만료를 함께 갱신하며 접근 JWT는 계속 1시간이다.
- 생성 응답 유실 복구를 위한 최초 준비 쿠키 보관은 30일 + 5분으로 맞췄다. 준비 상태의 사용자 생성 허용 시간은 여전히 5분이다.
- 기간 만료·로그아웃·이관으로 종료된 인증은 연장하지 않는다. 연장 응답 유실 시 브라우저의 쿠키 수명이 이전 값에 머물 수 있는 한계와 연장·폐기 경합의 검증 계획을 기록했다.

#### 주요 변경 파일

- `docs/plans/39-t01-auth-architecture.md`
- `LOG.md`

#### API 변경

- 실행 API 변경 없음. 설계상 사용자 생성 결과 복구·토큰 발급 성공 시 정식 인증과 응답 쿠키를 30일 연장한다.

#### DB 변경

- 실제 스키마 변경 없음. 설계상 정식 인증 만료 시각을 갱신 가능하도록 하고 종료 상태를 원자적으로 검증한다.

#### 검증

- 문서의 기존 14일 고정·비연장 조건과 검증 계획을 새 정책에 맞춰 점검했다.
- 애플리케이션 테스트 미실행: 실행 코드 변경 없음.

#### 남은 작업 및 주의 사항

- HTTP·쿠키 보안 계약, DB 상세 설계 및 연장·폐기 동시성 구현과 테스트는 후속 작업이다. 설계 반영이며 기능 구현 완료가 아니다.

### #39 T01 카카오 로그아웃·이관의 인증 종료와 자동 연장 대상 구분

#### 변경 이유 및 내용

- 사용자 확인에 따라 카카오 로그아웃 시 기존 브라우저 인증을 종료하고 새 UUID 사용자로 시작하는 정책을 재확인했다. 카카오 계정의 기존 기록은 보존한다.
- UUID에서 카카오 계정 연결로 이관이 완료되면 이전 UUID 인증을 종료한다. 로그아웃·이관으로 명시적으로 종료한 인증은 기간 만료와 구분하고 자동 14일 연장·복원 대상에서 제외한다.
- 자동 연장 요청은 기간 만료 정책 변경으로 다루되, 만료 후 복원을 위한 쿠키 보관·허용 기한 등은 상세 설계로 남겼다. 기존 ADR과 일치하며 실행 코드 변경은 없다.

#### 주요 변경 파일

- `docs/plans/39-t01-auth-architecture.md`
- `LOG.md`

#### API 변경

- 없음. 계정 전환·인증 종료 설계만 반영했다.

#### DB 변경

- 없음.

#### 검증

- ADR-0001의 카카오 로그아웃·이관 정책과 일치함을 확인했다.
- 애플리케이션 테스트 미실행: 실행 코드 변경 없음.

#### 남은 작업 및 주의 사항

- 기간 만료 후 자동 연장의 쿠키 보관·복원 허용 기한·DB 기록 보존 계약을 정한다. 방 참여 중 계정 전환 제한은 유지한다.

### #39 T01 만료된 준비 쿠키의 새 발급 합의와 정식 인증 연장 검토

#### 변경 이유 및 내용

- 사용자 생성 없이 준비 5분이 만료되면 준비 API에서 새 비밀값·준비 기록을 발급하기로 했다. 기존 만료 기록을 되살리거나 준비 API에서 사용자를 생성하지 않는다.
- 정식 인증 자동 14일 연장 요청은 재방문 시 연장·만료 후 복원·명시적 폐기 후 복원의 의미를 확인 중이다. 기존 고정 만료·폐기 거절 정책을 변경 완료한 것으로 기록하지 않는다.

#### 주요 변경 파일

- `docs/plans/39-t01-auth-architecture.md`
- `LOG.md`

#### API 변경

- 실행 API 변경 없음. 준비 만료 시 새 발급 동작만 설계에 확정했다.

#### DB 변경

- 없음.

#### 검증

- 애플리케이션 테스트 미실행: 실행 코드 변경 없음.

#### 남은 작업 및 주의 사항

- 자동 연장 대상과 시점을 확인하고 쿠키 보관 기간·기존 인증 폐기 정책과 함께 설계한다. 이미 브라우저에서 삭제된 비밀값 쿠키의 자동 복원은 보장할 수 없다.

### #39 T01 반복 준비 요청에서 기존 유효 쿠키 유지 합의

#### 변경 이유 및 내용

- 유효한 준비·정식 쿠키가 있는 준비 요청은 기존 인증 기록과 비밀값을 유지하고 다음 단계만 안내하도록 결정했다. 불필요한 준비 기록 생성과 기존 사용자 연결 손실을 줄인다.
- 준비 상태는 사용자 생성 API로, 정식 인증은 토큰 API로 안내한다. 준비 API는 사용자·JWT를 생성하지 않고 준비 5분·정식 14일의 고정 만료를 연장하지 않는다.
- DB 장애는 새 준비 발급으로 처리하지 않으며 쿠키 없는 동시 최초 요청의 중복 허용은 유지한다.

#### 주요 변경 파일

- `docs/plans/39-t01-auth-architecture.md`
- `LOG.md`

#### API 변경

- 실행 API 변경 없음. `POST /api/auth/preparations`의 유효 쿠키 재사용 동작을 설계에 반영했다.

#### DB 변경

- 없음.

#### 검증

- 기존 세 API 책임 분리·고정 만료·다중 탭 정책과의 일관성 확인. `git diff --check` 통과.
- 애플리케이션 테스트 미실행: 실행 코드 변경 없음.

#### 남은 작업 및 주의 사항

- 응답 DTO·HTTP 상태·오류 코드 및 무효 쿠키를 가진 준비 요청의 새 시작 정책을 확정한다.

### #39 T01 준비·사용자 생성·토큰 발급의 세 API 분리 합의

#### 변경 이유 및 내용

- 준비와 사용자 생성, 기존 사용자 JWT 발급의 책임을 구분하도록 세 POST API를 설계에 채택했다. 최초 접속은 준비·생성 두 번이며 생성 응답에서 JWT도 발급한다.
- 토큰 API는 새 사용자를 생성하지 않고 유효한 정식 쿠키로 기존 사용자를 복원한다. 생성 API의 같은 쿠키 재요청은 기존 생성 결과를 복구하며 정식 인증 수명을 연장하지 않는다.

#### 주요 변경 파일

- `docs/plans/39-t01-auth-architecture.md`
- `LOG.md`

#### API 변경

- 실행 API 변경 없음. 설계 경로는 `POST /api/auth/preparations`, `POST /api/auth/anonymous-users`, `POST /api/auth/tokens`이며 DTO·HTTP 상태·오류 상세 계약은 미확정이다.

#### DB 변경

- 없음. 스키마·마이그레이션 변경 없음.

#### 검증

- 설계 문서의 준비·생성·복원 흐름과 API 역할의 일관성을 확인했다.
- `git diff --check` 통과. 애플리케이션 테스트 미실행: 실행 코드 변경 없음.

#### 남은 작업 및 주의 사항

- API별 요청·응답·오류 상세 계약과 기존 쿠키를 가진 준비 요청의 처리, 쿠키 보안·DB 상세 구조를 확정한다.
- 기존 카카오 인증 경로는 변경하지 않았다. 실제 API 개발은 상세 설계 후 수행한다.

### #39 T01 여러 탭의 서로 다른 준비 쿠키에 따른 별도 사용자 생성 허용

#### 변경 이유 및 내용

- 최초 접속한 여러 탭이 각각 다른 준비 쿠키를 발급받아 별도 UUID 사용자가 생성되는 상황은 허용하기로 결정했다. 탭 간 Web Locks·대표 선출·공통 초기 요청 번호를 추가하지 않아 구현 복잡도를 줄인다.
- 같은 비밀값의 재시도·동시 생성은 기존 사용자로 수렴시키는 DB 잠금·유일 제약을 유지한다. 서로 다른 비밀값의 사용자 자동 병합은 하지 않는다.
- 추가 DB 기록, 공유 쿠키 덮어쓰기와 탭별 JWT의 사용자 차이, 이후 복원 시 사용자 전환·복원 불가 가능성을 설계의 허용 범위로 명시했다. 요청별 JWT·세션 사용자 일치 및 데이터 격리 검증은 유지한다.

#### 주요 변경 파일

- `docs/plans/39-t01-auth-architecture.md`
- `LOG.md`

#### API 변경

- 없음. 설계만 반영했으며 실행 코드는 미구현이다.

#### DB 변경

- 없음. 스키마·마이그레이션 변경 없음.

#### 검증

- 같은 비밀값 중복 방지와 서로 다른 비밀값의 별도 사용자 허용 정책이 충돌하지 않는지 확인했다.
- `git diff --check` 통과. 애플리케이션 테스트 미실행: 실행 코드 변경 없음.

#### 남은 작업 및 주의 사항

- HTTP·쿠키 보안·DB 상세 계약, 만료 기록 정리와 발급 속도 제한을 구체화한다.
- 여러 탭의 최초 접속 경합에서는 브라우저당 사용자 한 명을 보장하지 않는다. 카카오 전환·로그아웃의 탭 간 처리는 후속 설계로 남긴다.

### #39 T01 첫 쿠키 장기 보관과 생성 응답 유실 복구 합의

#### 변경 이유 및 내용

- 사용자 생성 성공 응답이 유실되어도 기존 사용자를 찾을 비밀값이 브라우저에 남도록, 첫 준비 쿠키를 14일 + 5분 동안 보관하는 설계를 확정했다.
- 서버의 준비 상태 생성 허용 기간은 발급 후 5분, 정식 인증 수명은 생성 성공부터 14일 고정으로 유지한다. 생성되지 않은 준비 쿠키는 5분이 지나면 보관 여부와 관계없이 사용할 수 없다.
- 성공 응답과 후속 쿠키 재설정은 기존 정식 만료 시각에 맞추며 인증 기간을 연장하지 않는다. 같은 쿠키가 남아 있고 정식 인증이 유효한 경우에만 기존 사용자로 복원하며, 만료된 준비 기록 정리·발급 제한·여러 탭 경합은 후속 설계로 남겼다.

#### 주요 변경 파일

- `docs/plans/39-t01-auth-architecture.md`
- `LOG.md`

#### API 변경

- 없음. 쿠키 보관·복구 설계만 반영했으며 실제 HTTP 계약은 미구현이다.

#### DB 변경

- 없음. 스키마·마이그레이션 변경 없음.

#### 검증

- 기존 생성·복원 정책과 새 쿠키 보관 정책의 일관성을 검토하고 검증 계획에 경계 사례를 추가했다.
- `git diff --check` 통과. 애플리케이션 테스트 미실행: 실행 코드 변경 없음.

#### 남은 작업 및 주의 사항

- HTTP 계약, 쿠키 보안 속성, 여러 탭의 준비 쿠키 경합, DB 상세 구조·정리 및 발급 속도 제한을 정한다.
- 브라우저가 쿠키를 삭제·차단하거나 다른 준비 쿠키로 덮어쓰면 기존 사용자 복구가 보장되지 않는다.

### #39 T01 단일 인증 기록의 준비·정식 전환 방식 합의

#### 변경 이유 및 내용

- 사용자 생성 응답이 유실되어도 같은 쿠키로 기존 사용자를 찾도록, 준비 시 만든 하나의 인증 기록을 사용자와 연결하여 정식 인증으로 전환하는 설계를 확정했다.
- 같은 기록의 내부 PK를 유지하고 사용자 생성·사용자 FK 연결·정식 만료 시각 설정을 하나의 트랜잭션으로 처리한다. 같은 비밀값의 경합은 기록 잠금과 해시 유일 제약으로 제어하며, 유효한 정식 인증으로 반복 요청하면 기존 사용자로 JWT를 발급하는 방향을 기록했다.
- 별도 상태 컬럼 사용 여부와 쿠키 보관·전환 응답 유실 복구 계약까지 확정한 것은 아니다. 실행 코드는 미구현이다.

#### 주요 변경 파일

- `docs/plans/39-t01-auth-architecture.md`
- `LOG.md`

#### API 변경

- 없음. 설계 문서만 변경했다.

#### DB 변경

- 없음. 단일 인증 기록 방식을 합의했으며 실제 스키마·마이그레이션은 미구현이다.

#### 검증

- 설계 문서의 기존 생성·복원 흐름과 새 결정의 일관성을 확인했다.
- `git diff --check` 통과. 애플리케이션 테스트 미실행: 실행 코드 변경 없음.

#### 남은 작업 및 주의 사항

- 준비 허용 5분과 브라우저 쿠키 보관 기간을 구분하고 정식 전환 응답 유실 시 복구 계약을 정한다.
- 정확한 상태 표현·DB 제약·HTTP 계약 및 여러 탭의 서로 다른 준비 쿠키 경합은 후속 설계 대상이다.

### #39 T01 준비 인증 전환의 공개 아키텍처 비교 조사

#### 변경 이유 및 내용

- 실제 공개 사례를 근거로 설계를 비교해 달라는 요청에 따라 AWS의 요청 식별·변경 원자성, Stripe API v1의 응답 재전달과 v2의 복구 차이, Shopify Payment Service의 요청 기록·잠금·복구 지점을 조사했다.
- 기업의 공개 패턴과 WhatBap에 제안하는 단일 세션 상태 전환·별도 준비 기록 구조를 구분한다. 구체적인 테이블 구조나 응답 재전달 방식을 채택한 것은 아니다.
- 준비 쿠키가 브라우저에서 5분 후 삭제되면 생성 성공 응답 유실 후 복원에 한계가 있으므로, 서버의 준비 허용 기간과 브라우저 보관·인증 전환 계약을 구분해야 함을 확인했다.

#### 주요 변경 파일

- `docs/plans/39-t01-bootstrap-architecture-research.md`
- `LOG.md`

#### API 변경

- 없음. 공개 자료 조사와 설계안 비교만 수행했다.

#### DB 변경

- 없음. 테이블 구조는 미확정이다.

#### 검증

- AWS·Stripe·Shopify 공식 문서와 기술 블로그를 대조했다.
- 애플리케이션 테스트 미실행. 실행 코드 변경 없음.

#### 남은 작업 및 주의 사항

- 준비·정식 세션 저장 구조, 동시 처리, 쿠키 보관과 전환 실패 복구 정책을 선택하고 구체화한다.

### #39 T01 준비 쿠키 5분과 정식 인증 전환 시점 합의

#### 변경 이유 및 내용

- 준비 쿠키로 사용자 생성을 요청할 수 있는 기간을 발급 후 5분 고정으로 결정했다. 수신 후 즉시 생성 요청이 가능하며 서버에서도 만료를 검증하는 설계다.
- 사용자·인증 세션 생성 성공부터 정식 인증의 14일 고정 수명을 계산한다. 준비 기간이 지났어도 이미 생성된 유효 세션이 있다면 새 UUID를 생성하지 않는다.
- 준비 검증 방식·쿠키 전환 응답 유실·여러 탭 경합은 후속 설계로 남겼다. 실제 인증 코드는 미구현이다.
- 후속 결정으로 준비 쿠키는 DB 준비 기록으로 검증하기로 했다. 비밀값 해시·준비 상태·발급 및 만료 시각을 저장하고 사용자 생성과 정식 인증 연결·상태 전환을 원자적으로 처리하는 설계를 반영했다. 원본 비밀값과 JWT 원문은 저장하지 않는 방향이며 정확한 스키마·정리 정책은 미정이다. 앞선 준비 검증 방식 미정 항목을 대체하며 실행 코드 변경은 없다.

#### 주요 변경 파일

- `docs/plans/39-t01-auth-architecture.md`
- `LOG.md`

#### API 변경

- 없음. 설계만 변경했다.

#### DB 변경

- 없음.

#### 검증

- 문서 변경 후 `git diff --check` 실행.
- 애플리케이션 테스트 미실행. 실행 코드 변경 없음.

#### 남은 작업 및 주의 사항

- 준비 쿠키 검증·전환·동시 요청 계약을 구체화한다. 쿠키의 브라우저 만료와 서버의 유효성 판정을 구분한다.

## 2026-11-08

### #39 T01의 SHA-256 인증 세션 방식 합의와 구현 준비

#### 변경 이유 및 내용

- 사용자와 인증 세션의 관계, SHA-256과 Argon2id의 차이, 서버 메모리 1GB 제약을 검토하고 서버 생성 32바이트 무작위 비밀값의 SHA-256 해시만 DB에 저장하는 방식으로 합의했다.
- 비밀값 쿠키의 해시로 세션을 찾고 사용자 FK를 따라 복원하는 구조를 정리했다. 쿠키에 공개 세션 식별자를 추가하는 방식은 채택하지 않았다.
- 현재 코드의 확장 지점과 구현 전 남은 JWT·쿠키·HTTP·DB 계약을 문서화했다. 다른 브라우저는 별도 UUID 사용자라는 정책과 이관 후 원본 인증 폐기를 유지한다.
- 후속 설계 대화에서 사용자가 접근 JWT 유효 시간을 발급 시점부터 1시간으로 결정했다. 설계 문서에 반영했고, 복원 인증 수명은 별도 미정으로 남겼다. 실행 코드의 설정은 아직 변경하지 않았다.
- 이어서 복원 쿠키·인증 세션은 발급 후 30일 고정으로 결정했다. 재방문·JWT 갱신으로 연장하지 않으며 추후 연장 정책을 도입해도 만료된 인증을 자동 복구하지 않는 기준을 설계에 기록했다. 실행 코드 변경 없음.
- 접근 JWT는 만료 응답 후 갱신하기로 결정했다. 1GB 서버에서 이용을 중단한 브라우저의 불필요한 예약 갱신을 피하려는 근거와, 활성 사용자에게 발생하는 만료 응답·재시도 비용을 기록했다. 실제 자원 절감은 미측정이며 활동 기반 사전 갱신보다 항상 유리하다고 단정하지 않는다.
- 후속 검토에서 갱신 요청 개별 비용과 사용자 수만으로 큰 자원 절감이나 방식 전환을 단정할 수 없음을 정정했다. 현재는 이용자가 적은 단계에서 구현·운영의 단순함을 위해 만료 응답 후 갱신을 선택하고, 향후 갱신·재시도 지연과 사전 갱신의 실제 서버 부하를 비교해 재검토하기로 합의했다.
- 요청마다 JWT와 연결된 DB 세션을 함께 확인하는 ② 방식을 선택했다. UUID·카카오 전환 후 기존 인증을 거절하고 세션·사용자 JOIN으로 DB 왕복을 줄이는 설계 방향을 기록했다. 처리량은 미측정이며 기존 JWT 호환성·동시 요청·WebSocket 폐기 반영은 후속 설계로 남겼다. 실행 코드 변경 없음.

- 인증 오류를 프론트의 복구 행동에 따라 구분하기로 합의했다. JWT 만료, 쿠키 부재, 세션 무효, DB 일시 장애, 응답 없는 네트워크 오류를 구분하며 장애를 새 사용자 생성으로 처리하지 않는다. 재시도 횟수·간격은 제한하되 구체적인 수치는 검토 중이다. 설계 문서만 변경했다.

- 후속 결정으로 안전하게 반복할 수 있는 일시 장애 요청은 최초 요청 포함 총 3회(자동 재시도 최대 2회), 지수 백오프를 적용하기로 했다. 구체적인 대기 시간·배수·무작위 분산·전체 시간 제한은 미정이다. 실행 코드 변경 없음.

- 이어서 재시도 기준 1초·2초에 ±50% 균등 무작위 지터를 적용하기로 합의했다. 첫 재시도 0.5~1.5초, 두 번째 1~3초 범위에서 매번 새로 뽑는다. 총 3회 시도를 유지하며 4초 기준의 추가 재시도는 하지 않는다. 전체 시간 제한과 적용 위치는 후속 설계로 남겼다. 실행 코드 변경 없음.

- 운영 주소의 루트 경로를 첫 요청 1회와 0.5초 간격의 후속 순차 요청 20회로 측정했다. 모두 HTTP 200이었고 첫 요청 약 625ms, 후속 중앙값 약 203ms·최대 약 760ms였다. DB를 조회하지 않는 경로이므로 인증 API 처리 시간이나 부하 상황의 근거로 확대하지 않는다. 이후 사용자 결정으로 요청별 타임아웃을 10초로 설계에 반영했다. 전체 시간 제한과 서버 DB 제한은 미정이며 실행 코드는 변경하지 않았다.

- 사용자가 10초는 요청별 제한이 아니라 전체 제한이라는 뜻으로 정정했다. 설계를 요청·지터 대기·자동 재시도 전체 최대 10초로 수정했다. 총 3회는 상한이며 시간 예산이 소진되면 남은 시도를 중단한다. 이전 요청별 10초 해석은 대체되었으며 별도 요청별 제한은 미정이다. 실행 코드 변경 없음.

- 사용자 결정으로 요청별 최대 3초와 요청·대기·재시도 전체 최대 10초를 함께 적용하기로 했다. 각 시도는 3초와 전체 남은 시간 중 더 짧은 시간만 대기한다. 최대 3회 시도를 유지하되 전체 제한이 먼저 소진되면 중단한다. 운영 인증 API 실측 검증과 적용 위치·JWT 갱신 예산 결합은 후속 설계로 남겼다. 실행 코드 변경 없음.

- 사용자 결정으로 복원 쿠키·인증 세션 수명을 발급 후 30일 고정에서 14일 고정으로 변경했다. 요청·JWT 갱신으로 연장하지 않는 정책은 유지한다. 만료 자체는 기존 사용자나 기록 삭제 또는 새 UUID 생성 작업이 아니며, 새 익명 이용 시작 시 별도 UUID를 생성하는 흐름과 구분한다. 실행 코드 변경 없음.

- 사용자 결정으로 최초 생성은 서버가 HttpOnly 비밀 쿠키를 먼저 전달하고, 후속 요청에서 사용자·인증 세션을 생성하는 ② 방식을 채택했다. 같은 쿠키의 재요청은 유효한 기존 사용자로 수렴하도록 하며 DB 고유 제약·트랜잭션과 응답 유실·동시성 검증 계획을 기록했다. 준비 쿠키 검증·수명·전환, 14일 기산점과 여러 탭 경합은 후속 설계로 남겼다. 실행 코드 변경 없음.

#### 주요 변경 파일

- `docs/plans/39-t01-auth-architecture.md`
- `LOG.md`

#### API 변경

- 없음. 설계와 구현 준비만 진행했다.

#### DB 변경

- 없음. 실제 스키마·마이그레이션은 미구현이다.

#### 검증

- 실제 사용자·인증 모델, JWT 검증 코드, 기존 테스트 경로와 마이그레이션 스크립트 위치를 확인했다. `.venv/Scripts/python.exe` 존재 확인.
- `git diff --check` 통과 및 신규 설계 문서 후행 공백 검사 통과.
- 애플리케이션 빌드·테스트: 미실행. 실행 코드 변경 없음.

#### 남은 작업 및 주의 사항

- 복원 쿠키와 접근 JWT의 역할·수명·폐기 반영, 실제 배포 관계에 맞는 쿠키·CORS·CSRF 및 HTTP 응답 계약을 다음으로 설계한다.
- 기존 변경 사항을 보존했다. GitHub 하위 이슈 생성·커밋·실제 기능 구현은 하지 않았다.

## 2026-11-07

### 완전 랜덤 추천을 GET 조회 API와 최소 메뉴 응답으로 변경

#### 변경 이유 및 내용

- 추천 이력을 저장하거나 서버 상태를 변경하지 않는 완전 랜덤 추천의 성격에 맞게 HTTP 메서드를 `POST`에서 `GET`으로 변경했다. 기존 요청 본문의 추가 제외 조건은 같은 기능을 유지하도록 반복 가능한 `exclusions` 쿼리 파라미터로 옮겼다. 로그인 사용자의 알레르기·섭취 불가·비선호 자동 제외와 음식 종류 우선 무작위 선택 로직은 변경하지 않았다.
- 랜덤 결과 화면에서 사용하는 메뉴 식별·표시 정보만 제공하도록 `selected_menu`를 `menu_id`, `food_code`, `name`, `base_name` 네 필드로 축소했다. 외부 응답에서 후보·제외·종류 수를 제거했지만 내부 계산은 추천 로직과 단위 테스트를 위해 유지한다.
- 매번 다른 결과를 조회하는 API가 중간 캐시에 저장되지 않도록 `Cache-Control: no-store`를 추가했다.

#### 주요 변경 파일

- `app/recommend/router.py`
- `app/recommend/schemas.py`
- `tests/test_random_pick.py`
- `LOG.md`

#### API 변경

- 수정: `POST /api/recommendations/random`을 `GET /api/recommendations/random`으로 교체했다. 이전 JSON 요청 본문 `{ "exclusions": [...] }`은 `?exclusions=값1&exclusions=값2` 형식의 선택적 쿼리 파라미터로 변경했다. 기존 POST 호출은 `405 Method Not Allowed`를 반환한다.
- 수정: 성공 응답의 `data`는 `selected_menu`만 포함하고, `selected_menu`는 `menu_id`, `food_code`, `name`, `base_name`만 반환한다. 기존 `category_large`부터 `sodium_mg`까지의 상세 속성과 `excluded_count`, `candidate_count`, `menu_type_count`는 랜덤 API 응답에서 제거했다. 공통 `success`, `data`, `message` 래퍼는 유지한다.

#### DB 변경

- 없음. 기존 `menus`, `user_food_exclusions` 조회 방식과 스키마는 변경하지 않았다.

#### 검증

- `python -m pytest -q tests/test_random_pick.py tests/test_menu_display.py`: 30개 통과. GET 호출, 사용자·쿼리 제외 조건 동시 적용, 네 필드 응답, 캐시 방지 헤더, 기존 POST의 405 응답과 메뉴 표시명 규칙을 검증했다.
- `python -m pytest -q -rs`: 238개 통과, 2개 실패, 1개 건너뜀. 실패한 `tests/test_user_meals.py` 2건은 이번 변경 파일과 무관한 기존 식사 기록 표시명 기대값과 현재 `format_menu_name` 결과의 불일치다. MySQL 방 통합 테스트 1건은 `MYSQL_TEST_DATABASE_URL` 미설정으로 건너뛰었다. 기존 경고 2건이 발생했다.
- `python -m compileall -q app tests`, `git diff --check`: 통과.

#### 남은 작업 및 주의 사항

- 프론트엔드 호출을 GET과 쿼리 파라미터 방식으로 변경해야 한다. 상세 메뉴 속성이나 후보 통계를 사용하던 호출부가 있다면 제거된 응답 필드에 맞춰 수정해야 한다.
- 전체 테스트의 기존 식사 기록 표시명 실패 2건은 별도 작업에서 기대 계약을 확인한 뒤 수정해야 한다. 운영 MySQL 및 실제 프론트엔드 연동 검증, 배포·커밋·푸시는 수행하지 않았다.

### #39 명세를 구현 순서와 완료 기준이 있는 9개 작업 초안으로 분할

#### 변경 이유 및 내용

- 사용자가 `to-tickets`를 요청하여 #39의 명세를 독립적으로 검증 가능한 9개 작업으로 나누고 각 작업의 선행 조건과 완료 기준을 작성했다.
- UUID 생성·복원을 먼저 제공한 뒤 개인 기록, 방 접속, 닉네임, 사진 편집을 독립 작업으로 구성했다. 카카오 이관은 단일 UUID의 신규 계정 이관부터 기존 계정·다중 UUID 병합으로 확장한다.
- 각 기능 작업에 데이터 변경·API·연동 계약·관련 테스트를 함께 포함했다. 전체 전환·기존 DB 회귀 검증은 마지막 통합 작업으로 두되 개별 테스트를 뒤로 미루지 않는다.
- 작업별 명세 시나리오 대응과 의존성 도식을 작성했다. 계획 번호 T01~T09는 실제 GitHub 이슈 번호가 아니다.
- #39의 최신 본문·댓글과 하위 이슈를 조회했으며 기존 하위 이슈는 없었다. 사용자 검토 전이므로 하위 이슈 생성이나 부모 이슈 수정은 하지 않았다.

#### 주요 변경 파일

- `docs/plans/39-uuid-user-tickets.md`
- `LOG.md`

#### API 변경

- 없음. 구현 작업 초안만 작성했다.

#### DB 변경

- 없음

#### 검증

- PowerShell 문서 검사: 작업 9개의 필수 섹션·완료 기준, 선행 조건의 순서·순환 부재, 개별 기능 작업의 사용자 시나리오 56개 전체 포함, 소스 경로·구현 코드 조각 제외 및 후행 공백 확인 통과.
- `git diff --check`: 통과.
- 애플리케이션 빌드·테스트: 미실행. 실행 코드 변경 없음.

#### 남은 작업 및 주의 사항

- 작업 크기·선행 관계·분할 방식의 사용자 확인 후 작업별 GitHub 하위 이슈와 차단 관계를 생성하고 `ready-for-agent`를 적용한다.
- 부모 #39의 본문·제목·상태는 변경하지 않는다. 실제 기능 구현과 운영 사진 저장소·배포는 이번 작업에 포함하지 않았다.

### #39 구현 명세 작성 및 GitHub 이슈 게시

#### 변경 이유 및 내용

- 사용자가 `to-spec`을 요청하여 합의한 UUID 기본 이용, 선택적 카카오 기록 이관, 프로필 편집과 방 접속 정책을 구현 명세로 정리했다.
- 지정된 7개 섹션과 사용자 시나리오 56개를 작성하고, 구현 결정에는 구체적인 소스 파일 경로나 코드 조각을 넣지 않았다.
- 기존 FastAPI REST·WebSocket 통합 테스트를 주 검증 경계로 하고, 격리 DB·임시 업로드 폴더 및 전용 MySQL 동시성 검증으로 보완하는 방식을 사용자에게 확인받았다.
- 빈 양식이던 기존 [GitHub #39](https://github.com/pknu-wap/WHATBOB_BE/issues/39)의 본문을 명세로 채웠다. 제목은 `[feat] UUID 기본 이용과 선택적 카카오 기록 이관`으로 정리하고 기존 `enhancement` 라벨을 유지한 채 `ready-for-agent`를 적용했다.
- 기존 설계 문서에 로컬 명세·GitHub 링크와 명세 게시 완료·기능 미구현 상태를 명시했다. 개발은 로컬 사진 저장, 운영 저장소는 추후 추가라는 합의를 유지했다.

#### 주요 변경 파일

- `docs/specs/39-uuid-users-and-kakao-migration.md`
- `docs/UUID_USER_DESIGN.md`
- `LOG.md`

#### API 변경

- 없음. API 구현을 변경하지 않고 향후 구현할 동작과 검증 기준을 명세화했다.

#### DB 변경

- 없음

#### 검증

- PowerShell 문서 검사: 7개 섹션의 순서, 사용자 시나리오 56개의 형식·연속 번호, 구현 결정의 파일 경로·코드 조각 제외, 주요 합의 포함 여부 및 후행 공백 확인 통과.
- 게시 후 `gh issue view`로 원격 본문이 로컬 명세와 일치함을 확인했다. `enhancement`, `ready-for-agent` 라벨과 단일 triage 상태를 확인했다.
- `git diff --check`: 통과.
- 애플리케이션 빌드·테스트: 미실행. 명세와 작업 기록만 변경했으며 실행 코드 변경 없음.

#### 남은 작업 및 주의 사항

- #39의 실제 구현·마이그레이션·테스트 실행은 후속 작업이다. 명세의 테스트 계획을 실행 결과로 해석하지 않는다.
- 운영 사진 저장소와 배포는 범위 밖이다. 실제 브라우저 인증 복원·프론트 연동과 MySQL 동시성은 구현 단계에서 확인한다.

### #39 합의한 사용자·계정 이관·프로필·방 정책 설계 초안 통합

#### 변경 이유 및 내용

- 여러 차례 구체화한 #39의 최종 검토용 설계를 `docs/UUID_USER_DESIGN.md`에 모았다. 이전 결정과 사용자 정정을 구분하고, 현재 유효한 동작과 구현 시 적용할 기술 기준을 함께 제시했다.
- 사진 저장은 개발 중 로컬 파일로 구현하고 운영 저장소는 운영 준비 때 추가하기로 확정했다. S3를 포함한 외부 저장소는 선택·생성하지 않았다.
- UUID 자동 이용, 선택적 카카오 로그인과 UUID 폐기·기록 이관, 프로필 우선순위, 닉네임·사진 편집, 방 참여 중 계정 전환 제한과 30초 재접속 유예를 정리했다.
- 현재 코드는 수정하지 않았으며 문서 상태를 설계 검토 대기로 명시했다.

#### 주요 변경 파일

- `docs/UUID_USER_DESIGN.md`
- `LOG.md`

#### API 변경

- 없음. 설계 문서이며 현재 API 동작을 변경하지 않았다.

#### DB 변경

- 없음

#### 검증

- 합의한 용어·ADR과 통합 설계의 정책을 대조했다. 문서의 상대 링크, 주요 확정 항목, 후행 공백과 `git diff --check`를 확인했다.
- 빌드·테스트: 미실행. 실행 코드 변경 없음.

#### 남은 작업 및 주의 사항

- 전체 설계에 대한 사용자 확인 후 구현으로 진행한다. API·프론트 연동 계약과 인증 복원·갱신은 구현 단계에서 구체화한다.
- 운영 사진 저장소는 추후 결정·연결한다. 기존 사용자 변경 사항과 작업 기록은 보존했다.

### #39 UUID 폐기·카카오 계정 기록 이관 모델과 기준 프로필 정정

#### 변경 이유 및 내용

- 사용자가 카카오 연결 시 기존 UUID들을 폐기하고 기록을 카카오 계정으로 옮기는 모델을 명시했다. 앞서 가장 오래된 UUID 사용자 자체를 대표로 남긴다는 해석을 대체했다.
- 프로필은 가장 먼저 생성된 UUID 사용자의 닉네임·사진을 유지하고 이후 수정할 수 있다. 카카오 프로필을 최초 로그인 때 자동 적용한다는 제안은 채택하지 않았다.
- 알레르기·섭취 불가·비선호 목록은 모두 합치는 것으로 확정했다.
- 용어집의 `대표 사용자`를 `기준 프로필`로 바꾸고 `UUID 사용자` 및 기록 이관·UUID 폐기 의미를 반영했다. ADR도 사용자 정정에 맞췄다.
- 후속 답변에서 방에 참여 중인 사용자는 카카오 로그인을 할 수 없다고 확정했다. 방 참여 중 이관을 허용하자는 제안은 채택하지 않았다.
- 프로필은 기본 이미지를 제공하되 사용자가 본인 사진을 직접 업로드해 변경할 수 있도록 확정했다. 준비된 이미지 중에서만 선택하는 제안은 채택하지 않았다.
- 연결이 끊겨도 30초 동안 방 참여를 유지하도록 요청했다. 기존 일반 참여자는 유예 종료 후에도 오프라인 멤버로 남으므로, 새 30초가 지난 뒤의 퇴장·방 종료 여부는 추가 확인 중이다.
- 카카오 로그아웃 후 새 UUID와 랜덤 닉네임으로 시작하고 카카오 계정의 기존 기록은 보존하는 것으로 확정했다.
- 카카오 계정에서 사용자가 직접 수정한 프로필은 더 오래된 UUID의 기록을 나중에 이관해도 유지한다. 기준 프로필 정의와 ADR에 이 우선순위를 반영했다.
- 후속 답변에서 30초 유예 만료 후 일반 참여자는 자동 퇴장하고 방장은 방을 종료하는 것으로 확정했다. 이전의 유예 만료 후 처리 미확정 상태를 대체한다.
- 방 참여 중에는 로그인과 로그아웃을 모두 제한하며, 같은 카카오 계정이 다른 기기에서 방에 참여 중인 경우의 로그인도 제한하기로 확정했다. 별도 ADR에 방 재접속·계정 전환 규칙을 기록했다.
- 닉네임 변경 시 다른 사용자가 이미 사용 중인 이름은 거절하는 것으로 재확인했다.
- 프로필 사진 업로드는 JPG·PNG·WebP, 최대 5MB를 허용하고 업로드·교체·삭제를 지원한다. 사진 삭제 시 기본 이미지로 복귀하는 방식에 동의했다.
- UUID 도입 이전 카카오 계정의 프로필 보존 여부는 설명을 요청받은 상태로 미확정이다. 기존 계정 프로필을 우선한다는 제안을 합의된 예외로 적용하지 않는다.
- 설명 후 사용자가 UUID 도입 이전부터 있던 카카오 계정으로 합치도록 선택했다. 해당 기존 계정의 프로필·기록을 보존하고 UUID 기록을 이관하며, 기존 계정 처리 미확정 상태를 이 결정으로 대체했다.
- 닉네임 입력은 2~20자, 한글·영문·숫자·공백 허용, 앞뒤 공백 제거로 확정했다. 다른 사용자가 사용하는 닉네임은 거절한다.
- 사진 저장 위치는 사용자가 미정으로 답변했다. 아직 외부 저장소를 선택하거나 생성하지 않았다.
- 후속 답변에서 개발은 로컬 파일로 저장하고 운영 저장소는 추후 추가하는 것으로 확정했다. 사진 저장 위치 미정 상태는 이 결정으로 대체됐다.

#### 주요 변경 파일

- `GLOSSARY.md`
- `docs/adr/0001-uuid-users-with-optional-kakao-linking.md`
- `docs/adr/0002-room-reconnect-and-account-switching.md`
- `LOG.md`

#### API 변경

- 없음. 설계 문서만 수정했으며 계정 이관·프로필 편집·병합은 미구현이다.

#### DB 변경

- 없음

#### 검증

- 실제 사용자·방·제외 목록·식사 기록·피드백 제약을 읽어 기록 이관 시 참조와 중복 처리 필요성을 확인했다.
- 용어집·ADR의 확정 규칙과 문서 연결을 PowerShell로 확인했다. `git diff --check` 통과.
- 빌드·테스트: 미실행. 실행 코드 변경 없음.

#### 남은 작업 및 주의 사항

- 이관 후 기존 세션과 UUID 사용자 재방문 인증 계약을 구체화해야 한다. 기존 카카오 계정으로 합치기와 닉네임 길이·허용 문자·중복 거절은 확정됐다.
- 프로필 사진 업로드 저장 위치, 제외 목록 합산 상한 및 UUID 사용자 재방문 인증 계약도 최종 합의가 필요하다. 사진 형식·최대 용량·삭제 동작은 확정됐다.

### #39 UUID 기본 이용과 선택적 카카오 계정 연결 방향 확정

#### 변경 이유 및 내용

- 사용자의 최종 확인에 따라 카카오 로그인을 전체 제거하는 앞선 방향을 대체했다. 첫 방문에 UUID 사용자와 표시 닉네임을 자동 생성하고, 개인 기능과 같이 고르기는 로그인 없이 이용하며 카카오 로그인은 원하는 경우 기록을 이어 쓰는 선택 기능으로 유지한다.
- 같은 카카오 계정에 여러 UUID 사용자가 연결되면 가장 먼저 생성된 UUID 사용자를 대표로 삼고 기록을 합치는 방향을 확인했다. 대표는 최초 로그인 순서가 아닌 사용자 생성 시점을 기준으로 한다.
- `GLOSSARY.md`에 `카카오 계정 연결`과 `대표 사용자`를 정의하고, 기본 이용 흐름과 선택적 계정 연결의 결정을 ADR에 기록했다. 닉네임 변경 가능·중복 금지 원칙은 유지한다.

#### 주요 변경 파일

- `GLOSSARY.md`
- `docs/adr/0001-uuid-users-with-optional-kakao-linking.md`
- `LOG.md`

#### API 변경

- 없음. 설계 문서만 반영했으며 UUID 사용자 생성·카카오 연결·사용자 병합은 아직 구현하지 않았다.

#### DB 변경

- 없음

#### 검증

- 기존 인증 및 사용자 계약과 새 설계 방향을 대조했다. 용어집과 ADR이 로그인 필수 조건을 두지 않고, 카카오 로그인을 선택적 계정 연결로 설명하는지 확인했다.
- 빌드·테스트: 미실행. 설계 문서만 변경했으며 실행 코드 변경 없음.

#### 남은 작업 및 주의 사항

- 최초 카카오 연결 시 프로필 선택, 재로그인과 닉네임 변경의 우선순위, 기록·제외 조건 및 진행 중인 방의 병합 충돌 처리를 구체화해야 한다.
- 기존 카카오 사용자 전환과 브라우저 인증 유지·갱신 계약을 정한 후 최종 합의를 확인하고 구현 범위를 확정한다.

### #39 카카오 로그인 전체 제거 및 UUID 사용자 식별 방향으로 정정

#### 변경 이유 및 내용

- 사용자가 앞선 답변을 정정하여 같이 고르기를 포함한 모든 기능에서 카카오 로그인을 제거하고 UUID로 사용자를 구분하는 방향을 확정했다. 아래의 카카오 로그인 유지·계정 연결 계획은 이 결정으로 대체된다.
- 같은 브라우저에서는 기록과 표시 닉네임을 유지하고, 다른 기기 또는 브라우저 데이터 삭제 후에는 새 사용자로 시작하는 범위를 확인했다.
- `GLOSSARY.md`에서 게스트·카카오 로그인 사용자의 구분을 제거하고 `사용자`로 통일했다. 최초 생성 후 유지하는 `표시 닉네임` 정의는 유지한다.
- 후속 답변에서 개인 추천·알레르기·비선호·식사 기록과 방 참여를 같은 UUID 사용자에게 연결하는 것으로 확정했다. 표시 닉네임은 단어 조합과 숫자로 생성하며, 이미 사용 중인 이름이면 다시 생성하는 방향에 동의했다.
- 첫 방문 시 별도 가입 없이 UUID와 닉네임을 자동 생성하는 방식과 사용자의 닉네임 변경 허용을 후속 답변에서 확인했다. `표시 닉네임` 정의에 변경 가능 여부를 반영했다.
- 사용자는 이후 UUID 이용 중 카카오 로그인으로 기록을 이어가고, 같은 카카오 계정에 여러 UUID가 연결되면 가장 먼저 생성된 UUID를 기준으로 프로필을 선택하고 데이터를 합치는 흐름을 요청했다. 앞선 전체 로그인 제거 결정과 충돌하므로 선택적 카카오 로그인 유지 여부를 재확인 중이다. 병합 및 프로필 선택의 세부 규칙은 아직 확정하지 않았다.

#### 주요 변경 파일

- `GLOSSARY.md`
- `LOG.md`

#### API 변경

- 없음. 설계 변경을 기록했으며 카카오 API 제거와 UUID 인증은 아직 구현하지 않았다.

#### DB 변경

- 없음

#### 검증

- 용어집과 기존 설계 기록을 대조하여 정정된 사용자 구분과 이전 결정의 대체 관계를 확인했다.
- 용어집에 단일 사용자 주체와 표시 닉네임의 중복 금지 조건을 반영했다. PowerShell 용어 확인 및 `git diff --check` 통과.
- 빌드·테스트: 미실행. 용어·작업 기록만 수정했으며 실행 코드 변경 없음.

#### 남은 작업 및 주의 사항

- 선택적 카카오 로그인 유지 여부를 먼저 재확인하고, 기존 사용자 전환 및 여러 UUID의 병합·프로필 우선순위·인증 계약을 구체화해야 한다.
- 전체 설계 합의 후 카카오 필수 사용자 구조, HTTP·WebSocket 인증, 프로필 API·문서·테스트 및 기존 데이터의 전환 방식을 함께 반영한다.

### #39 게스트 이용·카카오 로그인·표시 닉네임 용어 정리 시작

#### 변경 이유 및 내용

- `grill-with-docs` 설계 대화에서 일반 이용은 카카오 로그인 없이 UUID 기반으로 사용자를 구분하고, 같이 고르기에는 카카오 로그인을 유지하는 방향을 확인했다.
- 처음 한 번 무작위로 생성하고 같은 사용자에게 유지하는 이름을 `표시 닉네임`으로 정의했다. `게스트 사용자`, `카카오 로그인 사용자`와 함께 `GLOSSARY.md`에 기록했다.
- 현재 코드는 사용자 생성에 카카오 ID를 요구하며 재로그인 시 카카오 닉네임을 동기화한다. 위 방향은 새 설계이며 실행 코드에는 아직 적용하지 않았다.

#### 주요 변경 파일

- `GLOSSARY.md`
- `LOG.md`

#### API 변경

- 없음. 설계 논의 중이며 기존 API 계약을 유지한다.

#### DB 변경

- 없음

#### 검증

- 현재 인증·사용자·방 코드와 프로필 계약을 읽어 카카오 필수 사용자 구조, 닉네임 동기화, 정수 사용자 ID 의존성을 확인했다.
- PowerShell로 합의한 용어 3개와 용어집의 후행 공백 여부 확인 통과. `git diff --check` 통과.
- 빌드·테스트: 미실행. 용어 문서만 추가했으며 실행 코드 변경 없음.

#### 남은 작업 및 주의 사항

- 게스트 기록과 카카오 계정 연결, 게스트 재방문 식별 범위, 같이 고르기 인증 범위, 기존 회원 처리, UUID 및 인증 계약을 구체화해야 한다.
- 설계 대화와 최종 합의 확인이 끝난 뒤 구현 범위를 확정한다. 기존 프로필 문서의 카카오 닉네임 동기화 정책과 새 표시 닉네임 정책을 구현 시 함께 조정해야 한다.

## 2026-11-06

### 주문서 질문을 긍정형 문구와 예·아니요 답변으로 표시

#### 변경 이유 및 내용

- 저탄수·고단백 질문에 수치 범위를 직접 표시하던 선택지를 `예 / 아니요 / 상관없어요`로 통일해 읽기 쉽게 했다. ‘저탄수화물 식사를 하고 싶나요?’, ‘고기 요리를 먹고 싶나요?’처럼 긍정형 질문을 사용한다. 12개 주제의 204개 후보 전체에 적용해 상위 7개와 탐색 2개 모두 같은 표시 방식을 사용한다. 질문 최대 24자 제한을 유지하며 새 문구의 최대 길이는 23자다.
- 기존 질문 코드·ID, 답변 `option_id`, 조건 `rules`, 추천 점수 및 피드백 연결을 유지한다. 저탄수 질문의 예는 `low_carb`/15g 이하, 아니요는 `rich_carb`/15g 초과다. 고기 질문은 `meat`/`non_meat`다. `any`의 중립 처리도 유지한다.
- 응답 DTO를 만들 때 표준 질문 코드·주제·답변 ID·조건의 일치를 확인하고 문구와 표시 이름만 새 정의로 변환한다. 기존 DB를 재적재하거나 중요도를 초기화하지 않아도 배포·재시작 후 새 표시를 사용할 수 있다. 같은 조건의 이전 LLM 질문은 주제별 표준 긍정형으로 표시하고, 조건이 수동 변경됐거나 알 수 없는 질문은 기존 문구를 유지해 의미가 바뀌지 않게 한다.
- 신규 시드·공유 JSON도 새 표시를 사용한다. LLM 질문 생성 안내에 예가 첫 번째 조건에 해당하는 긍정형 및 숫자 기준 비노출을 명시했다. 이번 변경에서 메뉴 데이터 파일은 수정하지 않았다.

#### 주요 변경 파일

- `app/recommend/question_pool.py`
- `app/recommend/order_sheet.py`
- `scripts/seed_menus.py`
- `data/order_sheet_questions_pool.json`
- `tests/test_binary_questions.py`, `tests/test_question_presentation.py`
- `docs/RECOMMENDATION_VARIETY.md`, `LOG.md`

#### API 변경

- `GET /api/recommendations/order-sheet/questions`: `question_text`를 긍정형으로, `options[].label`을 예·아니요·상관없어요로 표시한다. 기존 응답 JSON 구조와 답변 ID·조건 필드는 유지한다.
- `POST /api/recommendations/order-sheet/weights`: `top_questions`에도 같은 표시를 적용한다. 요청 형식·가중치 계산 변경은 없다.
- `POST /api/recommendations/order-sheet`: 계약 변경 없음. 프론트는 버튼의 표시 글자가 아닌 해당 선택지의 기존 `option_id`를 보낸다. `예`/`아니요`나 공통 `yes`/`no` 값을 새 답변 ID로 사용하지 않는다.

#### DB 변경

- 없음. 테이블·컬럼·제약 조건·마이그레이션 파일 변경과 실제 DB 갱신은 없다. DB 저장 문구까지 갱신하려는 경우 기존 `--questions-only` 시드를 선택적으로 사용할 수 있다.

#### 검증

- `.\.venv\Scripts\python.exe -m pytest -q tests/test_question_presentation.py tests/test_binary_questions.py tests/test_order_sheet_contract.py tests/test_question_selection.py`: 38개 통과. 숫자 경계·누락 정보, 고기 분류, 기존 DB 표시 변환과 비변경, 수동 조건·알 수 없는 코드 보호, 이전 LLM 긍정형 변환, HTTP 조회 후 기존 ID로 예·아니요·상관없음 제출 및 추천 점수, 재시드 시 사용 중인 ID·중요도 보존을 검증했다.
- `.\.venv\Scripts\python.exe -m pytest -q -rs`: 199개 통과, 1개 건너뜀. MySQL 방 통합 테스트는 `MYSQL_TEST_DATABASE_URL` 미설정으로 실행하지 않았다. 기존 경고 2건 유지(Starlette AnyIO, 요청 alias 메타데이터).
- 공유 JSON을 작업 전 HEAD와 비교해 204개 모두 질문 코드·주제·답변 ID·조건·초기 점수·중요도가 동일하고 표시 문구만 바뀐 것을 확인했다.
- `git diff --check`: 통과.

#### 남은 작업 및 주의 사항

- 수정한 백엔드를 배포·재시작하고 프론트의 질문 목록을 다시 조회해야 한다. 프론트는 `question_text`, `options[].label`을 표시하고 기존 `option_id`를 보내야 한다. 실제 프론트 코드는 이번 작업 폴더에 없어 화면 검증은 미실행이다.
- 운영 MySQL 및 실제 외부 LLM 호출, 원격 배포는 미실행이다. 검증은 인메모리 SQLite와 모의 LLM 응답을 사용했다. 커밋·푸시는 수행하지 않았다.

### 엔지니어링 스킬용 GitHub 이슈 트래커·triage 라벨·도메인 문서 규칙 초기 설정

#### 변경 이유 및 내용

- 설치된 엔지니어링 스킬들이 동일한 이슈 트래커와 도메인 문서 규칙을 사용하도록 `setup-matt-pocock-skills` 절차에 따라 사용자가 승인한 설정을 적용했다.
- 이슈와 명세는 `pknu-wap/WHATBOB_BE`의 GitHub Issues에서 관리하며, 여러 줄 본문은 `--body-file`로 전달하고 PR의 triage 요청 처리 설정은 기본값 `no`를 유지한다.
- 기본 triage 역할 5개를 문서화하고 GitHub에 없던 `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human` 라벨을 생성했다. 기존 `wontfix`와 다른 라벨은 유지했다.
- 단일 컨텍스트의 루트 `GLOSSARY.md`와 `docs/adr/` 읽기 규칙을 정의했다. 용어집과 ADR은 실제 용어·결정이 정리될 때 생성하도록 두었다.
- 기존 `AGENTS.md`에 `Agent skills` 블록과 설정 문서 포인터를 추가했다. 초기 설정 과정에서 GitHub CLI를 설치하고 인증된 저장소 접근을 확인했다.

#### 주요 변경 파일

- `AGENTS.md`
- `docs/agents/issue-tracker.md`
- `docs/agents/triage-labels.md`
- `docs/agents/domain.md`
- `LOG.md`

#### API 변경

- 없음

#### DB 변경

- 없음

#### 검증

- `gh repo view --json nameWithOwner,url,hasIssuesEnabled`: 대상 저장소와 GitHub Issues 활성화 확인.
- GitHub 라벨 생성 후 `gh label list --repo pknu-wap/WHATBOB_BE --limit 100 --json name`: 기본 triage 라벨 5개 존재 확인.
- PowerShell 검증: `Agent skills` 블록 단일 존재, 설정 파일 3개와 포인터, 역할·라벨 매핑 5개, 저장소 이름과 PR 설정 확인 통과.
- `git diff --check` 및 새 설정 파일 3개의 `git diff --no-index --check`: 공백 오류 없음.
- 애플리케이션 빌드·테스트: 미실행. 에이전트 설정 문서와 GitHub 라벨만 변경하여 실행 코드·API·DB 영향 없음.

#### 남은 작업 및 주의 사항

- 설정은 `docs/agents/*.md`에서 직접 수정할 수 있다. 이슈 트래커 변경 또는 초기 설정 재시작 시 스킬을 다시 실행한다.
- 작업 전부터 존재하던 `LOG.md`, `tests/test_user_meals.py`, `.agents/skills/`, `skills-lock.json` 변경은 보존했다.

### 사용자별 식사 기록(user_meals) 저장 및 조회 API 추가

#### 변경 이유 및 내용

- 기존 백엔드에는 주문서 추천 만족도 피드백(`recommendation_feedbacks`)만 있었고, 주문서·완전 랜덤·방 추천 결과를 아울러 사용자가 실제로 먹은 음식을 저장하고 최신순으로 조회하는 테이블과 API가 없었다.
- 신규 `user_meals` 테이블(`UserMeal` 엔티티)을 추가해 사용자별 식사 일시(`eaten_at`), 메뉴 참조(`menu_id`), 저장 시점의 표시 메뉴명 스냅샷(`menu_name`)을 기록하도록 구현했다.
- `POST /api/users/me/meals`로 본인이 먹은 음식 `menu_id`와 선택적 `eaten_at`(ISO-8601, 생략 시 현재 UTC 시각)을 저장하고, 존재하지 않는 메뉴 ID 요청은 `FoodNotFoundException`(`404 Not Found`)으로 공통 오류 응답을 반환한다.
- `GET /api/users/me/meals`로 본인의 식사 기록을 `eaten_at DESC, id DESC` 최신순으로 페이지네이션(`limit`, `offset`) 조회하며, `menus`와의 단일 `LEFT OUTER JOIN`으로 카테고리·조리 특성·영양성분 DTO를 함께 반환해 N+1 조회를 방지했다. 메뉴 원본이 삭제돼도 `menu_id`만 `NULL`로 전환되고 `menu_name` 기록은 보존된다.

#### 주요 변경 파일

- `app/menus/models.py`
- `app/recommend/common.py`
- `app/users/schemas.py`, `app/users/repository.py`, `app/users/service.py`, `app/users/router.py`
- `tests/test_user_meals.py`
- `LOG.md`

#### API 변경

- 추가: `POST /api/users/me/meals` (`201 Created`, Bearer JWT 필수) — 요청 `{ "menu_id": int, "eaten_at"?: string }`, 응답 `ApiResponse[UserMealItemResponse]`.
- 추가: `GET /api/users/me/meals` (`200 OK`, Bearer JWT 필수, `Cache-Control: no-store`) — 쿼리 `limit`(기본 50, 1~200), `offset`(기본 0), 응답 `ApiResponse[UserMealsResponse]` (`items`, `total_count`).

#### DB 변경

- 신규 테이블 `user_meals`: `id`(PK), `user_id`(`users.id` FK, `ON DELETE CASCADE`), `menu_id`(`menus.id` FK, `ON DELETE SET NULL`, nullable), `menu_name`(`VARCHAR(200)`), `eaten_at`(`DATETIME`), `created_at`(`DATETIME`), 복합 인덱스 `ix_user_meals_user_eaten_at`(`user_id`, `eaten_at`). 서버 기동 시 `app/main.py`의 `Base.metadata.create_all(bind=engine)`로 자동 생성된다.

#### 검증

- `.venv\Scripts\python.exe -m compileall -q app scripts tests` 및 `git diff --check`: 통과.
- 인메모리 SQLite 기반 직접 실행 검증으로 빈 목록 조회(`Cache-Control: no-store`), 식사 기록 저장 및 타임존(`+09:00`·`Z`) 정규화 최신순 정렬, `FoodNotFoundException`(`404`), 메뉴 삭제 시 `SET NULL` 스냅샷 유지 및 사용자 삭제 시 `CASCADE`, 단일 `OUTER JOIN` 쿼리 수(N+1 방지), 입력 검증(`ValidationError`), DB 오류 롤백을 확인했다.
- 샌드박스 내 루프백 소켓 제한으로 `TestClient`를 사용하는 전체 `pytest` 실행은 로컬 터미널 또는 CI에서 추가 확인이 필요하다.

#### 남은 작업 및 주의 사항

- 로컬 터미널(샌드박스 외부) 또는 CI 환경에서 `.venv\Scripts\python.exe -m pytest -q tests/test_user_meals.py` 및 전체 테스트 스위트 실행 결과를 확인한다.
- 운영 MySQL 배포 시 서버 재기동 후 `user_meals` 테이블과 `ix_user_meals_user_eaten_at` 인덱스가 정상 생성되었는지 확인한다.

## 2026-11-05

### 음식 종류별 동일 확률 완전 랜덤 추천 API 추가

#### 변경 이유 및 내용

- 기존 랜덤 초안은 메뉴 행 전체에서 바로 하나를 뽑아 데이터가 많은 피자 같은 종류의 선택 확률이 높아지고, 프론트가 호출할 API와 DTO가 없었다.
- 알레르기·섭취 불가·비선호 및 요청별 추가 제외 조건을 먼저 적용한 뒤 `menu_group` 기준 음식 종류를 동일 확률로 하나 뽑고, 선택된 종류 안에서 실제 메뉴 하나를 다시 무작위 선택하도록 변경했다. 햄버거/버거, 돈까스/돈가스, 리조또/리소토는 같은 종류로 취급한다.
- 선택적 JWT 인증을 적용해 로그인 사용자는 본인의 DB 제외 조건을 자동으로 사용하고, 익명 사용자는 요청의 추가 제외 조건만 사용한다. 클라이언트가 다른 사용자 ID를 지정하는 입력은 제공하지 않는다.
- 화면 표시명과 메뉴 속성, 제외·후보·음식 종류 수를 공통 응답 형식으로 반환한다. 완전 랜덤 기능은 주문서 점수, 최근 추천, 피드백을 사용하거나 추천 이력을 저장하지 않는다.

#### 주요 변경 파일

- `app/recommend/random_pick.py`
- `app/recommend/router.py`, `app/recommend/schemas.py`
- `tests/test_random_pick.py`
- `LOG.md`

#### API 변경

- 추가: `POST /api/recommendations/random` — 요청 `{ "exclusions": [] }`, 응답 `data.selected_menu`, `excluded_count`, `candidate_count`, `menu_type_count`. Bearer JWT는 선택 사항이며 유효한 토큰이 있으면 해당 사용자의 제외 조건을 자동 적용한다.

#### DB 변경

- 없음. 기존 `menus`, `user_food_exclusions` 테이블을 조회하며 추천 이력은 생성하지 않는다.

#### 검증

- `python -m pytest -q tests/test_random_pick.py -x`: 5개 통과. 종류 우선 선택, 메뉴명 동의어 그룹, 사용자·요청 제외 조건, 후보 없음 오류, 인증 사용자의 API 응답을 검증했다.
- `python -m pytest -q -rs`: 135개 통과, 1개 건너뜀. MySQL 테스트 DB 환경 변수가 없어 기존 방 동시 참가 통합 테스트를 건너뛰었고 기존 경고 2건이 발생했다.

#### 남은 작업 및 주의 사항

- 실제 전체 메뉴 데이터와 운영 MySQL에서 응답 시간 및 종류 분포를 확인해야 한다. 현재 구현은 식사 메뉴를 조회한 후 Python에서 제외 조건과 그룹을 계산한다.
### 일반 참여자 직접 퇴장 및 WebSocket 접속 상태 유예 처리

#### 변경 이유 및 내용

- 일반 참여자가 방 전체를 종료하지 않고 본인만 나갈 수 있도록 퇴장 API를 추가했다. 인증 사용자와 경로의 사용자 ID가 같고 방장이 아닐 때만 퇴장할 수 있으며, 해당 참여자의 주문서를 먼저 삭제한 뒤 참여자 행을 삭제한다.
- 참여자 퇴장 후 남은 인원과 완료 인원을 다시 계산한다. 미완료 참여자가 나가 남은 참여자가 모두 완료 상태가 되면 `all_members_completed=true`로 응답하고 `MEMBER_LEFT` 이벤트에도 같은 집계 결과를 포함한다.
- `room_members`에 온라인 여부와 마지막 연결 종료 시각을 저장한다. 일반 참여자의 마지막 WebSocket 연결이 끊기면 연결 종료 시각을 기록하고 60초 동안 온라인 상태를 유지한다. 유예 시간 안에 재접속하면 예약을 취소하고, 미복귀 시 오프라인으로 전환해 `MEMBER_OFFLINE` 이벤트를 전송한다. 오프라인 참여자의 멤버십과 주문서는 유지한다.
- 오프라인 상태에서 다시 연결하면 온라인으로 복구하고 `MEMBER_ONLINE` 이벤트를 전송한다. 여러 WebSocket 중 마지막 연결이 종료됐을 때만 유예를 시작하며, 서버 재시작으로 인메모리 예약이 사라진 경우에도 주기적 정리 작업이 DB의 종료 시각을 기준으로 오프라인 전환을 복구한다.
- 방장의 기존 60초 방 종료 유예와 1시간 방 만료·삭제 동작은 유지했다. 방장 WebSocket 상태도 방 상세 응답의 접속 상태 필드와 일치하도록 갱신한다.

#### 주요 변경 파일

- `app/rooms/models.py`, `app/rooms/schemas.py`
- `app/rooms/service.py`, `app/rooms/router.py`, `app/rooms/websocket.py`
- `app/rooms/cleanup.py`, `app/core/config.py`, `.env.example`
- `scripts/migrate_room_lifecycle.py`
- `tests/test_rooms.py`, `tests/test_room_deletion.py`, `LOG.md`

#### API 변경

- 추가: `DELETE /api/rooms/{room_id}/members/{user_id}` — 일반 참여자가 본인 ID로만 호출할 수 있다. 방장은 호출할 수 없고 기존 `DELETE /api/rooms/{room_id}`를 사용해야 한다. 성공 응답은 `room_id`, `user_id`, `deleted`, `member_count`, `completed_member_count`, `all_members_completed`를 포함한다.
- 수정: `GET /api/rooms/{room_id}`의 참여자 항목에 `is_online`, `disconnected_at`을 추가한다. 단순 오프라인 참여자도 완료 인원 계산에 계속 포함한다.
- 추가 WebSocket 이벤트: `room.updated`의 `MEMBER_OFFLINE`, `MEMBER_ONLINE`, `MEMBER_LEFT` 사유를 추가했다. `MEMBER_LEFT`에는 퇴장 후 인원 및 완료 집계를 포함한다.

#### DB 변경

- `room_members.is_online`: 필수 온라인 여부 컬럼을 `FALSE` 기본값으로 추가.
- `room_members.disconnected_at`: nullable 마지막 WebSocket 연결 종료 시각 추가.
- `scripts/migrate_room_lifecycle.py`가 기존 MySQL의 `room_members` 테이블에도 두 컬럼을 추가하도록 확장됐다. 신규 DB는 `Base.metadata.create_all`로 새 구조를 생성한다.

#### 검증

- `python -m pytest -q tests/test_rooms.py tests/test_room_deletion.py -x`: 39개 통과. 직접 퇴장 권한, 방장 퇴장 거부, 주문서 삭제, 완료 상태 재계산, 퇴장 이벤트, 접속 유예, 재접속 취소, 오프라인 데이터 유지 및 정리 작업 복구를 검증했다.
- `python -m pytest -q -rs`: 181개 통과, 1개 건너뜀. `MYSQL_TEST_DATABASE_URL`이 없어 MySQL 동시 참가 통합 테스트는 실행하지 않았다. 기존 경고 2건이 발생했다.
- `python -m compileall -q app scripts tests`, `git diff --check`: 통과.

#### 남은 작업 및 주의 사항

- 배포 전 기존 MySQL에서 `python -m scripts.migrate_room_lifecycle`를 실행하고 새 컬럼 추가를 확인해야 한다. 이번 자동화 테스트는 인메모리 SQLite를 사용했다.
- WebSocket 연결과 유예 예약은 애플리케이션 프로세스별 메모리에 있으므로 여러 서버 인스턴스의 즉시 이벤트 전달을 일관되게 하려면 Redis 같은 공유 상태 및 메시지 계층이 필요하다. DB 기반 정리 작업은 누락된 오프라인 전환을 보정한다.

### 방장 퇴장·연결 종료 및 1시간 만료 기반 방 자동 삭제

#### 변경 이유 및 내용

- 종료 수단이 없어 방과 참여자 데이터가 계속 남던 문제를 해결했다. 방장만 `DELETE /api/rooms/{room_id}`로 즉시 종료할 수 있으며, 종료 상태와 사유를 먼저 저장하고 연결된 참여자에게 `room.closed` 이벤트를 보낸 뒤 주문서·참여자·방 데이터를 순서대로 삭제한다.
- 방 생성 시 설정 가능한 1시간 기본 유효기간을 저장하고, 서버 수명주기에 주기적인 정리 작업을 연결했다. 만료된 방, 방장의 재접속 유예 시간이 지난 방, 이전 삭제 실패로 `CLOSED`에 남은 방을 다시 정리한다. 일반 API 요청에서도 만료 여부를 확인해 사용을 차단한다.
- 방장의 마지막 WebSocket 연결이 종료되면 시각을 DB에 기록하고 기본 60초 동안 재접속을 기다린다. 유예 시간 내 재접속하면 예약과 DB 표시를 취소하며, 일반 참여자 연결 종료나 전체 주문서 완료는 방 삭제 조건으로 사용하지 않는다.
- DB 종류와 외래키 실행 설정에 관계없이 `room_member_order_sheets`, `room_members`, `rooms` 순서로 명시적으로 삭제한다. 동시 종료 처리는 방 행 잠금과 `CLOSED` 중간 상태로 직렬화하며, 중단된 작업은 정리 루프가 재처리한다.
- 기존 MySQL `rooms` 테이블에 생명주기 컬럼과 만료 인덱스를 추가하는 일회성 마이그레이션 스크립트를 제공한다.

#### 주요 변경 파일

- `app/rooms/models.py`, `app/rooms/schemas.py`
- `app/rooms/service.py`, `app/rooms/router.py`, `app/rooms/websocket.py`
- `app/rooms/cleanup.py`, `app/main.py`
- `app/core/config.py`, `.env.example`
- `scripts/migrate_room_lifecycle.py`
- `tests/test_room_deletion.py`, `LOG.md`

#### API 변경

- 추가: `DELETE /api/rooms/{room_id}` — 방장만 호출할 수 있다. 성공 시 `room_id`, `deleted`, `close_reason=HOST_LEFT`, `closed_at`을 반환하고 관련 데이터를 삭제한다. 비방장은 403, 없는 방은 404다.
- 수정: `POST /api/rooms` 응답에 `expires_at`을 추가한다. 기본값은 생성 시각으로부터 3,600초 뒤이며 환경 변수로 조정할 수 있다.
- 수정: `GET /api/rooms/{room_id}` 응답에 `expires_at`을 추가한다. 만료됐지만 아직 정리 주기가 실행되지 않은 방은 410으로 차단하고 `EXPIRED` 종료 상태를 기록한다.
- 추가 WebSocket 이벤트: `room.closed` — `room_id`, `reason`, `closed_at`을 포함하며 이벤트 전송 후 해당 방 연결을 닫는다.

#### DB 변경

- `rooms.expires_at`: 필수 만료 시각 및 인덱스 추가.
- `rooms.closed_at`: nullable 종료 시각 추가.
- `rooms.close_reason`: nullable 종료 사유(`HOST_LEFT`, `HOST_DISCONNECTED`, `EXPIRED`) 추가.
- `rooms.host_disconnected_at`: nullable 방장 연결 종료 시각 추가.
- 기존 MySQL에는 배포 전 `python -m scripts.migrate_room_lifecycle`를 한 번 실행해야 한다. 신규 DB는 `Base.metadata.create_all`로 새 구조를 생성한다.

#### 검증

- `python -m pytest -q tests/test_rooms.py tests/test_room_deletion.py -x`: 31개 통과. 기존 방 생성·참여·조회·주문서·WebSocket 회귀와 방장 삭제, 비방장 거부, 연관 데이터 삭제, 만료 차단, 재접속 취소, 유예 만료, 전체 선택 완료 시 방 유지 동작을 검증했다.
- `python -m pytest -q -rs`: 130개 통과, 1개 건너뜀. `MYSQL_TEST_DATABASE_URL`이 없어 MySQL 동시 참가 통합 테스트는 실행하지 않았다. 기존 경고 2건이 발생했다.
- `python -m compileall -q app scripts tests`, `git diff --check`: 통과.

#### 남은 작업 및 주의 사항

- 실제 MySQL에서 마이그레이션 스크립트와 만료·연쇄 삭제를 검증해야 한다. 이번 테스트는 인메모리 SQLite를 사용했다.
- WebSocket 연결 상태는 애플리케이션 프로세스 메모리에 있으므로 여러 서버 인스턴스에서 정확한 실시간 접속 수가 필요하면 Redis 같은 공유 연결 상태 저장소가 필요하다. DB에 기록된 연결 종료 시각과 정리 작업은 서버 재시작 후에도 동작한다.
- 프론트는 명시적 방 나가기에 DELETE API를 사용하고 `room.closed` 수신 시 방 화면을 종료해야 한다. 페이지 새로고침은 재접속 유예 시간 안에 WebSocket을 다시 연결해야 한다.

### 누락된 식사 후보 복구, 추천 종류 분산 및 질문 순환·상관없음 복원

#### 변경 이유 및 내용

- `--meals-only`에서 빵류를 통째로 제외해 햄버거·피자·샌드위치·핫도그·토스트·또띠아 등이 빠지던 문제를 수정했다. 식사용 빵류 기본 이름을 구분해 `main_meal`로 포함하고 온도 속성을 따뜻한 요리로 반영한다. 음료·케이크·과자까지 일괄 복구하지 않는다. 기존 메뉴 재시드 시 ID를 유지하고 원본 기반 속성도 갱신한다.
- 같은 점수에서 작은 메뉴 ID가 계속 이기던 정렬을 제거했다. 기본 메뉴 종류를 먼저 구분해 한 종류는 결과에 하나만 포함하고, 비슷한 점수의 종류·상품을 추첨한다. 버거/햄버거 등 동의어는 같은 종류로 취급한다. 피자 상품 수가 많아도 개수만으로 결과를 독점하지 않는다.
- 식별된 사용자의 최근 3회 추천 종류와 개별 메뉴의 최신 싫어요를 다음 추천의 순위 패널티로 반영한다. 이후 같은 메뉴에 좋아요를 주면 싫어요 패널티를 해제한다. 기존 알레르기·섭취 불가·명시적 비선호 제외 조건은 먼저 적용하고 해제하지 않는다.
- 탐색 질문을 사용자 JWT 또는 익명 브라우저 쿠키별로 순환한다. 직전 탐색 주제를 가능한 범위에서 피하고 덜 노출된 주제부터 추첨한다. 서버가 재시작돼도 DB 상태를 사용한다. 질문 GET 시 노출 횟수를 증가시키고 추천 제출 시 이중 증가는 제거했다. 상위 7개+탐색 2개와 요청 안의 주제 중복 방지는 유지한다.
- 두 취향 선택지에 `any`/`상관없어요`를 세 번째로 추가했다. 질문 문구 최대 24자, 기존 취향 선택지 ID와 질문 코드·ID는 유지한다. `any`는 메뉴 점수를 올리지 않고 질문 중요도 학습에서도 제외한다. 이전 두 선택지 DB도 재시드 전 조회·제출이 가능하도록 보완했다.
- 추천 관련 API에 선택적 서비스 JWT 연동을 추가했다. 토큰이 있으면 사용자 ID를 자동 연결하고 본문에 다른 사용자 ID가 오면 403이다. 토큰 없는 기존 요청은 유지한다. 피드백 메뉴는 해당 추천 결과에 포함되어야 하며 추천과 피드백 사용자가 다르면 거부한다.
- 공유 메뉴 JSON/CSV와 질문 JSON을 재생성했다. 식사 후보 파일은 기존 2,320개에서 7,503개로 늘었고 질문은 204개다. DB나 외부 API에 접근하지 않는 재생성 스크립트와 프론트·배포 연동 문서를 추가했다.

#### 주요 변경 파일

- `app/recommend/order_sheet.py`, `app/recommend/variety.py`
- `app/recommend/question_pool.py`, `app/recommend/schemas.py`, `app/recommend/router.py`
- `app/menus/models.py`, `app/auth/dependencies.py`
- `scripts/seed_menus.py`, `scripts/export_recommend_data.py`
- `data/menus_meals_only.json`, `data/menus_meals_only.csv`, `data/order_sheet_questions_pool.json`
- `tests/test_recommend_variety.py`, `tests/test_question_selection.py`, `tests/test_binary_questions.py`
- `tests/test_menu_display.py`, `tests/test_order_sheet_contract.py`, `tests/test_recommend.py`
- `docs/RECOMMENDATION_VARIETY.md`, `LOG.md`

#### API 변경

- `GET /api/recommendations/order-sheet/questions`: `options`에 세 번째 `any` 추가, 응답은 `Cache-Control: no-store`. JWT 사용자 또는 `question_session` 쿠키별 탐색 순환 상태를 갱신하고 실제 조회 노출을 집계한다. 다른 출처에서 익명 쿠키를 유지하려면 프론트 `credentials: include`가 필요하다.
- `POST /api/recommendations/order-sheet`: 기본 종류별 최대 하나, 비슷한 점수의 후보 분산 및 사용자별 최근 노출·싫어요 반영. 종류 부족 시 `top_k`보다 적게 반환할 수 있다. `match_score`는 패널티 전의 답변 일치 점수이며 결과 순서와 단순 내림차순이 일치하지 않을 수 있다. JWT 사용 시 본문 `user_id` 생략 가능.
- `POST /api/recommendations/{recommendation_id}/feedback`: 추천에 없는 메뉴·사용자 불일치 400. `any`는 질문 가중치 학습에서 제외하지만 메뉴에 대한 사용자 평가는 저장한다. JWT 사용 시 본문 `user_id` 생략 가능.
- 기존 JSON 키, 응답 래퍼, 정상 HTTP 상태 및 중요도 alias는 유지한다. 유효하지 않은 Bearer 토큰은 401, JWT와 본문 사용자 불일치는 403이다.

#### DB 변경

- 추가 테이블 `question_selection_states`: `scope_key` 문자열 기본키, `last_clusters` JSON, `topic_counts` JSON, `updated_at` 날짜시간. 기존 프로젝트의 `Base.metadata.create_all`로 생성하며 별도 마이그레이션 파일은 없다.
- 기존 테이블 구조 변경 없음. 시드 실행 시 기존 메뉴 ID·추천·피드백 참조를 유지하면서 메뉴를 추가·갱신하고 질문 `options_json`에 `any`를 추가한다. 운영 DB에는 아직 반영하지 않았다.

#### 검증

- `.\.venv\Scripts\python.exe -m scripts.export_recommend_data`: 7,503개 메뉴와 204개 질문 공유 파일 생성 완료. 실제 CSV를 읽고 DB·외부 API는 사용하지 않았다.
- `.\.venv\Scripts\python.exe -m pytest -q tests/test_recommend_variety.py tests/test_question_selection.py`: 24개 통과. 메뉴 누락 복구, 기존 메뉴 재시드 시 속성 갱신·ID 유지, 종류별 분산·노출, 큰 점수 차이 유지, 최근 추천·사용자별 싫어요·좋아요 해제, `any` 중립·학습 제외·이전 두 선택지 DB 호환, 제외 조건과 후보 없음, 잘못된 피드백, JWT 사용자 연결 및 401/403, 탐색 주제 순환·DB 지속·브라우저 쿠키·주제 부족을 검증했다.
- `.\.venv\Scripts\python.exe -m pytest -q -rs`: 113개 통과, 1개 건너뜀. MySQL 방 통합 테스트는 `MYSQL_TEST_DATABASE_URL` 미설정으로 건너뛰었다. 기존 경고 2건 유지(Starlette AnyIO, 요청 alias 메타데이터).
- 별도 인메모리 SQLite에 실제 전체 CSV 시드: 메뉴 7,503개와 질문 204개 적재 성공. `any` 제출 시 서로 다른 기본 메뉴 5개 반환을 확인했다.
- `git diff --check`: 통과.

#### 남은 작업 및 주의 사항

- 운영 환경에서 `python -m scripts.seed_menus --meals-only --no-llm`으로 메뉴·질문 갱신과 신규 상태 테이블 생성 후 백엔드 재시작이 필요하다. `--questions-only`만으로는 빠진 메뉴가 복구되지 않는다. 기존 DB에 이미 있는 음료·디저트를 임의로 삭제하지 않는다.
- 프론트는 세 선택지 전체를 렌더링하고 질문 API를 새로 호출해야 한다. 로그인 사용자는 질문·추천·피드백에 서비스 JWT를 보내고 익명 질문은 쿠키를 유지한다. 자세한 절차는 `docs/RECOMMENDATION_VARIETY.md`에 기록했다.
- 운영 MySQL·실제 프론트 화면·동시 요청 검증 및 원격 배포는 미실행. 현재 검증 DB는 인메모리 SQLite다. 장기간 운영 시 오래된 익명 질문 상태 정리 정책을 검토한다. 커밋·푸시는 수행하지 않았다.

## 2026-11-04

### 로그인 후 사용자 프로필 조회 API 추가 및 카카오 프로필 필드 처리 보완

#### 변경 이유 및 내용

- 로그인은 성공하지만 닉네임·프로필 사진이 화면에 표시되지 않는 현상을 조사했다. 로그인 응답에는 이미 `user` 객체가 있었으나 `app/users/router.py`는 비어 있어 로그인 후 저장된 본인 정보를 다시 조회하는 API가 없었다. 실제 프론트 코드는 작업 폴더에 없어 화면의 데이터 저장·조회 경로까지는 확인하지 못했다.
- 기존 방 API와 같은 `get_current_user` 인증 의존성을 사용해 `GET /api/users/me`를 추가하고 실제 앱에 라우터를 등록했다. 서비스 JWT의 사용자만 조회하고 Entity 대신 DTO를 반환한다.
- 사용자 응답 DTO를 `app/users/schemas.py`로 옮겨 로그인과 내 정보 조회가 공유하도록 했다. 기존 `app.auth.schemas.UserResponse` import와 로그인 응답 키는 유지한다.
- 카카오 사용자 정보 요청에 `secure_resource=true`를 추가해 HTTPS 이미지 URL을 요청한다. 최신 `kakao_account.profile`을 우선 읽고, 프로필 이미지가 없으면 썸네일 및 기존 `properties` 형식을 보완적으로 읽는다. 누락되거나 null인 프로필 객체도 처리한다.
- 카카오 사용자 ID가 없거나 유효하지 않으면 잘못된 사용자 저장 대신 민감한 정보를 포함하지 않는 502 오류를 반환한다.
- 기존 사용자는 재로그인 시 전달받은 프로필로 갱신하며, 카카오 응답에서 빠진 항목으로 기존 정보를 지우지 않는 동작을 유지한다. 사용자 동의로 제공되지 않은 정보를 임의로 생성하지 않는다.
- 로그인 응답과 내 정보 응답의 필드 위치, 인증 헤더 및 동의항목 확인 방법을 `docs/USER_PROFILE_API.md`에 기록했다. 카카오 공식 REST API 문서의 HTTPS 이미지 요청 및 프로필 동의항목을 확인했다.

#### 주요 변경 파일

- `app/auth/kakao.py`
- `app/auth/schemas.py`
- `app/users/router.py`
- `app/users/schemas.py`
- `app/main.py`
- `tests/test_user_profile.py`
- `docs/USER_PROFILE_API.md`
- `LOG.md`

#### API 변경

- 추가: `GET /api/users/me` — `Authorization: Bearer <서비스 JWT>`로 인증한 본인 정보를 200으로 반환한다. 본문은 `id`, `kakao_id`, `nickname`, `email`, `profile_image`를 담는 사용자 객체 자체이며 별도 `data`/`user` 래퍼는 없다. 토큰 누락·무효·만료 또는 사용자 부재 시 기존 인증 규칙에 따라 401을 반환한다.
- `POST /api/auth/kakao/login`, `GET /api/auth/kakao/callback`: 기존 응답 형식을 유지한다. 카카오 프로필 대체 필드와 HTTPS 이미지 요청을 적용하며, 카카오 사용자 ID가 유효하지 않은 비정상 응답은 502로 처리한다.
- 로그인 응답의 화면용 필드는 `user.nickname`, `user.profile_image`; 내 정보 조회 응답은 `nickname`, `profile_image`다.

#### DB 변경

- 없음. 테이블·컬럼·제약 조건 및 마이그레이션 파일 변경 없음. 기존 `users` 테이블을 사용하며 실제 DB에 직접 접근하거나 데이터를 갱신하지 않았다.

#### 검증

- `.\.venv\Scripts\python.exe -m pytest -q tests/test_user_profile.py tests/test_auth.py tests/test_cors.py tests/test_rooms.py`: 46개 통과.
- 신규 테스트 22개: 최신/기존 프로필 필드, 썸네일, 필드 우선순위, 누락·null, HTTPS 요청 파라미터, 비정상 카카오 ID, 실제 앱에 등록된 내 정보 경로, 로그인 후 JWT 조회 및 CORS, 재로그인 동기화·기존 값 보존, 타 사용자 ID로 본인 조회 대체 불가, 토큰 누락·무효·만료·사용자 부재·잘못된 subject·잘못된 인증 방식의 401 처리를 검증했다.
- `.\.venv\Scripts\python.exe -m pytest -q -rs`: 100개 통과, 1개 건너뜀. MySQL 방 통합 테스트는 `MYSQL_TEST_DATABASE_URL` 미설정으로 건너뛰었다. 기존 경고 2건 유지(Starlette AnyIO, 요청 alias 메타데이터). 사용자 DTO를 ConfigDict로 옮겨 기존 Pydantic 설정 경고는 해소했다.
- `git diff --check`: 통과.

#### 남은 작업 및 주의 사항

- 카카오 실제 로그인, MySQL 및 프론트 화면 수동 검증, 원격 배포는 미실행. 외부 카카오 API는 모의 응답, DB는 인메모리 SQLite로 검증했다.
- 프론트 저장소 위치를 요청했으며 아직 확인하지 못했다. 화면이 로그인 응답의 `user`를 저장하고 새로고침 시 서비스 JWT로 `/api/users/me`를 조회하는지 실제 호출부 확인이 필요하다. 화면 표시 문제의 완전한 해결 여부는 아직 확인하지 못했다.
- 카카오에서 정보 자체가 제공되지 않으면 앱의 프로필 닉네임·프로필 사진 동의항목 설정과 실제 사용자 동의를 확인해야 한다. 과거에 비어 있던 프로필은 재로그인으로 갱신할 수 있다.
- DB 재적재는 필요 없으며 백엔드 재시작·배포 후 신규 API가 제공된다.

### 상위 질문과 탐색 질문 전체에서 같은 주제 중복 제거

#### 변경 이유 및 내용

- 기존 질문 선택은 이미 사용한 질문 ID만 탐색 후보에서 제외해서 같은 `cluster_key`의 다른 문구가 상위 질문과 탐색 질문에 함께 포함될 수 있었다. 탐색 질문 두 개 사이에서도 같은 주제가 반복될 수 있었다.
- 질문 ID 대신 주제를 기준으로 선택한다. 상위 질문은 서로 다른 주제의 중요도 최상위 질문으로 구성하고, 탐색 후보에서는 상위 질문의 모든 주제를 제외한다. 탐색 질문을 하나 뽑을 때마다 그 주제의 나머지 문구도 후보에서 제외한다.
- 반환 상한을 활성 질문 수가 아닌 활성 주제 수로 계산하고, 주제가 부족하면 반복 질문으로 채우던 처리를 제거했다. 기본 7+2 구성을 유지하되 활성 주제가 8개이면 7+1개, 7개 이하이면 가능한 상위 질문만 반환한다.
- 탐색 질문의 중요도·사용 횟수 기반 가중 추출과 `random_seed` 재현성은 유지한다. 기존 메뉴 표시명 변경도 유지했다.

#### 주요 변경 파일

- `app/recommend/order_sheet.py`
- `app/recommend/router.py`
- `tests/test_question_selection.py`
- `LOG.md`

#### API 변경

- `GET /api/recommendations/order-sheet/questions`: 상위·탐색 질문 전체의 `cluster_key`가 중복되지 않는다. 활성 주제가 요청 개수보다 적으면 실제 반환 개수가 줄어들고 `total_count`, `exploitation_count`, `exploration_count`는 실제 개수를 나타낸다.
- 경로, 요청·응답 JSON 키, HTTP 상태 코드 및 기본 요청 개수 변경 없음. API 설명에 활성 주제 수에 따른 반환 상한을 명시했다.

#### DB 변경

- 없음. 테이블·컬럼·제약 조건, 마이그레이션 파일 및 저장 데이터 변경 없이 조회 시 선택 로직만 수정했다.

#### 검증

- `.\.venv\Scripts\python.exe -m pytest -q tests/test_question_selection.py tests/test_binary_questions.py tests/test_recommend.py tests/test_order_sheet_contract.py`: 29개 통과, 기존 경고 2건 유지.
- 신규 테스트 11개: 20개 시드에서 상위 7개·탐색 2개의 ID 및 주제 중복 없음, 동일 시드 재현성, 활성 주제 수 1·7·8·9·12개와 요청 개수 경계, 탐색만으로 여러 주제를 채우는 경우, 비활성 질문·주제 제외 및 HTTP 개수 일치, 빈 질문 풀의 기존 도메인 오류를 검증했다.
- 기존 추천·피드백·알레르기 제외·두 선택지·24자 제한 및 API/DB alias 호환성 테스트도 통과했다.
- `git diff --check`: 통과.

#### 남은 작업 및 주의 사항

- 프론트는 고정 9개 대신 응답의 실제 질문 개수로 표시해야 한다. 현재 12개 주제가 활성화된 데이터에서는 기본 7+2개를 반환한다.
- 실제 MySQL 및 프론트 화면을 통한 수동 확인, 원격 배포는 미실행. DB 재적재 없이 백엔드 재시작·배포로 적용한다.

### 추천 메뉴의 원본 구분자 이름을 화면 표시명으로 정리

#### 변경 이유 및 내용

- 식품 데이터의 `김밥_김치`, `닭튀김_땡초 치킨` 같은 원본 이름이 추천 화면에 그대로 표시되는 문제를 해결했다.
- 응답을 만들 때 `format_menu_name()`으로 표시명을 생성한다. `김밥_김치`는 `김치김밥`, `볶음밥_계란`은 `계란볶음밥`으로 조합한다.
- `국밥_순대국밥`처럼 뒷부분이 완성된 음식명이거나 `닭튀김_땡초 치킨`, `스파게티_베이컨 크림 파스타`처럼 상품명이 별도로 있는 경우 중복 분류를 제거한다.
- `간편조리세트` 구분은 정리하고 실제 상품명을 표시한다. 조합을 확신하기 어려운 재료·양념·조리 상태는 `두부찌개 (소고기·된장)`, `갈비탕 (소금제외)`처럼 괄호로 보존한다.
- DB의 원본 이름과 기본 이름은 유지하고, 추천 후보 필터링·점수·순위·추천 기록은 기존 데이터를 사용한다.

#### 주요 변경 파일

- `app/menus/display.py`
- `app/recommend/order_sheet.py`
- `app/recommend/schemas.py`
- `tests/test_menu_display.py`
- `LOG.md`

#### API 변경

- `POST /api/recommendations/order-sheet`: `data.recommended_menus[].name`을 원본 이름 대신 정리한 화면 표시명으로 반환한다. 응답 키·타입, `menu_id`, `food_code`, `base_name` 및 점수는 유지한다.
- `POST /api/recommendations/{recommendation_id}/feedback`: 계약 변경 없음. 정리된 메뉴 이름 대신 기존과 같이 메뉴 ID로 피드백을 전송한다.

#### DB 변경

- 없음. 테이블·컬럼·제약 조건과 마이그레이션 파일, 저장된 메뉴 데이터 및 공유 원본 데이터 변경 없음. 재적재 없이 백엔드 재시작·배포로 적용한다.

#### 검증

- `.\.venv\Scripts\python.exe -m pytest -q tests/test_menu_display.py`: 25개 통과. 원본 이름 형식·공백·빈 구분자·중복 음식명·상품명·복수 재료·조리 상태를 검증하고 실제 공유 메뉴 2,320개의 비어 있지 않은 표시명과 구분자 제거를 확인했다.
- HTTP 추천 응답에서 자연스러운 이름, 동일한 메뉴 ID·음식 코드·기본 이름·점수 및 추천 기록을 확인했다. 표시명에서 빠진 원본 분류명으로도 제외 필터가 동작하고 기존 메뉴 ID로 피드백이 저장되는지 검증했다. DB 원본 이름이 유지되는지도 확인했다.
- `.\.venv\Scripts\python.exe -m pytest -q -rs`: 67개 통과, 1개 건너뜀. MySQL 방 통합 테스트는 `MYSQL_TEST_DATABASE_URL` 미설정으로 건너뛰었다. 기존 경고 3건 유지.
- `git diff --check`: 통과.

#### 남은 작업 및 주의 사항

- 실제 MySQL과 프론트 화면을 통한 수동 확인 및 원격 배포는 미실행. 이름의 완성 여부를 확신하기 어려운 형식은 의미 손실을 피하기 위해 기본 음식명과 세부 내용을 괄호로 함께 표시한다.

### 주문서 질문을 두 선택지와 최대 24자 문구로 변경

#### 변경 이유 및 내용

- 프론트에서 질문마다 답변 버튼 두 개를 표시할 수 있도록 12개 주제의 질문 후보 204개를 두 선택지로 구성했다. 재료·조리법처럼 기존 선택지가 많던 주제는 고기류/그 외 재료, 밥·면/그 외 조리 형태 등으로 묶어 기존 메뉴 분류가 빠지지 않게 했다. `상관없어요` 선택지는 새 질문에서 제거했다.
- 공백·문장부호 포함 질문 문구 길이를 최대 24자로 제한했다. 응답 DTO에도 문구 1~24자 및 선택지 정확히 두 개 제약을 적용했다. 내장 질문의 실제 최대 길이는 17자다.
- 질문 후보 정의를 `app/recommend/question_pool.py`에 모으고 공유 JSON을 재생성했다. 초기 점수와 중요도 계산, 기본 질문 수 9개(7+2), 기본 추천 수 5개는 기존 방식을 유지한다.
- 선택적 LLM 생성에도 두 선택지와 24자 제한을 안내하고, 제한을 넘는 문구는 잘라 쓰지 않고 후보에서 제외한다. 외부 호출 없이 모의 응답으로 검증했다.
- 과거 질문의 선택지 의미를 바꿔 기존 피드백 계산이 달라지는 일을 방지하기 위해 새 질문 코드 `q_binary_v1_...`를 사용한다. 시드 실행 시 이전 질문은 삭제·덮어쓰기 없이 비활성화하고 새 질문을 추가한다.
- 기존 메뉴나 테이블 구조를 건드리지 않고 질문만 갱신하는 `--questions-only` 옵션을 추가했다. 이 옵션은 LLM을 호출하지 않는다.

#### 주요 변경 파일

- `app/recommend/question_pool.py`
- `app/recommend/schemas.py`
- `scripts/seed_menus.py`
- `data/order_sheet_questions_pool.json`
- `tests/test_binary_questions.py`
- `tests/test_recommend.py`
- `tests/test_order_sheet_contract.py`
- `LOG.md`

#### API 변경

- `GET /api/recommendations/order-sheet/questions`: 질문별 `options`가 정확히 두 개, `question_text`가 공백 포함 최대 24자가 된다. 새 질문 ID·코드와 선택지 ID는 조회 응답에서 읽어 사용해야 한다.
- `POST /api/recommendations/order-sheet/weights`: `top_questions`에도 같은 문구·선택지 제약을 적용한다.
- 경로, 공통 응답 JSON 키와 중요도 alias(`info_gain_score`, `ml_weight`)는 유지한다. 추천·피드백 요청 형식 변경 없음.

#### DB 변경

- 테이블·컬럼·제약 조건 및 마이그레이션 파일 변경 없음.
- 질문 갱신 시 `order_sheet_questions`에 새 질문·선택지·초기 점수·중요도를 저장하고 이전 질문의 `is_active`를 `false`로 변경한다. 기존 추천·피드백이 참조하는 이전 질문 ID와 선택지 조건은 보존한다.
- 실제 로컬 MySQL 반영을 시도했으나 계정 인증 오류(1045)로 실패했다. 실제 DB 데이터는 갱신하지 못했으며, 위 전환 동작은 인메모리 SQLite에서 검증했다.

#### 검증

- `.\.venv\Scripts\python.exe -m pytest -q -rs`: 42개 통과, 1개 건너뜀. MySQL 방 통합 테스트는 `MYSQL_TEST_DATABASE_URL` 미설정으로 건너뛰었다. 기존 경고 3건(Starlette AnyIO, 인증 DTO Pydantic 설정, 요청 alias 메타데이터)이 남아 있다.
- 추가 검증: 전체 CSV 메뉴 분류의 두 선택지 커버리지, 영양 수치 경계에서 조건 중복·누락 없음, 24자 허용/25자 거부, 선택지 1개·3개 거부, 공유 JSON 일치, LLM 초과 문구 제외, 두 버튼 각각의 HTTP 추천·피드백, 이전 추천 피드백 보존, 재시드 중복 방지 및 사용된 질문 중요도 유지.
- `.\.venv\Scripts\python.exe -m scripts.seed_menus --questions-only`: 실행했으나 로컬 MySQL 계정 인증 오류(1045)로 실패. 실제 MySQL 반영 및 원격 배포 미완료.
- `git diff --check`: 통과.

#### 남은 작업 및 주의 사항

- DB 인증 설정을 해결한 뒤 `.\.venv\Scripts\python.exe -m scripts.seed_menus --questions-only`를 실행해야 실제 질문 조회에 반영된다. 배포 환경에서는 `python -m scripts.seed_menus --questions-only`를 실행한다.
- 새 응답 DTO는 두 선택지·24자 제약을 검사하므로 기존 DB를 사용하는 환경은 질문 갱신 후 새 백엔드로 전환해야 한다. 프론트에 저장된 이전 질문 대신 질문 목록을 새로 조회해야 한다.
- 기존 질문·피드백은 보존되지만 새 질문의 중요도는 새 초기 점수부터 시작한다. 외부 LLM 실제 호출과 원격 배포는 미실행이며 커밋·푸시는 수행하지 않았다.

### 식약처 법정 19대 알레르기 유발물질 표준화 및 조회 API 추가

#### 변경 이유 및 내용

- 「식품 등의 표시·광고에 관한 법률 시행규칙 [별표 2]」 기준 대한민국 식약처 고시 19대 알레르기 유발물질(알류, 우유, 메밀, 땅콩, 대두, 밀, 고등어, 게, 새우, 돼지고기, 복숭아, 토마토, 아황산류, 호두, 닭고기, 쇠고기, 오징어, 조개류, 잣)을 공식 표준 목록 및 감지 사전으로 정의했다.
- 프론트엔드 알레르기/섭취불가 선택 화면(체크박스/칩) 렌더링에 사용할 수 있도록 `GET /api/recommendations/allergens` 엔드포인트를 추가했다.
- `scripts/seed_menus.py`의 `_derive_allergens`를 고도화하여 2,320개 메뉴 파싱 시 19대 법정 유발물질을 정밀하게 추출해 `menus.allergen_tags`에 적재하도록 연동했다. 기존 "해산물" 등 상위 호환 키워드도 보존했다.

#### 주요 변경 파일

- `app/recommend/common.py`
- `app/recommend/schemas.py`
- `app/recommend/router.py`
- `scripts/seed_menus.py`
- `tests/test_recommend.py`
- `LOG.md`

#### API 변경

- `GET /api/recommendations/allergens`: 신규 추가 (200 OK, 식약처 법정 19대 알레르기 유발물질 목록 및 설명 반환)

#### DB 변경

- 없음 (`menus.allergen_tags` JSON 컬럼 구조 그대로 호환)

#### 검증

- `.\.venv\Scripts\python.exe -m pytest tests/test_recommend.py`: 7개 테스트 케이스 전체 통과 (`7 passed in 1.87s`).
- 19종 법정 알레르기 유발물질 반환 검증 및 제외 필터링 테스트 정상 검증 완료.

#### 남은 작업 및 주의 사항

- 오라클 클라우드 서버 배포 후 새 알레르기 태그 반영을 위해 `python -m scripts.seed_menus --meals-only` 재실행 권장.

## 2026-11-03

### 주문서 추천의 명칭을 샤논 엔트로피 초기 점수와 사용자 피드백 통계 기반 알고리즘으로 정정

#### 변경 이유 및 내용

- 기존 주석의 ML 학습·클러스터링·L2 정규화 로지스틱 학습 표현이 실제 구현과 달라 정정했다. 초기 질문 점수는 선택지별 메뉴 분포의 샤논 엔트로피와 커버리지로 계산하고, 미리 지정된 주제의 반복 정도로 초기 중요도를 보정한다.
- 사용자 피드백 이후에는 조건 일치/불일치 그룹의 만족도를 평활화하고, 만족도 차이 등을 시그모이드로 변환한 결과와 초기 점수를 후기 수에 따른 비율로 혼합한다. 계산식과 추천 순위 기준은 변경하지 않았다.
- 내부 이름을 `calculate_question_entropy_score`, `recalculate_question_weights`, `initial_score`, `importance_weight`, `recalculate_from_feedback`, `answer_match_results`, `feedback_importance`, `feedback_ratio` 등 실제 역할에 맞게 변경하고 호출부·시드 스크립트·테스트를 함께 수정했다.
- 이전 기록의 ML·재학습 표현은 당시 기록으로 보존하며, 현재 구현에 대한 설명은 이번 정정 내용을 따른다. 선택적인 LLM 질문 생성 기능은 그대로 유지한다.

#### 주요 변경 파일

- `app/recommend/order_sheet.py`
- `app/recommend/router.py`
- `app/recommend/schemas.py`
- `app/menus/models.py`
- `scripts/seed_menus.py`
- `tests/test_recommend.py`
- `tests/test_order_sheet_contract.py`
- `LOG.md`

#### API 변경

- 기존 경로·응답 JSON 키·기본값은 유지한다. 질문 조회 및 중요도 조정 응답의 `info_gain_score`, `ml_weight`는 호환용 alias로 유지하고 설명을 정정했다.
- `POST /api/recommendations/order-sheet/weights`: 기존 `adjustments[].ml_weight`, `run_ml_retrain` 요청을 계속 지원하며 새 이름 `adjustments[].importance_weight`, `recalculate_from_feedback`도 허용한다. 내부 Python 속성명만 변경했다.

#### DB 변경

- 스키마·데이터 변경 및 마이그레이션 없음. 새 ORM 속성을 기존 `order_sheet_questions.info_gain_score`, `order_sheet_questions.ml_weight`, `recommendation_feedbacks.feature_match_vector` 컬럼에 명시적으로 매핑한다.

#### 검증

- `.\.venv\Scripts\python.exe -m pytest -v`: 18개 통과. 기존 추천·인증·CORS 테스트와 신규 HTTP 호환성 테스트 2개를 실행했다.
- 신규 테스트는 기존/새 요청 키 모두 처리되는지, 기존 응답 키가 유지되는지, 새 ORM 속성으로 기존 이름의 DB 컬럼을 읽고 쓰는지 인메모리 SQLite에서 확인했다.
- `git diff --check`: 통과.
- 경고 3건: Starlette의 AnyIO deprecated alias, 기존 인증 DTO의 Pydantic 설정 방식, FastAPI/Pydantic의 요청 필드 alias 메타데이터 경고. 테스트에서 실제 기존/새 요청 키의 처리는 모두 통과했다.

#### 남은 작업 및 주의 사항

- 기존 DB 컬럼·API JSON 키·공유 질문 JSON 파일에는 호환성을 위해 이전 이름이 남아 있다. 이 이름까지 제거하려면 프론트엔드 계약 변경과 DB 마이그레이션을 별도로 진행해야 한다.
- 실제 MySQL 및 외부 LLM 호출은 이번 작업에서 미실행. 커밋·푸시는 수행하지 않았다.

## 2026-10-30

### 식약처 100g 기준 칼로리를 실제 한 끼(1인분, 700~1,100kcal 중심) 기준으로 환산 및 주문서 무게감(`heaviness`) 기준 개편

#### 변경 이유 및 내용

- 식약처 원본 CSV(`data/foods_nutrition.csv`)의 `에너지(kcal)`가 `100g`/`100ml` 기준 수치여서 메뉴 칼로리가 100~230kcal 수준으로 낮게 저장되던 문제를 해결하기 위해, `식품중량`(1인분 중량)과 한 끼 식사 구성(단품 반찬·국·찌개의 공깃밥·기본찬 동반 식사량)을 반영해 실제 **한 끼(1인분) 칼로리**(`_estimate_meal_kcal`)로 환산하도록 변경했다 (정규 식사 2,320건 평균 약 `769.2kcal`).
- 한 끼 식사 칼로리 기준에 맞춰 무게감(`heaviness`) 분류 임계값과 주문서 `heaviness` 클러스터 질문/선택지 문구를 현실적인 한 끼 기준으로 개편했다:
  - `light`: `< 700 kcal` ("가볍고 부담 없는 저칼로리 한 끼 (700kcal 미만)", 967건)
  - `moderate`: `700 ~ 950 kcal` ("적당히 든든한 일반 한 끼 식사 (700~950kcal)", 623건)
  - `heavy`: `>= 950 kcal` ("묵직하고 기름진 고칼로리·치팅데이 한 끼 (950~1,100kcal 이상)", 730건)
- `main_ingredient` 키워드 매칭 시 `"가오리"`가 `"오리"`(`poultry`)에 오매칭되던 문제를 수정하여 해산물(`seafood`)로 정확히 분류되도록 개선했다.
- 로컬 MySQL(`watbab` 스키마) 자동 생성(`_ensure_mysql_database_exists`) 및 재실행 시 기존 `menus`·`order_sheet_questions` 레코드의 칼로리·속성·엔트로피 점수를 갱신하도록 `seed_database`를 보완하고, MySQL 적재 및 공유 데이터 파일(`data/menus_meals_only.csv`, `data/menus_cleaned_all.csv`, `data/menus_meals_only.json`, `data/order_sheet_questions_pool.json`) 갱신을 완료했다.

#### 주요 변경 파일

- `scripts/seed_menus.py`
- `data/menus_meals_only.csv`, `data/menus_cleaned_all.csv`, `data/menus_meals_only.json`, `data/order_sheet_questions_pool.json`
- `LOG.md`

#### API 변경

- 없음 (`GET /api/recommendations/order-sheet/questions` 응답 내 `heaviness` 클러스터 선택지 라벨 및 `POST /api/recommendations/order-sheet` 추천 메뉴의 `kcal`, `heaviness` 데이터 값만 현실 한 끼 기준으로 갱신)

#### DB 변경

- 스키마 변경 없음. 로컬 MySQL `watbab` 데이터베이스의 `menus`(2,320건) 및 `order_sheet_questions`(204건) 데이터 적재·갱신 완료.

#### 검증

- `.\.venv\Scripts\python.exe -m scripts.seed_menus --meals-only`: 로컬 MySQL `watbab` DB에 메뉴 2,320건 및 질문 204건 갱신 완료.
- `.\.venv\Scripts\python.exe -m pytest -v tests/test_recommend.py`: 6개 테스트 모두 통과 (`6 passed in 1.60s`).

#### 남은 작업 및 주의 사항

- 없음

## 2026-10-28

### CSV 기반 메뉴 시드 적재 및 LLM·ML 주문서(Order Sheet) 추천 시스템 구현

#### 변경 이유 및 내용

- 식약처 식품영양성분 CSV(`data/foods_nutrition.csv`, 19,738건)를 파싱·정제하여 정규화 속성(`meal_type`, `has_soup`, `cooking_style`, `main_ingredient`, `flavor_intensity`, `temperature`, `heaviness`, `allergen_tags`)과 영양 성분을 추출하고 MySQL에 적재하는 시드 파이프라인을 구현했다.
- 12개 의미 클러스터에 걸쳐 CSV 컬럼 조건식(`rules`)과 매핑된 200개 이상의 주문서 질문 후보 풀(LLM 추가 생성 연동 포함)을 구성했다.
- 초기 피드백 데이터가 0건일 때 음식 분포의 정규화 샤논 엔트로피(Information Gain)와 클러스터 다양성 감쇠를 통해 질문 중요도(`info_gain_score`, `ml_weight`)를 산출하도록 구현했다.
- 주문서 질문 조회 시 가중치 최상위 7개(활용, 서로 다른 클러스터 우선)와 후보 탐색 질문 2개(Exploration)를 합쳐 총 9개의 질문을 반환함으로써 탈락 후보 질문도 피드백을 받아 재학습될 수 있도록 했다.
- `AGENTS.md` 도메인 규칙에 따라 추천 시 사용자의 알레르기·섭취불가·비선호 음식을 가장 먼저 제외(Hard Filter)하고, 후보가 없을 때 제외 조건을 임의로 해제하지 않고 `NoAvailableFoodException`(404)을 반환하도록 구현했다.
- 개발팀 사전 피드백 튜닝 및 실사용자 만족/불만족 피드백 누적 시 질문별 매칭 여부 벡터(`feature_match_vector`)를 바탕으로 가중치(`ml_weight`)를 재학습해 MySQL에 갱신하는 로직을 추가했다.

#### 주요 변경 파일

- `app/menus/models.py`
- `app/recommend/schemas.py`
- `app/recommend/common.py`
- `app/recommend/order_sheet.py`
- `app/recommend/router.py`
- `app/main.py`
- `app/core/config.py`, `.env.example`
- `scripts/seed_menus.py`
- `data/foods_nutrition.csv`
- `tests/test_recommend.py`
- `LOG.md`

#### API 변경

- 추가: `GET /api/recommendations/order-sheet/questions` — 가중치 상위 7개 + 탐색 2개(기본 총 9개) 주문서 질문지 반환 (`ApiResponse[OrderSheetQuestionsResponse]`).
- 추가: `POST /api/recommendations/order-sheet` — 주문서 응답 제출 시 알레르기·비선호 제외 후 가중치 합산 점수로 상위 메뉴 추천 및 이력 저장 (`ApiResponse[OrderSheetRecommendResponse]`).
- 추가: `POST /api/recommendations/{recommendation_id}/feedback` — 추천 결과 만족/불만족 피드백 저장 및 질문 중요도 가중치 자동 재학습 (`ApiResponse[RecommendationFeedbackResponse]`).
- 추가: `POST /api/recommendations/order-sheet/weights` — 사전 튜닝용 질문 가중치 수동 조정 및 일괄 ML 재학습 실행 (`ApiResponse[OrderSheetWeightTuneResponse]`).

#### DB 변경

- 테이블 추가 (`Base.metadata.create_all` 기반):
  - `menus`: 식품코드, 메뉴명, 대·중·소분류, 정규화 속성 7종, 영양성분 6종, `allergen_tags`(JSON)
  - `user_food_exclusions`: 사용자별 `ALLERGY` / `UNABLE` / `DISLIKE` 키워드
  - `order_sheet_questions`: 질문 코드, 클러스터 키, 질문 문구, 선택지·조건식(`options_json`), `info_gain_score`, `ml_weight`, 노출/긍정 피드백 카운트
  - `recommendations`: 사용자 주문서 응답 및 추천 결과 메뉴 ID 이력
  - `recommendation_feedbacks`: 추천 만족 여부(`is_satisfied`) 및 질문별 매칭 벡터(`feature_match_vector`)

#### 검증

- `.venv/Scripts/python.exe -m pytest -v tests/test_recommend.py`: 6개 통과 (2.78s). CSV 파싱 및 200+ 질문 풀 생성, 7+2 탐색·활용 9개 질문 추출, 알레르기·비선호 선제 제외(Hard Filter), 후보 부재 시 404 도메인 예외 처리, 피드백 누적 시 ML 가중치 재학습, 개발팀 가중치 수동 튜닝을 인메모리 SQLite로 검증했다.
- 실제 외부 MySQL 서버 적재 및 외부 LLM API 네트워크 호출은 미실행. 로컬 MySQL 실행 후 `python -m scripts.seed_menus`로 실제 DB 적재를 수행해야 한다.

#### 남은 작업 및 주의 사항

- 로컬 또는 배포 환경의 MySQL 서버가 실행 중일 때 `.venv/Scripts/python.exe -m scripts.seed_menus`를 1회 실행하여 `menus` 및 `order_sheet_questions` 테이블에 초기 데이터를 적재한다.
- LLM 추가 질문 생성을 사용하려면 `.env`에 `LLM_API_KEY`를 설정한다 (미설정 시 내장 200+ 질문 풀로 동작).

### Vercel 카카오 콜백 주소 적용 및 CORS 허용 설정

#### 변경 이유 및 내용

- 사용자 요청에 따라 기본 설정, 로컬 `.env`, `.env.example`의 `KAKAO_REDIRECT_URI`를 `https://whatbobfe-app.vercel.app/auth/callback`으로 변경했다.
- 로그인 URL 생성과 카카오 토큰 교환은 기존의 동일한 `settings.KAKAO_REDIRECT_URI` 참조를 유지하므로 두 요청에 같은 Vercel 주소가 적용된다.
- 앞선 롤백 이후 CORS 설정을 다시 적용했다. Vercel Origin과 로컬 `localhost:8081`, `127.0.0.1:8081`을 허용하며 자격 증명 허용은 유지한다.
- 표준 HTTP 메서드 및 `Authorization`, `Content-Type`, `ngrok-skip-browser-warning` 요청 헤더를 명시했다. 다른 Origin 및 사용자 정의 헤더는 허용 목록에 추가해야 한다.

#### 주요 변경 파일

- `app/core/config.py`
- `.env` (Git 제외), `.env.example`
- `app/main.py`
- `tests/test_auth.py`, `tests/test_cors.py`
- `LOG.md`

#### API 변경

- `GET /api/auth/kakao/login/url`: 반환하는 로그인 URL의 `redirect_uri`가 Vercel 콜백 주소로 변경됐다. 필수 state 계약은 유지한다.
- `POST /api/auth/kakao/login`: 카카오 토큰 교환의 `redirect_uri`가 같은 Vercel 주소로 변경됐다. 요청·응답 DTO는 유지한다.
- CORS 사전 요청 및 실제 응답에 위 Origin과 요청 헤더를 허용한다. 경로 추가·삭제는 없다.

#### DB 변경

- 없음.

#### 검증

- `.venv/Scripts/python.exe -m pytest -q tests/test_auth.py tests/test_cors.py`: 10개 통과. 로그인 URL과 모의 토큰 교환의 Vercel 주소, state 처리, 코드 전달, Origin 3개의 사전 요청, ngrok·인증·JSON 헤더, 미등록 Origin 거부 및 실제 응답을 검증했다. 기존 사용 중단 예정 경고 2개 발생.
- 실제 로드한 설정의 Redirect URI 일치와 `.env`의 Git 제외 상태 확인.
- `git diff --check`: 통과.
- 실제 브라우저 로그인, ngrok 및 DB 연동은 미실행. 테스트는 외부 서비스와 DB에 연결하지 않았다.

#### 남은 작업 및 주의 사항

- 카카오 개발자 콘솔에 위 Redirect URI를 등록하고 백엔드를 재시작해야 한다. 별도 배포 환경에 기존 환경 변수가 있으면 같은 주소로 갱신해야 한다.
- 프론트는 ngrok API 요청에 `ngrok-skip-browser-warning` 헤더를 추가하고 콜백에서 state 검증 및 코드 전달을 수행해야 한다.
- 아래 롤백 및 로컬 콜백 기록은 과거 이력이며 현재 설정은 이 작업 내용을 따른다.

### Vercel 및 ngrok용 CORS 설정 변경 롤백

#### 변경 이유 및 내용

- 사용자 요청으로 직전 작업인 아래 CORS 명시 설정을 되돌렸다.
- `app/main.py`의 `allow_origins`, `allow_methods`, `allow_headers`를 기존 `["*"]`로 복원하고 `allow_credentials=True`는 유지했다.
- 해당 설정 전용으로 추가했던 `tests/test_cors.py`를 삭제했다. 2026-10-27 카카오 로그인 프론트 콜백 및 state 작업은 유지했다.

#### 주요 변경 파일

- `app/main.py`
- `tests/test_cors.py` (삭제)
- `LOG.md`

#### API 변경

- 경로 및 DTO 변경 없음. 명시적인 CORS Origin·헤더 제한을 해제하고 직전 작업 이전의 와일드카드 설정으로 복원했다.

#### DB 변경

- 없음.

#### 검증

- `.venv/Scripts/python.exe -m pytest -q tests/test_auth.py`: 5개 통과, 기존 사용 중단 예정 경고 2개 발생.
- `git diff --exit-code -- app/main.py`: 통과. 현재 HEAD와 동일하게 복원됨을 확인했다.
- `git diff --check`: 통과.
- 실제 브라우저 및 ngrok 연동은 미실행. DB 및 외부 서비스에 연결하지 않았다.

#### 남은 작업 및 주의 사항

- 실행 중인 백엔드는 재시작 또는 자동 재로딩 후 반영 여부를 확인한다.
- 아래 CORS 명시 설정 기록은 변경 이력이며 현재 코드에는 적용되지 않는다.

### Vercel 프론트와 ngrok 요청을 위한 CORS 설정 명시

#### 변경 이유 및 내용

- 자격 증명을 허용하는 CORS 설정의 와일드카드 Origin을 명시적인 목록으로 변경했다. `https://whatbobfe-app.vercel.app`, `http://localhost:8081`, `http://127.0.0.1:8081`을 허용한다.
- 요청 헤더는 `Authorization`, `Content-Type`, `ngrok-skip-browser-warning`을 명시하고 기존에 허용하던 표준 HTTP 메서드는 유지했다.
- 프론트는 ngrok으로 보내는 API 요청에 `ngrok-skip-browser-warning` 헤더를 직접 추가해야 한다.

#### 주요 변경 파일

- `app/main.py`
- `tests/test_cors.py`
- `LOG.md`

#### API 변경

- 경로 및 요청·응답 DTO 변경 없음. CORS 사전 요청과 실제 응답에서 위 Origin을 허용하며 목록 밖 Origin은 CORS 허용 대상에서 제외한다. 명시하지 않은 사용자 정의 요청 헤더도 CORS 허용 대상에서 제외한다.

#### DB 변경

- 없음.

#### 검증

- `.venv/Scripts/python.exe -m pytest -q tests/test_auth.py tests/test_cors.py`: 10개 통과. 허용 Origin 3개의 POST 사전 요청과 ngrok·인증·JSON 헤더 허용, 미등록 Origin 거부, Vercel Origin의 실제 GET 응답을 검증했다.
- 기존 의존성 및 Pydantic 사용 중단 예정 경고 2개 발생.
- `git diff --check`: 통과.
- 실제 Vercel 및 ngrok 경유 브라우저 요청은 미실행. 테스트는 외부 서비스와 DB에 연결하지 않았다.

#### 남은 작업 및 주의 사항

- 백엔드 재시작 또는 재배포 후 프론트에서 ngrok 헤더를 넣어 호출하고 브라우저 연동을 확인한다.
- 다른 프론트 Origin이나 사용자 정의 헤더가 필요하면 허용 목록에 추가해야 한다.

## 2026-10-27

### 프론트 콜백 주소로 카카오 로그인 연결 및 state 전달 추가

#### 변경 이유 및 내용

- Expo 프론트가 인가 코드를 받도록 기본 설정, 로컬 `.env`, 예시 설정의 Redirect URI를 `http://localhost:8081/auth/callback`으로 변경했다.
- 로그인 URL 생성 시 프론트가 만든 `state`를 필수 쿼리로 받아 카카오에 전달하고, URL 파라미터는 `urlencode`로 인코딩하도록 변경했다. state 생성·저장·콜백 검증은 프론트에서 수행하는 계약이다.
- 프론트는 콜백에서 받은 코드를 기존 POST 로그인 API로 전달한다. 기존 백엔드 GET 콜백은 호환성을 위해 유지하고 용도를 설명했다.

#### 주요 변경 파일

- `app/auth/router.py`
- `app/core/config.py`
- `.env` (Git 제외), `.env.example`
- `tests/test_auth.py`

#### API 변경

- 수정: `GET /api/auth/kakao/login/url`에 비어 있지 않은 `state` 쿼리 파라미터가 필수가 됐다. 누락·빈 문자열은 422이며 반환하는 로그인 URL에 state와 설정된 프론트 Redirect URI를 포함한다.
- `POST /api/auth/kakao/login`의 요청 `{code}`와 토큰 응답 계약은 유지한다.
- `GET /api/auth/kakao/callback`은 유지하지만 프론트 콜백 구성에서는 사용하지 않는다.

#### DB 변경

- 없음.

#### 검증

- `.venv/Scripts/python.exe -m pytest -q tests/test_auth.py`: 5개 통과. URL 및 특수문자 state 전달, state 누락·빈 값 거절, 프론트 코드의 서비스 전달, 토큰 요청의 Redirect URI를 검증했다. 외부 카카오 요청과 로그인 서비스는 모의 처리했다.
- 기존 의존성 및 Pydantic 설정 방식의 사용 중단 예정 경고 2개가 발생했다.
- 실행 설정의 Redirect URI 일치와 `.env`의 Git 제외 상태를 확인했다.
- `git diff --check`: 통과.

#### 남은 작업 및 주의 사항

- 카카오 개발자 콘솔에 프론트 콜백 주소를 등록하고 백엔드를 재시작해야 한다.
- 프론트 `web-auth`의 state 생성·저장·검증과 POST API 호출은 미확인이다. URL API 호출부는 state를 전달하도록 맞춰야 한다.
- 실제 브라우저 로그인과 DB 연동 테스트는 미실행이다. 이번 검증은 외부 서비스와 DB에 접속하지 않았다.

## 2026-10-05

### 카카오 로그인 로컬 인증 설정 적용

#### 변경 이유 및 내용

- 카카오 로그인 로컬 연동을 위해 전달받은 테스트용 REST API 키를 Git에서 제외된 `.env`에 설정했다. 키 값은 작업 기록과 예시 설정에 남기지 않았다.
- `.env`의 Redirect URI가 `http://localhost:8000/api/auth/kakao/callback`인지 확인했다.
- 애플리케이션 설정의 기본 Redirect URI도 이미 같은 주소여서 코드 변경은 필요하지 않았다.

#### 주요 변경 파일

- `.env`: 로컬 카카오 REST API 키 설정 (Git 제외)
- `LOG.md`: 설정 변경 기록

#### API 변경

- 없음. 기존 카카오 로그인 및 콜백 경로와 요청·응답 계약은 변경하지 않았다.

#### DB 변경

- 없음.

#### 검증

- `.env`의 키와 Redirect URI가 요청받은 값과 일치하는지 값 노출 없이 확인했다.
- `.env`의 Git 제외 상태를 확인했다.
- 애플리케이션 테스트: 미실행. 환경 설정과 예시 파일만 변경했다.

#### 남은 작업 및 주의 사항

- 실제 연동용 REST API 키와 Redirect URI가 확정되면 해당 환경의 설정을 갱신하고 카카오 개발자 콘솔의 등록 주소와 일치시키며 로그인 흐름을 확인한다.

## 2026-10-01

### 프로젝트 초기 설정

#### 변경 이유 및 내용

- WhatBap 백엔드 개발을 위한 규칙 문서 `AGENTS.md`를 작성했다.
- 계층별 책임, API 및 응답 설계, 예외 처리, 데이터베이스, 코드 작성, 테스트, 보안 및 변경 작업 규칙을 정리했다.
- 알레르기 / 섭취 불가능 / 비선호 음식 제외와 친구 선택 교집합 계산에 관한 도메인 규칙을 정의했다.
- 작업 시작 전 최근 기록을 확인하고, 의미 있는 변경 완료 후 기록하는 `LOG.md` 워크플로우를 추가했다.
- 이번 초기 설정 범위는 개발 규칙 및 기록 문서 작성이다. 애플리케이션 생성이나 기능 구현 완료를 의미하지 않는다.

#### 주요 변경 파일

- `AGENTS.md`: 백엔드 에이전트 개발 규칙 및 작업 기록 절차
- `LOG.md`: 개발 이력 및 초기 설정 기록

#### API 변경

- 없음. `AGENTS.md`의 API 경로는 설계 예시이며 실제 엔드포인트를 추가한 것은 아니다.

#### DB 변경

- 없음. DB 스키마 및 마이그레이션을 생성하지 않았다.

#### 검증

- 문서 확인: 개발 규칙, 작업 기록 절차, 초기 설정 날짜 및 두 파일의 참조 관계를 확인했다.
- 빌드 및 애플리케이션 테스트: 미실행. 이번 작업은 Markdown 문서 작성이며 애플리케이션 코드는 포함하지 않는다.

#### 계획된 주요 기능

아래는 개발 대상이며 구현 완료 목록이 아니다.

- 사용자 정보 및 음식 취향 설정
- 알레르기 / 비선호 / 섭취 불가능 음식 관리
- 음식 및 카테고리 관리
- 음식 추천 및 추천 결과·피드백 관리
- 식사 기록
- 친구와 같이 고르기: 방 생성·참여, 사용자별 선택 저장, 공통 후보 계산

#### 남은 작업 및 주의 사항

- 백엔드 기술 스택을 확정하고 프로젝트 기본 구조를 설정한다.
- 실제 빌드 및 테스트 명령을 확인해 `AGENTS.md`에 반영한다.
- DB 모델과 음식 / 카테고리 Entity를 설계한다.
- 추천 API와 공통 응답·예외 규격을 확정한다.
- 두 문서를 백엔드 루트에 함께 배치하고 실제 구현 진행에 맞춰 기록을 갱신한다.
