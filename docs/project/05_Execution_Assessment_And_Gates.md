---
id: CDPD-LITE-GOVERNANCE
title: "チケット実行・独立Assessment・Human Gate"
project: CDPD-CHATGPT-ONLY
type: governance
status: PLANNED
version: 0.3.0-plan
updated: 2026-10-17
---


# Pro作業標準・Authority

## 同一Ticketの作業契約
- EXECUTIONでソース入力/版/変更許可を宣言。作業結果はCandidate ZIPまたはdiff。
- `RESULT.json` の固定必須フィールド: `ticket_id`, `role`, `status` (PASS/FAIL/BLOCKED/NOT_RUN), `source_lock`, `outputs`, `tests`, `risks`, `next_dependencies`。
- `REPORT.md` には変更ファイル、設計判断、検証コマンド、ExitCode/件数、失敗と未実行を記載。
- 実行チャットは独立Assessmentを兼務しない。設計GateレビューはL01/L02と別セッションが行う（新チケット番号は発行しない）。
- 異常系の再現は原則Synthetic UI/Mock。CDPD障害診断が直接UI操作を行ってはならない。

## Gate境界
- [GATE-DESIGN](../gates/GATE-DESIGN.md)：L01/L02の独立レビュー、具体的なTarget不一致時挙動と監視契約の承認。
- [GATE-RELEASE](../gates/GATE-RELEASE.md)：L10-12とWindows実機の受入証拠、SHIRABEの切替可否。
- 過去のユーザー合意はスコープ承認であり、Gate決裁の記録ではない。

## リリース許可の分類
`GO`（必須条件達成）/ `NO_GO`（重大未解決）/ `HOLD`（必要入力不足）。Assistantが利用者に代わってGateを承認しない。

## Source Lock
`SOURCE_LOCK.json`は添付v0.2.1とInventory Candidateのbyte hash。SHIRABE現行のAuthorityはL09の入力で取得する。CandidateをStable Authorityに読み替えない。

## 2026-10-18 Human Gate 1決裁の記録

[正式Human Decision](../gates/GATE-DESIGN_APPROVAL_20261018.json)によりGATE-DESIGNをAPPROVEDとして記録した。現在状態は[CURRENT_STATE](../CURRENT_STATE.json)、N-01/N-02のL06対応は[Issue台帳](../issues/00_ISSUES.md)、可用性制約の採用は[ADR-001](adr/ADR-001_Cumulative_Loss_Availability.md)を参照する。本追記は元DAGや製品Authorityを変更しない。
