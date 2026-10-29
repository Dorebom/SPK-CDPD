---
id: CDPD-LITE-RISKS
title: "リスクとロールバック"
project: CDPD-CHATGPT-ONLY
type: risk
status: PLANNED
version: 0.3.0-plan
updated: 2026-10-17
---


# リスク・除外・ロールバック

| Risk | 影響 | 対応 / Stop Condition |
|---|---|---|
| R-01: 誤Target採取 | 別会話の構造・情報取得 | browser epoch + target_id + originを操作/採取前後で照合。変化なら拒否 |
| R-02: ChatGPT DOM変更 | Locator不整合 | 捕捉時の証拠優先。Candidate Baselineとの差分と機能影響を分離。自動修正しない |
| R-03: Web二重送信 | 重大な二重実行 | `web_submit_established` 不確定ならSHIRABE再調停。CDPDは送らない |
| R-04: SHIRABEソース不在 | 利用側互換未確認 | L09をBLOCKED。元Stable変更しない |
| R-05: CDP高権限 | 機密漏えい | loopback専用・ACL・専用プロファイル・コマンドallowlist・最小メタデータ |
| R-06: Worker停止 | 証拠取りこぼし | S0耐障害Spool、Worker OS timeout/kill、PARTIALの明記 |
| R-07: Chrome停止 | DOMが採れない | 事前履歴を保存。Chrome停止から失敗時DOMを復元したと主張しない |
| R-08: Scheduler実行 | 想定外の自動アクセス | 手動登録のみ。未起動ブラウザではSKIPでPASS禁止 |
| R-09: 正本誤改修 | Stable authority逸脱 | 別worktree/diff、再起動境界、受入後に人が適用 |

## ロールバック
1. SHIRABEのactive runtime/Stable Source Lockは導入前に固定する。
2. CDPDを止めるときはWatchdogのみ停止。Chrome・ユーザープロファイルは削除しない。
3. 任意登録したTask Schedulerは人が無効化し、元の配布版へ戻す。
4. SHIRABEのConsumerを前版へ戻すとき、CDPD Bundleは改変せず履歴として保持する。
5. 旧契約shimを復活させない。前版へ切り戻したときの依存packageはその版専用環境に閉じる。
