---
id: CDPD-L02-EVIDENCE
title: "Evidence・秘匿・Baseline・評価レポート契約"
project: CDPD-CHATGPT-ONLY
ticket: CDPD-L02
role: EXECUTION
status: CANDIDATE
version: 0.3.0-l02-candidate.4-fix003
updated: '2026-10-18'
---

# Evidence・秘匿・Baseline・評価レポート契約


## 1. 四つの結果を分離する

`CaptureOutcome.capture_status`は採取範囲、`verify_bundle().integrity_valid`は完全性、レポートの`assessment_status`は静的互換性評価、Web応答/SHIRABE WORKはConsumerの別契約である。
COMPLETEであっても元のUI操作は失敗してよい。PARTIALでもS0は有用だが、WORK成功やRelease承認へ変換しない。
本TicketのRESULT.jsonに記録するPASSも設計の静的自己検証だけであり、製品実装のPASSではない。

## 2. Bundleの保存形式（既存形状の再利用）

```text
bundles/C-<generated-uuidhex>/
  TRIGGER.json       # unchanged cdpd.event/v1
  EVENTS.json        # cdpd.events/v1 / events[]
  RESULT.json        # cdpd.capture-result/v1
  STRUCTURE.json    # optional cdpd.structure/v1
  MANIFEST.json     # cdpd.bundle-manifest/v1
  COMMIT.json       # cdpd.commit/v1、最後に作成
```

`EvidenceStore.commit()`が許可する4ファイル名を増やさない。S0/S1/S2/S3という新しいrootファイルをこのBundle内に無造作に追加しない。
TRIGGERは従来eventを維持。EVENTSの`events[]`は従来`received_utc`/`event`を維持し、追加`profile`でenvelopeのsequence/Binding/時刻根拠/固定reasonを保持する。
RESULTには既存capture_status/failure_code/assessment_status等を維持し、**汎用保存に対しては追加領域だが、本ChatGPT Profileでは必須の`profile`**に以下の新領域を置く。STRUCTUREにも本Profileで必須の`profile`を追加する。FIX002ではS1のReport向け限定局所射影を`feature_shapes`、機能観測を`probes`、S2構造を従来の`nodes`へ固定する。追加の未型定義fieldは保存しない。既存APIの既存必須fieldを削除・改名しない。

| RESULT.profileの項目 | 内容 |
|---|---|
| profile_schema / capture_id | ChatGPT Profile v1と要求識別 |
| binding_record / binding_record_raw_json / binding_record_sha256 | 元L01 record、保存時に参照したそのUTF-8 JSON原文と原bytes hash。原文はL01閉Schemaへ厳密parseし、typed recordとの同一性も検査する。DOM/会話原文ではない |
| control_generation / supervisor_instance_id | 採取時に照合した制御世代。権限の自己申告ではない |
| cause_event_ids / initial_trigger_event_id / trigger_envelope | 初回cause不変、最初のID一致、元eventとBinding/世代/reasonをEnvelopeごと型固定。TRIGGERとの完全一致が必要 |
| source_cutoff_seq / history_coverage | 120秒/1MiB/1000件の取得境界、欠落下限・unknown・間引き |
| stages | S0/S1/S2/S3のstatus/reason/duration、未実行はNOT_RUN |
| observation_relation | SAME_DOCUMENT_VERIFIED / DIFFERENT_DOCUMENT / UNKNOWN / CURRENT_OBSERVATION。Target同一性とは別 |
| timing | failed/accepted/capture-start/finish、latency。UTCは観測、順序はseq/同一process clock |
| identity_checks | before/after/current/keeper/leaseの成否、相違した安全なcode |
| sanitization | 構造script/probe catalogのhash、allowlist版、余剰field除去ではなく違反時拒否 |
| worker_termination | NORMAL / KILLED / EXITED_ERROR / UNCONFIRMED / NOT_STARTED |
| document_markers | trigger / before / afterの固定frame_id/loader_id。故障時不明を同一と推測しない |

