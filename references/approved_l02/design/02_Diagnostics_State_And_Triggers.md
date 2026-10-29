---
id: CDPD-L02-DIAGNOSTICS
title: "障害・状態遷移・永続受理の詳細設計"
project: CDPD-CHATGPT-ONLY
ticket: CDPD-L02
role: EXECUTION
status: CANDIDATE
version: 0.3.0-l02-candidate.4-fix003
updated: '2026-10-18'
---

# 障害・状態遷移・永続受理の詳細設計


## 1. 適用と入力の優先順位

本書はL02の設計Candidate。製品Runtime、SHIRABE移行、ブラウザ操作、独立Assessment、Human Gate決裁は実行しない。
今回のFIX003指示＋[F-04修正要求](../rework/fix003/REWORK_GUIDE.input.md) → Source Lock済みChatGPT専用計画 → L01 FIX001のTarget/lease契約 → v0.2.1の再利用API、の順で適用する。FIX001/FIX002の修正記録は履歴として保持し、今回の設計自己検証を独立Assessmentの閉鎖判断へ読み替えない。
旧汎用計画やInventoryの未確定SHIRABE情報で本スコープを拡張しない。最新SHIRABEソースはL09で必要だが、L02の不足入力ではない。
[入力検証](../evidence/inputs/input_verification.json)、[依存Lock](../DEPENDENCY_LOCK.json)、[コード根拠](../evidence/source_excerpts.md)を参照する。

**継承:** 専用Windows 11/Chrome、exact endpoint `http://127.0.0.1:9222`、page合計1枚、exact origin `https://chatgpt.com`、Browser Epoch/Target固定、4時間lease、5秒時計不連続、暗黙rebind禁止。
L01のSchemaは[無変更コピー](../contracts/chatgpt_target_binding_v1.schema.json)。そのrecord、状態機械、既知CDP断時の失効をL02で緩めない。

## 2. 現行ソースで確認した事実と変更境界

| 根拠 | v0.2.1の現在の挙動 | L02で規定する拡張（後工程が実装） |
|---|---|---|
| `service.py:51–60` | `record()`はdequeへ追加しTrue。永続化なし | TrueはMEMORY_ACCEPTEDの意味だけを維持。永続ACKは別のprofile transportで表現 |
| `service.py:62–95` | 同期collectの後、4種ファイルをまとめてcommit | S0を先に独立保存し、1回workerを起動。最終commitはsupervisorだけ |
| `contracts.py:10–13` | kind6種、failure9種。eventに時刻/sequenceなし | enumを増やさず、外側envelopeでevent_id/sequence/Binding/固定reasonを付与 |
| `adapter/__init__.py:17–40` | Queueはbounded/nonblocking、満杯でdropped++ | booleanをdurable ACKと呼ばない。critical予備、欠落下限、独立spoolへ接続 |
| `adapter/__init__.py:67–93` | providerは1回、例外本文を記録せず、診断例外は抑制 | 診断失敗で戻り値・元例外を変えない。開始前の診断準備失敗は未実行として拒否 |
| `store.py:30–61` | 4種JSON＋Manifest＋COMMIT | ファイル集合/Manifestのschemaを維持。新profile情報は既存JSON内の追加領域 |

追加profile helperの名前は設計インタフェースであり現行import可能なAPIではない。
`DiagnosticEvent`、`CaptureRequest`、`CaptureOutcome`、`create_facade()`、3操作の主要署名を変えない。
`InstrumentedFacade`のoperation_id付き署名は元Facadeと異なるまま。契約validatorは元providerへ適用する。

## 3. 障害分類とトリガー

機械可読の正本は[diagnostic_event_matrix.json](../contracts/diagnostic_event_matrix.json)。現行は26規則（FIX001でEVT-25、FIX002でEVT-26を追加、FIX003では全26規則を維持）。未定義の組合せは固定エラーで拒否し、ブラウザへ接続しない。
「UI要素がない」と「タブがない」を同じ復旧処理にしない。

