## Problem Statement

현재 WhatBap의 사용자 생성과 인증은 카카오 계정을 전제로 한다. 사용자는 가입·로그인 없이 추천과 같이 고르기를 시작하고, 같은 브라우저에서 음식 취향과 식사 기록을 이어 쓰며, 필요할 때만 카카오 계정에 기록을 모으고 싶다.

현재는 로그인 없는 이용자의 UUID 계정과 재방문 복원, UUID 기록의 카카오 계정 이관, 직접 편집하는 고유 닉네임·프로필 사진 업로드가 없다. 방 연결 종료 후 일반 참여자를 오프라인으로 남기는 기존 동작도, 60초 유예 후 자동 퇴장시키려는 새 정책과 다르다.

## Solution

첫 방문에 UUID 사용자와 중복 없는 랜덤 표시 닉네임·기본 프로필 이미지를 자동 제공한다. 개인 추천, 음식 제외 조건, 식사 기록과 방 참여를 같은 사용자에게 연결한다. 카카오 로그인은 모든 기능의 필수 조건에서 제외하고 기록을 계정에 모아 이어 쓰는 선택 기능으로 유지한다.

카카오 로그인에 성공하면 해당 UUID의 기록을 새로 연결되거나 이미 존재하는 카카오 계정으로 이관하고 UUID를 폐기한다. 같은 카카오 계정으로 이관되는 데이터는 모두 보존해 합친다. 프로필 출처·직접 편집 우선순위와 방 참여 중 계정 전환 제한을 명확히 적용한다.

닉네임과 프로필 사진은 사용자가 수정할 수 있다. 사진은 개발 단계에서 로컬 파일로 저장하고 운영 저장소 연결은 추후 작업으로 둔다.

## User Stories