元document_markerとcaptureのframe/loaderが両方確かで一致する場合のみSAME_DOCUMENT_VERIFIED。**この名称はブラウザdocumentの一致だけを表し、同じ会話の証明ではない（C-01）。** 会話コンテキストについて連続観測の証拠がなければ不明とし、故障時と同じ会話であるとの因果説明はしない。既存の安全な観測で差異を確認できた場合だけ記録し、会話URL/本文を取得して推測しない。L01の保証範囲は変更しない。
障害incidentでtrigger時のmarkerが無い場合はUNKNOWN。採取したDOMは失敗**後**の観測であり、失敗時点の完全snapshotではない。
MANUAL_CAPTURE/SCHEDULED_CAPTUREは故障時点との因果比較ではなくCURRENT_OBSERVATIONと明示する。この用途では故障時document照合を要求せず、採取自体の前後document同一性とrecipeを必須とする。UNKNOWNな障害incidentを後からCURRENT_OBSERVATIONへ付け替えてPASSにしない。

## 3. 確定・改ざん検査

全payloadをSchema/size/privacy検査→4JSONをexclusive write/flush/fsync→Manifest（パス・bytes・SHA-256）→COMMIT（Manifest hash）→verify_bundle、の順で公開する。
上限10MiBはpayloadだけでなくManifest/COMMITを含む。現行storeはpayloadだけを数えるため、L06で総量確認を追加する。
COMMITの有無だけでは公開成功でない。verifyで必須file集合、hash/サイズ、JSON parse、profile整合まで検査後に参照を返す。
COMMIT/Manifest/JSON内の重複key、NaN、重複path、不明file、symlink/junction/reparse pointは拒否する。
SHA-256は改変検出であって署名/同一ユーザー攻撃への認証ではない。canonical JSONやZIPを再生成すれば元bytesのhashが変わる。

| 結果 | 成立条件 | 構造/レポート |
|---|---|---|
| COMPLETE | policy/同一性/全required stage/coverage/history/commitが成立 | 許可構造あり。これだけでレポートPASSにはならない |
| PARTIAL | 入力は成立するが接続/失効/timeout/切詰め/履歴欠落等で不足 | identity前後不成立なら構造なし。valid bounded truncationだけ限定構造を許す |
| REJECTED | policyで採取不許可 | ブラウザへ接続せず、保存許可のあるメタデータだけ |
| INPUT_REJECTED | request/envelopeを安全に解釈不能 | CaptureOutcomeを捏造しない。安全な固定エラーだけ |
| STORAGE_FAILED | S0/finalが実保存不能 | 新Bundle pathなし。過去S0の存在とfinal不在を別記 |

reportは**Bundleの外**へ新しいimmutable report packageとして保存する。BundleのRESULT.assessment_statusはNOT_PERFORMEDのままにし、後日書き換えてManifestを壊さない。
reportは元Bundle Manifest hashと適用Baseline hashを参照する。report自体のManifest/COMMITも独立に作成する。

## 4. DOM/ログの許可・禁止項目

| 種別 | 許可するデータ | 標準では出力しないデータ |
|---|---|---|
| 構造node | allowlisted tag/role、親index、shadow境界、hidden/disabled/visible bool、丸めたbbox | textContent/innerText/innerHTML/outerHTML、input value、aria-label/name、任意attribute、id/classの実値 |
| probe | **コードに固定した**locator_id、matcher版hash、match/visible/enabled count、bounded coverage | 任意selector/JS、matched elementのlabel/text/href、近似一致から自動操作 |
| CDPメタデータ | 固定command名、成否code、件数、相対時間、Target/epoch/frame識別 | full URL/title、WebSocket URL、request/response body、headers/Cookies、console args、stack trace |
| 操作event | opaque IDs、固定状態/失敗code、タイミング、dispatch可能性 | prompt/response、添付名/ローカルパス、provider.operation_evidence全文、例外str/repr |

