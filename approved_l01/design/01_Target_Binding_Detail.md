---
id: CDPD-L01-DESIGN-TARGET-BINDING
title: 専用スコープ・Target Binding詳細設計
project: CDPD-CHATGPT-ONLY
ticket: CDPD-L01
role: EXECUTION
status: CANDIDATE
version: 0.3.0-l01-candidate.1
updated: '2026-10-17'
related:
  - 01_Target_State_Machine.md
---

# CDPD-L01 — Target Binding 詳細設計

## 1. Authorityと到達点

本書はCDPD-L01の**設計Candidate**。実装、独立Assessment、Human Gate承認、ライブ操作は行わない。
本書で決めた追加制約・数値を含め、L02との独立設計レビューとGATE-DESIGNで採用可否を決める。
入力計画の状態・チケット・Source Lockは変更しない。最新SHIRABEソースはL09の入力でありL01の不足入力ではない。

正本の優先順位は、今回のCDPD-L01実行指示 → 添付CDPD.zip内のL01/計画/スコープ → Source Lockで固定したv0.2.1ソース → 同梱Inventory（CANDIDATE_PARTIAL）。
v0.2.1の汎用対応や任意Browser IDを、ChatGPT専用計画の必須条件より優先しない。
入力照合は[入力検査](../evidence/input_checks.json)、保持API/参照位置は[ソース調査](../evidence/source_inspection.json)を参照。

## 2. 保証すること・しないこと

Bindingの識別子を `B=(endpoint, expected_browser_id, target_id, expected_origin)` とする。
公開操作と診断採取の双方が、同じ不変レコードのBを受け取り、**その都度実接続と照合した場合だけ**ブラウザ処理を許す。

`permit ⇒ live_endpoint = B.endpoint ∧ live_epoch = B.expected_browser_id ∧ live_target = B.target_id ∧ origin = B.expected_origin ∧ unique_page ∧ lease_valid`

さらに、実際のコマンドは照合した同一WebSocket／同一Target session／同一Pageオブジェクトへ送る。
独立した「検査用接続」だけが正しく、操作接続が別ブラウザを指す実装は不適合。

**保証境界:** 意図せず別タブ・再起動後の別ブラウザへ乗り換えないこと。同じTarget内の会話同一性は既存ChatIdentity・MessageIdentityの責務であり、Bindingだけで保証しない。
失敗時刻のDOMの完全復元、複数CDP呼出しの原子性、送信のexactly-once、外部からの手動介入阻止は保証しない。
コマンド送出後に障害が起きた場合、既に行われた副作用は取り消せない。未確定状態を保持し、自動再送しない。

信頼前提は、専用ブラウザ・ローカル実行ファイル・同一ユーザーの設定/保存領域が信頼できること。
CDPは認証された読取専用権限ではない。Hash/Schema/専用プロファイルは同一OSユーザーの悪意ある改変を防ぐ認証機構ではない。

## 3. 最小運用制約

| 項目 | v1の規定 | 拒否条件 |
|---|---|---|
| OS / 対象 | Windows 11、適合確認したChrome/Chromium、ChatGPT Web | 他OS/他サイトを対応保証に含めない |
| 接続先 | **`http://127.0.0.1:9222`の完全一致** | localhost、IPv6、別port、末尾slash、認証情報、query、fragment、redirect |
| プロファイル | 一般利用と異なる専用user-data-dir。既存日常プロファイルをコピーしない | 起動記録・所有者・専用設定を確認できない |
| インスタンス | 管理対象のCDP browserを1つ | 別ブラウザをport競合から探し直さない |
| ページ | ChatGPT Page1枚、かつ専用browser内のtop-level `type=page`合計1枚 | ChatGPT0枚/2枚、別サイトやabout:blankタブの併存も拒否 |
| 非page Target | worker/service_worker/iframeはタブ枚数に数えず、そのDOMへattachしない | 未知のpage subtype（prerender等）は選択せず拒否 |
| 操作者 | Consumer1つ・同時Web操作1つ。操作中の手動タブ生成/切替/Reloadを禁止 | 排他所有が確立できない、revoked/superseded |
| Origin | 正規化結果`https://chatgpt.com`のみ | www別host、HTTP、非標準port、他Origin、opaque origin |
| 採取範囲 | main frame＋open shadowの構造のみ | 全frame・iframe内部・raw DOM・本文取得へ拡張しない |
| 復旧 | 現作業を停止し、**明示的な次のPreflight**で新Binding | 自動rebind、ブラウザ再起動、候補探索による回避 |