1. As a first-time user, I want a UUID identity to be created automatically, so that I can start without registration or login.
2. As a first-time user, I want a random display nickname and default profile image, so that I can be identified immediately.
3. As a UUID user, I want my generated nickname to be unique, so that room participants can distinguish me.
4. As a returning UUID user, I want the same browser to restore my identity automatically, so that my nickname and records remain available.
5. As a UUID user on another device or after clearing browser data, I want a new identity, so that unrelated browser sessions are not silently linked.
6. As a UUID user, I want personal recommendations without Kakao login, so that authentication does not interrupt choosing food.
7. As a UUID user, I want allergy, inability and dislike settings attached to my identity, so that recommendations respect my exclusions.
8. As a UUID user, I want to save and retrieve meal records, so that my eating history survives a browser revisit.
9. As a UUID user, I want recommendations and feedback attached to my identity, so that personalization continues across visits.
10. As a UUID user, I want to create and join rooms without Kakao login, so that I can choose food with friends immediately.
11. As a room participant, I want my personal identity, nickname and exclusions to follow me into the room, so that group participation uses the same user.
12. As a user, I want my private records accessible only through my own authenticated session, so that knowing another user's UUID does not grant access.
13. As a UUID user, I want optional Kakao login, so that I can move my records to an account when I choose.
14. As a UUID user connecting Kakao for the first time, I want all my records moved to that account, so that I can continue using them after login.
15. As a user with an existing Kakao account, I want UUID records merged into that account, so that no duplicate Kakao account is created.
16. As an existing Kakao user from before this feature, I want my current profile and history preserved, so that the transition does not erase my account.
17. As a user with multiple UUID identities, I want records from each identity merged when each browser logs into the same Kakao account, so that my history becomes available together.
18. As a user, I want only UUID records that I control to be migrated, so that another browser's records cannot be claimed by supplying an identifier.
19. As a user completing migration, I want the migrated UUID retired, so that obsolete identities cannot continue accessing migrated records.
20. As a user, I want failed Kakao authentication or migration to leave my UUID and records intact, so that an unsuccessful login causes no data loss.
21. As a user retrying login, I want records migrated at most once, so that retries do not duplicate meals, recommendations or feedback.
22. As a user logging in concurrently, I want one coherent Kakao account and complete records, so that concurrent requests do not lose data.
23. As a user merging exclusions, I want allergies, inability settings and dislikes combined, so that no exclusion is silently dropped.
24. As a user with overlapping exclusions, I want duplicate entries within the same exclusion type removed, so that the merged list remains usable.
25. As a user with a large merged exclusion list, I want every entry retained, so that an existing request-size limit does not truncate my settings.
26. As a user merging meal history, I want every original record and eating time preserved, so that repeated meals are not mistaken for duplicates.
27. As a user merging recommendation history and feedback, I want the original ordering preserved, so that my latest evaluations retain their meaning.
28. As a user with a new Kakao-linked account, I want the profile from my earliest-created migrated UUID used by default, so that profile selection does not depend on login order.
29. As a user who edited my Kakao-linked profile, I want that edited profile retained during later migrations and logins, so that old profiles do not overwrite my changes.
30. As a user, I want service profile choices preserved instead of being replaced by Kakao's nickname and image, so that my WhatBap profile remains under my control.
31. As a user, I want to edit my nickname before or after Kakao login, so that I can choose how I appear.
32. As a user, I want nicknames validated as 2–20 characters using Korean, English letters, digits and spaces, so that display names follow a clear rule.
33. As a user, I want surrounding whitespace removed, so that accidental spaces do not produce misleading names.
34. As a user choosing a nickname already used by someone else, I want a clear rejection, so that display nicknames remain unique.
35. As a user keeping my current nickname, I want the save to succeed, so that my own name is not treated as another user's duplicate.
36. As a user, I want to upload my own profile photo, so that I can personalize the default image.
37. As a user, I want JPG, PNG and WebP uploads up to 5 MB accepted, so that supported photos work predictably.
38. As a user uploading an invalid or oversized image, I want the upload rejected without changing my existing photo, so that failures preserve my profile.
39. As a user, I want to replace my profile photo, so that I can update my appearance.
40. As a user, I want deleting my photo to restore the default image, so that I can stop displaying an uploaded photo.
41. As a user logging out of Kakao, I want a fresh UUID and random nickname, so that I can resume login-free use separately.
42. As a user who logged out, I want my Kakao records preserved, so that logging out does not delete my account history.
43. As a user logging in again, I want the new UUID's records added to my Kakao account, so that my later activity is not lost.
44. As a room participant, I want login and logout blocked until participation ends, so that my identity does not change during a room session.
45. As a user whose Kakao account is participating in a room on another device, I want login on this device rejected, so that account migration does not occur during that participation.
46. As a participant whose final room connection disconnects, I want my participation retained for 60 seconds, so that a brief interruption does not remove me immediately.
47. As a participant reconnecting within that grace period, I want my membership and completion state restored, so that I can continue where I left off.
48. As a participant with another tab still connected, I want my membership retained, so that closing one connection does not start an unnecessary exit.
49. As an ordinary participant absent beyond the grace period, I want to leave automatically, so that disconnected members do not remain indefinitely.
50. As a remaining room participant, I want participant and completion counts updated after automatic departure, so that room progress stays accurate.
51. As a host absent beyond the grace period, I want the room closed, so that participants are not left in a room without its host.
52. As a participant receiving a room update or closure, I want consistent REST and WebSocket state, so that the screen reflects the actual room lifecycle.
53. As a participant during the 60-second grace period, I want account switching to remain blocked, so that temporary disconnection cannot bypass the restriction.
54. As a user after my participation ends, I want account switching available again, so that stale room rows or delayed cleanup do not block me indefinitely.
55. As a developer, I want profile photos stored locally during development, so that upload behavior can be developed without cloud setup.
56. As a developer, I want image storage to be configurable, so that an operational storage provider can be added later without changing user-facing behavior.

## Implementation Decisions

### 사용자와 브라우저 인증