| 検知内容 | generic kind / failure | profile reason | 採取方針 |
|---|---|---|---|
| Locator 0件 | ACTION_FAILED / TARGET_MISSING | LOCATOR_NOT_FOUND | Binding有効なら構造採取 |
| Locator複数 | ACTION_FAILED / TARGET_AMBIGUOUS | LOCATOR_MULTI_MATCH | 同上。どれかをクリックしない |
| hidden/disabled/遮蔽候補 | ACTION_FAILED / NOT_INTERACTABLE | ELEMENT_NOT_INTERACTABLE | 同上。実clickで検査しない |
| 操作/観測Timeout | ACTION_FAILEDまたはOBSERVATION_FAILED / TIMEOUT | ACTION_TIMEOUT / OBSERVATION_TIMEOUT | 同じTargetを検証して採取。再送不可 |
| Provider例外／未分類failure result | ACTION_FAILED / UNKNOWN | PROVIDER_FAILURE_UNCLASSIFIED | 元例外・戻り値を変更せず同じBindingのguardへ。未送出とは断定しない |
| 状態不明・矛盾 | OBSERVATION_FAILED / UNKNOWN | OBSERVATION_UNDETERMINED | DOM変更と断定しない |
| Target/epoch/origin/ページ集合不一致 | ACTION_FAILED / TARGET_MISSING等 | TARGET_IDENTITY_MISMATCH / PAGE_SET_VIOLATION | Binding失効、S0のみ |
| 既知CDP断/Chrome終了 | OBSERVATION_FAILED / DISCONNECTED | CDP_CHANNEL_LOST / CHROME_EXITED | 再接続せずS0のみ |
| heartbeat期限/Consumer終了 | OBSERVATION_FAILED / TIMEOUTまたはUNKNOWN | HEARTBEAT_EXPIRED / CONSUMER_EXITED | 独立keeper生存＋同じBの新規検証成功時だけ採取 |
| Worker停止/期限 | OBSERVATION_FAILED / UNKNOWNまたはTIMEOUT | WORKER_FAILED / WORKER_DEADLINE | 当該要求を閉じる。診断から再帰採取しない |
| Spool満杯 | OBSERVATION_FAILED / UNKNOWN | SPOOL_BACKPRESSURE | 欠落を記録。新規操作を止める。診断stormを起こさない |

### 3.1 AdapterとEnvelopeの接続（F-01）

[固定変換表](../contracts/adapter_event_mapping.json)を境界とする。例外発生と未知のfailure_kindの戻り値は、継承Adapterではいずれも`ACTION_FAILED/UNKNOWN`になる。例外が起きたと決め付けず、共通理由`PROVIDER_FAILURE_UNCLASSIFIED`を使う。generic eventのkind/failure/operation_id/producer/correlationは無変更。
Adapterが戻り値の診断読取りで失敗した`OBSERVATION_FAILED/UNKNOWN`は、既存の`OBSERVATION_UNDETERMINED`へ接続する。生のexception、reason文字列、debug dictは変換の入力に含めない。元例外は同じオブジェクトを呼出元へraiseし、返却済みresultは同じオブジェクトを返す。
`ACTION_FAILED/UNKNOWN`（及びFIX002で追加した`ACTION_FAILED/DISCONNECTED`）だけで`NOT_DISPATCHED`を証明してはならない。この変換では既存の安全なdispatch観測`POSSIBLE/CONFIRMED/UNKNOWN`を保持し、他の場合は`UNKNOWN`へ保守化する。分類後もprovider最大1回、再送0回。
FIX003の明確化: `OBSERVATION_FAILED/UNKNOWN`のintrospection経路は、信頼された呼出側が別途観測したtemplateのdispatch状態を保持する。既知の`NOT_DISPATCHED`を保持することと、getter例外から未送出を推論することは異なる。独立した未送出根拠がないtemplateは`UNKNOWN`にする。generic観測失敗には非mutationもあるため、この経路を一律にACTION_FAILEDへ変換しない。これらは既存F-01/F-05の変換・呼出回数・Schemaを変更する追補ではない。dispatch文字列は操作Admissionや自動再送の許可にならない。
`ACTION_STARTED/NONE`のprofile開始情報は、L04の**Provider呼出し前**の同期Admission境界で生成する。旧Adapterのnonblocking queueへemitしただけでは操作開始を許可しない。後からqueueをdrainして得た開始eventは、既に実行された操作の事後履歴にすぎず、遡ってAdmissionを成立させない。
複数の理由が同じgeneric pairを持つ場合は、信頼されたHookの固定reasonが必須。曖昧なpairをLocator失敗／Target喪失のどちらかへ推測変換しない。

