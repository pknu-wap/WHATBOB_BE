# 방 공통 상태·내구 알림 계약 (#45)

구현 범위는 기존 방·참여자·주문서 완료·접속 기록이다. 라운드·후보·투표는 후속 티켓에서 이 공통 상태에 확장한다.

## HTTP와 WebSocket

- `POST /api/rooms`, `POST /api/rooms/{room_id}/join`, `POST /api/rooms/{room_id}/order-sheet/complete` 성공 응답은 기존 명령 결과와 `common_state`를 반환한다. 명령 결과와 전체 공통 상태 적용은 별도로 처리한다.
- `GET /api/rooms/{room_id}`는 기존 필드에 `state_version`, `common_state`를 추가한다. 참여자에게만 전체 `common_state`를 반환한다. 외부인은 기존 빈 `members`와 `common_state: null`을 받는다. 본인 퇴장·방 삭제 응답은 기존 정보 범위를 유지한다.
- 생성·참여의 Idempotency-Key 재전송과 작업 결과 조회는 기존 명령 결과를 유지한다. 현재 방 참여 자격이 사라졌다면 저장된 과거 `common_state`는 `null`로 가린다. 이전 배포에서 저장한 명령 결과에도 `common_state`가 없을 수 있다.
- `room.updated`는 `type`, `reason`, `user_id`, `occurred_at`, `state_version`, `common_state`를 포함한다. 기존 집계·방 필드도 최상위에 호환용으로 제공하지만, 화면 교체에는 `common_state` 전체를 사용한다. `reason`은 변경의 참고 정보이며 별도 부분 상태를 요구하지 않는다.
- `room.closed`와 연결 정리·인증 오류는 기존 프로토콜을 유지하며 새 내구 재시도 대상에 포함하지 않는다. 종료 알림은 방 화면을 닫는 별도 신호다.

`common_state`의 정확한 필드:

```json
{
  "room_id": "공개 UUID", "state_version": 2, "status": "WAITING",
  "host_user_id": 1, "max_members": 8,
  "created_at": "2026-11-17T00:00:00+00:00", "expires_at": "2026-11-17T01:00:00+00:00",
  "member_count": 1, "is_full": false,
  "completed_member_count": 0, "all_members_completed": false,
  "members": [{
    "user_id": 1, "nickname": "표시 이름", "profile_image": "/static/default-profile.svg",
    "role": "HOST", "joined_at": "2026-11-17T00:00:00+00:00",
    "is_online": true, "disconnected_at": null,
    "order_sheet_completed": false, "order_sheet_completed_at": null
  }]
}
```

`nickname`, `profile_image`도 기존 데이터에 따라 `null`일 수 있다. 참여자 정렬은 참여 시각·내부 참여 ID 순이며, 유예 중 참여자도 인원에 포함한다. 전원 완료는 현재 인원이 1명 이상이고 완료 인원과 같은 경우다. 개인 답변·선호·인증·발송 운영 정보는 포함하지 않는다. `join_url`, `is_member`, `my_role`은 공통 상태 밖에 있다.

클라이언트는 방별 마지막으로 **전체 적용한** 버전을 보관한다. 더 큰 버전의 `common_state`만 원자적으로 교체하고, 같은 버전·이전 버전은 무시한다. 예를 들어 버전 5 알림 이후 도착한 버전 4 HTTP 응답의 명령 성공은 처리하되 화면은 되돌리지 않는다. 숫자·부분 집계만으로 적용 버전을 올리지 않는다. 최초 입장·재접속에는 GET으로 최신 상태를 복구한다. `connection.ready`는 전체 상태를 뜻하지 않는다.

접속 DB 기록 변화도 같은 버전이다. 마지막 연결 단절은 `disconnected_at`을 기록하며 기존 `is_online` 값이 true일 수 있다. 유예 안 복귀는 끊김 시각만 지워져도 변경이다. 여러 탭 중 하나 종료·같은 값·ping/pong·재시도는 새 버전을 만들지 않는다. 접속 기록은 실제 네트워크 상태의 즉각적인 증명이 아니다. 닉네임·사진 교체·초기화는 실제 참여·유예 중 409 `ROOM_PARTICIPATION_ACTIVE`로 차단하며 PENDING 요청만으로 차단하지 않는다.

## 저장·실행·복구

최종 업무 트랜잭션이 방별 한 버전과 전체 원본 snapshot을 함께 저장한다. 기존 방은 마이그레이션 후 버전 0에서 시작하고 새 방은 첫 snapshot이 1이다. 기존 DATETIME 값은 DB에 저장된 정밀도로 읽어 같은 버전의 조회·응답·snapshot을 일치시킨다. 상위 계정 전환·프로필 트랜잭션도 중간 commit 없이 최종 경계에서 저장하고 호출 경계가 commit 후 발송한다.