- 사용자·인증 모듈에 UUID 사용자 생성, 재방문 복원·갱신, 선택적 카카오 계정 연결과 로그아웃 계약을 제공한다. 기존 사용자·추천·식사·방 모듈의 권한 검사는 같은 현재 사용자 식별 경계를 사용한다.
- 기존 내부 정수 PK와 관련 외래키는 가능한 한 유지한다. UUID 사용자 식별과 카카오 계정 귀속은 구분하며 UUID 사용자에는 카카오 ID를 필수로 요구하지 않는다.
- UUID는 사용자 식별자이며, 본인 확인에는 별도 비밀 인증 정보를 사용한다. 공개 UUID나 기존 익명 질문 순환 값만으로 개인 기록에 접근할 수 없게 한다.
- 기존 Bearer 접근 토큰 및 WebSocket의 최초 인증 메시지 흐름을 활용한다. 브라우저가 보관한 재방문 인증 정보로 같은 사용자를 복원하고 접근 토큰 만료 시 자동 갱신할 수 있게 한다. 접근 토큰 만료만으로 새 UUID를 만들지 않는다.
- 최초 생성·복원·갱신·로그아웃 응답은 프론트가 현재 사용자 유형, UUID의 유효 여부, 표시 닉네임·사진과 인증 상태를 일관되게 갱신할 수 있어야 한다. UUID와 계정 정보는 Request/Response DTO로 표현한다.
- 기존 내 정보·비선호·식사 기록·추천 및 방 API에서 UUID 사용자도 본인 데이터로 동작하게 한다. 카카오 로그인 없는 이용과 인증 정보가 없는 요청은 구분한다.
- 2026-11-13 T02 사용자 승인: 랜덤·주문서 메뉴 추천은 유효한 서비스 JWT와 DB 인증 세션 검증 후 제공한다(B). 기존 JWT 없는 메뉴 추천 호출은 인증 오류로 바뀌며 인증 실패 시 비인증 추천으로 자동 전환하지 않는다. 적용 범위·연동 영향과 남은 설계는 [T02 아키텍처](../plans/39-t02-personal-data-architecture.md)를 따른다. 이 항목은 승인된 설계이며 아직 실행 코드에 적용하지 않았다.
- 2026-11-13 T02 추가 승인: 주문서 질문 조회도 동일한 JWT·DB 인증 세션 검증을 필수로 하고, 서버가 확인한 사용자 기준으로 질문 선택 이력을 이용한다(①). 기존 상위·탐색 질문 선택 알고리즘은 유지하며 JWT 없는 질문 경로로 우회하지 않는다. 기존 비인증 질문 이력의 삭제·귀속 이관은 이번 선택에 포함하지 않으며, 아직 실행 코드에 적용하지 않았다.
- 2026-11-13 T02 추가 승인: 피드백도 JWT·DB 인증 세션 검증 후 현재 본인 소유 추천에만 남기며 평가 메뉴가 해당 추천에 포함됐는지 확인한다. 거절된 요청은 피드백·통계를 변경하지 않는다. 사용자 없는 기존 추천은 보존하되 일반 사용자 피드백은 받지 않는다. 반복 평가·수정·중복 전송은 별도 설계하며 아직 실행 코드에는 적용하지 않았다.
- 2026-11-13 T02 후속 선택: 평가 변경과 변경 이력 보존을 허용한다(D04). 사용자가 개인 메뉴 선호의 누적 반영을 추가 요청했다(D05). 기존 최신 평가 방식의 다음 한 번만 적용되는 효과를 수정하는 것이 아니라 집계 방식을 바꾸는 설계이며, 누적 단위·계산식·수정·재전송·공통 통계의 세부는 미결정이다. 아직 실행 코드에는 적용하지 않았다.
- 2026-11-13 D05 후속 승인: 개인 선호 누적의 단위는 같은 사용자·추천·메뉴별 최신 유효 평가 한 건이다. 서로 다른 추천의 유효 평가는 누적하고 수정 이력은 모두 보존하며 통신 재전송은 새 평가로 집계하지 않는다. 계산식·공통 통계·적용 범위 등 나머지 세부는 아직 미결정이며 실행 코드에 적용하지 않았다.
- 2026-11-13 D06 승인: 알레르기·섭취 불가·비선호의 유형별 전체 목록 저장은 기존 비선호와 같이 DB 잠금으로 직렬화하고 마지막 성공 저장을 우선한다. 목록 버전 검사는 필수로 도입하지 않으며 오래된 화면의 전체 저장도 유효하면 반영한다. 다른 유형·사용자 데이터는 보존하고 저장 실패는 롤백한다. 알레르기·섭취 불가 API 확장은 아직 실행 코드에 적용하지 않았다.
- 2026-11-13 D07 승인: 알레르기 신규 등록은 서버의 선택 목록에서만 허용하며 목록 밖 자유 입력은 받지 않는다. 고정 코드의 유효성을 서버가 검증하고 실제 제외 조건으로 매핑한다. 기존 목록 밖 알레르기 데이터는 임의 삭제·변환하지 않으며 조회·편집 호환은 후속 정리한다. 섭취 불가·비선호를 같은 목록으로 제한한 결정은 아니며 아직 실행 코드에 적용하지 않았다.
- 2026-11-13 D08 승인: 섭취 불가 신규 등록도 서버의 음식·재료 선택 목록에 한정하며 직접 입력은 받지 않는다. 목록의 구체적 항목·범위·매핑은 미결정이고 알레르기 목록 재사용을 승인한 것은 아니다. 기존 목록 밖 데이터는 보존하며 조회·편집 호환은 후속 정리한다. 비선호 입력 범위와 실행 코드는 아직 변경하지 않았다.
- 2026-11-13 D09 승인: 섭취 불가는 개별 메뉴가 아닌 재료 중심의 서버 선택 목록으로 제공하고, 재료 코드를 메뉴 분류·태그·키워드와 연결해 제외한다. 초기 8개 항목은 데이터 매핑 검토가 필요한 초안이며 모든 항목·범위를 확정한 것은 아니다. 대표 주재료와 추정 태그를 전체 재료 명세로 간주하지 않으며 아직 실행 코드에 적용하지 않았다.
- 2026-11-13 D10 승인: 개인 메뉴 선호의 누적은 오늘 주문서 답변 점수에 대한 보조 가산·감점으로 반영하고 유효 평가 건수·만족 비율을 참고하며 영향에 한도를 둔다(②). 실제 반영 강도·수식·스케일은 같은 T02 설계에서 구현 전에 정하고, 100점·±10점·기준 건수 4의 설명 예시를 확정값으로 사용하지 않는다. 강제 제외를 우선하며 아직 실행 코드에 적용하지 않았다.
- 2026-11-13 D11·D12 최신 선택: 여러 신호를 학습하는 추천 모델을 사용하고 실제 사용자 피드백으로 조정한다. 모델 종류·학습 신호·주기·예측·데이터 부족 처리와 D10 연결은 [체크리스트](../plans/39-t02-architecture-checklist.md)에서 선택한다. 여러 항목을 trade-off와 함께 제시하고 사용자가 선택·보류·질문하는 방식으로 진행하며 미응답이나 추천을 승인으로 간주하지 않는다. 아직 실행 코드에 적용하지 않았다.
- 2026-11-13 D13 후속 승인: 체크리스트 1A·2A·3A·4A·5B·6A·7A·8B를 선택했다. 오늘 답변 기본 점수의 제한 재정렬, 명시적 만족 라벨을 학습하는 공용 로지스틱 모델, 주기 학습·개인 입력 갱신, 현재 백엔드 예측·별도 학습, 인증·제외 검증 후 기존 추천 대체 경로와 최근 평가 중시로 설계한다. 9·10번은 보류이고 실제 계수·시간·학습·버전·검증 계약과 실행 코드 적용은 남아 있다.
- 2026-11-13 D14 최신 선택·일정: 9B(기존 값을 정식 항목과 정확히 대응한 뒤 편집)와 10A(주문서·피드백 본문 사용자 번호 제거·JWT 전용)를 승인했다. 대응이 불명확한 원본은 보존한다. 오늘 2시간 제한은 아키텍처 설계이며 구현·실제 검증은 내일(2026-11-14) 진행한다. [설계 마무리 계획](../plans/39-design-completion-plan.md)을 따른다.
- 카카오 사용자 정보 조회에서는 계정 식별을 검증하되 서비스에서 정한 프로필을 매번 카카오 닉네임·사진으로 동기화하지 않는다.

