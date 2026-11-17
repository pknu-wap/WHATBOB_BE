# #36 T03 (+ D20) 최종 설계 확인안

- 상태: 2026-11-17 사용자 확인 완료 (`T03-D01`, `T03-D02`, `T03-D03`, `D20` 전체 확정).
- 범위: 참여자 개인 주문서 제출 및 검증, 제출 즉시 개인 1순위 후보 메뉴 산출(`Eager Evaluation`), 개인별 추천 이력(`Recommendation` / `RecommendationItemSnapshot`) 누적 반영, 전원 제출 완료 시 동일 메뉴 병합(`Deduplication`) 및 정규화 제안자 연결 테이블(`room_round_candidate_proposers`) 저장, 후보 수(2개 이상 → `VOTING`, 1개 → 즉시 `RESULT`, 0개 → 실패 정리)에 따른 자동 단계 전환.
- 선행 조건: `T02` (라운드 시작 및 질문 배정), `D20` (주문서 추가 제외 조건 정책 — 1번 확정).
- 관련 티켓: [T03 티켓](36-tickets/T03-submit-order-sheets-and-candidates.md), [D20 결정 티켓](36-tickets/D20-extra-exclusions-decision.md).

## 1. 확정된 핵심 아키텍처 결정 요약 (`T03-D01` ~ `T03-D03`, `D20`)

| 결정 ID | 주제 | 사용자 확정 선택 | 핵심 설계 계약 |
| :--- | :--- | :--- | :--- |
| **T03-D01** | 개인별 1순위 추천 메뉴 계산 시점 | **1번 확정 (개별 제출 시 즉시 채점 `Eager Evaluation / Load Spreading`)** | 참여자가 주문서를 제출할 때마다 즉시 그 참여자의 1순위 추천 메뉴를 계산해 `room_round_members`에 기록한다. 마지막 제출자 요청 시 전원 분을 한꺼번에 계산하느라 지연(`Tail Latency`)이 발생하는 현상을 방지한다. |
| **T03-D02** | 동일 메뉴 병합 시 제안자(`contributors`) DB 저장 구조 | **1번 확정 (정규화 연결 테이블 분리 `Normalized Junction Table`)** | 투표 후보 테이블(`room_round_candidates`)과 후보 제안자 연결 테이블(`room_round_candidate_proposers`)을 2개로 분리하여 `(candidate_id, user_id)`를 한 줄에 1명씩 정규화 저장한다. 외래키(`FK`)와 유니크 제약을 보장하고 향후 투표 중 퇴장(`T06`) 시 퇴장자 행만 안전하게 삭제할 수 있게 한다. |
| **T03-D03** | 그룹 추천 결과의 누적 학습·다양성 이력 연동 | **2번 확정 (참여자 각자의 개인 추천 장부에 매번 누적 `Individual Cumulative Blending`)** | 같이 고르기를 반복 사용할수록 추천이 정교해지도록, 각 참여자의 주문서 추천 산출 시 기존 개인 추천 이력(`Recommendation` 및 `RecommendationItemSnapshot`)에 정식 누적 기록하고 최근 노출 감점(`Exposure Penalty`)과 개인화 점수 보정(`Personal Adjustment`)을 그대로 반영한다. 구성원이 일부 바뀌어도(`A, B` → `A, B, C`) 이전 누적 기억이 유지된다(`No Cold Start`). |
| **D20** | 주문서 제출 시 임시 추가 제외 키워드(`extra_exclusions`) 허용 여부 | **1번 확정 (시작 시 고정된 프로필 제외 스냅샷만 사용 `Immutable Shared Filter`)** | 그룹 방 주문서 제출 시에는 별도의 임시 추가 제외 입력(`exclusions`)을 받지 않고, `T02` 시작 시점에 고정된 전원 프로필 제외 조건 스냅샷(`room_rounds.exclusion_snapshot_json`)만 일관되게 사용한다. 먼저 제출한 사람의 후보에서 나중에 제출한 사람이 뺀 음식이 튀어나오는 '기대치 불일치(`Expectation Mismatch`)'와 제출 도중 메뉴 0개 고갈 문제를 원천 차단한다. |

