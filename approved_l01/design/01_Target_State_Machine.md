---
id: CDPD-L01-DESIGN-STATE-MACHINE
title: Target Binding状態遷移・失効・復旧
project: CDPD-CHATGPT-ONLY
ticket: CDPD-L01
role: EXECUTION
status: CANDIDATE
version: 0.3.0-l01-candidate.1
updated: '2026-10-17'
related:
  - 01_Target_Binding_Detail.md
---

# Target Binding 状態機械

## 1. 適用範囲

[Target詳細設計](01_Target_Binding_Detail.md)のBとleaseを扱う。SHIRABE WORK、Web応答完了、Bundle整合性、独立Assessmentとは別の状態機械である。
本書と[状態定義JSON](../tests/fixtures/state_contract.json)は設計の対応表であり、製品Runtimeの実装ではない。

## 2. 状態定義

| 状態 | 意味 | 操作 | DOM採取 | 新Binding発行 |
|---|---|---|---|---|
| UNBOUND | 使用可能なrecordなし | 禁止 | 禁止 | 明示Preflight開始のみ |
| PREFLIGHT | 初期条件・live identity検証中 | 禁止 | 禁止（CDP identity metadataは可） | 全検査後に1回 |
| BOUND | record、live identity、leaseが有効 | 独立の操作認可＋guard成功時のみ | policy＋guard成功時のみ | しない |
| NAVIGATING | 同じTargetのdocumentが遷移中 | 遷移を既に起こした1回を除き新規禁止 | 当該採取要求はPARTIAL、構造なし | しない |
| INVALIDATED | identity不一致、切断、所有/状態違反等 | 禁止 | 禁止 | 次の明示Preflightのみ |
| EXPIRED | UTC/monotonic期限切れ | 禁止 | 禁止 | 次の明示Preflightのみ |
| RELEASED | 正常終了時に明示解放 | 禁止 | 禁止 | 次の明示Preflightのみ |

BOUNDは操作認可の代わりではない。DIAGNOSTIC_ONLY/RECONCILIATION_REQUIRED overlay中はBOUNDでも新規mutation禁止。
古いレコードに対する失効は終端。状態JSONをBOUNDへ書き戻して同じbinding_idを復活させない。

## 3. 明示的な再Bindingの唯一の許可点

`EXPLICIT_PREFLIGHT`は、人または認可された起動処理が**次の作業開始境界**で明示要求する操作。
前操作が進行中/送信未確定なら拒否し、業務側で再調停してから再要求する。単に別operation_idを振って送信をやり直すことも禁止。
成功すれば新しいbinding_idを1回発行し、旧recordを再利用しない。失敗時に旧recordへrollbackして操作再開しない。

次の処理は再Bindingを発行できない: 引数なしbind、observeの内部fallback、例外handler、Worker、Watchdog、定期採取、未確定送信のretry。
`REVALIDATE_OK`/`NAVIGATION_SETTLED`は**同じB/同じbinding_idの検証**であり、Targetの変更や期限延長を伴わない。

## 4. 優先規則

1. malformed/未承認policy/current-state不明を先に拒否。
2. epoch・ID・Origin違反、page集合違反、明示revocation、既知のCDP切断は失効をラッチ。
3. TTL/時計の違反を適用。違反が重なった場合は最初に観測した固定reasonを主理由にし、他は安全な補助コードにする。
4. identity違反がない同一Target内の遷移だけNAVIGATINGとして扱う。
5. すべて有効なときにだけ操作/captureを許可。表にない状態・eventの組合せはDENY。

**共通割込み規則:** BOUNDとNAVIGATINGの両方に対して、TTL_REACHEDはEXPIRED、CLOCK_DISCONTINUITY/既知CDP断/epoch・ID・Origin違反/page集合違反/revocationはINVALIDATEDを優先適用する。PREFLIGHT中の同じ失敗はCHECKS_FAILEDとなりUNBOUNDへ戻す（旧recordは復活しない）。以下の個別表よりこの共通規則を優先する。