### 기록 이관과 프로필 우선순위

- 현재 UUID의 유효한 인증과 카카오 계정 인증을 모두 확인한 뒤 해당 계정으로 이관한다. 요청에 전달된 임의의 UUID나 사용자 ID만으로 다른 기록을 가져오지 않는다.
- 카카오 계정이 있으면 그 계정을 사용하고 없으면 계정 귀속을 만든다. 성공 후 원본 UUID와 그 인증 정보는 폐기하고 현재 브라우저를 계정 인증 상태로 전환한다.
- 이관, 원본 UUID 폐기, 계정 귀속과 인증 전환은 실패 시 원본을 보존할 수 있도록 일관되게 처리한다. 재시도·동시 로그인에도 동일한 기록이 두 번 이관되지 않게 한다.
- 새로운 카카오 계정의 기본 프로필은 가장 먼저 생성된 이관 대상 UUID의 프로필이다. 원본 UUID 폐기 후에도 이 출처 시점을 보존하여, 늦게 로그인한 더 오래된 UUID를 비교할 수 있어야 한다.
- 사용자가 카카오 계정에서 직접 수정한 프로필은 이후 UUID 이관·재로그인으로 덮어쓰지 않는다. UUID 도입 이전부터 존재한 카카오 계정의 프로필과 기록도 보존한다.
- 음식 제외 조건은 유형별로 합집합을 만들고 기존 정규화 규칙을 활용해 같은 항목을 중복 제거한다. 기존 비선호 요청의 100개 제한 때문에 병합 결과를 자르지 않으며, 합쳐진 전체 목록을 조회·편집할 수 있게 계약을 맞춘다.
- 식사·추천·피드백은 원래 ID와 일시·평가 순서를 유지해 귀속만 옮긴다. 같은 메뉴와 시각이라는 이유로 식사 기록을 삭제하거나 피드백을 재생성하지 않는다.
- 사용자별 질문 순환 상태도 유실되지 않게 이관한다. 누적 노출은 합치고 직전 질문 상태는 최신 기록을 기준으로 한다.
- 제외 조건과 후보 없음 처리는 기존 도메인 규칙을 유지한다. 병합을 이유로 알레르기·섭취 불가·비선호를 해제하지 않는다.
- DB 제약과 적절한 트랜잭션으로 닉네임 충돌, 계정 중복 생성 및 같은 계정으로의 동시 이관을 제어한다.