단일 API 프로세스에서 기록 ID별 메모리 잠금과 DB 재확인을 공유한다. 발송기는 최대 16건 동시 실행, 한 번 탐색 최대 100건, 저장 복구·실패 기한 문맥 최대 100건을 유지한다. 실패 문맥이 가득 차면 새 최초 전송을 보류한다. 다른 방까지 하나의 메모리 잠금으로 묶지 않는다. 각 DB 세션은 소켓 호출 전에 닫으며 DB 작업은 별도 스레드에서 수행한다.

각 시도는 현재 DB 방·참여 자격을 방 단위로 다시 확인하고 현재 연결을 고른다. DB 오류는 수신자 없음으로 저장하지 않는다. 최초 시작 표시의 commit 확인 전에는 소켓을 호출하지 않는다. 각 연결은 최대 2초, 실패 기한이 있으면 남은 시간 중 짧은 제한을 적용한다. 일부 실패도 방 전체 시도 실패이며 원래 버전·본문을 현재 유효한 연결 전체에 재전송한다.

실패 간격은 결과 취합 시각부터 1·3·5·10·30초, 이후 최대 30초다. 전체 마감은 **첫 실패 관찰 시각 + 60초**로 고정한다. 살아 있는 프로세스는 경과 시간도 함께 검사한다. 마감에는 미완료 기다림을 취소하며 이미 전송한 데이터의 회수나 앱 도착 시각은 보장하지 않는다. 폴링은 실행 완료 후 1초 대기하므로 지정 시각보다 늦게 실행될 수 있다.

관찰한 전송 결과의 저장 실패는 메모리에 실제 결과·원래 시각을 유지하고 저장부터 복구한다. commit 응답 유실은 새 세션으로 재확인한다. 저장 복구 자체는 소켓 재시도 횟수나 기한을 소비·초기화하지 않는다. 재시작은 미시작 기록을 복구하고, 저장된 실패 기한은 유지하며, 시작 표시만 있고 최초 실패 기한이 없는 기록을 `UNKNOWN_AFTER_START`로 종료한다. 방 종료가 늦은 발송 결과 저장보다 우선한다.

종료 사유는 `SUCCESS`, `NO_RECEIVERS`, `DEADLINE_EXCEEDED`, `EXPIRED`, `ROOM_CLOSED`, `UNKNOWN_AFTER_START`다. 종료·원문 SQL NULL 제거·최소 요약을 같은 commit에 저장한다. 없는 실제 전송 결과·실패 시각은 추정하지 않는다. `attempt_count`는 저장한 시도 결과 수이며 시작 표시만으로 완료된 시도를 추정하지 않는다.

종료 시각부터 3일 경계(`finished_at <= now - 3 days`)가 된 요약만 삭제한다. 별도 단일 작업은 시작 직후 한 번, 완료 후 1시간 대기하며 100행씩 최대 10묶음·1,000행을 독립 commit한다. 실패한 묶음만 rollback하고 다음 실행에서 남은 대상을 다시 찾는다. 정확한 삭제 시각·운영 처리량은 보장하지 않는다. `ix_room_notifications_due`와 `ix_room_notifications_retention`을 제공하며 운영 누적·지연 관측은 별도다.

## 명시적 마이그레이션

운영 실행·배포는 이 작업에서 수행하지 않았다. 모든 쓰기 프로세스를 중단하고 복원 가능한 백업을 확인한 운영자가 다음 명령을 실행한다.

```text
python -m scripts.migrate_room_notifications --check
python -m scripts.migrate_room_notifications --apply --maintenance-confirmed
python -m scripts.migrate_room_notifications --verify
```

`DATABASE_URL`이 대상 DB이며 기본 보존 manifest는 `.tools/migrations/room-notifications.json`이다. 기존 테이블의 원래 컬럼 지문·행 수를 보존한다. DDL은 개별 확정되므로 중단 시 쓰기를 계속 막고 동일 manifest로 재실행한다. 원래 컬럼·데이터가 바뀌었거나 기존 새 컬럼·인덱스·FK가 호환되지 않으면 중단한다. 버전 컬럼만 추가되거나 인덱스 추가 도중 중단된 경우 검사 후 재개한다. 완료 검증 후에만 API를 기동한다. `create_all`은 기존 rooms 컬럼을 마이그레이션하지 않는다.

`room_notifications.room_id`는 nullable FK / `ON DELETE SET NULL`이며 이력용 내부 ID·공개 UUID는 독립 보관한다. 방 물리 삭제는 기존 요약 수명을 바꾸지 않는다. 새 발송 시각 컬럼은 MySQL DATETIME(6), payload는 nullable JSON(SQL NULL)이다. 실제 MySQL 보존·재실행·부분 적용·FK·버전 경합 검증과 실제 REST/WS·제어된 장애 검증을 수행했다. 프론트 브라우저와 1GB 운영 부하 검증은 수행하지 않았다.