INVALIDATED/EXPIRED/RELEASEDで`bind_chatgpt_page()`を呼んでも状態は変えない。revoked recordの_BoundPage参照を持っていても利用不可。

## 5. 状態遷移表

| 現状態 | Event | 次状態 | Guard | 処理 |
|---|---|---|---|---|
| UNBOUND | EXPLICIT_PREFLIGHT | PREFLIGHT | old work settled; exclusive owner | new record not yet issued |
| PREFLIGHT | CHECKS_OK | BOUND | G0-G7 all true | mint binding_id and immutable record once |
| PREFLIGHT | CHECKS_FAILED | UNBOUND | no prior usable binding | save failure metadata; no browser content command |
| BOUND | REVALIDATE_OK | BOUND | same record and G0-G7 true | no new binding_id; no expiry extension |
| BOUND | SAME_TARGET_NAV_STARTED | NAVIGATING | same target and expected origin; no competing page | stop new UI commands/capture while navigation unresolved |
| NAVIGATING | NAVIGATION_SETTLED | BOUND | same epoch/id/origin; full live revalidation | same binding_id; not rebind |
| NAVIGATING | NAVIGATION_UNVERIFIED | INVALIDATED | deadline, unknown origin, unexpected target, or lost context | no DOM; no automatic recovery |
| BOUND | TARGET_LOST | INVALIDATED | pinned target destroyed or missing | poison all retained BoundPage/ops references |
| BOUND | PAGE_SET_CHANGED | INVALIDATED | another page created or page subtype becomes unsupported | do not choose remaining/current/focused page |
| BOUND | ORIGIN_CHANGED | INVALIDATED | not exact expected origin | stop content commands; no fallback |
| BOUND | BROWSER_EPOCH_CHANGED | INVALIDATED | fresh path fingerprint differs | no automatic browser restart |
| BOUND | CDP_DISCONNECTED | INVALIDATED | an established operational/control CDP channel is known lost | no automatic reconnect/replay; save S0 |
| BOUND | TTL_REACHED | EXPIRED | UTC or monotonic deadline reached | no grace extension for capture |
| BOUND | CLOCK_DISCONTINUITY | INVALIDATED | clock drift >5s or suspend/resume detected | next explicit preflight only |
| BOUND | EXPLICIT_RELEASE | RELEASED | caller ends binding at safe boundary | revoke operating and capture rights, do not close browser |
| BOUND | OWNER_CRASH | BOUND | last committed active record exists; no recorded revocation | DIAGNOSTIC_ONLY; Worker fresh-checks same record |
| BOUND | CAPTURE_POSTCHECK_FAILED | INVALIDATED | identity/page/lease changed before commit | discard uncommitted structure; metadata only |
| BOUND | CAPTURE_NAVIGATION_RACE | NAVIGATING | same target navigation detected during one capture | PARTIAL for this request; no automatic capture retry |
| BOUND | SUPERSEDED | INVALIDATED | trusted current binding_id differs | old queue item is rejected, never retargeted |
| BOUND | START_NEW_OPERATION | BOUND | owner live, full guard and no active/uncertain operation | operation_id from caller; binding_id unchanged |
| BOUND | MUTATION_OUTCOME_UNKNOWN | BOUND | identity still verifies but sent result is unknown | RECONCILIATION_REQUIRED overlay, no new mutation |
| BOUND | EXPLICIT_PREFLIGHT | PREFLIGHT | IDLE; old work settled and current record explicitly revoked | old record becomes terminal; failed replacement cannot restore it |
| INVALIDATED | EXPLICIT_PREFLIGHT | PREFLIGHT | work settled; old record remains revoked | new record only after checks |
| EXPIRED | EXPLICIT_PREFLIGHT | PREFLIGHT | work settled; no automatic lease renewal | new record only after checks |
| RELEASED | EXPLICIT_PREFLIGHT | PREFLIGHT | authorized owner at next safe boundary | new record only after checks |