### 방 참여와 계정 전환

- 방 참여 요청 시작부터 응답 대기 중에도 로그인·로그아웃·계정 전환을 차단한다. 참여 성공 시 방 참여 중 제한을 유지하고, 참여 실패가 확정되면 요청 중 차단을 해제한다. 응답 유실·시간 초과 시 서버에서 참여 여부와 처리 결과를 확인한 뒤 해제 여부를 판단하며, 처리 중 요청의 뒤늦은 성공 가능성을 단순 미참여 조회로 배제하지 않는다. 프론트 차단과 서버 검증을 함께 적용한다.
- 로그인·로그아웃 시 현재 사용자의 유효한 방 참여 여부를 확인한다. 로그인 대상 카카오 계정이 다른 기기에서 유효한 방에 참여 중인 경우도 거절한다.
- 계정 전환과 방 생성·참여가 동시에 실행되어도 제한이 우회되지 않도록 상태 확인과 변경을 직렬화한다. 전환 거절 시 프로필·기록·UUID를 변경하지 않는다.
- 온라인 여부만으로 방 참여를 판단하지 않는다. 60초 유예 동안은 참여 상태와 로그인·로그아웃 제한을 유지한다.
- 사용자의 마지막 연결 종료 후 60초 안에 재접속하면 종료 예약을 취소하고 기존 참여·완료 상태를 유지한다. 여러 탭 중 하나만 종료된 경우는 마지막 연결 종료로 보지 않는다.
- 60초가 지나면 일반 참여자의 주문서·참여 정보를 기존 퇴장 의미에 맞게 정리하고 인원·완료 집계를 재계산한다. 방장의 유예가 끝나면 기존 방 종료·이벤트·관련 데이터 정리 흐름을 사용한다.
- WebSocket 상태 변화와 DB 정리 결과를 일관되게 알린다. 재접속·유예 만료 경합과 서버 재시작 후 정리 누락을 처리한다.
- 만료되거나 이미 종료된 방 때문에 로그인이 무기한 차단되지 않도록 유효한 참여 상태를 판정한다.

### 닉네임과 프로필 사진