ブラウザ内でallowlistへ射影してからプロセス境界を越える。取得後にraw DOMをdiskへ一度書いてからredactする方式は禁止。
Consumerで禁止fieldを拒否、Workerで固定script/返り値shape検査、Supervisorで保存前再検査の3境界を置く。
秘匿違反が疑われたpayloadを「隔離rawログ」として保管しない。内容を捨て、固定codeだけ残す。
入力ログやL01提供証拠は開発入力であり、製品の自動採取payloadとは別。opaque correlationが秘匿情報を含まないことはConsumerの信頼前提。

## 5. 機能probeとDOM比較

既存構造scriptはtag/role/状態だけで、ChatGPT固有Locatorとの対応を返さない。このままでは「composerが壊れた」と断定できない。
L06/L08で、v0.2.1のソース上の固定matcherを**読取専用で限定再実装するprobe catalog**を追加する。任意Plugin SDKは作らない。
ソースにはcomposerの`#prompt-textarea`やcontenteditable、送信buttonのdata-testid、生成stop/streamingの候補がある。これは現行実Webの有効性を確認した事実ではない。[ソース抜粋](../evidence/source_excerpts.md)を参照。

最初のcatalogは`composer.input`（必須）、`composer.send`（state依存）、`generation.stop`（GENERATINGのみ必須）、`attachment.input`（観測できる状態のみ）、`response.container`（RESPONSE_READYのみ）を対象とする。
結合候補は重複DOM nodeをインメモリでdedupし、複数は曖昧として残す。lookupがbounded上限で終わったならmatch=0を欠落確定としない。
`attachment.input`がhiddenなのは異常と限らない。送信buttonはIDLE_EMPTYでdisabledを許し、入力欄を書き換えて有効化を試さない。生成中にstopが出る状態を試験のために作る操作もしない。
本文を読まない静的probeだけでは、完了した応答の同一性/正しいZIP取得/論理的な業務成功を証明できない。対応できない機能はNOT_EVALUATED/UNKNOWNを明記し、別のWeb操作試験へ引き継ぐ。

## 6. Baselineの採用契約

初回はBaseline Candidateのみ作成し、比較結果はUNKNOWN / BASELINE_NOT_APPROVED。最初の観測を自動的に正解にしない。
承認対象はsource Bundle Manifest hash、normalized profile hash、probe catalog hash、context recipe ID、製品/ブラウザ適合profile、local policy hashを一体で固定する。
context recipeはIDLE_EMPTY/IDLE_INPUT_PRESENT/GENERATING/RESPONSE_READY。Consumerの自己申告だけでなく、許可probeとの矛盾がないことを必要とする。判別不能ならUNKNOWN。
locale/viewportの分類は信頼された設定値を使用し、titleや会話本文から推定しない。異なるrecipe/catalog/正規化版の比較でPASSを出さない。

Humanが対象hashを明示承認後、Supervisorの別のローカル手続がapproved pointerを切り替える。approver/UTC/old/new hashを記録し、Candidate内容が承認前から変わっていたら拒否。
承認記録テンプレートの生成を実承認としない。本L02ではBaseline本体/approval/pointerを生成しない。

## 7. 判定優先順位

機械規則は[report policy](../contracts/report_policy.json)。優先順位を **UNKNOWN > BREAKING > REVIEW > PASS** とする。

1. Bundle不整合、policy拒否、PARTIAL、Binding/lease/文書/必要coverage不明、障害incidentのdocument関係不明（明示CURRENT_OBSERVATIONは除外）、Baseline未承認・版/context不一致、必要probe未完了ならUNKNOWN。
2. 1がなく、完全に観測された必須機能の契約違反ならBREAKING。例: IDLE_EMPTYで必須composerが0件。
3. 1/2がなく、利用可能だが任意機能差分・構造差分・サポート版変更など調査すべき差分があればREVIEW。
4. 適用される全required項目が判定され、許容範囲内、未判定項目がなく、重要差分もない場合のみPASS。