「page合計1枚」は、計画のChatGPT1枚制約を実装しやすくする**追加の安全側制約（設計Candidate）**である。
これにより専用browser内のタブ切替は必ずpage数違反かTarget変化になる。OSの別アプリや、別プロファイルの通常Chromeへのフォーカス移動はBindingを変えない。
`document.hasFocus()`を識別の根拠にしない。ログイン/認証の一時タブは**Preflight前**に人が完了・閉鎖する。運用要件として複数タブ必須ならDESIGN_BLOCKEDとして計画へ戻す。

Chromeは内部に複数プロセスを持つため、`chrome.exe`件数で「1インスタンス」を判定しない。対象CDP listenerと専用起動記録の関係をL03で検査する。
`/json/version`の存在だけではuser-data-dirやOSのlistener公開範囲を証明できない。
起動記録なしで既存9222へ接続する場合は`PROFILE_UNVERIFIED`。PID/プロセス開始情報は起動所有者の補助確認でありbrowser epochにはしない。

## 4. 現行ソースで確認した差分

パスの基点は、固定ZIPを展開した`CDPD_v0.2.1/`。引用行は[ソース抜粋](../evidence/source_excerpts.md)とhashで追跡可能。

| ID | v0.2.1で確認した事実 | 今回の設計判断 / 実装担当 |
|---|---|---|
| SRC-01 | `session.py:917–987`はexact_url→target_id→focused/pattern-lastの順。両引数指定ではURLが優先 | L04: URLは選択に使わず、固定Targetの追加事後条件に限定 |
| SRC-02 | 同ファイル:955–966で引数なし選択、:977で_bound上書き | L04: 未登録なら拒否。登録後の引数なしは固定Bindingの再検証だけ |
| SRC-03 | :927–973の失敗戻りで過去_boundの破棄は行わない | L04: 失敗時に操作許可を失効。既存_boundや保持済みopsを使えないようにする |
| SRC-04 | :843–856のページ一覧は接続時のsnapshot | L03/L04: fresh Target一覧と現行context.pagesで検査。初回cacheの件数を信用しない |
| SRC-05 | :287–307でTarget IDをcache、Noneもcacheし得る | L04: cacheは補助。guardは実CDPで再照合し、cached IDだけでは許可しない |
| SRC-06 | 8モジュールに引数なしbind呼出し14箇所、bound取得23箇所、`bound.ops.`参照26箇所 | L04: bindだけでなく既存bound参照・全ブラウザprimitiveへguardを適用 |
| SRC-07 | `contracts.py:74–92`のexpected_browser_idは任意、from_dictは余分なkeyを拒否 | 外側envelopeで必須化し、4項目targetだけを既存TargetBindingへ渡す |
| SRC-08 | `collector.py:90–120`は/json/versionのws pathをSHA-256し先頭32hexを採用 | アルゴリズムを継承。ただしGUIDのないpath/別host/別portを拒否 |
| SRC-09 | :180–225はTarget/frame/前後遷移検査を持つが、単一page数とleaseを未検査 | L06: 型・epoch・page数・期限の前後guardを追加。構造採取自体は再利用 |
| SRC-10 | `service.py:77–93`はPOLICY_DENIEDのみREJECTED。他の採取エラーはPARTIALでSTRUCTUREなし | この意味論を維持。Target不一致をCOMPLETEにしない |
| SRC-11 | `api.py:416–431`のFactoryはofflineで契約検証、Facadeの公開操作は3つ | `create_facade()`と3メソッドの署名/結果型は維持する |

SRC-06の「8モジュール」は実際の呼出しを含むファイル数。全参照リストは[調査JSON](../evidence/source_inspection.json)。
この調査は提供側コードに限る。最新SHIRABEの参照箇所を確定したと主張しない。

## 5. Bindingレコード