- 표시 닉네임은 단어 조합·숫자로 자동 생성하며 충돌 시 다시 생성한다. 직접 수정 시 앞뒤 공백 제거 후 2~20자와 한글·영문·숫자·공백 제약을 적용한다.
- 다른 사용자가 사용 중인 이름은 충돌 응답으로 거절한다. 본인 이름을 그대로 저장하는 요청은 허용한다. 생성·편집·이관에서 같은 유일성 규칙을 사용한다.
- UUID 사용자와 카카오 로그인 사용자 모두 프로필을 편집할 수 있다. 직접 편집한 사실과 기준 프로필 출처를 보존하여 이관 우선순위를 적용한다.
- 기본 프로필 이미지와 본인 사진 업로드·교체·삭제를 제공한다. 사진 삭제는 기본 이미지로 돌아가는 동작이며 사용자 기록 삭제가 아니다.
- JPG·PNG·WebP, 최대 5MB를 허용한다. 확장자뿐 아니라 실제 이미지와 크기를 검증하고 실패 시 기존 프로필을 보존한다.
- 로컬 업로드 모듈은 설정 가능한 데이터 디렉터리, 서버가 생성한 파일명과 안전한 파일 경계를 사용한다. 업로드 데이터는 소스 버전 관리에서 제외한다.
- 교체·삭제 시 다른 사용자나 이관된 계정이 참조하는 파일을 삭제하지 않도록 소유권과 참조를 확인한다. 추후 운영 저장소를 연결해도 프로필 편집 계약을 유지할 수 있게 저장 책임을 분리한다.

### API·DB 변경의 호환성

- 기존 서비스 계층과 DTO 패턴을 유지하고 Entity를 직접 응답하지 않는다. 기존 조회·추천·피드백·방 계약의 변경은 호출부 영향을 확인해 API 문서에 명시한다.
- 본인 인증 실패, 권한 없음, 잘못된 입력, 닉네임·방 상태 충돌과 예상하지 못한 저장 실패를 구분하여 기존 오류 응답 관례를 따른다.
- 기존 DB의 카카오 필수 조건을 완화하고 UUID·프로필 출처·편집 상태·재방문 인증에 필요한 데이터와 제약을 마이그레이션한다. 애플리케이션 시작 시 테이블 생성만으로 기존 스키마가 변경된다고 가정하지 않는다.
- 기존 카카오 데이터와 참조를 보존한다. 기존 데이터의 중복 닉네임 등 새 제약과 충돌하는 값은 사전 점검으로 명확히 보고하고, 해결되지 않은 충돌을 숨기거나 보존 대상 프로필을 임의로 바꾸지 않는다.
- 기능 구현 후 실제 메서드·경로·요청·응답, DB 변경과 검증 결과를 프로젝트 기록과 연동 문서에 남긴다.

## Testing Decisions

### 합의한 검증 경계

- 사용자 확인을 받은 주 경계는 실제 FastAPI 애플리케이션의 REST API·WebSocket 진입점이다. 내부 함수별 테스트를 대량 추가하기보다 사용자 요청부터 저장·응답·이벤트까지의 관찰 가능한 동작을 검증한다.
- 기존 테스트 클라이언트, 의존성 대체와 격리된 SQLite DB를 재사용한다. 사진은 임시 디렉터리에 저장하고 실제 파일 업로드·조회·교체·삭제 결과를 확인한다.
- 카카오의 외부 HTTP 응답과 시간만 통제한다. UUID 이관·권한 검사·데이터 저장·닉네임 유일성·파일 검증 자체를 모의 처리하지 않는다.
- 60초 유예는 테스트 시계를 제어하거나 만료 기준 시각을 주입해 확인한다. 실제로 60초를 기다리는 테스트를 기본으로 삼지 않는다.
- HTTP·WebSocket만으로 관찰하기 어려운 재시작 복구는 정리 작업의 상위 실행 경계에서 호출하고, 결과는 방 조회·이벤트·저장 상태로 확인한다.

### 검증할 모듈과 시나리오