## 6. 事象ごとの具体的判断

### 6.1 タブ内遷移

同じepoch/TargetでChatGPTの新規会話やprojectに移ること自体は禁止しない。既存Web APIが権限を持って行う操作であり、CDPDは実行しない。
遷移中に採取開始/継続した要求は失敗として閉じ、構造を保存しない。遷移完了後の別の明示採取要求は、同じrecordの再検証成功で実施可能。
main frame/loaderの変化をBrowser Epoch変化と誤解しない。会話の同一性・生成結果の因果関係は既存結果モデルで確認する。

### 6.2 閉鎖・再生成・2枚目

Target Aが閉じられ、同じURLでTarget Bが開かれてもAのBindingをBへ移さない。
Aが残ってBが増えた場合も単一page違反。Bを自動closeしたりAをactivateしたりしない。
一度検知した違反はBが自然に閉じても自動解除しない。旧キュー項目は新Bindingへ付け替えない。

### 6.3 Browser再起動・CDP断

同じport/URL/target_idを名乗ってもepochが違えば拒否。GUIDを取り出せない方式も拒否。
運用/制御接続の**既知のCDP断**はINVALIDATEDとし、当該recordで自動reconnectしてDOM採取を再試行しない。S0を残し次のPreflightへ。
Workerが正常に新規接続を開く/採取完了後に自分のsessionをdetachすることは、既知の異常CDP断とは区別する。

### 6.4 Caller crashと独立Worker

Caller停止そのものは、browser再起動やTarget喪失の証拠ではない。
last committed active recordがあり、revoked/supersededが記録されておらず、**独立Watchdogが発行時からの時計anchorを保持しTTL有効を確認できれば**、Watchdogは**DIAGNOSTIC_ONLY**として新しい自分のsocketから同じBを厳格再検証できる。
これはbinding_idを発行し直す処理ではなく、旧operationを再実行する処理でもない。
Target/epoch検査が失敗すれば新規DOMなし・S0のみ。Callerが切断/失効を既に記録していた場合は6.3が優先する。
現在状態が壊れている/世代確認不能/独立時計keeper不在の場合、単にrecordファイルが存在するだけでは採取許可しない。CallerとWatchdogが両方停止した後のファイルだけからの自動復旧はS0のみとし、次の明示Preflightを要する。

### 6.5 送信後の観測不能

送信コマンドが既に出た可能性がある場合、Bindingを取り直してもその操作の成否は確定しない。
Web APIのsubmission情報と元operation_idを保持し、業務側の再調停へ渡す。
CDPDがPARTIAL/COMPLETEを返したことを理由にWeb送信をretryしない。

### 6.6 同時性と旧参照

guardは単一writerの現在状態からBを取得し、処理の前後で世代を照合する。操作を停止してから診断へ移ることを標準とする。
既にin-flightの操作が残る場合、その副作用をrollbackできるとは扱わない。採取は「失敗後の観測」であり完全再現ではない。
独立Worker間でもcaptureを多重起動しない。排他手段の具体化はL02/L05/L07。L01はこの意味論を引き継ぐ。

## 7. Workerの終了条件

各要求はCOMPLETE、PARTIAL、REJECTED、入力/保存エラーのいずれかで1回終了する。IDENTITY失敗後の自動再採取loopは禁止。
CDPDの既存CaptureOutcomeで、POLICY_DENIED以外のidentity/接続失敗はPARTIAL、STRUCTUREなしを維持する。
実保存できなければBundleを存在するものとして報告しない。

## 8. 実装境界と未実行

本書の状態機械はL03/L04/L06/L07への設計入力。L01ではbrowser、Watchdog、Web送信、実機障害注入を実行していない。
[試験定義](../tests/01_Target_Test_Cases.md)のTGT-01〜TGT-05および拡張試験は、該当後工程でactual evidenceを取得するまでNOT_RUNとする。
