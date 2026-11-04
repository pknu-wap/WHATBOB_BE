# WhatBap Backend Development Log

WhatBap 백엔드의 주요 개발 및 변경 사항을 기록한다.
최신 작업을 위에 추가하며, 기록 방법은 같은 디렉터리의 `AGENTS.md`를 따른다.

## 2026-11-04

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