`GUARDED_STRUCTURE`は採取許可を自己宣言する値ではない。各要求でL01 G0–G7、policy、current generation、live keeper、leaseを検証する。
policy拒否が最優先でREJECTED。入力parse不能はINPUT_REJECTED。その他のidentity/lease/接続問題はPARTIAL・構造なしというL01の意味論を維持する。
このpolicy優先は、GUARDINGの入場判定など、同じ判定境界で適用可能な条件に対する規則である。既に受理した終端化イベントを後続イベントで並べ替える規則ではない。現行状態機械ではGUARDINGで`POLICY_DENIED`を受理するとREJECTEDを選ぶが、先に`KNOWN_CDP_LOSS`でFINALIZINGへ移った後の`POLICY_DENIED`は新しい遷移を作らず、構造なしPARTIALを維持する。先に受理したpolicy拒否は消さない。両順序とも新規DOM/COMPLETE/再接続/再送は不可である。この位相の明確化で既存状態遷移・割込み優先値を変更しない。

## 4. データ契約と識別子

[Envelope Schema](../contracts/diagnostic_envelope_v1.schema.json)が外側の型正本。内側`event`は`cdpd.event/v1`を変更せず保持する。

| 項目 | 意味・生成者 |
|---|---|
| `event_id` | 計装SDKがイベント生成時にUUID v4を1回発行。配送再読込でも同じ値 |
| `producer_seq` | producer instance内のsequence domainごとに単調増加、1始まり。queue投入を試みる前に割当て、再利用しない。通常eventと別mailboxのheartbeatを混ぜない（5.5） |
| `occurred_at_utc` | 観測時刻。UTC秒精度、順序証明には使用しない |
| `binding_id` / `binding_sha256` | L01原recordのIDと**元UTF-8バイト列**のSHA-256。再serializeしたhashを代用しない |
| `control_generation` | supervisorの現在状態世代。Bindingレコードの一部ではなく、入場時比較する値 |
| `event.*` | caller-owned IDs/correlationを無変更で維持。パス生成に使わない |
| `profile.reason` | matrixの固定コードだけ。例外メッセージを入れない |
| `dispatch_state` | NOT_DISPATCHED/POSSIBLE/CONFIRMED/UNKNOWN/NOT_APPLICABLE。業務成功とは別 |
| `document_marker` | 既に安全に観測したframe_id/loader_id、なければnull。本文/URLなし |
| `loss_lower_bound` / `loss_count_exact` | 同じ基本streamの通常event損失について確定している累積下限と、総数を確定できるか。heartbeatも同じcounterのsnapshotを運び、coalesce回数を加算しない。crash後に0で埋めない（5.5） |

Preflight未成立の診断はbinding_id/hashが両方null、generation=0。このenvelopeはメタデータ記録のみ可で、DOM許可を作らない。
型成功だけではreasonとevent kind/failureの整合、現在Binding、権限は保証できない。matrix照合・状態照合を別途必須にする。
JSONはUTF-8、duplicate key/非有限数/未知key/制御文字/32KiB超を拒否する。raw exceptionをエラー内容へ複写しない。
IDはtrim・置換・hash化してごまかさない。不適合値は拒否する。correlationは信頼されたConsumerがopaque IDだけを渡す契約であり、Schemaが値の機密性まで証明するとは主張しない。

## 5. 永続スプールとACK境界

### 5.1 採用構成

汎用ネットワークIPCは置かない。**同じWindowsユーザーが所有するローカルNTFS上のファイルmailbox＋supervisor単一writer journal**を使う。
Consumer内にはnonblocking計装queueと専用writer threadを置ける。プロセスはConsumer / Supervisor / one-shot Workerの3責務だけ。

```text
Consumer event -> memory queue -> inbox/<sdk-generated-uuid>.ready.json
                                      |
                                Supervisor validate
                                      |
                             single-writer journal fsync
                                      |
                          receipt/<event-id>.json = ACK_DURABLE
```

