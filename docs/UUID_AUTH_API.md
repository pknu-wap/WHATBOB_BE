# UUID 사용자 생성·복원 및 계정 전환·프로필·방 복구 API — T01~T09

2026-11-13: T01~T09 백엔드 구현 및 격리 SQLite·MySQL 8.4.13 검증 완료. 운영 DB 직접 변경·배포·실제 교차 사이트 브라우저 검증·실제 1GB 운영 서버 부하 측정은 수행하지 않았다.
기준 계약은 `docs/plans/39-design-baseline.md`, `docs/plans/39-t01-http-contract-proposal.md`, `docs/plans/39-account-transition-contract.md`, `docs/plans/39-implementation-architecture.md`다.

## 최초 이용과 재방문

웹 쿠키 명령에는 다음을 모두 사용한다.

- 정확한 허용 `Origin`과 `X-Whatbob-Request: 1` 헤더.
- `fetch`의 `credentials: "include"` 또는 Axios의 `withCredentials: true`.
- HTTPS. HTTP용 쿠키 완화는 제공하지 않는다.

세 경로는 JSON 본문을 요구하지 않으며, 본문의 사용자 번호·UUID·비밀값을 인증에 사용하지 않는다.

| 단계 | 요청 | 응답과 다음 행동 |
| --- | --- | --- |
| 시작 | `POST /api/auth/preparations` | 200, `next_action: CREATE_USER` 또는 `ISSUE_TOKEN`, `notice` |
| 생성 | `POST /api/auth/anonymous-users` | 새 사용자 201, 같은 쿠키의 생성 결과 복구 200 |
| 복원·갱신 | `POST /api/auth/tokens` | 유효한 정식 인증이면 200 (`X-Whatbob-Auth-Context` 선택 검증). 준비 상태에서는 사용자 생성 없이 409 |
| 본인 조회 | `GET /api/users/me` | `Authorization: Bearer <JWT>`로 조회. 쿠키만으로는 조회할 수 없음 |

처음에는 준비 요청에서 받은 쿠키로 생성 요청을 보낸다. 준비 요청이 `ISSUE_TOKEN`을 반환하면 생성 대신 토큰 요청을 보낸다. JWT가 메모리에 없다는 이유만으로 새 사용자를 만들지 않는다.

생성·복원·갱신 성공 응답 필드는 `access_token`, `token_type: bearer`, `expires_in: 3600`, `user`, `is_new_user`다.
`user`는 기존 `id`, `kakao_id`, `nickname`, `email`, `profile_image`와 새 `uuid`를 포함한다. 카카오 미연결 여부는 `kakao_id == null`로 판별하며 UUID 존재 여부로 판별하지 않는다.

새 UUID 사용자의 `profile_image`는 API가 제공하는 `/static/default-profile.svg`다. 프론트 주소가 아니라 **API 기준 주소**에 대해 이 경로를 해석한다. 기존 카카오 사진 URL은 유지한다.

준비 응답의 `notice`는 기본 `null`이다. 이전 정식 인증이 만료되었거나 정상 조회 결과 인증 기록이 없어 새 준비를 발급하면 `PREVIOUS_SESSION_UNAVAILABLE`을 반환한다. 이전 사용자·기록을 삭제했다는 의미는 아니다.

`POST /api/auth/tokens` 및 계정 전환 API는 선택 헤더 `X-Whatbob-Auth-Context`(기대하는 `session_id` 또는 사용자 `uuid`)를 지원한다. 다른 탭에서 계정이 전환되어 현재 쿠키/세션과 일치하지 않으면 `409 AUTH_CONTEXT_CHANGED`를 반환한다.

## 쿠키와 세션