[JSON Schema](../contracts/chatgpt_target_binding_v1.schema.json)を正本とする。例は合成値であり実ブラウザへの接続権限ではない。

```json
{
  "schema": "cdpd.chatgpt-target-binding/v1",
  "binding_id": "22222222-2222-4222-8222-222222222222",
  "epoch_algorithm": "cdpd.browser-ws-path.sha256-128/v1",
  "target": {
    "endpoint": "http://127.0.0.1:9222",
    "target_id": "SYNTHETIC-TARGET-A",
    "expected_origin": "https://chatgpt.com",
    "expected_browser_id": "254095102094427e3ef2c333da294588"
  },
  "issued_at_utc": "2026-10-17T12:00:00Z",
  "expires_at_utc": "2026-10-17T16:00:00Z"
}
```

| フィールド | 規則 |
|---|---|
| schema / epoch_algorithm | 定数。不明な版/方式を推測・後方fallbackしない |
| binding_id | 成功した明示PreflightごとにUUID v4。Browser Epochではなくレコード世代識別子 |
| target | 既存TargetBindingと同じ4項目。全文完全一致でConsumer/Workerへ配布 |
| expected_browser_id | 必須32hex。空/None/現在値への書換えは禁止 |
| issued_at_utc / expires_at_utc | UTC秒精度。差は**14400秒（4時間）**、期限は排他的 |

JSONにactive/approvedという自己申告boolを入れない。Schema成功はlive bindingや採取許可の証明ではない。
JSON parserはduplicate key、非有限数、未知key、64KiB超を拒否する。target_idは非空ASCII可視文字を受け、大小文字も含めそのまま保持する。
`run_id`/`ticket_id`/`operation_id`はこのレコードで生成しない。呼出元所有IDは既存DiagnosticEventへそのまま渡す。
イベントとBindingの対応はL02のスプール外側メタデータで`binding_id`とrecord hashを紐付ける。呼出元correlationのkeyを上書きしない。

### 5.1 既存TargetBindingとの接続

```python
# 契約の射影例。L01ではブラウザへ接続しない。
target = TargetBinding.from_dict(binding_record["target"])
```

envelope全体を`TargetBinding.from_dict()`や`CaptureRequest.from_dict()`へ渡さない。
期限・revocationは4項目TargetBindingでは表現できないため、**envelopeを保持するprofile guardを別途必須**とする。
これを省いて4項目だけを再利用する経路はChatGPT Profile v1適合ではない。

### 5.2 期限と状態

Preflightの**最終検査成功時**にissuedを採番し、4時間後をexpiresとする。4時間は本設計の運用上限であり性能予測ではない。
`issued <= now_utc < expires`を各操作・各採取の前後で確認する。owner/Worker起動時は残時間を自プロセスのmonotonic deadlineに写し、以後はUTCとmonotonicの両方で早い方を使う。
`abs((utc_now-utc_anchor)-(mono_now-mono_anchor)) > 5秒`または時刻を確証できない場合は`CLOCK_DISCONTINUITY`で失効。異なるプロセスのmonotonic絶対値をファイル共有しない。
PCのsleep/resumeが検出された場合も失効。時刻巻戻しによる期限延長は許さない。
期限中でもrevoked/superseded/Target不一致なら即時失効。期限切れ中の応答待ちは未確定終了となり、自動再送しない。
同一レコードのexpires書換えやHeartbeatによる延長は禁止。次の明示Preflightで新binding_idを発行する。
独立Workerの期限検査には、発行時から時計anchorを保持する生存中のownerまたは独立Watchdogによる有効性確認も必要とする。Caller crash後にDOM取得できるのはWatchdogがその連続性を保持する場合。古いファイルと起動時UTCだけでは、起動前の時計巻戻しを否定できないため、keeper不在/clock continuity不明ならS0のみとする（具体受渡しはL02/L07）。

### 5.3 最小の状態所有

