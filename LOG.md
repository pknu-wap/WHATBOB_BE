# WhatBap Backend Development Log

WhatBap 백엔드의 주요 개발 및 변경 사항을 기록한다.
최신 작업을 위에 추가하며, 기록 방법은 같은 디렉터리의 `AGENTS.md`를 따른다.

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