- 기본 이름: `__Secure-whatbob_auth`. `HttpOnly; Secure; SameSite=None; Path=/api/auth`, Domain 생략.
- 계정 전환 준비 쿠키: `__Secure-whatbob_transition` (`TRANSITION_PREPARE_COOKIE_NAME`). `HttpOnly; Secure; SameSite=None; Path=/api/auth`, 유효기간 300초.
- 비밀값: `secrets.token_urlsafe(32)`의 정규 URL-safe Base64 문자열 43자. 응답 JSON에는 포함하지 않는다.
- 서버 저장: SHA-256 해시 32바이트. 원문 비밀값이나 JWT를 DB에 저장하지 않는다.
- 준비 인증: 5분. 처음 쿠키 보관 기간은 30일+5분이다.
- 정식 인증: 30일. 유효한 정식 쿠키의 토큰 발급 성공 시 현재부터 30일 연장한다. 준비 요청·일반 API 조회는 연장하지 않는다.
- 유효한 준비·정식 인증의 준비 요청은 새 쿠키를 발급하지 않는다.
- 생성 응답 유실 시 **기존 준비 쿠키**로 생성 요청을 다시 보낸다. 서버가 이미 생성했다면 같은 사용자로 복구한다. 새 준비부터 반복하면 독립된 인증을 만들 수 있다.
- 브라우저의 타사 쿠키 차단을 `SameSite=None`이나 CORS가 해제하지는 않는다. 준비 후 쿠키가 전달되지 않으면 생성 반복을 멈춘다.

## 오류 처리

오류는 `{"detail":{"code":"...","message":"..."}}`다. 메시지 대신 코드로 분기한다. 인증 응답에 `Cache-Control: no-store`, JWT 성공 응답에 `Pragma: no-cache`를 적용한다.

| 상황 | 상태·코드 | 처리 |
| --- | --- | --- |
| 쿠키 미전달 | 401 `AUTH_COOKIE_MISSING` | 시작 절차와 쿠키 전달 설정 확인. 생성 무한 반복 금지 |
| 쿠키 형식 오류 | 400 `INVALID_AUTH_COOKIE` | 중단. 서버가 쿠키를 임의 삭제·교체하지 않음 |
| 준비만 존재 | 409 `AUTHENTICATION_NOT_READY` | 생성 API 호출 |
| 기록 없음 | 401 `SESSION_NOT_FOUND` | 준비 API를 통한 새 시작. 이전 변경 요청 자동 재생 금지 |
| 준비·정식 인증 만료 | 401 `PREPARATION_EXPIRED` / `SESSION_EXPIRED` | 만료 인증을 부활시키지 않음 |
| 명시적 종료 | 401 `SESSION_REVOKED` | 자동 새 사용자 생성 금지 |
| 사용자 복원 불가 | 401 `SESSION_UNAVAILABLE` | 중단·상태 확인 |
| 접근 JWT 만료 | 401 `ACCESS_TOKEN_EXPIRED` | 같은 화면의 갱신을 합치고 원래 요청은 최대 한 번만 재시도 |
| 잘못된 접근 JWT | 401 `ACCESS_TOKEN_INVALID` | 만료 갱신 루프 금지 |
| 인식 가능한 구형 JWT | 401 `REAUTHENTICATION_REQUIRED` | 카카오 재로그인. 서명을 검증한 뒤에만 이 코드로 분류 |
| 탭 간 인증 문맥 불일치 | 409 `AUTH_CONTEXT_CHANGED` | 최신 인증 상태를 먼저 다시 조회 |
| 방 참여·유예 중 계정 전환 | 409 `ROOM_PARTICIPATION_ACTIVE` | 방 나가기 완료 후 전환 재시도 |
| 출처·추가 헤더 거부 | 403 `CSRF_REJECTED` | Origin·헤더 설정 확인 |
| DB 일시 장애 | 503 `AUTH_TEMPORARILY_UNAVAILABLE` | 기존 인증·기록 유지. 새 사용자 생성으로 처리하지 않음 |
| DB 내부 오류 | 500 `AUTH_INTERNAL_ERROR` | 내부 SQL·비밀값 노출 없이 오류 안내 |