L03/L04では単一Consumerの信頼されたメモリ状態、L05以降はL02が定義する単一writerのlocal state/spoolを使用する。
Workerは「最後に見つけた任意のJSON」でなく、**current binding_id / record hash / revoked・supersededの状態**を検査する。
状態を読む間に世代が変われば中止。診断完了前にも再検査する。ownerプロセスが停止しただけではBindingを別IDへ置換しない。
未完了・破損・競合・状態取得不能・時計keeper不在ならメタデータのみ。永続化/排他/heartbeatの具体実装はL02/L05/L07の担当であり、L01では実装しない。

## 6. Browser Epochの確定方式

### 6.1 定義

`epoch_algorithm = cdpd.browser-ws-path.sha256-128/v1`

`expected_browser_id = SHA256(UTF8(browser_ws_path)).hexdigest()[0:32]`

既存`collector.py:114`と同じ計算法。hostやportはhashに混ぜず、**endpointを別フィールドで完全照合**する。
`Browser.getVersion`の製品版、PID、target_id、独自UUIDをbrowser epochの代用にしない。

### 6.2 導出と拘束の順序

1. 接続設定のendpoint完全一致を、ソケット接続より前に確認。
2. 当該loopbackへ直接`GET /json/version`。redirectを追わず、proxy/名前解決/別port探索を使わない。HTTP 200、JSON object、64KiB以下を必須とする。
3. `webSocketDebuggerUrl`は`ws://127.0.0.1:9222/devtools/browser/<lowercase UUID v4>`のみを受理。認証情報、query、fragment、encoded path、追加segment、prefixのみは拒否。
4. pathをUTF-8でhash。既存Bindingを使う場合はexpectedと完全一致しなければ接続前に中止。
5. **検査したそのWebSocket URLへ**Web API/Workerを接続。Web APIがHTTP endpointをPlaywrightへ渡して再discoveryする実装は禁止。
6. 接続後の再discoveryも同じpathであることを確認。以後のTarget列挙・照合・attachはこの接続へ固定する。

PlaywrightのWebSocket直接接続はEXT-02、Chromiumのpath生成/公開はEXT-03で確認。
EXT-03にはGUIDなしのdiscoverable modeが存在する。**そのモードを本製品は非対応とし`BROWSER_EPOCH_UNVERIFIED`**。
特定Chrome版がUUID pathを再起動時に変更することはL03/L10/L11の適合条件であり、L01で実機証明したとは扱わない。
DevTools handler再生成でepochが変わる場合も安全側に失効させる。Hashは128bit識別fingerprintであり、数学的な衝突不能性やブラウザ認証を主張しない。

### 6.3 Golden vector（合成）

- path: `/devtools/browser/11111111-1111-4111-8111-111111111111`
- expected_browser_id: `254095102094427e3ef2c333da294588`
- 同じpathならWeb APIとWorkerで同じ値。別pathなら旧Bindingを受理しない。
- prefixのみ、`/devtools/page/...`、空ID、UUID以外のsuffix、別endpointはhash比較以前に拒否。

## 7. Preflightと照合順序

新Bindingを発行できる唯一の入口は、既存操作の停止/終了を確認した**明示Preflight**である。
これはL03の責務。操作途中の引数なしbind・例外handler・Watchdog・定期採取は発行しない。

| 順序 | 検査 / 動作 | 失敗時に禁止する処理 |
|---|---|---|
| G0 | policy・排他所有・profile起動記録、既存作業未確定の解決、record/schema/lease | ブラウザ操作・DOM読み出し |
| G1 | endpoint完全一致、/json/version厳格検査 | 別endpoint接続 |
| G2 | epoch照合、検証したws URLへの接続 | epoch未照合のTarget処理 |
| G3 | `Target.getTargets` fresh一覧、明示page filter、完全なpage集合/型/subtype検査 | 不完全一覧からの選択 |
| G4 | page合計1、ChatGPT1、既存の場合ID完全一致、新規の場合この1件を候補化 | focus/URL/最後の候補で代用 |
| G5 | 同じ接続上の`Target.getTargetInfo(targetId=...)`でtype/id/origin再照合 | 別Target attach |
| G6 | exact IDへattach、main frame origin・frame識別確認。Web APIは接続済みcontextの正確なPageへ対応付け | DOM操作・構造採取 |
| G7 | page数・epoch・Target再確認、状態世代照合。不一致がなければBinding発行/再検証成功 | 途中変化を無視した成功 |

