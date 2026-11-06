# WHATBOB

**WHATBOB**은 "오늘 뭐 먹지?"라는 고민을 덜어주는 음식 추천 서비스입니다.

간단한 주문서 질문에 답하면 취향에 맞는 메뉴를 추천받을 수 있고, 친구들과 방을 만들어 함께 메뉴를 고를 수도 있습니다.

이 저장소는 WHATBOB의 백엔드 API 서버입니다.

### 주요 기능

- **카카오 로그인**: 카카오 OAuth 인가 코드로 로그인하고 서비스 JWT를 발급합니다.

- **사용자 정보 관리**: 내 프로필 조회, 비선호 음식 조회·수정·삭제를 지원합니다.

- **주문서 기반 메뉴 추천**
  - 12개 주제, 204개 질문 후보 중 중요도 상위 7개와 탐색 질문 2개를 골라 제시합니다. 주제는 서로 겹치지 않습니다.
  - 질문 답변은 `예 / 아니요 / 상관없어요` 세 가지입니다.
  - 질문 초기 점수는 메뉴 분포의 샤논 엔트로피로 계산하고, 사용자 피드백이 쌓이면 통계를 반영해 다시 계산합니다.
  - 같은 종류의 메뉴는 결과에 하나만 넣고, 최근 추천과 싫어요 피드백을 반영해 결과가 한쪽으로 쏠리지 않게 합니다.
 
- **완전 랜덤 추천**: 음식 종류를 같은 확률로 먼저 고른 뒤, 그 종류 안에서 메뉴 하나를 무작위로 고릅니다.

- **알레르기·비선호 음식 제외**: 식약처가 지정한 19대 알레르기 유발물질을 기준으로, 사용자가 먹을 수 없거나 싫어하는 음식은 추천 후보에서 먼저 뺍니다. 후보가 없을 때도 이 제외 조건은 풀지 않습니다.

- **친구와 같이 고르기**
  - 방 생성·참여·조회·퇴장, 참여자별 주문서 완료 처리
  - WebSocket으로 참여자 상태를 실시간 전달하고, 연결이 끊겨도 60초 동안 재접속을 기다립니다.
  - 방장이 나가거나 생성 후 1시간이 지나면 방을 자동으로 삭제합니다.

## 기술 스택

| 구분 | 사용 기술 |
| --- | --- |
| Language | Python |
| Framework | FastAPI, Uvicorn |
| ORM / DB | SQLAlchemy 2.0, MySQL (PyMySQL) |
| 인증 | Kakao OAuth, PyJWT |
| 설정 | pydantic-settings (`.env`) |
| 테스트 | pytest (인메모리 SQLite) |

## 팀 소개

| <img src="https://github.com/hide-squid.png" width="100"> | <img src="https://github.com/pepertuna.png" width="100"> | <img src="https://github.com/Yungal.png" width="100"> |
| :---: | :---: | :---: |
| [hide-squid](https://github.com/hide-squid) | [pepertuna](https://github.com/pepertuna) | [Yungal](https://github.com/Yungal) |
| Backend | Backend | Backend |

## 라이선스

아직 라이선스를 정하지 않았습니다.
