# #50 · #52 티켓 분할 및 아키텍처 설계 확정본 (T01 ~ T04)

2026-11-18 사용자 합의 완료. 상위 명세:
- [#50 카카오 토큰 갱신·로그아웃과 계정 재연결](../specs/50-kakao-token-lifecycle-and-logout.md)
- [#50 초기 설계](../plans/50-kakao-account-selection-after-logout.md)
- [#52 웹에서 카카오 계정 연결과 왓밥 로그아웃](../plans/52-t01-web-auth-design.md)

---

## 티켓 의존 그래프 (Task Graph)

```text
T01 (카카오 토큰 암호화 저장·자동 갱신·폐기와 안전한 왓밥 로그아웃)
 │
 ▼
T02 (로그아웃 후 카카오 재인증·대상 확인·안전한 이관 확정)
 ├──▶ T03 (웹 인증 프록시 연동·카카오 콜백·카카오톡 인앱 브라우저 안내)
 └──▶ T04 (Expo 앱 iOS·Android 세션 복원 및 시스템 브라우저 카카오 재연결)
```

---

## 확정된 아키텍처 결정 요약

| 결정 ID | 항목 | 확정 선택 및 물리적 규칙 |
| :--- | :--- | :--- |
| **T01-D01** | 카카오 토큰 DB 저장 구조 | **선택지 2번 (분리된 전용 테이블 `kakao_session_credentials`)** — 매 API 요청마다 조회되는 `auth_sessions` 행을 가볍게 유지하고, 카카오 토큰은 전환 준비(`transition_id`) 또는 정식 세션(`auth_session_id`)에 결합된 전용 테이블에 대칭키 암호화(Fernet/AES, 키는 `JWT_SECRET` 또는 전용 `KAKAO_TOKEN_ENCRYPTION_KEY`에서 파생)하여 저장한다. 원문 토큰은 API 응답·클라이언트 저장소·로그에 절대 노출하지 않는다. |
| **T01-D02** | `LOGOUT` 확정 시 카카오 서버 호출 방식 | **선택지 2번 (로컬 DB 즉시 전환 + 백그라운드 워커 단일 경로 폐기)** — `POST /api/auth/transitions/{id}/commit`(`LOGOUT`)은 로컬 DB 트랜잭션 안에서만 기존 `AuthSession` 폐기(`revoked_at`), 새 `UUID 사용자` 및 새 `AuthSession` 발급, 종료된 세션의 카카오 자격 증명을 `PENDING_REVOCATION` 상태로 전환하고 즉시 응답한다. DB 잠금 밖의 전담 백그라운드 워커/디스패처가 만료 시 `grant_type=refresh_token` 갱신(새 리프레시 토큰·만료 정보 로테이션 반영, 생략 시 기존 값 유지) 후 카카오 `POST /v1/user/logout`을 호출한다. |
| **T01-D03** | 카카오 폐기 재시도 및 원문 파기 기준 | **고정 규칙 (`#36 T01N` 동일 60초 상한 + 즉시 원문 파기)** — 일시적 네트워크/5xx 오류는 `1·3·5·10·30초` 간격(최대 60초 기한)으로 재시도한다. 폐기 성공(`REVOKED`), 영구 오류/기한 만료(`FAILED`), 또는 토큰 부재 레거시 세션(`SKIPPED_NO_CREDENTIALS`)으로 종료되는 즉시 DB의 암호화된 액세스·리프레시 토큰 원문을 `NULL`로 영구 파기하고 상태 메타데이터만 남긴다. |
| **T02-D01** | 카카오 OAuth `state` 및 `prompt=login` 관리 주체 | **선택지 2번 (백엔드 서버 세션 장부 발급·검증)** — 카카오 인가 시작 시 서버가 현재 `AuthSession`을 확인해 `requires_reauth=True`이면 인가 URL에 `prompt=login`을 강제 부착하고, 서버가 발급한 최신 1회용 `state`의 SHA-256 해시와 만료 시각을 현재 `AuthSession`에 덮어써 기록한다. `KAKAO_LINK` 준비(`prepare`) 시 전달된 `state`를 현재 세션의 최신 해시와 대조·소진하여 이전 탭/지연된 콜백을 서버에서 차단한다(하위 호환을 위해 세션에 발급된 서버 `state`가 없을 때의 기존 `expected_state` 검증도 함께 지원하되, 세션에 서버 발급 `state`가 존재하면 반드시 서버 `state` 일치를 요구한다). |
| **T03-D01** | 웹 카카오 복귀(`redirect_uri`) 수신 주체 | **선택지 1번 (웹 콜백 화면 먼저 착륙 후 `prepare` API 호출)** — 카카오 인증 직후 웹 콜백 화면으로 복귀해 로딩 화면을 표시하고 `?code=...&state=...`를 제거한 뒤, 동일 출처 프록시를 통해 `POST /api/auth/transitions`(`prepare`)를 호출하여 반환된 대상 정보(`nickname`, `masked_email`)로 확인 화면을 표시한다. |
| **T04-D01** | Expo 모바일 앱 세션·전환 열쇠 전달 및 보관 | **선택지 1번 (모바일 앱 전용 헤더·응답 봉투 정식 지원)** — 웹은 기존 `HttpOnly` 쿠키만 사용한다. 모바일 앱이 `X-Whatbob-Client: app` 헤더로 요청하거나 앱 전용 인증 헤더(`X-Whatbob-Auth-Secret`, `X-Whatbob-Transition-Secret`)를 보내면, 백엔드는 `Origin` 헤더 없이도 `X-Whatbob-Request: 1`과 앱 클라이언트 헤더를 검증하고 응답 헤더(`X-Whatbob-Auth-Secret`, `X-Whatbob-Transition-Secret`) 및 본문 필드로 비밀열쇠를 내려주어 앱이 `expo-secure-store`에 보관·복원할 수 있게 한다. 또한 카카오 `https://` 콜백을 앱 스킴(`whatbob://auth/kakao/callback`)으로 즉시 중계하는 브릿지 엔드포인트(`GET /api/auth/kakao/app-callback`)를 제공한다. |

---

## 티켓별 상세 명세 (Vertical Slices)

### T01: 카카오 토큰 암호화 저장·자동 갱신·폐기와 안전한 왓밥 로그아웃
- **Parent**: `#50`
- **Blocked by**: None (즉시 시작 가능)
- **What to build**:
  - 카카오 인가 코드 교환(`POST https://kauth.kakao.com/oauth/token`)에서 `access_token`, `refresh_token`, `expires_in`, `refresh_token_expires_in`을 수집한다.
  - 전용 테이블 `kakao_session_credentials`에 암호화된 토큰 쌍과 만료 시각, 폐기 상태(`ACTIVE`, `PENDING_REVOCATION`, `REVOKED`, `FAILED`, `SKIPPED_NO_CREDENTIALS`)를 저장한다.
  - `KAKAO_LINK` 준비 단계에서는 준비 내역(`transition_id`)에 임시 결합하고, 확정(`commit`) 시 새로 발급된 `AuthSession`(`result_session_id`)에 결합한다. 취소·만료된 준비의 임시 자격 증명은 즉시 파기한다.
  - `LOGOUT` 확정(`commit`) 시 로컬 DB 트랜잭션 안에서 현재 `AuthSession`을 폐기(`revoked_at`)하고 새 `UUID 사용자`·새 `AuthSession`을 발급하며, 종료된 세션의 카카오 폐기 상태를 기록한다(자격 증명이 있던 세션은 `PENDING_REVOCATION`, 레거시 세션은 `SKIPPED_NO_CREDENTIALS`).
  - 백그라운드 워커/디스패처가 `PENDING_REVOCATION` 작업을 실행한다: 액세스 토큰이 만료(또는 만료 임박/401 응답)되었으면 `grant_type=refresh_token`으로 갱신(새 리프레시 토큰 반환 시 교체, 생략 시 기존 유지, 동일 세션 직렬화)한 뒤 카카오 `POST https://kapi.kakao.com/v1/user/logout`을 호출한다.
  - 일시적 네트워크/5xx 오류는 `1·3·5·10·30초` 간격(최대 60초 기한)으로 재시도하며, 종료(`REVOKED` 또는 `FAILED`) 즉시 DB의 암호화된 토큰 원문을 영구 삭제(`NULL`)한다.
  - `TransitionCommitResponse` 및 `TransitionStatusResponse`에 카카오 폐기 상태(`kakao_logout_status`: `PENDING` / `REVOKED` / `SKIPPED_NO_CREDENTIALS` / `FAILED` 등)를 노출하여 클라이언트가 결과를 확인할 수 있게 한다.

### T02: 로그아웃 후 카카오 재인증·대상 확인(닉네임/마스킹 이메일)·안전한 이관 확정
- **Parent**: `#50`
- **Blocked by**: `T01`
- **What to build**:
  - `LOGOUT` 확정으로 생성된 새 `UUID 사용자`의 `AuthSession`에 `requires_reauth = True`를 기록하고, 세션 복원(`POST /api/auth/tokens`) 및 전환 확정 응답에 `requires_reauth` 여부를 포함한다.
  - 카카오 인가 URL 발급(`GET /api/auth/kakao/login/url` 및 인증된 세션 기반 인가 시작) 시 현재 세션(`Cookie`, `Bearer JWT`, 또는 앱 시크릿 헤더)을 확인하여:
    - 서버가 생성한 1회용 `state`(또는 세션 바인딩 `state`)의 해시와 만료 시각을 `AuthSession`에 저장(최신 시도 1개만 유효하도록 덮어쓰기)하고 반환한다.
    - 해당 세션이 `requires_reauth=True`이면 카카오 인가 URL에 `prompt=login` 파라미터를 강제 포함한다.
  - `KAKAO_LINK` 준비(`POST /api/auth/transitions`) 시:
    - 세션에 발급된 최신 `state` 해시와 요청의 `state`를 검증하고 1회용으로 소진한다(불일치·만료·이전 탭의 구형 `state`는 `400 INVALID_OAUTH_STATE`로 거절).
    - 카카오 프로필에서 `표시 닉네임`과 `이메일`을 확인해 이메일은 서버에서 마스킹(`yu***@kakao.com`)한다.
    - **닉네임과 이메일이 둘 다 없으면** 준비를 중단(`422 KAKAO_TARGET_UNVERIFIABLE` 등 명확한 도메인 오류)하고 수집한 임시 카카오 토큰도 파기하여 사용자가 기존 `UUID 사용자` 세션을 그대로 유지하게 한다.
    - 최소 하나가 존재하면 `TransitionPrepareResponse` 및 `TransitionStatusResponse`에 `target_account`(`nickname`, `masked_email`)를 반환하고, 동일 `origin_session_id`의 이전 `PREPARED` 전환이 있으면 자동 `CANCELLED` 처리 및 임시 토큰을 파기한다.
  - 전환 취소 엔드포인트 `POST /api/auth/transitions/{transition_id}/cancel`을 추가한다:
    - 확인 화면에서 사용자가 취소하면 해당 `PREPARED` 전환을 `CANCELLED`로 변경하고 임시 보관된 카카오 토큰 원문을 즉시 파기하며, 준비 쿠키를 정리한다. 이후 해당 `transition_id`로 들어오는 `commit` 요청은 거절한다.

### T03: 웹(Web) 인증 프록시 연동·카카오 콜백·카카오톡 인앱 브라우저 안내
- **Parent**: `#50`, `#52`
- **Blocked by**: `T02`
- **What to build**:
  - 웹 동일 출처 프록시(Vercel `/api/auth/...` → 백엔드) 경유 시 `Cookie`, `Set-Cookie`, `Origin`, `X-Whatbob-Request`, `X-Whatbob-Auth-Context`, `Idempotency-Key` 전달 및 `Cache-Control: no-store` 보장 계약을 검증한다.
  - 웹 콜백 화면 착륙 후 `POST /api/auth/transitions`(`prepare`) → 대상 확인(`nickname`, `masked_email`) → `commit` 또는 `cancel` 왕복 흐름과, 카카오톡 인앱 브라우저(`KAKAOTALK` User-Agent) 감지 시 재연결 사전 안내 헬퍼/메타데이터 및 통합 시나리오를 검증한다.

### T04: Expo 앱(iOS·Android) 세션 복원 및 시스템 브라우저 카카오 재연결
- **Parent**: `#50`
- **Blocked by**: `T02`
- **What to build**:
  - 모바일 앱 전용 이중 전달 계약(Dual-Transport Contract)을 구현한다:
    - 앱이 `X-Whatbob-Client: app` 헤더와 `X-Whatbob-Request: 1`을 보내면 브라우저 `Origin` 헤더 없이도 CSRF 검사를 통과한다.
    - 백엔드는 쿠키(`__Secure-whatbob_auth`, `__Secure-whatbob_transition`)와 함께 앱이 `expo-secure-store`에 저장할 수 있도록 응답 헤더(`X-Whatbob-Auth-Secret`, `X-Whatbob-Transition-Secret`)로 비밀열쇠를 내려주고, 요청 시 `X-Whatbob-Auth-Secret` 및 `X-Whatbob-Transition-Secret` 헤더로 세션·전환 준비 인증을 수행할 수 있게 한다(웹 브라우저 요청에는 비밀열쇠 헤더를 노출하지 않고 기존 `HttpOnly` 쿠키만 사용한다).
  - 앱용 카카오 인가 URL 발급 시 `redirect_uri`를 백엔드 앱 브릿지 콜백(`GET /api/auth/kakao/app-callback`)으로 지정할 수 있게 지원하고, `GET /api/auth/kakao/app-callback`은 카카오가 전달한 `code`, `state`, `error`, `error_description`을 그대로 `whatbob://auth/kakao/callback?...`(설정 가능한 앱 스킴)으로 `302 Redirect`(`Cache-Control: no-store`)하여 모바일 시스템 인증 창(`openAuthSessionAsync`)이 자동으로 닫히며 앱으로 복귀하게 한다.
  - 앱 재시작 후 `X-Whatbob-Auth-Secret`을 통한 세션 복원(`POST /api/auth/tokens`), 로그아웃 후 `prompt=login` 재연결, 브릿지 콜백 복귀 후 `prepare`·확인·`commit`/`cancel` 전체 왕복을 통합 테스트로 검증한다.
