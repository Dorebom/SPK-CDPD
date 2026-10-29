---
id: CDPD-LITE-TEST-MATRIX
title: "最小受入・異常系マトリクス"
project: CDPD-CHATGPT-ONLY
type: verification
status: PLANNED
version: 0.3.0-plan
updated: 2026-10-17
---


# 受入試験マトリクス

## 必須シナリオ（Synthetic Web UIで先行）
| ID | 条件 | 期待 | 関連Ticket |
|---|---|---|---|
| TGT-01 | Chrome1、ChatGPT Page1 | Browser epoch/Target/Origin完全Binding | L01,L03,L10 |
| TGT-02 | 対象0枚 | 操作/採取BLOCKED | L03,L10 |
| TGT-03 | 対象2枚 | 操作/採取AMBIGUOUS、どちらも操作しない | L03,L10 |
| TGT-04 | current target消滅/別URL | 自動focused/last選択禁止 | L04,L10 |
| TGT-05 | browser再起動/epoch変更 | Binding失効、旧ID拒否 | L03,L04,L10 |
| BIND-03 | Web submit失敗 | 送信回数は最大1、再調停へ | L04,L09,L10 |
| EVT-02 | consumer crash | 別processでS0を読む | L05,L07 |
| CAP-02 | 事後Target差替え | DOM採取不可・PARTIAL/REJECTED | L06,L10 |
| CAP-05 | Bundle改ざん | verify_bundle FAIL | L06,L12 |
| WD-01 | SHIRABE実行側強制終了 | 独立Supervisorが既存イベントを採取 | L07,L11 |
| WD-02 | Chrome停止 | DOM不可明示、S0のPARTIAL | L07,L11 |
| WD-03 | worker hang | deadlineでworkerのみ停止 | L07,L11 |
| DIF-02 | 入力欄が欠落した合成DOM | BREAKINGと報告 | L08,L10 |
| DIF-03 | baseline不在 | 未承認扱い、PASS禁止 | L08,L10 |
| WIN-06 | rollback | 既存SHIRABEのStable/RUN変更なし | L09,L11,L12 |

## 評価条件
- 合成UIでの試験と本番ChatGPT Web試験を分ける。合成オリジンに対する例外は TEST_ONLY フラグでしか許可しない。配布版ではテスト用originを拒否する。
- 各試験記録に `expected/actual/status/command/exit_code/log_ref/source_sha` が必要。
- Windows実機の確認がなければ `L11` = NOT_RUNまたはBLOCKED。実ChatGPT Webにアクセスしなかったことは実機Synthetic試験の否定としない。
- 本番アクセスは別の許可判断。未許可時、実サービス項目だけ `NOT_RUN`。
- 最終Release Gateでは全P0のFAIL/未検証はNO-GO。Scheduler未登録そのものは評価不合格にしない（任意運用項目）。
