# Google 장소 ID 보강 설정 및 오류 확인

`GET /api/places/nearby`는 카카오 음식점을 조회한 뒤 Google Places API (New)의
Text Search로 이름·주소·좌표가 일치하는 유일한 장소의 ID만 연결한다.
Google 오류는 카카오 결과에 전파하지 않으므로 HTTP 200이더라도
`google_place_id`는 `null`일 수 있다. 왓밥 API의 401은 별도의 Bearer 인증 문제다.

## 배포 설정

1. Google Cloud Console에서 **서버 키가 속한 프로젝트**를 선택한다.
2. 해당 프로젝트의 결제 계정 연결과 **Places API (New)** 활성화를 확인한다.
   기존 Places API나 Maps JavaScript API만 활성화한 상태로는 이 호출을 사용할 수 없다.
3. API 및 서비스 → 사용자 인증 정보 → 서버용 API 키를 연다.
   API 제한에서 Places API (New)를 허용한다. API 제한 선택과 API 활성화는 별도 설정이다.
4. 애플리케이션 제한은 요청을 보내는 백엔드의 고정 외부 IP를 허용한다.
   브라우저 도메인(HTTP referrer) 제한 키는 서버 요청용으로 사용하지 않는다.
   로컬 PC에서 호출하면 운영 서버와 출발 IP가 다르므로 결과도 다를 수 있다.
5. 운영 서버 환경에 `GOOGLE_PLACES_API_KEY`를 설정하고 백엔드 프로세스를 재시작한다.
   설정과 클라이언트는 프로세스에서 캐시하므로 `.env` 수정만으로 실행 중인 프로세스에 반영되지 않는다.
6. 유효한 왓밥 토큰으로 주변 음식점을 재검색하고 응답과 서버 로그를 함께 확인한다.

## 로그 해석

- `GOOGLE_PLACES_API_KEY 미설정`: 실행 프로세스에 키가 전달되지 않아 보강을 건너뛴다.
- `status=403 reason=ACCESS_DENIED`: Google이 호출을 거부했다. 이 코드만으로 활성화,
  결제, 키 제한 중 어느 설정이 원인인지 확정할 수 없으므로 위 항목을 확인한다.
- `SERVICE_DISABLED`: 키의 프로젝트에서 API 활성화를 확인한다.
- `BILLING_DISABLED`: 결제 연결 상태를 확인한다.
- `API_KEY_SERVICE_BLOCKED`: 키의 허용 API를 확인한다.
- `API_KEY_IP_ADDRESS_BLOCKED`: 백엔드의 실제 외부 IP와 허용 IP를 확인한다.
- `API_KEY_HTTP_REFERRER_BLOCKED`: 서버용 IP 제한 키 사용 여부를 확인한다.
- `status=429`: 쿼터와 사용량을 확인한다.
- `ReadTimeout` 또는 보강 시간 예산 초과: 네트워크와 시간 제한을 확인한다.
- `reason=UNKNOWN`: 알려진 사유 코드가 없는 응답이다. HTTP 상태와 Cloud Console을 확인한다.

403 응답 본문의 메시지·metadata에는 키나 IP가 포함될 수 있으므로 원문은 로그에 남기지 않는다.
외부 API가 정상이어도 장소 미등록, 이름·주소·100m 이내 위치 불일치 또는 복수 일치 후보는
`null`로 유지한다. 카카오 ID를 Google ID 대신 채우지 않는다.

2026-11-27 로컬 `.env`의 키로 제보된 음식점 두 곳을 직접 조회했을 때
둘 다 HTTP 403, `PERMISSION_DENIED`, `ACCESS_DENIED`였다.
운영 서버의 키·출발 IP·Cloud 설정이 동일한지는 별도 확인이 필요하다.

공식 문서:
[Places API (New) 설정](https://developers.google.com/maps/documentation/places/web-service/get-api-key),
[사용량 및 결제](https://developers.google.com/maps/documentation/places/web-service/usage-and-billing),
[서버 키 제한](https://developers.google.com/maps/faq#keysystem).