## 프로필 관리 API — T04, T05

| 메서드·경로 | 요청 | 응답 및 규칙 |
| --- | --- | --- |
| `PATCH /api/users/me/nickname` | `{"nickname": "새닉네임"}` | `200 OK` (`UserResponse`). 앞뒤 공백 제거 후 2~20자 한글(완성형)·영문·숫자·공백만 허용. 전체 사용자 유일성(`nickname_key`) 검사, 타인과 중복 시 `409 NICKNAME_DUPLICATE`, 본인 현재 닉네임 재저장은 `200 OK` |
| `PUT /api/users/me/profile-image` | `multipart/form-data` (`file`) | `200 OK` (`UserResponse`). 최대 5MB·1,200만 픽셀 이하 정지 `JPEG`/`PNG`/`WebP`만 허용(`M4A`). EXIF 회전 반영·메타데이터 제거 후 긴 변 512px 이내로 정규화하여 `/uploads/profile-images/{filename}`으로 저장 |
| `DELETE /api/users/me/profile-image` | 없음 | `200 OK` (`UserResponse`). 기본 이미지(`/static/default-profile.svg`)로 복원하고 미참조 기존 파일 정리 |
| `GET /uploads/profile-images/{filename}` | 없음 | `200 OK` (`R5A` 공개 URL, `X-Content-Type-Options: nosniff`, `Cache-Control: public, max-age=86400`). 경로 조작이나 비정상 파일명은 `404` |

## 계정 전환(카카오 연결·병합·로그아웃·복구) API — T06, T07, T08

계정 전환은 2단계(`prepare` → `commit`)와 응답 유실 복구(`status` / `recovery`)로 동작하며, 모든 경로에 CSRF 헤더(`Origin`, `X-Whatbob-Request: 1`)가 필요하다.

| 단계 | 메서드·경로 | 요청 및 동작 |
| --- | --- | --- |
| 1. 전환 준비 | `POST /api/auth/transitions` | `Authorization: Bearer <JWT>` 필수. 선택 헤더 `Idempotency-Key`, `X-Whatbob-Auth-Context`. 본문 `{"action": "KAKAO_LINK", "code": "<kakao_code>"}` 또는 `{"action": "LOGOUT"}`. 성공 시 `200 OK` (`TransitionPrepareResponse`) 및 전환 준비 쿠키(`__Secure-whatbob_transition`, 5분) 설정 |
| 2. 전환 확정 | `POST /api/auth/transitions/{transition_id}/commit` | 전환 준비 쿠키 필수. 성공 시 `200 OK` (`TransitionCommitResponse`: 새 `access_token`, `user`, `is_new_user`) 및 정식 인증 쿠키(`__Secure-whatbob_auth`, 30일) 교체 |
| 3. 상태 조회 | `GET /api/auth/transitions/{transition_id}` | 전환 준비 쿠키 또는 Bearer JWT로 상태(`PREPARED`, `COMMITTED`, `CANCELLED`, `EXPIRED`) 조회 |
| 4. 확정 결과 복구 | `POST /api/auth/transitions/{transition_id}/recovery` | `COMMITTED` 응답이 네트워크 단절로 유실된 경우 전환 준비 쿠키로 확정 결과와 정식 인증 쿠키를 다시 수신 (`200 OK`) |

### `KAKAO_LINK` 무손실 이관·병합 규칙 (`R3A`)

