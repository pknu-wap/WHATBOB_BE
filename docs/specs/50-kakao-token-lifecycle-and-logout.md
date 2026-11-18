# #50 카카오 토큰 갱신·로그아웃과 계정 재연결

원본 이슈 [pknu-wap/WHATBOB_BE#50](https://github.com/pknu-wap/WHATBOB_BE/issues/50)

구현 대상은 왓밥 웹과 iOS·Android에 설치하는 Expo 앱이다. 두 환경 모두 카카오 계정 연결, 토큰 갱신·폐기, 로그아웃 후 계정 재연결을 지원한다.

## Problem Statement

왓밥에서 로그아웃한 사용자는 다시 연결할 카카오 계정을 직접 선택하고, 사용 중인 UUID의 기록이 어느 계정으로 이동하는지 확인하고 싶다. 카카오 인증 화면에서 이전 계정이 자동으로 선택되면 사용자가 의도한 연결 대상과 달라질 수 있다.

현재 백엔드는 웹 세션 쿠키로 왓밥 JWT를 재발급한다. 카카오 인가 코드로 받은 액세스 토큰은 프로필 조회에 사용하며, 카카오 토큰 수명 관리는 이 명세에서 추가한다. [카카오 토큰과 로그아웃 계약](https://developers.kakao.com/docs/ko/kakaologin/common#logout)

## Solution

카카오 계정 연결에서 발급받은 액세스 토큰과 리프레시 토큰을 해당 왓밥 세션에 연결해 서버에서 관리한다. 웹이나 앱에서 로그아웃을 확정하면 서버는 요청한 왓밥 세션을 폐기하고 새 UUID 사용자와 세션을 발급한다. 사용자는 현재 웹이나 앱에서 새 세션으로 이용을 이어간다. 서버는 종료된 세션의 카카오 토큰을 폐기하고, 폐기에 필요한 액세스 토큰이 만료됐으면 카카오 리프레시 토큰으로 갱신한다. [카카오 토큰 갱신](https://developers.kakao.com/docs/ko/kakaologin/rest-api#refresh-token), [카카오 토큰 폐기](https://developers.kakao.com/docs/ko/kakaologin/common#logout)

로그아웃 후 새 UUID 세션에서 다시 연결할 때는 카카오 재인증으로 계정을 선택하고 연결을 시작한 웹이나 앱으로 돌아온다. 왓밥에서 연결 대상을 확인한 뒤 현재 UUID의 기록을 선택한 계정으로 이관한다.

## User Stories

1. As a Kakao-linked user, I want to log out of my current WhatBap web or app session and receive a new UUID session, so that I can continue using the same client as a UUID user.
2. As a user signed in on two devices, I want logout to end the Kakao token pair for the current session, so that my other device can keep using WhatBap.
3. As a user whose Kakao access token has expired, I want WhatBap to use the Kakao refresh token for logout, so that my token pair can be revoked without another login.
4. As a user whose Kakao refresh token was rotated, I want the replacement stored for my session, so that a subsequent operation can use the valid credential.
5. As a user logging out during a Kakao outage, I want the WhatBap session to end and the Kakao result to be reported accurately, so that I know whether provider revocation was confirmed.
6. As a user with a Kakao-linked session created before this feature, I want to be able to log out of WhatBap, so that the absence of stored Kakao credentials does not trap me in that session.
7. As a returning web or app user with an active WhatBap session, I want to restore my WhatBap access JWT, so that reloading the site, restarting the app, or waiting past JWT expiry does not interrupt use.
8. As a user reconnecting after logout, I want to authenticate with the same or a different Kakao account, so that a remembered Kakao session does not decide which account receives my records.
9. As a user who cancelled authentication, reloaded the site, or restarted the app, I want to try account selection again in the same UUID session, so that I can finish connecting when ready.
10. As a user who has authenticated with Kakao, I want to return to the web or app session that started the request, so that I can confirm which account will receive its UUID records.
11. As a user about to connect an account, I want to see its Kakao nickname and masked email when provided, so that I can check the destination before moving my records.
12. As a user whose Kakao account provides neither display field, I want the transfer to stop, so that my UUID records stay where they are until I can verify the destination.
13. As a user who chose the wrong account, I want to cancel before confirming, so that I can keep my UUID records and choose again.
14. As a user reconnecting the same Kakao account, I want my new UUID activity merged into it, so that I can see my history together.
15. As a user reconnecting a different Kakao account, I want only the current UUID activity moved to it, so that the previous Kakao account retains its own records.
16. As a user who started more than one reconnection attempt, I want an outdated callback or confirmation to be rejected, so that a late result cannot change the transfer destination.
17. As a user whose response was lost after confirmation, I want to recover the committed result, so that transport retries do not create extra users or repeat a transfer.
18. As a participant in a group menu room, I want my current session and participation preserved when the server rejects an account transition, so that an unsuccessful request does not interrupt the room.
19. As a web user opening WhatBap inside KakaoTalk, I want to know when account selection requires a regular browser, so that I can choose how to proceed with the UUID records in each browser.

## Implementation Decisions

### 카카오 토큰 갱신과 로그아웃

- 계정 연결의 인가 코드 교환에서 카카오가 반환한 토큰 쌍과 만료 정보를 수집한다. 확인 대기 중 필요한 자격 증명은 준비 기한이 있는 서버 측 상태에 둔다. 연결 확정 시 새 왓밥 세션에 연결하고, 취소·만료된 준비의 자격 증명은 정리한다. 카카오 토큰 원문은 서버에서 암호화해 보관하며 웹·앱 저장소, 전환 응답 및 로그에서 노출되지 않게 한다.
- 카카오 호출에 유효한 액세스 토큰이 필요할 때 `grant_type=refresh_token`으로 갱신한다. 현재 사용처는 만료된 액세스 토큰으로는 수행할 수 없는 카카오 토큰 로그아웃이다. 응답이 새 리프레시 토큰과 만료 정보를 제공하면 함께 교체하고, 해당 필드가 생략되면 기존 값과 만료 시각을 보존한다. 같은 세션의 갱신 작업을 직렬화한다. [카카오 토큰 갱신 계약](https://developers.kakao.com/docs/ko/kakaologin/rest-api#refresh-token)
- 기존 `LOGOUT` 준비·확정 흐름에서 요청한 웹 또는 앱의 왓밥 세션 폐기, 새 UUID·세션 발급 및 카카오 폐기 작업 기록을 하나의 로컬 트랜잭션으로 처리한다. 웹과 앱은 각각 확정 결과를 받아 왓밥 세션을 교체한다. 로컬 확정 이후 종료된 세션의 카카오 액세스 토큰으로 카카오 `POST /v1/user/logout`을 호출해 해당 토큰 쌍을 폐기한다. 카카오 네트워크 요청은 DB 잠금 범위 밖에서 수행한다. [카카오 로그아웃의 기기별 폐기 범위](https://developers.kakao.com/docs/ko/kakaologin/common#logout)
- 재시도 가능한 카카오 장애는 같은 폐기 작업에서 제한적으로 처리한다. 왓밥 로그아웃 결과와 별도로, 카카오 폐기가 처리 중인지, 성공이 확인됐는지, 토큰 부재나 영구 오류로 처리할 수 없는지, 응답 유실로 결과를 확인할 수 없는지 알린다. 확정된 왓밥 로그아웃은 카카오 장애 중에도 복구할 수 있어야 한다. 폐기 작업의 대상은 종료된 세션의 토큰으로 고정하며, 폐기가 끝났거나 처리 기한이 지나면 보관한 자격 증명을 정리한다.

### 로그아웃 후 다시 연결

- 로그아웃 확정에서 발급한 UUID 세션에 재연결 여부를 보존하고 웹과 앱의 세션 복원에서 제공한다. 두 환경 모두 로그아웃 후 재연결에서 같은 계정이나 다른 계정을 직접 선택할 수 있는 카카오 재인증 경로를 제공한다.
- 웹은 기존 세션 쿠키 기반 복원과 웹 카카오 콜백을 사용한다. 일반 브라우저에서 재연결할 때 카카오 재인증을 요청한다. 카카오톡 내장 브라우저에서 계정 선택이 불가능하면 일반 브라우저를 안내하고, 열린 일반 브라우저에서는 그 브라우저의 UUID 기록을 사용한다. [카카오 웹 인가 요청](https://developers.kakao.com/docs/ko/kakaologin/rest-api)
- iOS·Android 앱은 카카오 인증을 시작한 뒤 결과를 앱에서 받는다. 시스템 인증 화면을 사용할 수 있으며, 이관할 기록은 인증을 시작한 앱의 UUID 세션을 기준으로 결정한다. 앱은 앱을 다시 실행한 뒤에도 유효한 왓밥 세션을 복원한다. [카카오 iOS 로그인](https://developers.kakao.com/docs/ko/kakaologin/ios), [카카오 Android 로그인](https://developers.kakao.com/docs/ko/kakaologin/android), [Expo OAuth 안내](https://docs.expo.dev/guides/authentication/)
- 웹과 앱에서 돌아오는 인증 결과를 시작 요청 및 현재 UUID 세션에 결합하고 검증한 뒤 `KAKAO_LINK` 준비 단계를 호출한다. 인가 코드 흐름에서는 콜백의 `state`를 검증한다. 확인 화면에는 카카오 닉네임과 서버에서 마스킹한 이메일 중 제공된 값을 보여준다. 둘 다 없으면 사용자는 자신의 UUID 세션으로 돌아간다. 화면에 표시한 대상과 확정 시 이관 대상을 동일한 서버 측 준비 내역에 결합한다.
- 확정은 유효한 준비 내역과 현재 UUID 세션에서 사용자가 선택한 대상에 한해 처리한다. 취소·만료·세션 변경으로 무효화된 확정 요청은 거절한다. 기존 계정 이관의 무손실 병합과 확정 결과 복구 계약을 적용한다.
- 같이 고르기 방에 참여 중이거나 연결이 끊긴 뒤 60초 재접속 유예가 적용 중이면 기존 서버 정책에 따라 계정 전환을 차단한다. 서버가 `409 ROOM_PARTICIPATION_ACTIVE`를 반환하면 현재 왓밥 세션과 방 참여 상태가 유지된다. 카카오 계정 연결 시에는 기존 계약에 따라 연결 대상 계정의 참여도 검사한다.

## Testing Decisions

- 기존 인증 HTTP API 통합 테스트 경계를 사용한다. 왓밥 세션 복원과 T06~T08 전환 왕복 테스트를 기반으로 웹과 앱의 JWT 재발급, 연결 대상, 기록의 귀속, 다른 기기 세션, 상태 코드와 복구 응답을 검증한다. 기존 웹 세션 쿠키 계약도 회귀 검증한다.
- 통합 테스트에서 카카오 HTTP 응답을 제어해 유효한 액세스 토큰의 폐기, 만료 후 갱신, 새 리프레시 토큰의 반환·생략, 기존 세션의 자격 증명 부재, 카카오 장애·응답 유실, 중복 요청 및 재시작 후 같은 폐기 작업의 복구를 확인한다. 왓밥 공개 API 응답과 로그에 카카오 토큰 원문이 노출되지 않는지 확인한다.
- 연결 대상 확인과 취소, 정보 누락, 만료, 지연된 인증 결과 및 방 참여 제약을 공개 API로 검증한다. 같은 계정과 다른 계정을 각각 골라 이관 전후의 UUID 기록 및 기존 카카오 계정 기록을 비교한다.
- 실제 웹 브라우저에서 계정 연결·로그아웃·재연결, 같은 계정과 다른 계정 선택, 웹 콜백, 새로고침과 취소 후 재시도, 카카오톡 내장 브라우저 안내를 확인한다.
- 실제 iOS·Android 앱에서 계정 연결·로그아웃·재연결, 같은 계정과 다른 계정 선택, 인증 콜백 후 앱 복귀, 앱 재시작과 취소 후 재시도를 확인한다. 테스트용 카카오 계정으로 웹과 앱 각각에서 로그아웃한 세션의 토큰이 폐기되고 다른 세션이 유지되는지 확인한다.

## Out of Scope

- 카카오계정 자체의 로그인 상태 종료, 카카오 서비스 연결 해제 및 모든 기기 일괄 토큰 폐기.
- 별도 공개 카카오 토큰 갱신 API와 정기적인 선제 갱신.
- 앱과 웹 사이의 UUID 세션·기록 이전.

## Further Notes

- 확인한 프런트엔드 `main`의 웹 콜백은 단독 로그인 API로 연결되고, iOS·Android의 카카오 로그인 진입은 차단돼 있다. 웹은 기존 카카오 진입 경로를 UUID 계정 전환 API에 연결한다. 앱에는 카카오 인증 시작과 콜백, 앱 실행 간 세션 복원, UUID 계정 전환 API 연결을 구현한다. 앱 세션 자격 증명의 전달·저장 및 콜백 방식은 기존 백엔드 계약을 기준으로 구현 설계에서 확정한다. [프런트엔드 로그인 코드](https://github.com/pknu-wap/WHATBOB_FE/blob/21c3436b172e6c2d14449fc2b914e81f140cb548/app/src/app/login.tsx#L16-L28)
- 카카오 갱신은 필요할 때 서버가 호출하는 방식으로 설계했다. 카카오 장애 시 왓밥 로그아웃을 우선 완료하고 카카오 폐기 결과를 별도로 추적한다.
