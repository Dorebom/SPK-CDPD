---
id: CDPD-L01-TARGET-TEST-CASES
title: Target Binding試験定義・設計自己検証
project: CDPD-CHATGPT-ONLY
ticket: CDPD-L01
role: EXECUTION
status: CANDIDATE
version: 0.3.0-l01-candidate.1
updated: '2026-10-17'
---

# Target Binding 試験定義

## 1. 実施範囲の区別

**このTicketで実行するもの:** 入力Source Lock/Manifest確認、計画バリデータ、JSON Schema構文・正例/負例、時刻/epochの純粋関数設計検査、状態定義の整合検査、文書リンク/AC/Evidence/配布hash検査。
**このTicketで実行しないもの:** 製品コードの実装、実Chrome/CDP接続、実ChatGPT送信/採取、Windows障害注入、独立Assessment。

以下のTGT試験は全件**実装/実機 NOT_RUN**。Schema等の自己検証PASSをTGTのruntime PASSに読み替えない。
[機械可読定義](fixtures/runtime_test_cases.json)にもactual/command/exit_code/log_ref=nullを明記する。

## 2. 共通テスト環境

L03/L04/L06でFake CDP/合成Pageを実装し、L10で別セッションの独立評価、L11でWindows実機評価を行う。
合成originやfake HTTP応答は**test harness限定の注入**で用意する。production Schemaにはtest_onlyの抜け道や任意origin allowlistを追加しない。
配布版が`https://chatgpt.com`以外を拒否する試験を別に持つ。ライブ操作の許可がなければ実サービス項目はNOT_RUNのまま。

各試験でCDP/Playwrightのcommand監査を取得し、対象target_id、接続epoch、binding_id、dispatch回数、採取構造有無を比較する。
生の会話本文/URL query/Cookie/WS URL/認証情報は証拠へ出さない。Fake IDと固定failure codeで期待結果を固定する。

## 3. 後工程の試験一覧

