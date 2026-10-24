# quant-screener

**업종 대비 저평가 종목 자동 발굴 퀀트 스크리너** (국내 코스피·코스닥 우선)

공식 API(KRX OPEN API · OpenDART)로 시세와 재무제표를 자동 수집하고,
**같은 사업군 안에서** 싸면서(가치) 돈을 잘 벌고(퀄리티) 나빠지지 않는(추세·안정성) 종목을
**"정량 기준상 저평가 후보"** 로 골라 근거와 함께 보여줍니다.

> **현재 상태: 0단계(데이터 확보 검증) 거의 완료 — 1단계 착수 가능**
> - 1차 외부 검토(GPT) 반영 완료 (설계 v0.2)
> - 0단계 실측 결과: [docs/PHASE0_FINDINGS.md](docs/PHASE0_FINDINGS.md). 데이터 제공 시각만 평일 측정 중
> - 다음 단계: 1단계 (국내 최소 파이프라인 + 계산 검증)

## 개발 원칙
```
데이터 정확성·시점 정합성 > 검증 가능한 성과 측정 > 점수 공식 > 화면·기능
```
단계별 통과 조건(게이트)을 넘은 기능만 다음 단계로 진행하고, 화면에 노출합니다.

## 문서

| 파일 | 내용 |
|---|---|
| [docs/DESIGN.md](docs/DESIGN.md) | 설계안 본문 (v0.2) |
| [docs/DECISIONS.md](docs/DECISIONS.md) | 확정 사항 · 확정 대기 · 열린 질문 |
| [docs/EXPERIMENTS.md](docs/EXPERIMENTS.md) | 실험 기록 규칙과 이력 (과최적화 방지) |
| [docs/PHASE0_FINDINGS.md](docs/PHASE0_FINDINGS.md) | 0단계 데이터 확보 가능성 실측 결과 |
| [reports/phase0/](reports/phase0/) | 0단계 자동 점검 원자료 |
| [docs/V2_REVIEW.md](docs/V2_REVIEW.md) | 기존 v2.0 분석과 재설계 배경 |
| [docs/REVIEW_REQUEST_FOR_GPT.md](docs/REVIEW_REQUEST_FOR_GPT.md) | 외부 AI 검토 요청서 |
| [reviews/](reviews/) | 검토 의견과 응답 기록 |
| [legacy/](legacy/) | 기존 v2.0 단일 HTML |

## 검토 이력

| 회차 | 일자 | 검토자 | 의견 | 응답 | 결과 |
|---|---|---|---|---|---|
| 1 | 2026-10-24 | GPT 검토 | [의견](reviews/2026-10-24_gpt.md) | [응답](reviews/2026-10-24_claude-response.md) | 대부분 수용 → v0.2 |
| 2 | 2026-10-24~ | Claude·GPT 독립 답변 후 채택 ([방식](docs/DEBATE_PROTOCOL.md)) | [질문지](reviews/round2/QUESTIONS.md) | [Claude 답변(봉인)](reviews/round2/claude_position.md) | GPT 답변 대기 |

---
※ 개인 투자 판단을 돕는 도구이며 투자 권유가 아닙니다. 투자 판단과 결과의 책임은 투자자 본인에게 있습니다.
