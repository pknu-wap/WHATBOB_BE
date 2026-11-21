Part of #54

## Problem Statement

WhatBap 홈의 **최근에 먹은 메뉴**와 **요즘 인기 메뉴**에는 카드 자리만 있다. 사용자는 최근에 어떤 음식을 먹었는지, 요즘 다른 사용자들이 어떤 음식을 자주 먹는지 홈에서 바로 확인하고 싶다. 식사 기록에는 치즈버거와 불고기버거처럼 같은 음식 종류의 세부 메뉴가 여러 개 있고, 기존 일러스트가 모든 메뉴를 포괄하지 않는다. 출시 초기에는 식사 기록이 적어 사용자 데이터만으로 인기 목록을 채우기 어렵다.

## Solution

홈에 본인이 먹은 음식 종류를 최신순으로 최대 6개, 한국시간 기준 전날 하루의 식사 기록을 바탕으로 요즘 인기 메뉴를 최대 8개 표시한다. 집계에 필요한 사용자가 모이기 전에는 #54 댓글의 음식 8종을 소개한다. 실제 순위가 한 번 나온 후에는 기록이 줄어도 마지막 실제 인기 순위를 이어서 보여준다. 카드에는 음식 종류의 대표 일러스트를 사용하고, 사용할 그림이 없거나 대표 그림이 실제 메뉴와 크게 다르면 공통 이미지를 제공한다.

## User Stories

1. As a returning user, I want to see foods I recently ate on the home screen, so that I can recall my meals without opening my full history.
2. As a user who ate several burger variants, I want one burger card showing the latest meal, so that other food kinds have room in my recent list.
3. As a user with a long run of repeated meals, I want older distinct food kinds to fill the remaining slots, so that the latest six records do not consume all six cards.
4. As a user with fewer than six eligible food kinds, I want to see the available cards, so that each card represents a meal I recorded.
5. As a user with a meal timestamp in the future, I want it to become eligible when that time arrives, so that it does not displace an earlier meal in the home view.
6. As a user with a meal whose catalog menu was deleted, I want to retain its saved name in my full meal history, so that I can still recall what I ate.
7. As a user browsing home, I want deleted catalog menus excluded from recent cards, so that the home list reflects available menus.
8. As a UUID user, I want my own recent menu cards available with my existing session, so that I can use the feature before connecting Kakao.
9. As a user on a shared device, I want recent menu responses scoped to my own session, so that another account's meals are not exposed.
10. As a visitor without a session, I want to view popular foods, so that I can explore the product before signing in.
11. As an early user, I want the initial eight foods to appear in the agreed order, so that the home screen has useful cards before enough people contribute data.
12. As a visitor after launch, I want the popular list to reflect the previous day's meals in Korea time when enough people have recorded them, so that I can see which foods were popular that day.
13. As a visitor comparing popular foods, I want each person counted once per food kind, so that one person's repeated meals do not dominate the list.
14. As a visitor comparing similar menu entries, I want aliases such as 버거 and 햄버거 combined, so that their popularity is shown on one card.
15. As a visitor looking at foods tied by user count, I want the more recently eaten kind first, so that the order reflects recent activity.
16. As a visitor after a quiet day, I want the last actual popularity ranking to remain visible, so that the home screen does not revert to the initial list after it has real data.
17. As a visitor after a service restart, I want the saved actual ranking to be available, so that recovery preserves the ordering I saw before the restart.
18. As a visitor when a food kind loses all of its catalog menus, I want that card removed from the saved ranking, so that I can still browse the remaining foods in their established order.
19. As a visitor looking at popularity, I want menu-level cards without individual eating records, so that I can browse trends without seeing another person's history.
20. As a user scanning either menu list, I want similar menus to share representative artwork, so that I recognize food kinds consistently across the home screen.
21. As a user viewing a menu with missing or misleading artwork, I want a common image displayed, so that the card remains readable without suggesting a different dish.
22. As a designer, I want to reuse matching Figma illustrations, so that new cards retain the look of the existing artwork.
23. As an artwork requester, I want to approve generation that uses the supplied references, so that I can switch models before generation starts.
24. As a frontend developer, I want separate recent and popular menu endpoints, so that the two sections can load using their own authentication requirements.
25. As a frontend developer, I want to know whether a popular response is initial or aggregated and when its ranking was calculated, so that I can interpret the displayed result correctly.
26. As an operator, I want one ranking calculation at Korea's midnight and recovery of the latest missed run, so that restarts do not create gaps or duplicate publications.
27. As a catalog maintainer, I want each menu linked to a stable food kind, so that adding artwork or refreshing source data preserves consistent grouping.

## Implementation Decisions

### 최근에 먹은 메뉴

