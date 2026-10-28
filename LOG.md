# WhatBap Backend Development Log

WhatBap 백엔드의 주요 개발 및 변경 사항을 기록한다.
최신 작업을 위에 추가하며, 기록 방법은 같은 디렉터리의 `AGENTS.md`를 따른다.

## 2026-10-28

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