`Target.getTargets`には`[{"type":"page","exclude":false},{"exclude":true}]`の明示filterを使う。
不明/非対応のfilterを引数なしへfallbackして成功扱いにせず`PROTOCOL_UNSUPPORTED`。適合Chrome組合せを限定してL03で確認する。
`subtype`が非空、重複ID、対象TargetInfo欠落、途中eventで集合が変化した場合は`TARGET_UNVERIFIED`。
`TargetInfo.attached=true`は他クライアント接続の有無であり、同じタブをWeb APIとWorkerが共有する構成で拒否条件にしない。
Workerが新接続を開くことは**同じレコードの再検証**であってBinding再発行ではない。

### 7.1 運用中のイベント監視

初期一覧だけでは運用中のタブ増減を見逃す。L04のguardは当該browserへTarget discoveryを有効化し、create/destroy/change/crashを軽量に監視する。
page集合を変えるeventや未知状態はラッチし、次の許可より前にfresh一覧で照合する。epoch/ID/Origin違反、page追加は一度検知したら自動解除しない。
Workerも採取前後で一覧を取り、途中page変更を検知したら構造結果を破棄する。
**CDPイベントや複数照合を完全に原子的にすることはできない**。専用browserの排他運用、同一socket/sessionへのコマンド拘束、前後照合で扱う。

### 7.2 既存Web API内部経路

- 新プロファイルは登録Binding/guardなしでは操作を開始しない。`create_facade()`自体はofflineで構築/契約検証可能だが、未登録のsessionに対する操作は明示失敗する。
- `bind_chatgpt_page()`引数なしはBOUNDレコードの**再検証**だけ。UNBOUNDでは`BINDING_REQUIRED`、INVALIDATEDでは復活させない。
- `target_id=X`は固定値とのassertionであり、違うXへ切り替える要求ではない。
- `exact_url=U`は固定Targetのcurrent URLを追加照合するだけ。URLからPageを選ばない。IDとURLを両方指定してID違反をURL優先で通さない。
- `_bound_page()`でキャッシュを返すだけでは不十分。各`_PageOps`のevaluate/goto/click/upload/downloadなどにもガードを掛ける。既に渡したBoundPageが失効後に利用されても拒否する。
- Web API自身の承認済み同一Target内navigationは可能。CDPDにnavigation/activation/closeを与えない。
- 失敗時は古い_boundの利用許可を消す。エラー後に`if not bound: bind()`で回復しない。
- 同一operationのmutationを診断のため再実行しない。送出後の失敗はdispatch済み/送信未確定の既存意味論を維持する。

Guardの実装は、Web API内の小さな検証インタフェースとprofile側実装を注入する構成とし、Web APIにCDPDパッケージimportを必須化しない。
追加の内部型/注入箇所の具体化はL04に委ねるが、**標準エントリからguardを外してweak selectionへ戻れるdefault/fallbackは禁止**。

## 8. 採取時の同一性と画面遷移

L06は既存collectorを再利用し、envelope/状態のprofile guardを採取入口に必須化する。
G0→G7の順序で検証後、固定Targetのmain frameにだけisolated worldを作り、固定された構造scriptを実行する。
scriptの先頭でも`location.origin`が期待Originか確認し、違えばDOMを走査せず固定エラーだけを返す。任意JSや本文を返すAPIにはしない。
実行コンテキスト消滅/不明は失敗であり、default contextや別frameへ再実行しない。

採取前後でtarget_id/type/origin、main frame_id/loader_id、Target URL、page集合、current binding_id、leaseを照合する。
URLはインメモリで比較するがpath/query/titleや生のWebSocket URLをBundleへ保存しない。
同じTarget内でも採取途中にnavigation・SPA URL変更・epoch変化・期限切れがあれば、未commitの構造を捨て`NAVIGATION_DURING_CAPTURE`等を返す。
DOMが動的に変化すること自体は構造差分の対象であり、全DOMの原子的snapshotを主張しない。

**同一Target内のnavigation完了後**は同じBindingを再検証して採取可能。ただしそれは「失敗後に観測した現在状態」であり、失敗時刻/同じ会話の証明ではない。
失敗イベントのdocument情報・時刻と採取時刻の紐付けはL02へ引き継ぐ。
Worker中に失敗しても自分のCDP sessionをdetach/socket closeするだけ。Browser.close/Target.closeTarget/reload/activate/再送はしない。