- 원본 UUID 계정 또는 대상 카카오 계정이 만료되지 않은 방에 참여 중이거나 60초 재접속 유예 중이면 `409 ROOM_PARTICIPATION_ACTIVE`로 거절한다.
- 단일 트랜잭션 및 고정 잠금 순서(`KakaoLinkGuard(kakao_id) -> User(id ASC) -> AuthSession(id ASC) -> Room/Task`) 아래 다음을 원자적으로 수행한다:
  1. 음식 제외 조건(`ALLERGY`, `UNABLE`, `DISLIKE`): 유형별 합집합 병합(기존 100개 초과도 무손실 보존).
  2. 식사 기록(`user_meals`): 동일 메뉴·시각도 중복 제거하지 않고 원래 행 ID와 시각 그대로 대상 계정에 이전.
  3. 추천(`recommendations`)·피드백 원본(`recommendation_feedbacks`)·추천 시점 스냅샷(`recommendation_item_snapshots`)·유효 피드백 상태(`recommendation_feedback_states`): 원래 순서와 평가 상태 그대로 이전·재구성.
  4. 질문 선택 상태(`question_selection_states`) 및 멱등 작업 기록(`request_operations`): 카운트 합산 및 최신 클러스터 보존.
  5. 프로필 우선순위: 기존 레거시 카카오 계정(`is_legacy_kakao=true`) 또는 카카오 계정에서 직접 수정한 필드(`*_edited=true`)를 최우선 보존하고, 그렇지 않으면 가장 먼저 생성된 UUID 출처(`(source_created_at, source_id)` 오름차순)의 값을 필드별로 독립 선택.
  6. 원본 UUID 사용자의 모든 `AuthSession` 및 `User` 행을 물리 삭제(`R3A`)하고, 대상 카카오 계정에 새 정식 `AuthSession`을 발급.

### `LOGOUT` 왕복 규칙 (`T08`)

- 카카오 연결 계정에서 `LOGOUT` 확정 시:
  - 카카오 계정 데이터와 다른 기기 세션은 그대로 유지하고, **현재 브라우저의 `AuthSession`만 폐기(`revoked_at`)**한다.
  - 즉시 새로운 UUID 사용자와 새 정식 `AuthSession`·쿠키·JWT를 원자적으로 발급한다.
  - 이후 새 UUID에서 쌓은 기록은 다시 카카오 연결(`KAKAO_LINK`) 시 기존 카카오 계정에 무손실 병합된다.

## 기존 단독 카카오 경로

`GET /api/auth/kakao/login/url`, `GET /api/auth/kakao/callback`, `POST /api/auth/kakao/login` 경로는 유지한다.
복원 쿠키가 있는 브라우저가 기존 단독 로그인 경로를 호출하면 409 `ACCOUNT_LINKING_NOT_AVAILABLE`로 거절한다. UUID 기록 이관과 쿠키 전환은 위의 `/api/auth/transitions` 경로를 사용한다.

## 방 재접속 60초 유예·재시작 복구·방 작업 취소 — T03

- 동일 사용자의 다중 WebSocket 연결 중 **마지막 연결이 끊어졌을 때만** `disconnected_at`(호스트는 추가로 `host_disconnected_at`)을 기록하고 60초 재접속 유예를 시작한다.
- 60초 이내에 재연결하거나 REST 방 활동을 수행하면 `disconnected_at`이 해제되어 참여 상태가 유지된다.
- 60초가 경과하면:
  - 일반 멤버(`MEMBER`): 자동 퇴장 처리, 해당 멤버의 주문서(`room_member_order_sheets`) 삭제, 방 인원·완료 수 재계산 및 `member_left` 브로드캐스트.
  - 호스트(`HOST`): 방 종료(`CLOSED`, `close_reason="HOST_DISCONNECTED"`).