- 현재 사용자의 식사 기록에서 연결된 메뉴가 있고 `eaten_at`이 조회 시각 이하인 기록을 고른다. 음식 종류별로 `eaten_at`이 가장 큰 기록을 한 건 선택하고, 같은 시각에는 식사 기록 ID 내림차순을 쓴다. 선택된 기록을 최신순으로 정렬해 최대 6개를 반환한다. 오늘과 내일 기록이 같은 종류에 있으면 내일 시각이 되기 전에는 오늘 기록을 사용한다.
- 개인 식사 기록 조회는 개별 기록을 반환하는 기존 계약을 따른다. 메뉴 연결이 삭제된 기록의 저장된 메뉴명은 개인 식사 기록에서 조회할 수 있다.

### 요즘 인기 메뉴

- FastAPI 서버 내부에서 예약 작업을 실행해 매일 `Asia/Seoul` 자정에 전날 하루치 식사 기록을 집계한다. `eaten_at`은 전날 자정 이상, 집계일 자정 미만인 범위를 UTC로 비교한다. 예를 들어 11월 20일 자정 집계에는 한국시간 11월 19일의 기록이 포함된다. 서버가 자정을 놓치면 재시작 시 가장 최근에 놓친 집계만 해당 집계일의 전날을 대상으로 처리한다.
- 연결된 메뉴가 삭제된 식사 기록은 순위 점수와 전환 기준 양쪽에서 제외한다. 집계 기간에 조건을 만족하는 식사를 남긴 서로 다른 사용자가 5명 이상이면 실제 순위를 발행한다.
- 음식 종류마다 해당 종류를 먹은 서로 다른 사용자 수를 센다. 사용자 수 내림차순과 해당 종류의 가장 최근 식사 시각 내림차순으로 정렬한다. 두 값까지 같으면 음식 종류의 고유 ID 오름차순을 최종 정렬 기준으로 사용한다. 실제 순위는 최대 8종이다.
- 실제 순위가 발행되기 전에는 **떡볶이 → 돈까스 → 햄버거 → 마라탕 → 파스타 → 국밥 → 짜장면 → 제육볶음** 순서로 표시한다. 실제 순위가 있는 상태에서 조건을 충족한 사용자가 5명 미만이면 DB에 저장한 마지막 실제 인기 순위를 사용한다.
- 마지막 실제 인기 순위 한 벌을 MySQL에 보관한다. 새 순위가 나오면 기존 항목을 교체하고 집계 기준 시각과 마지막 성공 집계 날짜를 한 트랜잭션에서 갱신한다. 5명 미만인 집계는 성공 날짜만 갱신한다. 저장 중 실패하면 기존 순위와 두 날짜를 보존하고, 동일한 날짜의 작업이 겹치면 이미 성공한 집계는 다시 게시하지 않는다.
- 저장한 실제 순위에서 현재 메뉴가 하나도 없는 음식 종류는 응답에서 제외한다. 나머지 항목의 상대 순서와 그 순위의 집계 기준 시각을 유지한다. 인기 API는 저장된 결과를 조회한다.

### 음식 종류와 메뉴 일러스트

- 음식 종류에 고유 식별자, 표시명, 선택적 대표 일러스트 경로를 둔다. 메뉴 한 행은 음식 종류 하나를 참조한다. 초기 매핑에 버거·햄버거, 돈가스·돈까스, 스파게티·파스타, 자장면·짜장면처럼 합칠 값을 명시한다. 그 밖의 메뉴는 `base_name`에 따라 연결하고, 현재 운영 카탈로그에서 음식 종류가 비어 있는 행을 점검한다.
- 기존 메뉴는 `food_code`를 기준으로 종류를 연결하고, 백필을 검증한 뒤 외래 키와 필수 제약을 적용한다. 이후 시드도 같은 분류 정책을 적용하고 수동 분류를 보존한다. 음식 종류 연결은 홈 메뉴 표시·집계에 사용한다.
- 같은 음식 종류는 대표 일러스트를 공유한다. 대표 그림이 실제 메뉴와 크게 다른 메뉴는 별도 예외 테이블에 `menu_id`를 한 행씩 등록한다. 최근 카드에 그 메뉴가 표시되면 공통 이미지를 제공한다. 메뉴를 삭제할 때 예외 행도 삭제한다. 인기 카드는 음식 종류의 대표 그림을 사용하고, 대표 그림이 없으면 공통 이미지를 제공한다. 그림 선택은 음식 종류와 카드 순위에 영향을 주지 않는다.
- 두 홈 API의 `image_url`은 백엔드 기준 상대 경로다. 대표 경로가 비어 있거나 연결된 파일이 배포본에 없으면 두 API 모두 조회 가능한 공통 이미지 경로를 반환한다. 공통 이미지 파일은 첫 배포부터 제공한다.
- Figma 원본에서 재사용할 그림을 확인한다. 이후 디자이너가 새 PNG를 제공하면 홈 카드 크기에서 확인하고, 백엔드 정적 파일로 배포해 URL이 열리는 것을 확인한 뒤 DB에 음식 종류별 대표 경로를 등록한다. 다음 홈 조회부터 해당 음식 종류에 새 경로가 반영된다.
- 제공된 일러스트를 참조한 새 음식 이미지 생성이 별도로 요청되면 생성 직전에 작업 요청자에게 다시 연락한다. 요청자가 모델을 변경하고 명시적으로 허락한 뒤에 이미지 생성 도구를 사용한다.