## 9. 公開API維持範囲と意図的な変更

### 9.1 維持する境界

| 契約 | 維持するもの | 新プロファイルで変えるもの |
|---|---|---|
| `create_facade(cdp_url=..., *, session=None)` | 署名、戻り値ChatGPTWebFacadeV2、offline契約検証 | 実操作には登録済みstrict session必須。sessionなしは操作時fail-closed |
| `chatgpt-web.facade/v2` | `open_project_chat/execute_prompt_and_wait/download_response_zip`の署名、要求/結果モデル、契約名major2 | weak target bindingの動作をサポートしない |
| `cdpd.diagnostics/v1` | `DiagnosticEvent`, `TargetBinding`, `CaptureRequest`, `DiagnosticService.record/capture`, `CaptureOutcome`, `verify_bundle`の既存data shape | profile guardにenvelopeを登録してcapture。epoch省略/任意Originは対応外 |
| `TargetBinding.from_dict()` | 既存4key（generic constructorでoptionalだった型形状を含む） | ChatGPT envelopeでは4key全部必須。full envelopeをこのparserへ渡さない |
| `CaptureOutcome` | capture_statusとassessment_statusの意味、保存失敗の例外 | Binding失敗を業務成功へ変換しない |
| `InstrumentedFacade` | 元Providerのwrapper、caller-owned operation_id/correlation | 元Facadeと署名が異なる点を維持、元Providerにだけ契約validatorを適用 |

**データshape/主要署名を維持することと、旧挙動が完全後方互換であることは違う。**
自動focused/last/URL選択、Browser IDなし、任意Origin、guardなしの操作は意図的に非互換。L04/L06で移行例と旧経路拒否試験を更新する。
既存JSON CLIのrequest shapeは維持可能だが、ChatGPT版のcapture起動には別のBindingレコード入力を必須化する。
CLIがレコードなしで素のcollectorへ抜ける経路は禁止。引数名/受渡しファイルの具体化はL02/L06へ引き継ぐ。
旧SHIRABE import/contract/compat shimは実装しない。

### 9.2 失敗結果の整合

| 事象 | profile理由コード | Web APIへの対応 | CDPD結果 |
|---|---|---|---|
| 登録なし/壊れたレコード | BINDING_REQUIRED / BINDING_RECORD_INVALID | SESSION_BINDING_FAILURE | 有効requestならPARTIAL・構造なし。parse自体不能は入力エラー |
| endpoint/profile未確認 | ENDPOINT_NOT_ALLOWED / PROFILE_UNVERIFIED | SESSION_BINDING_FAILURE | PARTIAL、接続/構造なし |
| GUIDなし/方式不明 | BROWSER_EPOCH_UNVERIFIED | SESSION_BINDING_FAILURE | PARTIAL、構造なし |
| epoch違反 | BROWSER_IDENTITY_MISMATCH | TARGET_IDENTITY_MISMATCH | PARTIAL、構造なし |
| 対象0 / ID消滅 | TARGET_MISSING | SESSION_BINDING_FAILURE | PARTIAL、構造なし |
| ChatGPT対象2以上 | TARGET_AMBIGUOUS | AMBIGUOUS_OBSERVATION | PARTIAL、構造なし |
| 他page併存 | SINGLE_PAGE_VIOLATION | AMBIGUOUS_OBSERVATION | PARTIAL、構造なし |
| target/type不一致 | TARGET_BINDING_INVALID | TARGET_IDENTITY_MISMATCH | PARTIAL、構造なし |
| Origin不一致 | TARGET_ORIGIN_MISMATCH | TARGET_IDENTITY_MISMATCH | PARTIAL、構造なし |
| 期限/時計/明示失効 | BINDING_EXPIRED / CLOCK_DISCONTINUITY / BINDING_REVOKED | SESSION_BINDING_FAILURE | PARTIAL、構造なし |
| live CDP接続失敗 | CDP_CONNECT_FAILED / CDP_COMMAND_FAILED | SESSION_BINDING_FAILURE | PARTIAL、S0のみ |
| policyが許可しない | POLICY_DENIED | 操作権限は別判定 | 既存どおりREJECTED |
| 採取途中の変化 | NAVIGATION_DURING_CAPTURE | 操作済み結果を成功に補正しない | PARTIAL、未commit構造を破棄 |

