# 외부 AI 검토 요청서 (GPT용)

---

## 🔁 3차 협의 요청 (2026-10-25) — 1단계 결과 기반

2차와 같은 독립 답변 방식입니다 ([`docs/DEBATE_PROTOCOL.md`](DEBATE_PROTOCOL.md)).
이번에는 **실제 구현과 실측 결과**를 보고 판단합니다.

### GPT가 할 일 (순서 중요)
1. 배경 자료를 읽는다.
   - [`docs/PHASE1_STATUS.md`](PHASE1_STATUS.md)
   - [`docs/DECISIONS.md`](DECISIONS.md)의 D-20·D-21
   - [`config/account_rules.yaml`](../config/account_rules.yaml)
   - [`src/qs/`](../src/qs/)
   - [`reports/phase1/`](../reports/phase1/)
2. [`reviews/round3/QUESTIONS.md`](../reviews/round3/QUESTIONS.md)의 Q1~Q6에 답한다.
   - 각 질문마다 선택 · 근거 · 반증 조건 · 확신도
   - ⚠️ `reviews/round3/claude_position.md`는 **읽지 않는다.**
3. 답변을 마친 **뒤에** `claude_position.md`를 읽고, 다른 항목만 반론하거나 수긍한다.

### 산출물
- `reviews/round3/gpt_position.md`
- `reviews/round3/gpt_rebuttal.md`

---

## (완료) 2차 협의 요청 (2026-10-24) — 독립 답변 방식

이번 회차부터는 **"GPT가 Claude 설계를 검토"하는 방식이 아닙니다.** 두 AI가 각자 독립적으로 답하고, 사전에 정한 기준으로 더 합당한 쪽을 채택합니다.
규칙: [`docs/DEBATE_PROTOCOL.md`](DEBATE_PROTOCOL.md)

### GPT가 할 일 (순서 중요)
1. 배경 자료를 읽는다: [`docs/DESIGN.md`](DESIGN.md), [`docs/PHASE0_FINDINGS.md`](PHASE0_FINDINGS.md), [`docs/DECISIONS.md`](DECISIONS.md)
2. [`reviews/round2/QUESTIONS.md`](../reviews/round2/QUESTIONS.md)의 Q1~Q10에 답한다.
   - 각 질문마다 **선택 · 근거 · 반증 조건 · 확신도(상/중/하)**
   - ⚠️ 이 단계에서는 `reviews/round2/claude_position.md`를 **읽지 않는다.** (앵커링 방지. Claude 답변은 GPT 답변보다 먼저 커밋되어 봉인됨)
3. 답변을 마친 **뒤에** `claude_position.md`를 읽는다. 의견이 다른 항목에 대해서만 반론하거나 수긍한다.
   - 수긍할 때는 무엇이 설득력 있었는지 밝힌다.
   - 반론할 때는 근거의 질(실측·공식 출처) 기준으로 제시한다.

### 산출물
| 파일 | 내용 |
|---|---|
| `reviews/round2/gpt_position.md` | 2단계 독립 답변 |
| `reviews/round2/gpt_rebuttal.md` | 3단계 교차 반론·수긍 |

(사용자가 이 두 파일을 저장소에 올리거나 Claude에게 전달합니다.)

---

## 1차 검토 요청 (참고용, 설계 v0.1 기준)

> 이 저장소는 Claude와 사용자가 논의해 만든 퀀트 스크리너 **설계안**입니다.
> 아직 구현 전이며, 착수 전에 독립적인 교차 검토를 받고자 합니다.
> **동의보다 반대 의견과 허점 지적이 더 유용합니다.**

## 읽을 순서
1. [`README.md`](../README.md) — 프로젝트 개요
2. [`docs/DESIGN.md`](DESIGN.md) — 설계안 본문 (가장 중요)
3. [`docs/DECISIONS.md`](DECISIONS.md) — 확정 사항과 열린 질문(선택안 비교)
4. [`docs/V2_REVIEW.md`](V2_REVIEW.md) — 기존 v2.0의 문제점과 재설계 배경
5. (선택) [`legacy/Global_Quant_Platform_v2.0.html`](../legacy/Global_Quant_Platform_v2.0.html) — 기존 코드

## 검토해 주셨으면 하는 것

### A. 사실 확인
- 데이터 소스(KRX OPEN API, OpenDART, SEC EDGAR)의 엔드포인트·제공 범위·호출 한도·이용 조건 설명 중 **틀렸거나 바뀐 것**
- 국내 정기보고서 제출 기한, 재무 계정 처리(누적/3개월 금액, 연결/별도, 지배주주) 설명의 정확성

### B. 방법론
- **미래 정보 사용(look-ahead)·생존자 편향·과최적화** 위험이 남아 있는 지점
- 업종 중립 백분위, PBR–ROE 잔차, F-스코어, 12-1 모멘텀의 조합이 타당한지 / 중복되거나 빠진 지표
- 가중치(가치 40·퀄리티 30·추세 20·안정성 10)와 위험 필터 기준값에 대한 의견
- 업종 그룹 크기 보정 규칙(소분류 10개 미만 시 상향)의 문제점
- 한국 시장 특유의 이슈(지주사 할인, 순환출자, 상장 자회사 중복 상장, 테마주 등)에서 놓친 것

### C. 구조·운영
- 수집기(GitHub Actions) + 정적 화면 구조의 약점, 더 나은 대안
- 무료 API 한도 안에서 최초 적재·일일 갱신이 현실적인지
- 비밀값 관리, 실패 시 복구, 데이터 품질 점검 방식

### D. 열린 질문 (`DECISIONS.md`의 ⏳ 항목)
- 각 항목에서 어떤 선택안을 고를지, 그리고 그 이유

## 답변 형식 (요청)

아래 형식으로 작성해 주시면 사용자가 그대로 `reviews/` 폴더에 저장해 Claude와 비교·반영합니다.

```markdown
# GPT 검토 의견 — YYYY-MM-DD

## 1. 치명적 문제 (착수 전 반드시 수정)
- [위치: DESIGN.md 4.2] 문제 … / 근거 … / 수정안 …

## 2. 중요 개선
- …

## 3. 사소한 개선
- …

## 4. 사실 오류 (확실한 것만, 출처 포함)
- …

## 5. 열린 질문에 대한 의견
| ID | 추천 선택안 | 이유 |
|---|---|---|

## 6. 동의하지 않는 설계 결정과 대안
- …
```

- **확실한 사실**과 **의견**을 구분해 주세요.
- 근거가 불확실하면 "확인 필요"라고 표시해 주세요.
