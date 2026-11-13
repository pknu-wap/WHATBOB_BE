# T02 추천 신호와 가중치의 업계 공개 사례

조사일: 2026-11-13. 공식 도움말·개발 문서·기업 공개 연구만 사용했다. 공개 사실과 WhatBap 적용 제안을 구분하며, 이 문서는 설계 승인이나 구현 변경이 아니다.

후속 사용자 선택: 이 조사 뒤 사용자가 D11의 여러 신호 학습형 추천 방향을 승인했다. 아래의 조사 당시 미승인 설명은 당시 상태이며, 최신 선택·미결정은 [T02 아키텍처](39-t02-personal-data-architecture.md)와 [체크리스트](39-t02-architecture-checklist.md)를 따른다. 실제 계수·모델 종류·운영의 선택은 여전히 미결정이다.

## 핵심 결론

공개 사례에서 확인되는 것은 **사용 맥락에 따라 신호를 다르게 쓰고, 여러 목표를 측정하며, 변경 효과를 실제 실험으로 판단하는 방식**이다. 이번에 확인한 자료에는 WhatBap의 개인 평가 영향 한도를 일률적으로 5%·10%·20%로 정할 근거가 없다. 아래의 후보 생성·순위화 연구, 이벤트 유형별 가중치 설정, 온라인 A/B 사례를 참고할 수 있지만 서로 다른 서비스의 계수를 그대로 옮길 수 있다는 근거는 아니다.

## 공식 자료에서 확인한 사실