UNKNOWN時も確認できた違反候補はfindingsに残すが、根本原因確定や全面互換性としない。部分的にBREAKING候補があってもoverallはUNKNOWNが優先する。
語義はSTATIC_UI_CONTRACT_COMPATIBILITY。HTML全体hash差分をBREAKINGにしない。bboxの小変化/動的node数は必要featureと切り離して扱う。
比較hashから除外するもの: capture_id、時刻、Target/Browser ID、sequence、発生ごとのbbox座標。exact tag/role親関係はlocal feature構造の参考差分として保持する。
生成中DOMの揺らぎは全面一致を要求せず、recipeの機能条件を判定する。正規化で隠す項目をpolicy hashで固定する。

reportにはscope、assessment_status、reason_codes、各findingのobserved/expected/baseline参照、source file＋JSON pointer、coverage参照、未実施の機能検証、推奨アクションを含める。仮説とconfidenceは`hypothesis_code=NOT_EVALUATED / confidence=NOT_ESTIMATED`という固定値で未評価を表し、根拠のない原因確率は生成しない。
AIによる任意の原因分析は後段の人/別処理。L08の判定器は固定規則で、selector自動修正・本番反映を行わない。DOM由来文字列を指示として実行しない。

### 7.1 Schema＋必須意味検証の2段階（F-04）

正本は[Report Schema](../contracts/report_v1.schema.json)、[report policy](../contracts/report_policy.json)、[設計oracle](../tests/fix001_model.py)。Schema適合だけでReportを受理しない。

```text
strict Report JSON → Report Schema
  → 元Bundle / Baselineの全file strict JSON・閉Schemaを検証
  → Manifest・COMMIT・個別file hash・必須file集合を検証
  → Profile内部整合・相互参照の必須前段を検証
  → 必要なRESULT/STRUCTURE領域とJSON pointerを解決
  → policy・適用recipeから期待値と個別判定を計算
  → 不明条件 / 必須違反 / 差分から全体判定を導出
  → 宣言されたReport全項目と導出結果を比較
```

Schemaだけでなく最後までの意味検証に成功してからReportのManifest/COMMITを作成する。受信者も同じ2段階で検証する。由来不明の`baseline_approved=true`、`all_required_ok=true`、`significant_drift=false`等を入場証拠にしない。
参照先欠如・hash不一致・required probe欠如ではUNKNOWNを生成できるが、矛盾するPASSの入力は拒否する。入力されたPASSを黙ってUNKNOWNへ修理するのではなく、証拠から別の正しいReportを生成する。

#### 入力の型・位置

RESULT.profileには以下を正式な追加領域として置く。収集済みの保存物から読み、呼出側引数で上書きしない。

| 項目 | この評価で必要な値 |
|---|---|
| `report_context` | recipe / context_verified / catalog_sha256 / normalization_sha256 / policy_sha256 / locale_class / viewport_class / browser_profile |
| `identity_checks` | before / after / current / keeper / lease の検査結果。全てtrueが必要 |
| `coverage_complete` / `history_coverage.complete` | 全範囲・必要履歴の確認。欠如やfalseはUNKNOWN |
| `policy_allowed` / `sanitization.passed` | 取得許可・秘匿検査結果。Schemaだけで利用権限を証明しない |
| `stages` | S0/S1/S2/S3各オブジェクトのstatus/reason_code/duration_ms。未完了・欠如ならUNKNOWN |
| `observation_relation` | 2章のdocument関係。CURRENT_OBSERVATIONはTRIGGER.kind=MANUALの場合だけ |