| 段階 | 呼出側が言えること | 言ってはならないこと |
|---|---|---|
| `record()` / `try_emit()`のTrue | MEMORY_ACCEPTED | 永続受理済み、障害時に必ず復旧可能 |
| inboxのready公開 | LOCAL_PUBLISHED | supervisorが採用済み、採取予約済み |
| journal row検証・flush/fsync・receipt公開後 | ACK_DURABLE、journal_seq/row hash付き | capture完了、電源断でも絶対に消えない |
| S0＋incident indexの永続化後 | CAPTURE_ACCEPTED、capture_id付き | DOM取得済み |
| COMMIT＋verify_bundle成功 | BUNDLE_COMMITTED | 業務成功、Baseline承認 |

inboxは同一ディレクトリ内の独占作成`.tmp`へ書き、flush→fsync→close→同一volume renameで`.ready.json`として公開する。生成UUID衝突時に既存データを上書きしない。
supervisorはreadyだけを対象にschema・最大長・payload hashを検査し、journalへ順番に追記する。
journalの1行は`journal_seq`, `event_id`, `payload_sha256`, `envelope`, `record_sha256`を持つ。record hashはそのフィールド自身を除いたcanonical JSON（sorted keys、compact separators、UTF-8、allow_nan=False）のhash。
追記は単一writer、1レコードごとにflush/fsync。ACK receiptはjournal fsync後にだけ同じ方式で公開する。
receiptにはsource payload hashも含め、呼出側は自分のevent_id/hashと一致するACKだけを認める。mtimeやファイル存在だけではACKにしない。

### 5.1.1 Receiptの厳密な意味（F-03）

型正本は[Receipt Schema](../contracts/diagnostic_receipt_v1.schema.json)。次の3hashは別物である。

| 項目 | ハッシュ対象 |
|---|---|
| `payload_sha256` | strict parseしたEnvelopeのcanonical JSON UTF-8（sorted keys/compact/非有限数禁止）。改行区切りを含めない |
| `journal_record_sha256` | journal行の`record_sha256`を除いたcanonical JSON。行末LFは含めない。journal_seq/event_id/payload_sha256/envelopeを含む |
| `current_projection.current_sha256` | 開始時に永続公開したcurrent snapshot全体のcanonical JSON。Binding recordのraw bytes hashとは別 |

`ack_purpose`は`EVENT_DURABILITY / OPERATION_ADMISSION / CAPTURE_RESERVATION`。`ACK_DURABLE`という文字列だけでは開始許可にならない。`ACK_DURABLE/CAPTURE_ACCEPTED`では正のjournal_seqとjournal_record_sha256、failure_code=nullが必須。非永続stageはrow情報をnullとする。
開始用は`ACK_DURABLE + OPERATION_ADMISSION`だけであり、`current_projection`にsupervisor_instance_id/control_generation/binding_id/binding_sha256/operation_id/armed_event_id/current_sha256を必須とする。通常のevent ACKにはcurrent_projectionを付けない。`CAPTURE_ACCEPTED`はCAPTURE_RESERVATION＋capture_id付き、操作認可ではない。

検証はSchemaだけで終わらせず、(a)受信者が保持する元Envelope、(b)該当journal行、(c)開始時current snapshot、(d)現在のlive current、を照合する。row hashは自分で再計算し、payloadの一致・event_id・journal_seqも個別確認する。row中のenvelopeが違えば失敗。
currentのactive_operation_refはoperation_id/producer_instance_id/armed_event_id/journal_seqを持つ。開始ACKはjournalとcurrentのwrite/flush/fsync・currentのatomic公開が完了してから、receipt自身をwrite/flush/fsync・atomic公開する。参照順はjournal→current→receipt。途中でcrashした場合、journalが残っていても開始許可は未成立。
開始直前にcurrent snapshot/hash、同じsupervisor instance、同じcontrol_generation、同じBinding、state=BOUND、revoked=false、overlay=NONE、active operationの一致を再確認する。生存・Target・leaseのguardも別途必須。受信ACKの整合検査結果を`receipt_hash_matches`とする設計モデルのboolは**検査結果の注入値**であり、Schemaで実fsyncを証明するfieldではない。

開始要求を送出したConsumer自身のmonotonicで2秒未満の間だけ入場可能。2秒ちょうどで期限切れ、取り消し済み・利用済みのAdmissionは拒否。ACK再読込で時計や使用済み状態をリセットしない。durable ACKの履歴上の有効性と、今回の操作開始の有効性を分離する。次の操作で過去ACKを使い回さない。
`failure_code`はSchema内の固定enumのみ。REJECTED/UNKNOWNは非nullの固定code、正常な受理stageはnull。exception str/repr、改行、未知の文字列は拒否する。