- 사용자·인증: UUID 자동 생성, 같은 인증 정보의 재방문 복원, 별도 브라우저의 독립성, 토큰 갱신, 타 UUID 접근 차단, 로그아웃 후 새 UUID 생성과 카카오 기록 보존.
- 계정 이관: 새·기존 카카오 계정, 다중 UUID, 생성 순서와 로그인 순서 역전, 직접 수정한 프로필, 기존 계정 보존, 원본 UUID 폐기·기존 인증 무효화, 실패 롤백과 중복 재시도.
- 추천·사용자 기록: 유형별 제외 목록 합집합과 중복 제거, 합계 100개 초과 시 무손실 조회·편집, 식사·추천·피드백의 원본 순서 및 개인화 상태 보존.
- 프로필: 닉네임 길이 1·2·20·21자, 허용·금지 문자, 공백 처리, 본인 이름 재저장과 타인 이름 충돌, 랜덤 생성 충돌 재시도.
- 사진: 세 가지 허용 형식, 용량 경계, 확장자와 실제 내용 불일치, 잘못된 이미지, 업로드 실패 시 기존 사진 유지, 교체·삭제와 기본 이미지 복귀, 타인의 파일 수정 차단.
- 방·WebSocket: 로그인·로그아웃 제한, 다른 기기의 대상 계정 참여, 마지막 연결과 복수 탭, 유예 안 재접속, 60초 만료 후 참여자 퇴장·방장 종료, 인원·완료 집계와 이벤트, 정리 작업 복구.
- 회귀: 기존 카카오 인증, 내 정보, 비선호·식사 API, 개인 추천·피드백, 방 생성·참여·퇴장·만료의 영향을 확인한다.

### 동시성·운영 DB 보완

- 실제 MySQL의 유일 제약·잠금·트랜잭션이 필요한 동시 카카오 이관, 닉네임 경쟁, 방 참여와 계정 전환 경쟁은 전용 MySQL 테스트로 보완한다.
- SQLite 결과만으로 MySQL 동시성 검증을 통과했다고 보고하지 않는다. MySQL 테스트 DB가 없으면 건너뛴 항목과 남은 검증을 명시한다.
- 기존 DB 전환은 대표적인 카카오 사용자·관련 기록을 가진 테스트 스키마에서 데이터·참조 보존과 새 제약을 검증한다.
- 기존 선례는 카카오 로그인·내 정보의 실제 앱 경로 테스트, 격리 DB의 비선호·식사 기록 테스트, 방 REST·WebSocket 및 접속 유예 테스트, 전용 MySQL 동시 참가 테스트다.

## Out of Scope

- 공동 후보 계산·그룹 추천 알고리즘 등 #36의 기능 구현.
- 운영 사진 저장소 선택·생성·연결, S3 도입 및 배포 작업.
- 카카오 연결 없이 다른 기기에서 UUID 사용자를 복구하거나 임의의 UUID 기록을 가져오는 기능.
- 추가 소셜 로그인 제공자와 별도 비밀번호 회원가입.
- UUID 사용자의 닉네임·사진을 카카오 프로필로 계속 자동 동기화하는 기능.
- 메뉴 데이터나 개인 추천 가중치 알고리즘의 별도 개선. 단, 사용자가 추가한 T02 D05·D11의 개인 평가 누적·여러 신호 학습형 추천은 포함하며 데이터·모델·학습·검증·예측 세부를 설계한다. 이를 그룹 추천이나 다른 알고리즘 전반의 개선 승인으로 해석하지 않는다.
- 프론트엔드 전체 화면 구현. 백엔드와 연동하기 위한 세션·응답·오류·업로드 계약과 연동 검증 사항은 포함한다.

## Further Notes

- 이 명세는 [기존 이슈 #39](https://github.com/pknu-wap/WHATBOB_BE/issues/39)를 구체화한 것이다. 기본 UUID 이용, 선택적 카카오 기록 이관 및 방 재접속·계정 전환에 관한 최신 합의를 따른다.
- 이전 대화의 카카오 로그인 전체 제거, 대표 UUID 유지, 방 참여 중 계정 이관, 준비된 사진만 선택하는 방식은 최종 결정에서 대체되었으므로 구현하지 않는다.
- 테스트 경계는 사용자가 확인했다. 새 기능의 구현·빌드·테스트가 완료되었다는 뜻은 아니며, 이 단계는 명세 작성과 게시다.
- 운영 사진 저장소는 미정이며 개발용 로컬 저장만 포함한다. 실제 운영 전에는 사진 영속성과 브라우저 인증·프론트 연동을 배포 환경에서 별도 확인해야 한다.