### API 계약

- `GET /api/users/me/home-card-recently-eaten-food-max-6`는 기존 Bearer 인증으로 본인이 최근 먹은 음식 종류의 홈 카드 데이터를 최대 여섯 개 반환하며 `Cache-Control: no-store`를 사용한다. 음식 종류마다 가장 최근의 식사 기록 한 건을 대표로 사용한다. `data.items` 항목에 `food_kind_id`, `menu_name`, `eaten_at`, `image_url`을 제공한다. 표시할 기록이 없으면 빈 배열을 반환한다.
- `GET /api/users/me/meals`는 개별 식사 기록을 조회한다. 예를 들어 같은 햄버거 종류인 치즈버거와 불고기버거를 먹었다면 식사 기록에는 두 건이 표시되고, 홈 최근 음식 종류 목록에는 둘 중 가장 최근 메뉴 한 건이 표시된다.
- `GET /api/menus/popular`는 공개 조회다. `data`에는 `source`, `as_of`, `items`가 들어간다. `source`가 `initial`이면 `as_of`는 `null`이고, `aggregated`이면 실제 순위의 집계 기준 시각이다. 항목은 `food_kind_id`, `name`, `image_url`을 포함한다. 응답에는 사용자 이름이나 개별 식사 기록을 포함하지 않는다.
- 성공 응답은 기존 `ApiResponse` 형식을 사용하고 날짜·시각은 UTC ISO 8601로 전달한다.

## Testing Decisions

- 두 홈 HTTP API의 인증, 상태 코드, 응답 구조, 카드 순서와 파일 URL을 관찰한다. 기존 식사 기록 API의 통합 테스트와 공개 카탈로그 조회 테스트를 선례로 삼는다. SQL 구조, 내부 호출 횟수, private 메서드를 API 계약으로 취급하지 않는다.
- 최근 메뉴는 본인 인증과 데이터 분리, 빈 목록, 같은 종류의 여러 메뉴, 중복 기록 뒤에 있는 다른 종류, 최대 6개, 동시각 정렬, 삭제된 연결, 공통 이미지, 미래 기록의 표시 경계를 검증한다. 미래 기록이 같은 종류의 과거 기록을 가리지 않는지 확인한다.
- 인기 메뉴는 토큰 없이 조회하고, 초기 8종과 순서, 4명·5명 전환, 동일 사용자 중복, 음식 종류 별칭 합산, 동률 처리, 최대 8종, 삭제 메뉴가 전환 기준과 순위 양쪽에서 제외되는지를 확인한다. 저장된 목록에서 메뉴가 전부 삭제된 종류만 빠지고 `as_of`와 나머지 순서가 유지되는지 확인한다.
- 제어한 시계로 FastAPI 내부 예약 작업이 한국시간 자정에 집계를 호출하는지 확인하고, 공개 인기 API에서 전날 자정 기록의 포함과 집계일 자정 및 전날 이전 기록의 제외를 검증한다. 같은 날짜 재시도, 집계 중 실패, 재시작 후 보충, 기준 미달 시 마지막 순위 보존도 확인한다.
- 현재 MySQL 스키마에서 종류 연결 마이그레이션을 재실행해 기존 메뉴·식사 데이터 및 수동 매핑 보존과 종류 연결 제약을 확인한다. 기존 MySQL 마이그레이션과 동시성 통합 테스트를 선례로 삼는다. 대표 경로 미등록, 파일 누락, 신규 PNG 등록에서 두 홈 API의 이미지 URL과 브라우저의 파일 응답을 확인한다.

## Out of Scope

- 추천 알고리즘과 같이 고르기 결과에 메뉴 일러스트를 추가하는 작업.
- 개인 식사 기록 등록의 시간 검증 정책 변경과 저장된 식사 시각 교정.
- 사용자 업로드 메뉴 사진, 관리자 이미지 편집 도구, MySQL의 이미지 바이너리 저장.
- 사용자 승인 전 참조 일러스트 기반 이미지 생성.

## Further Notes

- 초기 8종과 순서는 [#54 댓글](https://github.com/pknu-wap/WHATBOB_BE/issues/54#issuecomment-7006671972)을 따른다. 마지막 순위 보관의 근거는 ADR-0004에 기록했다.
- 초기 데이터 등록 전 운영 DB의 실제 음식 이름을 대조한다. 새 PNG를 기다리는 동안 공통 이미지 경로와 두 홈 API의 동작을 먼저 검증할 수 있다.