| 사례 | 확인한 동작과 범위 |
| --- | --- |
| YouTube 현재 공식 도움말 | 홈페이지 추천은 주로 **시청 이력**, 다음 동영상(Up Next)은 **현재 보고 있는 동영상**을 주요 신호로 삼는다. 좋아요는 비슷한 동영상에 대한 관심 예측, 싫어요·관심 없음은 앞으로 피할 추천을 판단하는 데 쓰며, 만족도 설문은 시청 시간 외 만족도를 이해하는 신호다. 기능에 따라 중요 신호가 다르다고 명시한다. [YouTube Help](https://support.google.com/youtube/answer/16089387?hl=en) |
| Google 2016 공개 연구 | **후보 생성 → 별도의 순위화** 두 단계로 설명한다. 개발 중 precision·recall·ranking loss 등 오프라인 지표를 쓰지만, 최종 효과는 실제 서비스의 A/B 실험으로 판단하며 오프라인 개선과 온라인 성과가 항상 일치하지 않는다고 설명한다. 이는 **2016년 공개 설계 사례**이며 현재 YouTube 전체 구현을 뜻하지 않는다. [공식 논문 PDF, §2](https://research.google.com/pubs/archive/45530.pdf), [Google Research 서지·초록](https://research.google/pubs/deep-neural-networks-for-youtube-recommendations/) |
| Google 2019 공개 연구 | 다음 동영상 순위화에서 **서로 경쟁하는 여러 목표**와 사용자 피드백의 선택 편향을 문제로 다룬다. 여러 목표를 함께 최적화하는 Multi-gate Mixture-of-Experts 등의 기법을 공개했다. 모든 평가를 단일 고정 가중치로 합치는 보편 공식을 제시한 자료로 읽어서는 안 된다. [공식 연구 초록](https://research.google/pubs/recommending-what-video-to-watch-next-a-multitask-ranking-system/) |
| Google 2026 공개 연구 | YouTube Music 홈페이지에서 후보 생성 등 상위 단계를 고정하고 순위화·서빙의 6개 개입과 한 조합에 대한 온라인 A/B 결과를 보고한다. 신규 발매곡 반응 개선과 아티스트 다양성 사이의 비용 등 목표 간 상충을 제시한다. 최근 사례에서도 실제 효과와 다른 목표의 손실을 함께 측정한다. [공식 연구 초록](https://research.google/pubs/breaking-the-loop-an-empirical-comparison-of-strategies-for-novelty-and-freshness-in-youtube-music/) |
| Netflix 현재 공식 도움말 | 시청·평가 이력, 취향이 비슷한 다른 회원, 장르·배우 등 작품 정보를 함께 사용한다. 서비스 이용이 초기 선호보다 우선하게 되며 **최근에 반응한 작품이 과거 작품보다 더 큰 영향**을 준다고 설명한다. 정확한 시간 감쇠식이나 계수는 이 도움말에 공개하지 않는다. [Netflix Help](https://help.netflix.com/en/node/100639) |
| Netflix 2017 공개 실험 사례 | 오프라인 평가 뒤 온라인 A/B로 만족도와 관련된 유지율·시청 시간 등을 측정한다. 많은 후보 알고리즘을 interleaving으로 먼저 추린 뒤 전통적인 A/B로 장기 행동을 확인하는 2단계 실험을 공개했다. **2017년 공개 운영 사례**이며 현행 전체 실험 체계를 확정하는 자료는 아니다. [Netflix Technology Blog](https://netflixtechblog.com/interleaving-in-online-experiments-at-netflix-a04ee392ec55) |
| Amazon Personalize 공식 제품 문서 | User-Personalization-v2·Personalized-Ranking-v2에서는 구매·클릭 등 **이벤트 유형마다 가중치**를 설정할 수 있다. 가중치는 0~1이고 유형 간 **비율**이 중요하다. 이는 이벤트의 학습 중요도이며, 최종 점수에서 개인 평가가 차지하는 비중이나 보정 상한과 같은 뜻이 아니다. [AWS 이벤트 설정 문서](https://docs.aws.amazon.com/personalize/latest/dg/optimizing-solution-events-config.html) |
| Amazon Personalize 실시간 반영 | 과거 이벤트, 실시간 이벤트 또는 둘의 조합으로 추천할 수 있다. 실시간 개인화를 지원하는 사용 사례·레시피에서는 기존 항목의 새 이벤트를 수집 후 수초 내 추천에 활용한다. 신규 항목·사용자 처리는 레시피에 따라 다르다. **AWS 제품 기능이며 amazon.com의 실제 운영 알고리즘과 동일하다고 단정할 수 없다.** [AWS 실시간 이벤트 문서](https://docs.aws.amazon.com/personalize/latest/dg/recording-events.html) |

## 공개 사실로부터 해석할 수 있는 범위

- **해석:** 오늘의 의도와 장기 취향을 서로 다른 입력으로 다루는 것은 YouTube의 화면별 신호 차이와 Netflix의 이력 활용에서 참고할 수 있다. 다만 음식 주문서와 동영상 시청 맥락은 다르므로 “오늘 답변은 항상 과거 평가보다 우선한다”는 WhatBap 규칙까지 도출되지는 않는다. [YouTube Help](https://support.google.com/youtube/answer/16089387?hl=en), [Netflix Help](https://help.netflix.com/en/node/100639)
- **미공개·미확인:** 위 도움말과 공개 초록만으로 각 서비스의 현재 전체 모델, 사용자별 정확한 계수, 감쇠 기간, 모든 예외 처리를 재현할 수 없다. 공개된 특정 연구의 성과도 WhatBap에서 같은 수치의 개선을 보장하지 않는다.
- **정정:** `10%`와 `(positive-negative)/(n+4)`는 WhatBap 에이전트가 제시한 초기 설명안이다. 위 공식 자료로 확인한 빅테크 공통 공식이나 검증된 WhatBap 값이 아니다. `5%·10%·20%`, 기준 건수 `4` 역시 업계 정답으로 제시할 근거가 없다.

## WhatBap 적용 시사점 — 제안, 미승인

현재 승인된 D10은 **오늘 주문서 점수를 기본으로 하되, 누적 개인 평가를 제한된 보조 점수로 반영하고 평가가 적으면 약하게 반영하는 방향**이다. 실제 강도·계산식은 미정이며 고정 90:10, 5%·10%·20% 또는 기준 건수 4를 승인한 상태가 아니다. [T02 D05·D10](39-t02-personal-data-architecture.md)

다음은 업계의 검증 방식을 작은 서비스에 맞게 적용하는 제안이다.

1. 먼저 같은 단위의 점수에서 “영향 한도”가 무엇인지 정의한다. 기본 점수의 일정 비율인지, 정규화 점수에 더하는 최대 보정값인지에 따라 같은 10%도 다르게 동작한다. 5·10·20은 필요하면 **비교할 실험 후보**로만 쓰고 초기 기본값으로 확정하지 않는다.
2. 저장된 평가로 후보별 순위 변화, 유효 평가가 적을 때의 변화, 수정 이력의 중복 기여 여부를 확인한다. 강제 제외된 메뉴가 다시 후보가 되는 일은 허용하지 않는다. 제외 후 후보가 없을 때 조건을 풀지 않는 기존 도메인 규칙을 유지한다. [루트 개발 규칙 §8](../../AGENTS.md), [T02 D05·D10](39-t02-personal-data-architecture.md)
3. 가능해지면 개인 보정을 끈 기준안과 후보안을 사용자 단위 온라인 실험으로 비교한다. 추천 만족도와 함께 평가 응답률·메뉴 반복 등을 관찰하되, 지표·실험 기간·표본 기준은 별도 설계한다. 이는 Google·Netflix의 공개 실험 방식에서 얻은 **적용 제안**이며 WhatBap의 측정 계약을 새로 승인한 것이 아니다. [Google 2016 §2](https://research.google.com/pubs/archive/45530.pdf), [Netflix 실험 사례](https://netflixtechblog.com/interleaving-in-online-experiments-at-netflix-a04ee392ec55)

코드·API·DB 변경 없음. 실행 코드 검증은 수행하지 않았으며, 본 조사는 공개 자료와 기존 승인 문서의 범위를 대조한 문서 작업이다.
