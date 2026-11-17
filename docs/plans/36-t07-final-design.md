# #36 T07 최종 설계 확인안

- 상태: 2026-11-17 사용자 확인 완료 (`T07-D01`, `T07-D02` 전체 확정).
- 범위: 연결 유실·재접속·새로고침 시 단일 조회(`GET`)를 통한 공통 상태 및 본인 전용 상태 통합 복구(`Unified State & Viewer Snapshot`), 서버 재시작 시 유예 시간 연장 없는 DB 기준 상태 복원, 동시 요청 경합 정합성 보장, 방 종료·만료·삭제 시 라운드 진행 테이블 연쇄 정리(`ON DELETE CASCADE`) 및 개인 추천 누적 장부(`Recommendation`) 영구 보존.
- 선행 조건: `T01`, `T01N`, `T02`, `T03`, `T04`, `T05`, `T06`.
- 관련 티켓: [T07 티켓](36-tickets/T07-recovery-and-concurrent-requests.md).

## 1. 확정된 핵심 아키텍처 결정 요약 (`T07-D01` ~ `T07-D02`)

| 결정 ID | 주제 | 사용자 확정 선택 | 핵심 설계 계약 |
| :--- | :--- | :--- | :--- |
| **T07-D01** | 재접속·새로고침 시 화면 복구를 위한 상태 조회(`GET`) 응답 구조 | **1번 확정 (공통 상태 + 본인 전용 복구 상태 통합 반환 `Unified State & Viewer Snapshot`)** | 인증된 방 참여자가 상태 복구 조회(`GET`)를 호출하면, 1회의 요청(`1 RTT`)과 단일 DB 스냅샷(`state_version`) 안에서 ① WebSocket 알림과 동일한 **방·라운드 공통 상태(`common_state`)**와 ② **요청자 본인에게만 허용된 개인 진행 상태(`viewer_state`: 내 주문서/투표 제출 완료 여부, 주문서 단계 시 내 9개 질문 목록 등)**를 함께 반환한다. 타인의 개별 답변·개별 선택은 절대 노출하지 않으며, 비참여자 조회의 정보 공개 범위를 확대하지 않는다. |
| **T07-D02** | 방 종료·만료·삭제 시 라운드 테이블 및 개인 추천 장부의 삭제·보존 경계 | **1번 확정 (개인 추천 누적 장부는 영구 보존 + 닫힌 방의 라운드 테이블은 `ON DELETE CASCADE` 청소 `Scoped Cascade & Personal Ledger Isolation`)** | 방장 퇴장·유예 만료·방 유효시간 만료로 `rooms` 행이 삭제될 때, 그 방에 종속된 일회성 라운드 진행 테이블(`room_rounds` → `room_round_members`, `room_round_candidates` → `room_round_candidate_proposers`)은 외래키 `ON DELETE CASCADE`로 남김없이 함께 정리하여 고아 레코드와 외래키 삭제 충돌을 원천 차단한다. 반면 `T03-D03`에 따라 생성된 각 참여자의 개인 추천 장부(`Recommendation`, `RecommendationItemSnapshot`)는 사용자 계정에 독립 보존(`ON DELETE SET NULL` 또는 비종속 참조)하여 이후 같은 멤버들이 다시 모였을 때 누적 추천 학습과 다양성 보정이 100% 유지되게 한다. |

## 2. 확정된 상세 복구·경합·정리 계약

1. **재접속 및 알림 누락 시 화면 복구 (`T07-D01`)**:
   - 참여자 앱이 일시적 연결 끊김 후 60초 유예 내에 복귀하거나 WebSocket 알림을 놓친 경우, 인증된 상태 조회(`GET`) 한 번으로 현재 단계(`WAITING`, `ANSWERING`, `ANSWERING_PAUSED`, `VOTING`, `VOTING_PAUSED`, `RESULT`)와 `state_version`, 그리고 본인의 제출·투표 완료 여부를 즉시 복원한다.
   - 프론트엔드는 조회 응답의 `state_version`을 기준으로 이후 도착하는 WebSocket 알림 중 같거나 낮은 버전의 과거 상태를 무시한다(`T01N` 버전 비교 규칙과 100% 동일).
2. **서버 재시작 복구 계약**:
   - 서버 재시작 시 DB에 저장된 기존 연결 끊김 시각(`disconnected_at`)과 유예 만료 기준을 연장하지 않으며, 이미 만료된 유예는 즉시 정리하고 남은 유예는 잔여 시간만 적용한다.
   - 진행 중이던 라운드 단계, 참여자별 사전 생성 질문(`T02`), 개별 제출 및 1순위 메뉴(`T03`), 투표 회차(`vote_round_no`) 및 투표 완료 상태(`T04`, `T06`), 방장 결정 대기 및 `decision_version`(`T05`, `T06`)은 모두 MySQL에 지속 저장된 상태를 기준으로 복원한다.
3. **동시 요청 경합 직렬화 (`Room` → `RoomRound` 잠금 순서 통일)**:
   - ① 방장 시작 vs 참여자 입·퇴장, ② 마지막 주문서/투표 제출 vs 다른 참여자 퇴장(명시적 퇴장 또는 60초 유예 만료), ③ 방장의 계속/취소 결정 vs 추가 연속 퇴장, ④ 60초 유예 만료 자동 퇴장 vs 아슬아슬한 재접속 등 모든 경합 경로에서 `Room` 및 활성 `RoomRound` 행 잠금(`SELECT ... FOR UPDATE`) 순서를 일관되게 적용하여 단 하나의 유효한 상태로만 수렴하게 한다.
4. **방 삭제·종료 시 참조 무결성 및 증분 마이그레이션 검증 (`T07-D02`)**:
   - 방 삭제 시:
     - `room_rounds` 및 하위 테이블(`room_round_members`, `room_round_candidates`, `room_round_candidate_proposers`): `ON DELETE CASCADE`로 자동 정리.
     - `T01N` 발송 대기/요약 테이블: `T01N-D08` 결정대로 `ON DELETE SET NULL` 및 이력 식별자 보존(3일 보관).
     - 개인 추천 기록(`Recommendation`, `RecommendationItemSnapshot`, `UserMeal`, `RecommendationFeedbackState`): 절대 삭제하지 않고 영구 보존.
   - 선행 티켓(`T01`~`T06`)의 증분 마이그레이션을 격리된 MySQL 환경에서 기존 데이터 보존·재실행 안전성·독립 세션 동시 경합 테스트와 함께 검증한다.
   - 사용자 보류 항목인 `D19`(결과 이후 같은 방에서 다시 고르기)는 본 티켓에서 임의로 결정하거나 구현에 포함하지 않고 결정 대기 상태를 유지한다.