Pythonのflush/fsyncの対応は[外部一次資料EXT-L02-01](../evidence/technical_references.json)。Windowsのdirectory metadataやハードウェアを含む完全電源断保証は未検証かつv1の保証外。**ここでのdurableはfsync完了を境界とするprocess-crash復旧契約**である。

### 5.2 順序と冪等性

同じproducer instance内の同じsequence domainはproducer_seq昇順で採用する。通常eventの欠落sequenceは最大2秒だけ待ち、次のrowにmissing rangeを明記して進める。別producer間はsupervisorのjournal_seqが採用順を表すが、実際の発生因果順を証明しない。別mailboxのheartbeatは5.4/5.5の独立liveness sequenceで扱い、通常eventの待ちや欠落判定を充足しない。
遅着eventは遅着として保存し、過去のincidentを黙って書き換えない。停止済み/失効Bindingの遅着で新しいDOM採取を開始しない。

同じevent_id＋同じpayload hashは元receiptを返す。event_id一致でhash違い、または同じproducer_instance_id＋sequence domain＋producer_seqの別event_idはEVENT_ID_CONFLICTとして拒否。原行は保持する。domainは5.5の固定tripleから導出し、呼出側に任意の別domainを指定させない。
重複ACK/ready再読込は許容するが、同じcapture_idのworker試行は最大1回。これは「UI操作のexactly-once」保証ではない。
重複抑止ledgerはBindingの期限＋120秒まで保持し、上限超なら新規受理を拒否する。ledger削除で期限内の重複を新規にしない。

### 5.3 操作開始と障害後の非干渉

操作開始時は`OPERATION_ARMED`を永続受理し、supervisorがactive operationとBindingを対応付けてからproviderを最大1回呼ぶ。
OPERATION_ARMEDのjournal行がactive operationの正本となる。current stateはその射影として世代付きで保存し、開始用ACKはjournalとcurrentの両方のflush/fsync完了後に公開する。途中crashでは開始ACKを返さず、復旧はmetadata-onlyとする。
待ちは2秒のprofile admission deadlineとし、ACK不能なら**provider呼出し前に停止**する。遅れてACKが届いても自動実行しない。未送出が確認できる時だけNOT_DISPATCHEDとし、それ以外は保守的なPOSSIBLE/UNKNOWNを残す。
公開Web APIの署名は維持し、この開始前チェックはprofile側のAdmission/InstrumentedFacade境界で実装する。新しい`record()->True`を操作認可に読み替えない。

送出後のlogging失敗で既存providerの戻り値/例外を変更しない。次のmutation開始を抑止し、元operation_idと送信未確定性をConsumerへ返す。CDPDは業務再送/新operation_idの採番で回避しない。
通常の障害通知はasync。待ち続けて主処理を固めるのではなく、失敗後の再調停と診断を分離する。

### 5.4 満杯・欠落・終了

[上限JSON](../contracts/diagnostic_profile_limits.json)に値を一元化。memory256（うちcritical16）、spool64MiB/4096records（うちcritical1MiB/32records）を採用する。
通常eventでcritical予備を消費しない。criticalにも空きがなければ受理を拒否し、永続ACKを返さない。古い未処理eventやactive incidentのS0を削除して成功にしない。
queue投入前に割り当てたseqと欠落counterを次の正常eventで報告する。shutdown正常時は最終high-water/欠落counterをflush、crash時の未flush件数は**unknown**として下限だけ表示する。
heartbeatは別mailbox/別seqで最新値をcoalesceし、通常のqueue満杯に巻き込ませない。liveness seqとevent seqを混ぜて欠落と数えない。
historyは120秒、1000event、1MiBのうち先に達した範囲。間引き・欠落・切詰めをS3に記録し、履歴が不足した採取をCOMPLETEにしない。

### 5.5 FIX003 — 履歴区間と累積lossの相互整合

新しい必須fieldを追加せず、既存Envelopeの`profile.loss_lower_bound / loss_count_exact`とRESULTの`history_coverage`を接続する。正本は[Bundle入力契約](../contracts/bundle_input_contract.json)と[必須前段](../tests/bundle_input_model.py)。保存前・読戻し・Report生成・受信に同じ規則を適用し、矛盾する入力を修理して受理しない。

