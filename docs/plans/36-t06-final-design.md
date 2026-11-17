# #36 T06 최종 설계 확인안

- 상태: 2026-11-17 사용자 확인 완료 (`T06-D01`, `T06-D02` 전체 확정).
- 범위: 투표 중 일반 참여자 퇴장 확정 시 일시 정지(`PAUSED`), 방장 계속 진행 승인 시 퇴장자 단독 제안 후보 소프트 비활성화(`is_active = False`), 남은 후보 수에 따른 무투표 당선(`1개`) 또는 새 회차 재투표(`2개 이상`, `vote_round_no + 1`) 전환, 기존 투표 칸 제자리 초기화(`NULL`) 및 이전 회차 지연 요청 차단(`Epoch Fencing`).
- 선행 조건: `T04` (복수 선택 투표 및 결과 확정), `T05` (일시 정지 및 방장 계속/취소 결정).
- 관련 티켓: [T06 티켓](36-tickets/T06-voting-departure-and-revote.md).

## 1. 확정된 핵심 아키텍처 결정 요약 (`T06-D01` ~ `T06-D02`)

| 결정 ID | 주제 | 사용자 확정 선택 | 핵심 설계 계약 |
| :--- | :--- | :--- | :--- |
| **T06-D01** | 퇴장자만 단독 제안했던 후보 정리 방식 | **1번 확정 (후보 행 유지 + `is_active = False` 도장 `소프트 비활성화 / Soft Deactivation`)** | 방장이 '계속 진행(예)'을 승인하면, 각 후보(`room_round_candidates`)의 제안자 연결(`room_round_candidate_proposers`) 중 현재 활성 참여자(`member_status = 'ACTIVE'`)가 1명도 남아 있지 않은 후보를 찾아 삭제(`DELETE`)하지 않고 `is_active = False`로 비활성화한다. 남은 활성 참여자도 함께 제안했던 메뉴는 `is_active = True`로 그대로 유지한다. 이를 통해 `T05-D02`의 참여자 소프트 삭제와 이력 보존 원칙을 100% 통일한다. |
| **T06-D02** | 재투표 전환 시 기존 표 초기화 및 이전 회차 지연 요청 차단 방식 | **1번 확정 (`vote_round_no` 번호표 대조 + 기존 투표 칸 제자리 `NULL` 초기화 `Epoch Fencing & In-Place Reset`)** | 재투표로 전환할 때 `room_rounds.vote_round_no`를 `+1` 증가시키고, 남은 활성 참여자 전원의 `room_round_members.voted_candidate_ids`와 `voted_at`을 별도 이력 테이블 복사 없이 제자리에서 즉시 `NULL`로 덮어써 초기화한다. 투표 제출 요청 시 클라이언트가 보낸 `vote_round_no`가 현재 `room_rounds.vote_round_no`와 다르면 이전 회차의 지연·재전송 요청으로 판단해 즉시 거절(`409 Conflict`)한다. |

## 2. 확정된 상세 처리 흐름 및 잠금 규칙

1. **투표 중 실제 퇴장 확정 기준 및 일시 정지 (`T05` 규칙 공유)**:
   - 단순 연결 단절 시에는 기존 **60초 재접속 유예(`grace period`)**를 먼저 적용하며, 60초 내 재접속 시 후보와 이미 제출한 투표 상태를 그대로 유지한다.
   - **명시적 방 나가기 요청(즉시)** 또는 **60초 유예 만료**로 실제 퇴장이 확정되면:
     - 대상 `Room` 및 활성 `RoomRound` 행을 잠근다(`with_for_update()`).
     - 퇴장자의 `room_round_members.member_status`를 `LEFT_PENDING`으로 변경한다.
     - **남은 활성 인원(`member_status = 'ACTIVE'`)이 2명 미만인 경우**: 방장 확인 없이 즉시 라운드를 취소(`CANCELLED`, 사유: 최소 인원 미달)하고 대기실(`WAITING`)로 복귀시킨다.
     - **남은 활성 인원이 2명 이상인 경우**: 라운드 상태를 투표 일시 정지(`VOTING_PAUSED`)로 전환하고 `decision_version`을 `+1` 올린다. 일시 정지 동안에는 추가 투표 제출과 자동 개표를 차단하며 중간 득표수나 개별 선택은 노출하지 않는다.
2. **방장의 '아니요(라운드 취소)' 선택 시**:
   - `decision_version` 일치 여부를 검증한 뒤 라운드를 `CANCELLED`로 종료하고 남은 인원과 함께 대기실(`WAITING`)로 복귀한다.
3. **방장의 '예(퇴장자 제외 후 계속 진행)' 선택 시 (`T06-D01`, `T06-D02`)**:
   - `decision_version` 일치 여부를 검증한 뒤 `member_status = 'LEFT_PENDING'`인 참여자들을 `LEFT_CONFIRMED`로 확정한다.
   - 남은 활성 참여자들의 주문서 답변과 시작 시점의 공통 제외 조건 스냅샷은 그대로 유지하며, 개인 1순위 후보 메뉴를 다시 추첨하지 않는다.
   - **후보 재정리 (`T06-D01`)**:
     - 현재 `is_active = True`인 후보들 중, 제안자(`room_round_candidate_proposers`) 가운데 `member_status = 'ACTIVE'`인 참여자가 단 한 명도 없는 후보(퇴장자만 제안했던 후보)를 모두 `is_active = False`로 변경한다.
   - **생존 후보(`is_active = True`) 개수에 따른 분기**:
     - **생존 후보가 1개만 남은 경우**:
       - 재투표를 진행하지 않고, 남은 활성 참여자들의 기존 투표 칸(`voted_candidate_ids`, `voted_at`)을 `NULL`로 정리한 뒤 해당 유일 후보를 무투표 당선(`is_winner = True`, `final_vote_count = 0` 및 무투표 표시)으로 기록하고 라운드 상태를 즉시 `RESULT`로 전환한다.
     - **생존 후보가 2개 이상 남은 경우 (`T06-D02`)**:
       - 남은 활성 참여자 전원의 `voted_candidate_ids = NULL`, `voted_at = NULL`로 제자리 초기화한다.
       - `room_rounds.vote_round_no`를 `+1` 증가시키고 라운드 상태를 `VOTING`으로 재개한다.
4. **새 회차 투표 제출 시 이전 회차 지연 요청 차단 (`Epoch Fencing`)**:
   - 투표 제출(`POST`) 요청에 포함된 `vote_round_no`가 현재 `room_rounds.vote_round_no`와 일치하지 않으면 이전 회차의 지연·재전송 요청으로 간주해 거절(`409 Conflict`)하며, 새 회차에서도 제출 완료 후 다른 후보 조합으로의 수정은 금지한다.
   - 모든 변경 사항은 `rooms.state_version` 증가 및 `T01N` Outbox 알림 저장과 함께 단일 트랜잭션으로 `commit`한다.