既存DiagnosticEvent.failure_codeのenumは変更しない。詳細profile理由は安全な結果/付帯証拠に記録し、イベントではTARGET_MISSING/TARGET_AMBIGUOUS/DISCONNECTED/RECONCILE_REQUIRED等の既存分類を使う（詳細対応はL02）。
`PARTIAL`は「対象違反が軽い」という意味ではなく、既存metadataだけを保存できたという意味。
Storage失敗ならBundle保存済みと報告しない。raw exception/url/titleを理由文字列へ混ぜない。

## 10. 設定・運用手順

1. 人が専用user-data-dirとChrome実行ファイルを設定。日常プロファイル/ブラウザ全体のデータを診断ZIPへ入れない。
2. L03の起動処理は対象9222が空いている時にだけ専用browserを起動する。既に利用中なら、同じ専用起動記録に紐付けられる場合だけ検査へ進む。他プロセスをkillしない。
3. CDP listenerがloopbackに限定されていることをOS側でも確認。LAN転送・reverse proxy・port forwardingを使わない。`--remote-allow-origins=*`やセキュリティ無効化で接続を通さない。
4. 人がログインを完了し、ChatGPTだけを1枚残す。ログイン用一時画面/他タブが残る間はPreflightをしない。
5. 明示Preflight → Binding成功 → Callerが承認済み作業を開始。CDPDが作業認可を代行しない。
6. 違反/切断/期限切れ時は停止し、既存証拠を保存。未確定送信を先に再調停し、次の明示Preflightで新recordを取得。
7. ブラウザ終了は人/起動管理側の明示操作。診断Workerは終了・再起動を代行しない。

本Ticketでは上記手順を定義しただけで、Chrome起動・CDP接続・ログイン・送信・Task Scheduler登録は行っていない。

## 11. 後工程への引継ぎ

| 担当 | 必須の引継ぎ |
|---|---|
| L02 | binding_id/hashと障害eventの関連、current/revoked状態の最小永続契約、S0のみの結果、diagnostic-only ownership、clock/expiry時の停止 |
| L03 | 専用起動/所有者、正確なendpoint、GUID epoch導出、0/1/2枚＋他page検査、対応Chrome/Playwright組合せ |
| L04 | guard必須化、14 bind/23 bound参照の閉鎖、全primitive guard、weak default除去、署名/結果型不変検査 |
| L06 | record必須、独立socket同一epoch、origin guard、前後検査、不一致構造破棄、結果意味維持 |
| L07 | Caller crashとCDP identity lossを区別、失効状態の保持、自動rebind/再送なし |
| L10/L11 | [TGT試験](../tests/01_Target_Test_Cases.md)の実装/実機評価。L01の静的検査をlive PASSに読み替えない |

新規チケットは増やさない。実装モジュールを本Candidateで変更しない。

## 12. 技術参照と残存リスク

参照詳細は[外部一次資料](../evidence/technical_references.json)。本書のルール/数値は設計判断であり外部仕様の保証ではない。

- EXT-01: `https://developer.chrome.com/blog/remote-debugging-port`
- EXT-02: `https://playwright.dev/python/docs/api/class-browsertype#browser-type-connect-over-cdp`
- EXT-03: `https://raw.githubusercontent.com/chromium/chromium/main/content/browser/devtools/devtools_http_handler.cc`
- EXT-04: `https://raw.githubusercontent.com/ChromeDevTools/devtools-protocol/master/json/browser_protocol.json`

L03/L10/L11ではGUID lifecycle、page filter/subtype、複数CDPクライアント、閉鎖/再起動、clock/suspend動作を実機確認する。
GUIDなしやProtocol未対応を見つけても弱い識別へ切り替えない。P0が未検証ならRelease採用は不可だが、設計定義が存在するL01自体を実装未着手だけでBLOCKEDとはしない。