stageはstatus=COMPLETE・reason_code=null・非負整数duration_msを正常完了の形状とする。NOT_RUNはduration_ms=0と固定reason、S0/S3は確定Bundleでは必須COMPLETE。設計oracleのfixtureもこの形状を使用するが、実機採取記録ではない。製品が追加の詳細時刻を別領域へ保存する場合も、このstatusとの矛盾を許さない。
STRUCTURE.profile.probes[]はfeature_id、match_count、visible_count、enabled_count、probe_completeを持つ。countは0〜2000の整数（bool不可）、visible/enabledはmatch以下。重複feature、未知feature、不正shape、必要field不足はUNKNOWN根拠にする。
STRUCTURE.profile.feature_shapesはfeature_idごとの正規化した要素列で、tag/role/親index/open-shadow境界のみ。tagはdiv/span/input/textarea/button/form/section/article、roleはnull/textbox/button/form/region/group、親indexは同じ列内の前方要素かnullに限定する。これはReport用の限定射影でありCollector全体のallowlistを拡張しない。表現できない形状を勝手に補正せず未評価にする。

Baseline承認は信頼されたローカル承認処理が読み出した記録を使用する。対象Baseline Manifest hashとrecipe/catalog/正規化/policy/locale/viewport/browser profileを束縛する。hash不一致、承認記録欠如、Baseline自体の整合性/必要機能の欠落は`BASELINE_NOT_APPROVED`。人による承認はこの静的モデルでは認証できず、製品側の責務である。tests内のapproved=Trueは**Mock fixtureだけ**であり実承認ではない。

#### 個別判定・全体判定

`report_policy.json/probe_contracts`が唯一のrecipe期待値表。composer.inputは全recipeで必須、composer.sendはIDLE_EMPTY/IDLE_INPUT_PRESENT、generation.stopはGENERATING、response.containerはRESPONSE_READYで必須。attachment.inputは対象recipeで任意・hidden可。未適用はNOT_APPLICABLEを出し、必須項目を呼出側のrequired=falseで外せない。
期待値はmatch_min/match_max（nullは上限なし）/visible_min/enabled_min。観測値はcount3種＋probe_complete。適用するprobeが欠如・未完了なら任意項目も含めUNKNOWN。全前提が成立して必須違反があるとBREAKING、必須違反なしで任意probe count差分または安全なfeature_shapesの差分があるとREVIEW、全て許容範囲で差分なしならPASS。動的ID/時刻/座標などの除外項目はnormalization_contractのhashで固定する。
UNKNOWN時も観測済みの必須違反findingを残す。全体のreason_codesは未知条件の定義順、次にREQUIRED_FUNCTION_VIOLATED、SIGNIFICANT_DRIFTの順で重複なく導出する。PASSの理由は空配列。先行版本の「未承認理由を残したPASS」のfixtureは負例へ変更する。
findingsにはfeature_id/required/verdict/reason_code、observed、expected、evidence_pointer、baseline_pointer、coverage_pointerが必要。欠損証拠や未適用の場合のnullを型定義する。pointerは実際に解決したprobeの配列indexと一致させる。1feature1finding、必要なfindingの省略/重複や、countを書き換えたPASSを拒否する。
not_provenで送信成功・応答内容・因果同一性・ZIP取得・WORK成功を保証外と明示。recommended_actionは固定分類のみ、automatic_action=NONE。生成Reportが元BundleやBaselineを更新しない。

#### 検証の限界と版

[設計モデル](../tests/fix001_model.py)は実際のhash付き**合成bytes**から上記を計算するが、CollectorやWindows保存・ACL・同一性観測の実装はない。hashは保存内容の完全性を検査するだけで、書き手の正しさや人の承認を暗号学的に証明しない。L08はこのモデルのdict引数をそのまま外部の自己申告で置き換えず、検証済みのBundle/承認境界へ接続する。
Receipt/Reportのv1は未採用Candidateのdraft契約である。FIX001/FIX002では必須項目を追加したが、FIX003は既存field間の意味検証を強化し、新しい必須fieldを増やさない。旧L02の緩いSchemaや旧前段へのfallbackは設けない。同じschema名でも設計Gateは対象FIX003のfile hashを固定する。既存汎用APIのenum/署名とL01 Schemaは変更しない。

### 7.2 FIX002: 元Bundleの完全な型検証と内部整合を必須前段にする

