# quant-screener

**업종 대비 저평가 종목 자동 발굴 퀀트 스크리너** — 코스피·코스닥(1단계), 미국(2단계)

매일 무료 공식 API(KRX OPEN API · OpenDART · SEC EDGAR)로 시세와 재무제표를 자동 수집하고,
**같은 업종 안에서** 싸면서(가치) 돈을 잘 벌고(퀄리티) 나빠지지 않는(추세·안정성) 종목을 기계적으로 골라 근거와 함께 보여줍니다.

> 현재 상태: **설계 단계 — 구현 전**. 외부 교차 검토(GPT) 진행 중.

## 문서

| 파일 | 내용 |
|---|---|
| [docs/DESIGN.md](docs/DESIGN.md) | 설계안 본문 — 구조, 데이터 소스, 가공 규칙, 점수 체계, 포트폴리오 규칙, 검증 계획 |
| [docs/DECISIONS.md](docs/DECISIONS.md) | 확정 사항과 열린 질문 (선택안 비교) |
| [docs/V2_REVIEW.md](docs/V2_REVIEW.md) | 기존 v2.0 분석 결과와 재설계 배경 |
| [docs/REVIEW_REQUEST_FOR_GPT.md](docs/REVIEW_REQUEST_FOR_GPT.md) | 외부 AI 검토 요청서 |
| [legacy/](legacy/) | 기존 v2.0 단일 HTML (참고용) |
| [reviews/](reviews/) | 외부 검토 의견 보관 |

## 핵심 아이디어 한눈에

```
매일 자동 수집 (KRX·DART·SEC)
   → 지배주주·TTM·우선주 합산 등 정확한 지표 계산
   → 업종별로 묶어 업종 안에서 백분위 점수
   → 가치 40 · 퀄리티 30 · 추세 20 · 안정성 10
   → 적자·F-스코어 낮음 제외, 일회성 이익·부채 급증 경고
   → 저평가 후보 + 판정 근거 + 성과 추적
```

## 검토자(GPT 등)에게
[`docs/REVIEW_REQUEST_FOR_GPT.md`](docs/REVIEW_REQUEST_FOR_GPT.md)부터 읽어 주세요.

---
※ 이 프로젝트는 개인 투자 판단을 돕는 도구이며 투자 권유가 아닙니다. 투자 판단과 결과의 책임은 투자자 본인에게 있습니다.