## 2. 확정된 상세 처리 흐름 및 잠금 규칙

1. **주문서 제출 요청 검증 및 멱등성(`Idempotency`)**:
   - 대상 `Room` 및 현재 활성 `RoomRound` 행을 잠그고(`with_for_update()`), 현재 라운드 상태가 `ORDER_SHEET`인지 확인한다(일시 정지 `PAUSED`나 이전/취소된 라운드의 지연 요청은 거절).
   - 제출자의 `room_round_members` 행을 확인한다:
     - 이미 제출 완료(`submitted_at IS NOT NULL`)한 참여자인 경우:
       - 제출한 답변 내용(`answers`)이 기존 저장된 답변과 동일하면 **동일 제출 재전송**으로 판단하여 새 추천 계산이나 상태 변경 없이 기존 성공 결과를 그대로 반환한다.
       - 다른 답변으로 수정을 시도한 경우에는 거절한다.
   - 제출된 질문 ID 집합이 `T02`에서 해당 참여자에게 배정된 9개 질문(`assigned_question_ids`)과 정확히 일치하는지, 중복 질문이나 유효하지 않은 `option_id`가 없는지 검증한다.
2. **제출 즉시 1순위 추천 메뉴 산출 및 개인 장부 누적 (`T03-D01`, `T03-D03`, `D20`)**:
   - `room_rounds`에 저장된 공통 제외 스냅샷으로 필터링된 생존 메뉴 풀을 사용한다(`D20`: 별도 추가 제외 키워드 미사용).
   - 제출된 9개 답변의 규칙(`evaluate_option_rules`)과 가중치(`importance_weight`), 그리고 해당 유저의 누적 이력(`select_diverse_menus_with_snapshots`)을 적용해 상위 추천 메뉴를 산출한다.
   - `T03-D03`에 따라 해당 유저의 `Recommendation` 및 `RecommendationItemSnapshot` 행을 생성(상위 트랜잭션과 함께 commit되도록 `flush`만 수행)하고, 1순위 메뉴(`primary_menu_id`)와 `recommendation_id`, 제출 답변, 제출 완료 시각을 `room_round_members`에 기록한다.
3. **전원 제출 여부 확인 및 후보 병합·단계 전환 (`T03-D02`)**:
   - **아직 미제출 참여자가 남아 있는 경우**:
     - 제출 완료 인원(`submitted_count`)만 갱신하고, 다른 참여자의 답변이나 먼저 뽑힌 1순위 메뉴는 외부(REST/WebSocket)에 비공개로 유지한다.
     - `rooms.state_version` 증가 및 Outbox 알림 저장 후 `commit`한다.
   - **현재 라운드의 활성 참여자 전원이 제출을 완료한 경우 (마지막 제출자)**:
     - 활성 참여자들의 `primary_menu_id`를 수집하여 동일 메뉴(`menu_id` 기준)를 하나의 후보로 병합(`Deduplication`)한다.
     - `room_round_candidates` 테이블에 고유 후보 메뉴 행을 생성하고, 각 후보를 1순위로 배출한 참여자들을 `room_round_candidate_proposers` 연결 테이블에 `(candidate_id, user_id)`로 기록한다.
     - **병합된 최종 후보 개수에 따른 자동 전환 규칙**:
       - **후보가 2개 이상인 경우**: 라운드 상태를 `VOTING`(투표 단계, `vote_round_no = 1`)으로 전환한다.
       - **전원 같은 메뉴가 나와 후보가 딱 1개인 경우**: 불필요한 투표를 생략하고 즉시 `RESULT`(결과 확정 단계)로 전환하며 해당 단일 후보를 최종 1위로 확정한다.
       - **유효 후보가 0개인 예외 경우**: 실패 사유와 함께 현재 라운드를 종료·초기화하고 방을 대기 상태(`WAITING`)로 되돌린다.
     - `rooms.state_version` 증가 및 Outbox 알림 저장 후 상위 트랜잭션을 `commit`한다.