正本は[Bundle入力契約](../contracts/bundle_input_contract.json)、[全file Schema](../contracts/bundle_payload_v1.schema.json)、[入力前段oracle](../tests/bundle_input_model.py)。L06の保存前と読戻し、L08のReport生成時・受信時で同じ規則を適用する。`prevalidated=true`などの呼出側boolで迂回しない。前段に通らなければpayloadを判定器へ返さず、固定codeを持つ`BundleInputError`とする。`derive_report`はそれを`UNKNOWN / INTEGRITY_INVALID`へ閉じる。未知fieldや不正型の入った元データを削除・補完・修理して受理しない。受信する宣言PASSの修理もせず、`validate_report_bytes`が拒否する。

**全fileの型:** TRIGGER/EVENTS/RESULT/MANIFEST/COMMITを必須とし、STRUCTUREだけ任意。履歴0件も型付き`EVENTS.events=[]`を必ず置く。Manifest/COMMITを含め全objectは未知field拒否、ただしopaque correlation mapは上限と文字型で制限する。RESULTの既存`started_utc/finished_utc/selected_scope/history_dropped_count/target_id`を省略しない。`profile`の全必須field、history行の`received_utc/event/profile(Envelope)`、STRUCTUREの従来node/coverage/binding/command_auditを閉Schemaで型定義する。元generic v0.2.1のprofileなしBundleをこのReportの正常入力へ自動変換しない。既存generic APIの実装やenumを変えるのではなく、未採用ChatGPT Profileの適格入力を厳密化する。

**型と上限:** 正常・未実行・不明を型で区別する。boolをintegerとして扱わず、整数tokenが必要なfieldでは`1.0`も拒否。NaN/Infinity/重複key、不正UTF-8、最大深さ48超過、file集合/サイズ/bytes/hash不一致は固定エラー。Bundle10MiB、TRIGGER32KiB、EVENTS1MiB/1000件は継承する。Schemaだけでなく必須意味検証までが契約。Windowsのpath/reparse point/ACL確認は実装側に残り、このin-memory oracleで実施したとはしない。

**相互整合:** Manifest.capture_idとRESULT.profile.capture_id、TRIGGERとtrigger_envelope.event、初回cause ID、Binding原文/hash/typed record/target ID、世代、history行のeventとEnvelope、同じproducer/Bindingのhistory、重複event ID/producer sequence、保持件数とloss下限、catalog hash、型付きdocument markerを照合する。source_cutoff_seqはSupervisor journal sequenceでありproducer_seqとの大小比較はしない。UTC並びからイベント順序や単調leaseを推測しない。Binding TTLの値は4時間で固定し、ライブlease/keeperの正しさは後続実装試験で確認する。

**COMPLETEの矛盾禁止:** capture failure_codeは`null`または互換表記`NONE`のみ。worker_termination=NORMAL、S0〜S3完備、取得許可・秘匿・同一性・coverage/history成立、STRUCTURE存在、非truncatedが必要。これはTRIGGER.failure_codeとは別で、元Web操作の失敗後に採取だけCOMPLETEとなることは妨げない。REJECTEDはPOLICY_DENIED・未起動・構造なし。PARTIALに未確認Worker/失効/秘匿異常の構造を残さない。正常Workerと検証済み同一性による上限切詰めまたは履歴不足だけ、限定構造付きPARTIALを認める。

**失敗後の出力:** 不正型は固定分類`BUNDLE_BYTES_INVALID / BUNDLE_JSON_INVALID / BUNDLE_SCHEMA_INVALID / BUNDLE_HASH_INVALID / PROFILE_INCONSISTENT`で処理する。内容や例外文はReportへ出さない。生成UNKNOWNは正しいReport Schemaと意味検証を通すが、無効BundleをBaseline適格にしない。Baseline自身も同じ前段を通り、document relation不明のBaselineは承認bool/hashがあっても不適格。UNKNOWN優先、typed pointer、4recipeの正例は維持する。