**streamとreset:** 基本streamを`(producer_id, producer_instance_id, binding_id, binding_sha256, control_generation)`の組とする。現在のBundleはtriggerと同じ基本streamの履歴だけを保持し、別producer/instance/Binding/世代の混在を拒否する。sequence domainは既存の固定triple `HEARTBEAT / NONE / HEARTBEAT_SAMPLE`だけを`LIVENESS_SAMPLE`、他を`DIAGNOSTIC_EVENT`と導出し、新fieldを追加しない。lossの母集団は基本streamに共通するDIAGNOSTIC_EVENTの損失1本であり、その基本stream開始からの累積下限であって各eventの増分ではない。heartbeat Envelopeもこの同じcounterのsnapshotを運ぶ。liveness sampleのcoalesce回数を通常event損失へ加算せず、domainごとに別の損失母集団を定義しない。`producer_seq`はproducer instance内でdomainごとに単調なsequenceを継承し、Binding/世代が変わっただけでは1へresetしない。新producer instanceや正規のBinding/世代変更は別stream・別Bundleで扱い、旧eventを新streamへ付け替えない。単独Bundleから過去の正当な世代変更や実process再起動を認証できるとは主張しない。

trigger Envelopeと保持historyに含まれるEnvelopeを基本stream＋domain＋`producer_seq`で照合する。履歴のevent_idはdomainをまたいで一意とし、同じtriggerをhistoryにも保存した場合は同一内容の観測1件としてcounterを二重計上しない。同じdomain/sequenceに相反するevent/counterを対応させてはならない。domainはsequenceの比較可能性を分けるためだけに使う。同じdomainのseq順で共通counterのloss下限が減少した場合、又は`loss_count_exact=false`からtrueへ戻った場合は`PROFILE_INCONSISTENT`として拒否する。別domainのseqやUTCからsnapshotの前後関係を作らず、全carrierの最大下限と不明状態を集計へ伝播する。生成側は総数不明を同じ基本streamで保持し、counter上昇や正常heartbeatから確定へ戻さない。

保持historyからDIAGNOSTIC_EVENTだけを抜き出した同一stream内の隣接sequenceを \(q_1<\dots<q_m\)、triggerを含む全domainの重複除去済みEnvelope集合を \(V\)、各Envelopeの累積loss下限を \(\ell_e\) とする。必須の下限は次式である。

\[
G=\sum_{i=2}^{m}\max(0,q_i-q_{i-1}-1),\qquad
L_{\min}=\max\!\left(\max_{e\in V}\ell_e,\ G\right).
\]

RESULTの集計下限 \(L_H\) は`history_dropped_count == history_coverage.loss_lower_bound`を満たし、さらに \(L_H\ge L_{\min}\) とする。各carrierは同じ通常event損失counterの重複snapshotであり、counterとgapも同じ欠落を報告し得るため、これらを無条件に加算しない。個別counterと集計はともに0〜9007199254740991（\(2^{53}-1\)）の整数とし、表現不能な値を丸めて受理しない。上限の根拠までは単一Bundleから再構成できないため、これは確認できた最小値との整合検査である。全Envelopeがexact=trueでも集計総数の確定を単独では証明せず、生成側で必要履歴全体の省略・欠落を確定できない場合は集計exact=falseを要する。triggerだけでlossを報告した場合も集計へ反映する。

\[
\text{history.complete}=\text{true}\ \Longrightarrow\
L_H=0\ \land\ \text{history.loss\_count\_exact}=\text{true}
\ \land\ \left(\forall e\in V:\ell_e=0\land e.\text{loss\_count\_exact}=\text{true}\right)
\ \land\ G=0\ \land\ \neg\text{sampled}.
\]

Envelopeの1つでも総数不明なら集計も`loss_count_exact=false`とし、history.complete=trueを許さない。sampling又は必要履歴の件数/byte上限による切詰めは`sampled=true, complete=false`として記録する。既知の省略数は集計下限に反映し、省略総数を確証できなければexact=falseを維持する。集計を正しく記録した履歴不足では、正常Worker・同一性等が成立する検証済み構造をHISTORY_INCOMPLETEのPARTIALとして保持できる。ReportはUNKNOWNであり、Baseline適格性を作らない。