- 서버 재시작 복구(`recover_rooms_after_restart`): 프로세스 시작 시 만료된 방·유예 만료 멤버를 먼저 정리하고, 인메모리 연결이 없는 `is_online=true` 멤버들의 `disconnected_at`을 시작 시각으로 기록해 60초 재접속 유예를 부여한다.
- 방 생성·참여 멱등성·상태 조회 및 취소:
  - `POST /api/rooms` 및 `POST /api/rooms/{room_id}/join`은 선택 헤더 `Idempotency-Key`를 지원한다.
  - `GET /api/room-operations/{operation_key}`는 작업 상태(`UNKNOWN`, `PENDING`, `SUCCEEDED`, `CANCELLED`), 성공 당시 `room_id`, 그리고 현재 방 상태(`room_status`: `WAITING`, `COMPLETED`, `CLOSED`, `DELETED`)를 구분해 반환한다.
  - 화면 이탈 시 `POST /api/room-operations/{operation_key}/cancellation`을 호출하면:
    - 아직 방 생성/참여가 확정되기 전(또는 요청이 도착하기 전)이면 취소 표식(`CANCELLED` tombstone)을 기록해 늦게 도착한 생성/참여 요청이 방을 만들거나 참여하지 못하게 차단(`409 OPERATION_CANCELLED`)한다.
    - 이미 성공(`SUCCEEDED`)한 생성/참여는 방을 삭제하거나 퇴장시키지 않고 현재 `SUCCEEDED` 상태와 `room_status`를 반환한다.

## 기존 MySQL 전환 — T01 & T09

T01~T09 전체 스키마 전환 및 파생 상태 백필 스크립트(`scripts.migrate_issue39`)는 MySQL 8.0.16 이상을 요구하며 실제 검증 버전은 8.4.13이다. 앱 시작 시 자동 실행되지 않는다.

```text
python -m scripts.migrate_issue39 --check
python -m scripts.migrate_issue39 --apply --maintenance-confirmed
python -m scripts.migrate_issue39 --verify
```

- `--check`: 읽기 전용 점검. 현재 스키마와 사용자 수, 미매핑 레거시 제외 건수(`unmapped_catalog_exclusions`), 소유자 없는 레거시 피드백 건수(`ownerless_feedbacks`)를 JSON으로 출력한다.
- `--apply --maintenance-confirmed`: 기존 테이블 지문(manifest)을 기록한 뒤 T01 컬럼(`uuid`, `nickname_key`, `is_legacy_kakao`, `auth_sessions`)과 T02~T08 컬럼·테이블(`users` 프로필 출처 컬럼, `user_food_exclusions` 카탈로그 컬럼, `recommendation_feedbacks` 메뉴 키·작업 키 컬럼, `request_operations`, `account_transitions`, `kakao_link_guards`, `recommendation_feedback_states`, `recommendation_item_snapshots`)을 멱등하게 추가·백필한다. 부분 실패 후 동일 명령으로 안전하게 재개할 수 있다.
- `--verify`: 모든 스키마·인덱스·유효 피드백 상태 백필 완료 여부 및 원본 데이터 무손실 보존 지문을 검증한다.

## 주기 정리 스크립트

- 만료·폐기 인증 세션 정리: `python -m scripts.cleanup_auth_sessions`
- 미참조 고아 프로필 이미지 정리 (`cleanup_orphan_profile_images`): 임시 파일 및 어떤 `users.profile_image`에서도 참조하지 않는 유예 시간 경과 파일을 안전하게 삭제한다.

## 검증 명령

일반 통합 테스트는 격리 SQLite·테스트 JWT 키를 사용한다.

```text
.tools\venv\Scripts\python.exe -m pytest -q
```

실제 MySQL 동시성·마이그레이션·E2E 테스트는 `MYSQL_TEST_DATABASE_URL`이 명시된 경우 실행한다(`tests/test_issue39_mysql_concurrency.py`, `tests/test_uuid_auth_mysql.py`, `tests/test_rooms_mysql.py`). 각 테스트는 무작위 이름의 격리 스키마를 생성·삭제한다.
서로 다른 사이트의 실제 쿠키 수용·차단(실제 HTTPS 브라우저 검증)과 1GB RAM 운영 서버 실측 부하 검증은 로컬 TestClient 범위 밖이므로 별도 배포 전 검증 대상이다.