| ID / AC | 初期条件 | 刺激・手順 | 期待結果 | 実装/評価担当 | L01実行状態 |
|---|---|---|---|---|---|
| TGT-01 / AC01,AC02 | 専用browser、合成epoch A、page Target Aが1枚 | 明示Preflight後、同じrecordをWeb/Workerへ配る | BOUND。双方のepoch/target一致。別Targetへのcommand=0 | L03/L04/L06/L10 | NOT_RUN |
| TGT-02 / AC02 | 専用browserのChatGPT pageが0枚 | Preflightを1回要求 | TARGET_MISSING。操作0、DOM採取0、Binding発行0 | L03/L10 | NOT_RUN |
| TGT-03 / AC02 | ChatGPT pageが2枚、片方だけfocused | Preflightを1回要求 | TARGET_AMBIGUOUS。どちらも選ばずBinding発行0 | L03/L10 | NOT_RUN |
| TGT-04 / AC02,AC04 | Aのrecord。Aを閉じ、同一URLのBをfocusedにする | 引数なしbind/操作/captureを要求 | TARGET_MISSINGまたはTARGET_BINDING_INVALID。Bへのcommand=0。auto rebind=0 | L04/L06/L10 | NOT_RUN |
| TGT-05 / AC01,AC02,AC04 | epoch Aでbinding。browserを再起動しepoch B、同じportへ | 旧recordで操作/capture要求。target_idが同じmockも実行 | BROWSER_IDENTITY_MISMATCH。DOM0、mutation0、restartによる回避0 | L03/L04/L06/L10/L11 | NOT_RUN |
| TGT-06 / AC01,AC02 | AのTarget URLをhttp/www/別port/別host/opaqueへ変更する各subcase | guard実行 | TARGET_ORIGIN_MISMATCH等。DOM0。元URLを自動navigateしない | L04/L06/L10 | NOT_RUN |
| TGT-07 / AC02 | ChatGPT A1枚＋他サイト/空タブ1枚 | Preflight要求 | SINGLE_PAGE_VIOLATION。非対象tabを自動closeしない | L03/L10 | NOT_RUN |
| TGT-08 / AC04 | recordなし、A1枚のみ | 引数なしbind、create_facadeから操作 | BINDING_REQUIRED。1枚という理由で自動選択しない | L04/L10 | NOT_RUN |
| TGT-09 / AC03,AC04 | 有効なAのrecord、操作なし | 引数なしbindを複数回呼ぶ | 同じbinding_id/Bで再検証。発行・expiry延長・focus選択は0 | L04/L10 | NOT_RUN |
| TGT-10 / AC02,AC04 | BOUND時にBoundPage/opsを取得し、後で失効させる | 既存参照のevaluate/goto/click/uploadを試みる | 旧参照も拒否。browser primitiveへのdispatch=0 | L04/L10 | NOT_RUN |
| TGT-11 / AC01 | 同一record、時計をexpiresの直前/等値/直後に設定 | guardを評価 | 直前のみ期限条件成立。等値/直後はBINDING_EXPIRED。自動延長0 | L04/L06/L10 | NOT_RUN |
| TGT-12 / AC02 | 同じepoch/Target、期待Origin内で許可されたnavigation | 遷移完了後に同じrecordを再検証 | BOUNDへ戻れる。新binding_idなし。会話同一性の保証は別 | L04/L06/L10 | NOT_RUN |
| TGT-13 / AC02 | 採取前後でframe/loader/SPA URLのいずれかが変化 | 1回captureする | NAVIGATION_DURING_CAPTURE、PARTIAL、STRUCTUREなし、自動再採取0 | L06/L10 | NOT_RUN |
| TGT-14 / AC01,AC02 | 運用中のcontrol/operation CDP channelが既知の異常切断 | 当該recordで処理継続を試みる | INVALIDATED。S0のみ。自動reconnect/rebind/mutation=0 | L04/L06/L10/L11 | NOT_RUN |
| TGT-15 / AC01,AC02 | Callerを強制終了。active record/TTL有効、独立Watchdogがclock anchor保持、browser alive。keeper不在subcaseも用意 | 独立Workerがrecordを読み自分のsocketで検証 | keeper有効時のみ同じBの採取可。不在はS0のみ。mutation/rebind=0 | L06/L07/L10/L11 | NOT_RUN |
| TGT-16 / AC01,AC04 | /json/versionがGUIDなしprefix、空suffix、別pathを返す各subcase | epoch導出する | BROWSER_EPOCH_UNVERIFIED。PIDやtarget_idからepochを補わない | L03/L06/L10 | NOT_RUN |
| TGT-17 / AC01 | endpointがlocalhost/IPv6/別port/userinfo/query/fragment/末尾slash | 入力検証する | ENDPOINT_NOT_ALLOWED。socket接続0 | L03/L06/L10 | NOT_RUN |
| TGT-18 / AC01 | /json/versionがredirect、巨大payload、duplicate key、ws別host/port | discovery検証する | 拒否。redirect先接続0。proxy経由0 | L03/L06/L10 | NOT_RUN |
| TGT-19 / AC01,AC04 | 最初のdiscoveryがepoch A、Playwright接続直前にBへ交代 | 同じHTTP URLによるrediscoveryを禁止した経路を試験 | 検証したWS A以外への接続0。再discovery差分で失効 | L03/L04/L10 | NOT_RUN |
| TGT-20 / AC03,AC04 | Aへ固定。exact_urlはA、target_idはBという矛盾した引数 | bindを呼ぶ | ID矛盾を拒否。exact_url優先で成功させない | L04/L10 | NOT_RUN |
| TGT-21 / AC01,AC02 | 接続時page一覧はAだけ。後からBを追加 | guard直前fresh一覧およびeventを検査 | 古いpages cacheから1枚と推定しない。単一page違反を検出 | L03/L04/L06/L10 | NOT_RUN |
| TGT-22 / AC01 | page subtype非空、重複Target ID、filter未対応の各subcase | Preflightを行う | TARGET_UNVERIFIED/PROTOCOL_UNSUPPORTED。不明Targetにattachしない | L03/L10 | NOT_RUN |
| TGT-23 / AC03 | 新schemaのvalid envelope | targetだけ既存TargetBinding.from_dictへ渡す。full envelopeも試す | target射影は形状適合。full envelopeの直接投入は拒否 | L04/L06/L10 | NOT_RUN |
| TGT-24 / AC03 | expected_browser_id省略/None、envelope未登録 | 標準SDK/CLI入口から操作/capture要求 | weak bindingの抜け道なし。実操作/DOM採取0 | L04/L06/L10 | NOT_RUN |
| TGT-25 / AC01,AC02 | wall clock巻戻し/大幅前進、sleep/resume、期限切れの各subcase | guard前後の時刻検査 | CLOCK_DISCONTINUITY/EXPIRED。期限延長0 | L04/L06/L10/L11 | NOT_RUN |
| TGT-26 / AC01,AC04 | capture途中にcurrent binding_idが更新/明示revoked | postcheck実行 | 古い要求の構造をcommitしない。新bindingへ付け替えない | L05/L06/L10 | NOT_RUN |
| TGT-27 / AC03,AC04 | 送信後に応答観測またはBinding検証に失敗 | failure hook→診断→復旧判断 | 元operation_id/送出情報を保持。送信回数最大1。診断成否でWORK成功にしない | L04/L09/L10 | NOT_RUN |
| TGT-28 / AC01,AC02 | isolated world context消滅/不明、採取直前に他Originへ遷移 | 固定script/guardを実行 | DOM走査前Origin guard拒否またはcontext失敗。default contextへのfallback=0 | L06/L10 | NOT_RUN |
| TGT-29 / AC01 | 9222 aliveだが専用launch記録なし/日常profile/外部listen | Preflightする | PROFILE_UNVERIFIED/運用前提拒否。他process kill/restart=0 | L03/L11 | NOT_RUN |
| TGT-30 / AC02,AC04 | page追加違反を検知した後、追加pageを閉じる | 引数なしbindや同じrecordで再開を要求 | 違反ラッチ維持。明示Preflightまでは拒否 | L04/L06/L10 | NOT_RUN |
| TGT-31 / AC01 | 同じ専用profileへConsumer2つ/Worker2つが競合 | 同時処理を要求 | 所有/排他が不明な側は停止。重複mutationや同時captureなし | L05/L06/L07/L10 | NOT_RUN |
| TGT-32 / AC01,AC03 | policy disabled、または保存先書込失敗 | capture要求 | policyはREJECTED。保存失敗は例外/入力保存エラー。Bundle保存成功を偽装しない | L06/L10 | NOT_RUN |
| TGT-33 / AC03 | 元FacadeとInstrumentedFacade | 元Providerでcontract検証、wrapperは別署名として扱う | create_facade/3操作の公開署名・結果型不変。旧SHIRABE互換なし | L04/L09/L10 | NOT_RUN |
| TGT-34 / AC01,AC04 | 操作許可のないライブChatGPTや任意TEST_ONLY値を持つrequest | 標準production入口へ投入 | policy/スキーマ/スコープで拒否。合成originを運用へ持ち込まない | L03/L06/L10/L11 | NOT_RUN |
| TGT-35 / AC02 | 専用browserはA1枚。OS focusを別アプリ/別profileの通常Chromeへ移す | same recordを再検証 | focusではTargetを変更しない。操作の可否は対象guard/操作状態で判断 | L04/L10/L11 | NOT_RUN |
| TGT-36 / AC01,AC02 | Workerを正常終了/Caller異常終了/既知CDP断を別fixture化 | 終了イベントを分類 | 正常detachやCaller crashを一律epoch変化にしない。既知異常CDP断は失効 | L04/L06/L07/L10 | NOT_RUN |

