# 사용자 음식 제외·식사 기록·개인화 추천 API — T02

기준 설계: `docs/plans/39-design-baseline.md`, `docs/plans/39-t02-personal-data-architecture.md`, `docs/plans/39-final-design-choices.md`, `docs/plans/39-implementation-architecture.md`.

## 1. 인증 및 소유권 규칙

- 모든 개인 데이터·추천·피드백 API는 `Authorization: Bearer <access_token>`을 필수로 요구하며, 미인증 요청은 `401 Unauthorized`를 반환한다.
- 요청 본문이나 쿼리로 `user_id`를 전달하는 것은 허용하지 않는다(`extra="forbid"`, `422 Unprocessable Entity`).
- 다른 사용자의 추천(`recommendation_id`)이나 추천 결과에 포함되지 않은 메뉴(`selected_menu_id`)에 대한 피드백은 거절한다(`404 Not Found` 또는 `400 Bad Request`).

## 2. 카탈로그 조회 API

| 메서드·경로 | 설명 | 응답 (`ApiResponse`) |
| --- | --- | --- |
| `GET /api/recommendations/allergens` | 식약처 법정 19대 알레르기 유발물질 표준 카탈로그 조회 | `catalog_version`, `total_count`, `items` (`code`, `name`, `label`, `category`) |
| `GET /api/recommendations/unable-ingredients` | 섭취불가 8대 재료군 표준 카탈로그 조회 (`M2A`) | `catalog_version`, `total_count`, `items` (`code`, `name`, `label`, `category`, `description`) |

## 3. 본인 음식 제외 조건 관리 API

각 제외 유형(`ALLERGY`, `UNABLE`, `DISLIKE`)은 독립적으로 조회·전체 교체하며, 한 유형을 저장해도 다른 유형의 데이터는 변경되지 않는다.

| 메서드·경로 | 요청 본문 | 응답 및 동작 |
| --- | --- | --- |
| `GET /api/users/me/allergies` | 없음 | `CatalogExclusionsResponse` (`exclusion_type="ALLERGY"`, `catalog_version`, `editable`, `codes`, `items`, `legacy_items`, `total_count`) |
| `PUT /api/users/me/allergies` | `{"codes": ["egg", "milk"]}` | 카탈로그 표준 코드 배열로 전체 교체(`[]` 전달 시 전체 해제). 미등록 코드는 `400 INVALID_CATALOG_CODE`로 전체 거절·롤백 |
| `GET /api/users/me/unables` | 없음 | `CatalogExclusionsResponse` (`exclusion_type="UNABLE"`, `catalog_version`, `editable`, `codes`, `items`, `legacy_items`, `total_count`) |
| `PUT /api/users/me/unables` | `{"codes": ["pork", "seafood"]}` | 8대 재료군 표준 코드 배열로 전체 교체(`[]` 전달 시 전체 해제). 미등록 코드는 `400 INVALID_CATALOG_CODE`로 전체 거절·롤백 |
| `GET /api/users/me/dislikes` | 없음 | `DislikedFoodsResponse` (`keywords`, `items`, `total_count`) |
| `PUT /api/users/me/dislikes` | `{"keywords": ["고수", "민트초코"]}` | 자유 키워드 목록 전체 교체(최대 100개, 대소문자 무시 중복 제거) |
| `DELETE /api/users/me/dislikes/{dislike_id}` | 없음 | 본인 비선호 항목 단건 삭제 (`204 No Content`, 타인/미존재 항목은 `404`) |

### 레거시 미매핑 제외 항목 보호

- 기존 DB에 표준 카탈로그 코드로 매핑되지 않은 레거시 `ALLERGY`/`UNABLE` 행이 남아 있는 경우:
  - 추천 필터링에는 기존 키워드가 그대로 적용되어 안전하게 제외된다.
  - 조회 시 `editable: false`와 `legacy_items`로 반환된다.
  - 매핑 정리 전에 `PUT /api/users/me/allergies` 또는 `PUT /api/users/me/unables`를 호출하면 기존 행 손실을 막기 위해 `409 EXCLUSION_MAPPING_REQUIRED`를 반환한다.
- 배치 매핑 스크립트:
  ```text
  python -m scripts.map_legacy_exclusions --check
  python -m scripts.map_legacy_exclusions --apply --maintenance-confirmed
  ```

## 4. 식사 기록 API (`Idempotency-Key` 지원)

| 메서드·경로 | 헤더 / 요청 | 응답 및 동작 |
| --- | --- | --- |
| `GET /api/users/me/meals` | `limit` (1~200, 기본 50), `offset` (기본 0) | `eaten_at DESC, id DESC` 정렬된 식사 기록 목록 (`items`, `total_count`) |
| `POST /api/users/me/meals` | 선택 헤더 `Idempotency-Key` (`1~64자`), 본문 `{"menu_id": 1, "eaten_at": "..."}` | `201 Created`. 동일 `Idempotency-Key` + 동일 본문 재전송 시 기존 생성 결과를 멱등 반환하며, 다른 본문으로 재사용 시 `409 IDEMPOTENCY_KEY_REUSED` 반환 |
| `DELETE /api/users/me/meals/{meal_id}` | 없음 | 본인 식사 기록 단건 삭제 (`204 No Content`) |

## 5. 추천 및 피드백 API (`M1A`, `M3A`)

| 메서드·경로 | 설명 | 주요 동작 |
| --- | --- | --- |
| `GET /api/recommendations/random` | 무작위 추천 | 본인 제외 조건(`ALLERGY`, `UNABLE`, `DISLIKE`) + `?exclusions=` 쿼리 제외 키워드를 하드 필터로 먼저 제거한 뒤 무작위 선택(`Cache-Control: no-store`) |
| `GET /api/recommendations/order-sheet/questions` | 주문서 질문 조회 | 인증 사용자 기준(`user:{user_id}`) 주제 노출 상태를 단일 트랜잭션으로 갱신 |
| `POST /api/recommendations/order-sheet` | 주문서 맞춤 추천 | 제외 조건 하드 필터 적용 후 규칙 점수, 최근 노출 감점, 개인화 로지스틱 보정(`±5%`, 반감기 90일, 수축 `k=5`, 최근 3일 동일 식사 특징 포함) 및 가중 근접 추첨(`beta=0.5`)을 반영해 추천 생성 및 추천 시점 특징 스냅샷(`recommendation_item_snapshots`) 저장 |
| `POST /api/recommendations/{recommendation_id}/feedback` | 추천 만족/불만족 피드백 | 원본 이력(`recommendation_feedbacks`)은 append-only로 보존하고, `(recommendation_id, menu_key)` 기준 최신 유효 1표 상태(`recommendation_feedback_states`)만 갱신. `Idempotency-Key` 재전송 시 중복 집계 방지 |

### 오프라인 개인화 모델 학습 (`M3A`)

- API 프로세스 내부에서는 학습을 수행하지 않으며, 검증을 통과한 아티팩트 JSON(`PERSONAL_MODEL_ARTIFACT_PATH`)만 읽어 추론한다.
- 아티팩트가 없거나 체크섬/스키마/유한수 검증에 실패하면 자동으로 개인 보정 0점·균등 추첨(`draw_weight=1.0`)으로 안전하게 폴백한다.
- 수동/배치 학습 명령:
  ```text
  python -m scripts.train_personal_model --check-data
  python -m scripts.train_personal_model --train
  python -m scripts.train_personal_model --evaluate
  python -m scripts.train_personal_model --publish
  ```