**保存boolと信頼境界:** `COMPLETE`や`sanitization.passed=true`はハッシュが一致しただけで正当化しない。型、failure/worker/stage/構造・参照との矛盾を先に排除する。一方、同一ユーザーが全証拠を首尾一貫して捏造したことや、生存・利用権限・実fsync・実観測の正しさをSchema/hashだけで証明する設計にはしない。L06/L07の信頼された観測・L08のHuman承認読出し境界が別途必要。

**fixture変更:** FIX001と前回Assessmentの正常fixtureは省略されていた必須項目を持つ完全な入力へ改訂した。その後で元反例を変異させ、毎回Manifest/COMMITを再計算する。`significant_drift`という未定義入力fieldは今版で明示拒否し、元チェックIDを残してUNKNOWNを期待する負例へ強化した。型不正を良例に戻すfallbackはない。

### 7.3 FIX003: 履歴とstageの意味検証

FIX002に追加した同じ必須前段へ、[状態設計5.5のstream/counter/window規則](02_Diagnostics_State_And_Triggers.md)を接続する。RESULT集計の自己整合だけで終わらせず、triggerと保持history各Envelopeのloss下限・exactness、保持同stream内の通常event sequence gapを照合する。通常eventとheartbeatは同じ基本streamの通常event損失counterを運び、domainはsequence順の比較可能性だけを分ける。集計下限は同じ欠落を複数回加算せず、共通累積counterの最大値と保持通常event内部gap数の最大以上を要する。個別exact宣言だけでは集計総数の確証にならず、完全な計数根拠がない生成側はexact=falseを維持する。既知loss、不明loss、sampled、必要履歴切詰めを伴うhistoryをCOMPLETE/PASSにしない。source/Baselineの両方で不整合を`PROFILE_INCONSISTENT`として拒否し、固定理由の別UNKNOWN Reportを生成する。Baselineに付いたMock又はHumanの承認記録は、内部整合拒否を免除しない。

**構造保持の許容表:** 以下はS0/S3のCOMPLETE、元Binding/時刻/参照/型/byte/hash等の前段条件を満たしたBundleに追加する条件である。構造を保持する各行ではworker_termination=NORMAL、policy_allowed=true、sanitization.passed=true、全identity_checks=true、対応Binding/documentの検証も必須。`STRUCTUREあり`はファイルの存在を指し、nodes=[]で不適合なstageを回避しない。

| 最終capture状態 | S1/S2の各stage | 構造・履歴の追加条件 | STRUCTURE | Reportの範囲 |
|---|---|---|---|---|
| COMPLETE | 両方COMPLETE / reason=null | truncated=false、coverage_complete=true、history.complete=true、capture failure=null/NONE | 必須 | 他の全比較条件も成立した場合のみPASS等を導出 |
| PARTIAL：bounded truncation | 各々COMPLETE / null又はPARTIAL / TRUNCATED | truncated=true、coverage_complete=false。PARTIAL stageを含む場合もこの実切詰め根拠が必要 | 検証済み範囲を保持可 | UNKNOWN |
| PARTIAL：履歴だけ不足 | 両方COMPLETE / reason=null | history.complete=false、loss/gap/unknown/sampledの集計が整合。DOMのcoverageは実際の採取に従う | 検証済み範囲を保持可 | UNKNOWN |
| PARTIAL：stage失敗/未実行 | どちらかFAILED又はNOT_RUN | failure_codeをTRUNCATEDに変えても保持不可。各status/reason/durationの型規則は別途必須 | 不可 | UNKNOWN |
| PARTIAL：Worker/identity/policy/sanitization不適合 | stageの自己申告COMPLETEでは救済しない | 正常Worker・許可・同一性等の保持前提を満たさない | 不可 | UNKNOWN |
| REJECTED：採取入場policy拒否 | S1/S2ともNOT_RUN、固定reason、duration=0 | POLICY_DENIED、policy_allowed=false、worker=NOT_STARTED、coverage_complete=false | 不可 | UNKNOWN |