## 4. L01内の再実行可能な静的検査

```powershell
# Candidateのルートから実行。ブラウザへ接続しない。
python .\tests\validate_l01_design.py
```

Python 3.11以降、jsonschema 4.x（Draft 2020-12とFormatChecker）が必要。検査環境の実版は[環境記録](../evidence/validation_environment.json)。
依存未導入の場合はテストをPASS扱いにせず終了コード2で止める。実行スクリプトは検査結果をstdoutへ出すだけでCandidateの固定ファイルを変更しない。
入力ZIP/ソース照合は実行前の別検査の記録を[入力検査](../evidence/input_checks.json)、[計画ログ](../evidence/plan_validation.stdout.txt)、[ネストManifest](../evidence/nested_manifest_checks.json)に残す。
この検査スクリプトのepoch/TTLの関数は**仕様例を確かめるテスト用モデル**。実製品実装の代用にしない。

## 5. 証拠に必要な項目

L10/L11で実行するときはtest_id / fixture version / expected / actual / status / command / exit_code / log_ref / source_sha / BrowserとPlaywright版 / 実行OSを保存する。
同一性違反ケースでは、対象外Targetへのcommand件数0、既に送出済みの場合を除くmutation件数0、自動rebind/再送/ブラウザ再起動0を確認する。
採取途中の変化ケースではSTRUCTURE.jsonがcommitされないことを確認する。S0だけのBundleはPARTIAL、保存不能は別エラーとして区別する。

## 6. AC対応

| L01 AC | 設計証拠 | 将来runtime試験 |
|---|---|---|
| AC01 | [詳細設計](../design/01_Target_Binding_Detail.md) §2/3/5/6/7/8/9、Schema | TGT-01/05/06/11/14/16〜19/22/24〜29/31/32/34/36 |
| AC02 | [状態機械](../design/01_Target_State_Machine.md) §2〜6、試験初期条件と期待結果 | TGT-01〜07/10/12〜15/21/25/28/30/35/36 |
| AC03 | [詳細設計](../design/01_Target_Binding_Detail.md) §4/5.1/9、元ソースAPI記録 | TGT-09/23/24/27/32/33 |
| AC04 | [詳細設計](../design/01_Target_Binding_Detail.md) §7.2、[状態機械](../design/01_Target_State_Machine.md) §3/6 | TGT-04/05/08〜10/16/19/20/26/27/30 |

L01のPASSは設計成果物と自己検証の完了。設計を承認する判断はL02後の独立レビュー/GATE-DESIGNに残す。