**window・遅着・順序:** gapを数えるのは保持historyに実在する通常eventの隣接行の間だけである。保持先頭より前のprefix、保持末尾より後のsuffix、history外のtriggerまでの距離はgapにしない。例えば保持seq41/42はprefix欠落を自己申告させず、seq41/43は内部gap1を要求する。heartbeatはcoalesceされるためliveness seqの飛びを通常eventの欠落に数えず、通常eventのgapをheartbeatで埋めない。通常1 / heartbeat2 / 通常3には通常gap1があり、通常1 / heartbeat100 / 通常2 / 通常3は通常eventが連続している。heartbeatが運ぶ共通通常event損失counterのloss/unknownも集計のmax/不明伝播へ含める。producer_seqをSupervisorの`source_cutoff_seq/journal_seq`又は別domainのliveness seqと比較しない。発生UTCと受信UTCは相関用でありseqの順序を覆さず、UTC値から欠落を免除しない。遅着eventは規定のseq順・stream・domain・cutoffで扱い、既COMMITの履歴を後から補完しない。

**可用性の代償:** 現行fieldには累積lossを120秒windowへ分割するreset/rangeの証明がない。そのため、同じstreamの累積counterが一度loss>0又はexact=falseを示すと、古い欠落がwindow外になったように見えても、そのstreamの後続Bundleは履歴COMPLETE/PASSへ戻せない。timer経過・新capture_id・UTCの書換えでcounterを0へresetしない。正当なstreamの切替は既存の明示Preflight/producer lifecycleに従い、自動resetや新operation_idによる未確定操作の再送に使わない。窓外の既知lossを将来除外する場合は、帰属区間・high-water・resetを検証する別契約が必要であり、FIX003では追加しない。この保守性は欠落を成功扱いにしないための設計上の選択である。

## 6. incident状態遷移

[状態定義JSON](../contracts/diagnostic_state_contract.json)が正本。通常遷移は次の通り。

```text
RECEIVED -> S0_DURABLE -> QUEUED -> GUARDING -> S1 -> S2 -> POSTCHECK
                                                        |
                                                 FINALIZING
                                           / COMPLETE / PARTIAL / REJECTED
```

入力不正はINPUT_REJECTED、保存不能はSTORAGE_FAILED（Bundle pathなし）。上位割込みはstorage error → supervisor/keeper喪失 → privacy/identity違反 → worker失敗/期限の順。
### 6.1 FINALIZINGの失敗ラッチ（F-02）