構造付きPARTIALには、実際の`STRUCTURE.truncated=true`又は`history_coverage.complete=false`のどちらかが必要であり、`coverage_complete=false`だけでは許可しない。capture failureはnull/NONE/TRUNCATED/HISTORY_INCOMPLETEの許容集合内とし、TRUNCATEDなら実truncated、HISTORY_INCOMPLETEなら履歴不足の対応を要する。stageがPARTIALならreasonはTRUNCATEDだけを許し、その実切詰め条件がないもの、FAILED/NOT_RUNの構造保持、COMPLETE stageの非null reasonを拒否する。最終failure_codeだけではstage結果の反証を消せない。構造なしPARTIALは従来の固定failure・型・S0/S3等の条件で保存でき、REJECTEDを後発失敗のすべてに遡って適用しない。

**保存から受信までの同じ条件:** L06は不適合な構造付きBundleをCOMMITしない。保存物の読戻し、L08のReport生成・受信は保存側の成功を前提にせず同じ検証を行う。各経路で前段拒否ならpayloadを返さず、`derive_report`はUNKNOWNを生成し、`validate_report_bytes`は同じ入力からの宣言PASSを拒否する。ReportがUNKNOWNになるだけでは、構造付き不適合Bundleの受理を正当化しない。

[FLT-63〜74](../tests/02_Fault_Injection_Cases.md#fix003追加試験)は、前回の5誤PASSとS1/S2矛盾2件、Baseline側のgap、正しい履歴不足・bounded PARTIAL・stream/window境界を製品受入へ接続する。前回23観点は[Execution replay](../tests/replay_assessment_fix003.py)に保持し、[追加自己検証](../tests/fix003_checks.py)と[変更記録](../rework/fix003/FIX003_CHANGE_RECORD.md)へ追跡する。これらは同じTicketの設計修正と自己検証であり、F-04の独立閉鎖を宣言しない。

## 8. 保存期限・ACL・容量

値は[limits](../contracts/diagnostic_profile_limits.json)に固定した設計Candidate。Bundle＋reportは7日、250captures、合計1GiBの先着上限。
active incident/S0/current Binding/現行と直前のapproved Baselineを自動削除しない。期限超過済みterminalから古い順に消し、削除理由を安全な監査recordへ残す。保護対象だけで満杯なら新規入場拒否。
spool/journalはactive要求と120秒historyおよび期限中dedup ledgerを保護してcompactする。上限64MiBを超えて黙って上書きしない。
Baseline領域は別枠64MiB、Candidate7日。activeと直前承認版はHumanによる廃止まで保持し、容量不足では新Candidate作成を拒否する。
Consumer側のEvidence参照はこのretentionを理解し、長期保持が必要なら許可されたコピーを自分の保存領域で管理する。CDPDがSHIRABE work_runsを直接改変しない。

保存rootは専用ローカルNTFS、同期ソフト/ネットワーク共有/ブラウザprofile内は禁止。ACLは実行ユーザー＋SYSTEMを基本にし、Administratorsの特権を防げるとは主張しない。
path/親dirのreparse point、既存junction、別rootへの逸脱を検査し、未確認なら保存を拒否する。Pythonのmode=0700だけでWindows ACLが確立したと扱わない。
raw情報の一時保存/スクリーンショット/HAR/MHTML/自動外部uploadは追加しない。ZIPの外部受渡しはHumanが確認して行う。

## 9. 後工程・レビュー論点

L05が永続性/ACK、L06が構造/commit/秘匿、L07が独立監視、L08がBaseline/レポート/任意Scheduler、L09がEvidence参照を実装する。
Gate前に確認する設計値: strict開始ACK（2秒）、heartbeat2/10秒、Worker10秒＋kill確認2秒、7日retention、UNKNOWN優先、probe静的範囲の限界。
未解決の製品検証は[注入試験](../tests/02_Fault_Injection_Cases.md)へ。設計の静的自己検証でWindows/実ChatGPTへの適合や権限を証明しない。
