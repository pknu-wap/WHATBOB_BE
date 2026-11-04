# 로그인 후 닉네임·프로필 사진 조회

## 로그인 직후

`POST /api/auth/kakao/login` 응답 형식은 기존과 같다. JSON 본문 기준으로
`user.nickname`과 `user.profile_image`를 화면에 사용한다.

```json
{
  "access_token": "서비스 JWT",
  "token_type": "bearer",
  "user": {
    "id": 1,
    "kakao_id": 900001,
    "nickname": "테스트닉네임",
    "email": null,
    "profile_image": "https://example.test/profile.jpg"
  },
  "is_new_user": true
}
```

## 새로고침·화면 재진입 후

저장된 서비스 JWT로 내 프로필을 다시 조회한다. 카카오 액세스 토큰 대신
로그인 응답의 `access_token`을 사용한다.

```http
GET /api/users/me
Authorization: Bearer <서비스 JWT>
```

`200 OK` 응답은 아래 사용자 객체 자체다. 별도의 `data` 또는 `user`로 감싸지 않는다.

```json
{
  "id": 1,
  "kakao_id": 900001,
  "nickname": "테스트닉네임",
  "email": null,
  "profile_image": "https://example.test/profile.jpg"
}
```

- 로그인 응답: JSON 본문의 `user.nickname`, `user.profile_image`.
- 내 정보 응답: JSON 본문의 `nickname`, `profile_image`.
- Axios를 사용하면 위 JSON 본문은 Axios 응답의 `data`에 해당한다.
- 토큰 누락·유효하지 않은 토큰·만료·존재하지 않는 사용자: `401 Unauthorized`.
- 사용자 ID를 요청으로 지정하지 않고 인증된 본인만 조회한다.

## 카카오 프로필 처리

- 최신 `kakao_account.profile`을 우선 읽고, 썸네일과 기존 `properties` 형식도 보완적으로 처리한다.
- 사용자 정보 요청에 `secure_resource=true`를 넣어 HTTPS 이미지 URL을 요청한다.
- 기존 사용자는 다시 로그인하면 전달받은 닉네임·이미지를 갱신한다. 응답에서 빠진 값으로 기존 정보를 지우지는 않는다.
- 카카오가 정보를 제공하지 않고 저장된 값도 없으면 `null`이다. 앱의 프로필 닉네임·프로필 사진 동의항목과 실제 사용자 동의를 확인해야 한다.
- `/api/users/me`는 저장된 정보를 조회하며 매번 카카오에 요청하지 않는다.

근거: [카카오 로그인 REST API — 사용자 정보 가져오기](https://developers.kakao.com/docs/ko/kakaologin/rest-api#req-user-info)

실제 프론트 코드와 브라우저 화면을 통한 검증은 별도로 필요하다.