`POSTCHECK`だけでなく、`FINALIZING`およびCOMMIT公開の直前まで、privacy/identity違反、Binding失効、lease満了、Worker異常/終了未確認、keeper喪失を受け付ける。単にDENYして要求を放置せず、未確定構造を破棄してFINALIZINGへ留まる。
incidentに単調な`failure_latched`と固定codeの`failure_reasons`を保持する。ラッチされた同じcapture_idでガードが再び正常に見えても解除しない。`COMMIT_COMPLETE_VERIFIED`はラッチなし＋全必要stage/coverage/history＋Worker正常終了確認＋最終guard＋S0/S3永続化＋Bundle検証が成立した場合だけ許可する。COMMITイベントは呼出側が自称する許可ではなく、Supervisorのfinalize処理がこの条件を成立させた結果である。
失敗をラッチしたら、旧S3の確定と旧Bundle検証成功も無効にする。失敗理由を反映したS3の再保存・再検証が必須である。失敗ラッチありの場合はstructureを0件にし、保存正常ならS0/S3を含む`COMMIT_PARTIAL_VERIFIED`で閉じる。正常・同一性検証済みの上限切詰め又は履歴不足だけはラッチなしPARTIALとして検証済み範囲を残せる。S1/S2のFAILED/NOT_RUNを上限切詰めに読み替えない。構造保持の条件は[Evidence契約7.3](02_Evidence_And_Report_Contract.md#73-fix003-履歴とstageの意味検証)に従う。STORAGE_ERRORは最優先でSTORAGE_FAILED、Bundle参照を捏造しない。
Supervisor単一writerの同一critical sectionで、受理済み割込み→current/leaseの最終検査→公開する結果種別→COMMIT公開を順序付ける。既に受理した失敗をCOMMIT処理で追い越さない。これはCDP/OS全体の原子的snapshot保証ではなく、検知されていない過去の変化を検出したとは扱わない。
COMMIT後のイベントは別のメタデータへ記録し、既COMMITのstatus/hash/構造を変更しない。再worker起動、UI再送、暗黙rebindは0回。

保存正常時は必ずS3と最終commitへ進む。タブ/epoch変化を検知した場合は未commit構造を全破棄し、PARTIALのメタデータだけを保存する。
状態機械の終端に再入場しても再採取しない。後続明示要求は別capture_idであり、元の未確定Web操作を再実行するものではない。

同一operationで複数失敗が同時に届いた場合、supervisor単一writerが1incidentへcause event IDsを追加する。初回triggerとcutoff_seqは不変、identity失効など強い制約はラッチする。
terminal後の新eventは別のメタデータ記録とする。root causeを後から差し替えて同じBundleを書き直さない。

## 7. 実装への引継ぎとAC

L05: queue/mailbox/journal/receipt/currentの最小Coordinatorと欠落・event sideband。L04: begin-operation admissionと既存guardへの接続。L06: S0–S3とCOMMIT。L07: liveness/失効/worker監督。
L04は計画依存どおりL03後に進め、L05/L07完了前は本書のAdmission契約Fakeで正常/拒否/遅着ACKを検証する。FakeでのPASSを本番経路の使用可としない。L06はL05の最小Coordinatorに結合し、未実装のlive keeper/phase permitを使う試験は契約Fakeと明示する。L07で本物のpipe/heartbeat/Job Objectに置換して統合する。L10/L11で実統合を評価するまで本番登録はしない。**依存辺をL07へ逆向きに追加せず、12Ticket/DAGを変更しない**（C-02）。
本書で定義した状態・ACKは実装済みではない。[注入試験](../tests/02_Fault_Injection_Cases.md)で実装後に検証する。
AC01は状態JSON＋matrix＋再送禁止、AC03は5章とACK試験、AC05は試験定義で追跡する。

## FIX002 — F-05: ACTION_FAILED / DISCONNECTED の正式経路

[EVT-26](../contracts/diagnostic_event_matrix.json)と[Adapter mapping](../contracts/adapter_event_mapping.json)で`ACTION_FAILED / DISCONNECTED / PROVIDER_DISCONNECTED`を追加する。既存EVT-01〜25を変えず、OBSERVATION_FAILEDへの変換やUNKNOWNへの読み替えをしない。操作ID/producer/correlationと元resultを保持し、呼出は一度だけ。送出状態は独立の事実であり、断だけではNOT_DISPATCHEDを推測せずUNKNOWN/POSSIBLE/CONFIRMEDを維持する。

Supervisor/最小Coordinatorは同じBinding ID/hash/generationに対する断を、開始admissionと同じ単一writer順序で処理する。currentをINVALIDATED/revoked、停止overlay=STOPへ移し、以後の開始ACK・phase permitを不成立にする。行hash付きACKはそのeventの記録であって新操作許可ではない。単なるmetadata-only ruleの追加ではなく、[state event effect](../contracts/diagnostic_state_contract.json)と[制御効果oracle](../tests/fix002_model.py)へ接続する。

S0未永続ならS0を先に保存し、保存成功後だけKNOWN_CDP_LOSSを最終化へ投入する。S0_DURABLE/QUEUED/GUARDING/S1/S2/POSTCHECK/FINALIZINGではfailure latchを設定し、未commit構造・古いS3検証を失効させる。保存が成立すればS0/S3のPARTIAL、保存失敗ならSTORAGE_FAILEDでBundle pathを出さない。Workerを新規起動しない。既commitは書き換えず後続metadataと同じBindingの失効だけを扱う。既に別の明示Preflightで世代が進んだ古いeventはmetadata-onlyとし、新Bindingを誤って失効させない。

新mutation停止は、既に送信済みか不明な操作を取り消せたとの保証ではない。CDPDから再接続・再送・再読込・UI操作・自動Preflightを追加しない。実プロセス停止、単一writerの原子性、後続UI操作が起きない証拠はFLT-60/61/62の実装後試験へ引き継ぐ。
