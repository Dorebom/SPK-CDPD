---
id: CDPD-L02-WORKER
title: "Capture Worker・Watchdog・障害継続性"
project: CDPD-CHATGPT-ONLY
ticket: CDPD-L02
role: EXECUTION
status: CANDIDATE
version: 0.3.0-l02-candidate.4-fix003
updated: '2026-10-18'
---

# Capture Worker・Watchdog・障害継続性


## 1. 構成と責務

本書は設計Candidate。プロセスやジョブの生成・kill・CDP接続・Task Scheduler登録は本Ticketでは実施しない。

| プロセス | 許可すること | 許可しないこと |
|---|---|---|
| Consumer（SHIRABE等1つ） | Web操作APIを1回呼び、opaque IDs・状態eventを出す | 旧compat、診断結果から送信成功/失敗を補正する |
| Supervisor（独立1つ） | journal/state唯一writer、clock keeper、heartbeat、S0、worker起動と最終commit | UIクリック/再送/reload、ブラウザ再起動、Bindingの暗黙発行 |
| Capture Worker（最大1つ） | immutable requestを検証し、固定scriptで構造を読み、一時結果を返す | current state/正規Baseline変更、任意JS/CDP、Browser.close、COMMIT公開 |

SupervisorはConsumerより先に独立コンソール/同一ユーザーの明示起動で生存する。Consumerのkill tree/jobに含まれた構成は不適合。
製品用常駐サービスやネットワークdaemonは追加しない。Workerは既存Chromeへの直接CDPクライアントを再利用し、ChromeやPlaywright driverを子として起動しない。
全て同一Windowsユーザーの信頼域。IPCを認証境界と称さず、ローカルACLで別ユーザーのアクセスを制限する。

## 2. 最小の保存領域と単一writer

```text
<local-state-dir>/                   # ローカルNTFS、同期/共有フォルダ不可
  inbox/ receipt/ journal/           # event受渡し。producerはinboxだけ生成
  binding/records/<binding-id>.json  # L01原recordをbyte不変で保持
  binding/current.json              # Supervisorだけ更新、別世代で上書き
  heartbeat/consumer.json            # sequenceを伴う最新値、liveness専用
  incidents/<capture-id>/S0.json     # Supervisorが先に永続化
  incidents/<capture-id>/attempt.json
  staging/<capture-id>/              # 検証済み構造でもまだ非公開
  bundles/C-<uuidhex>/               # v0.2.1の4JSON+MANIFEST+COMMIT
  reports/ baseline-candidates/ baselines/  # Bundle外、別Manifest
```

`binding/current.json`はschema定数、supervisor_instance_id、control_generation、binding_id、record_sha256、L01 state、revoked、overlay、owner instance、active operation ref、updated_utcを持つ。
状態変更は原record改変でなく、単一writerがcurrentの世代を1ずつ進める。未完了・破損・世代競合・current/recordのhash不整合はDENY。
WorkerやCLIがstateファイルへactive=trueを自己宣言して復活させる経路を作らない。

mutex/排他handleをSupervisorが保持する。2つ目のSupervisorは起動拒否。残存ファイルだけから排他所有/生存を推測しない。
現在状態、操作開始ACK、capture attempt、COMMITへの参照の順をjournalに残す。原recordの置換、opaque correlationをディレクトリ名に利用することは禁止。

## 3. clock keeperとworkerへの許可

L01発行時からSupervisor自身がUTC/monotonic anchorを保持し、14400秒leaseと5秒不連続を検査する。異なるprocessのmonotonic絶対値を共有して比較しない。
Consumerは同じrecord/hash/generationを登録し、Supervisor登録ACK以前に操作を開始しない。keeper登録は新Bindingの明示Preflight中だけ。
Supervisor再起動では新supervisor_instance_idを発行し、旧Bindingの時計連続性は失われる。**ファイルが残っていても旧BindingのDOM採取は再開しない。** メタデータ復旧だけを行う。

Workerには起動時のimmutable CaptureRequest、L01 record、record hash、control generation、capture_idを渡す。加えてSupervisorが所有する**専用継承pipe**で各phaseの許可を確認する。汎用named/network RPCは不要。
Workerはfresh nonce付きPERMIT_CHECKを送信し、Supervisorはそのnonce・capture_id・binding/hash/generation・remaining_leaseを返す。Worker側のlease期限は**要求送出時の自分のmonotonic＋返された残時間**を上限に取り、応答受信時から足し直してIPC遅延だけ延命しない。permitを別要求へ再利用しない。古いファイルや同じheartbeat行を再読込してfreshにしない。
pipeはコマンド/許可/固定statusと長さ上限を持ち、Worker stdoutに本文や任意例外を出さない。1秒以内に確認できなければ停止する。

phase入口・各ブラウザ内容command直前・事後・最終commit前でlease/current generation/生存を再確認する。SupervisorとWorkerがそれぞれ自分のmonotonic期限を使い、早い方で停止する。
これはOS全体の原子的許可ではない。既に送信した読取要求を後から取消したとは扱わず、事後検査NGなら結果を捨てる。

## 4. heartbeatと停止判定

Consumerは2秒ごとに生存mailboxを更新する。seqはliveness専用、Consumer instance＋binding_id＋supervisor_instance_idに束縛する。
Supervisorは**新しいseqの受信時点の自分のmonotonic**から10秒経過でHEARTBEAT_EXPIREDを1回発行。未来のUTC値やmtimeで延命しない。
heartbeatはプロセス/記録経路の生存指標であり、ChatGPTの生成中/完了やUIスレッドの進行を証明しない。API timeoutは別の操作観測契約を使う。

期限超過はConsumer死亡と同義ではない。DIAGNOSTIC_ONLY overlayで新規mutationを禁止し、必要な同一Target診断を最大1回要求する。
その後heartbeatが戻っても自動的に操作を再開しない。未確定操作をConsumer/Humanが再調停する。
Consumer終了確認は登録したprocess handle/start identityに基づき、PID番号だけで別プロセスを同一所有者としない。
2秒/10秒/0.25秒pollは**設計値**であり性能測定値ではない。Windowsの負荷・sleep/resume試験はL11で行う。

## 5. 採取シーケンスと予算

| Stage | 内容 | 永続化/検査 |
|---|---|---|
| S0 | 初回trigger、元Binding、current世代、cutoff_seq、120秒history参照、時刻・dispatch状態 | Supervisorが先にfsync。S0を失敗後に捏造しない |
| S1 | 失敗feature周辺の許可構造と固定probe一致数。対象なしならno-targetを明示 | 一時領域。未検証の属性・本文をdiskへ出さない |
| S2 | main frame＋open shadowの許可構造、coverage、選択probe群 | 一時領域。固定同一Target、既存script相当のallowlist |
| S3 | stageごとの成否/理由/時間、loss/coverage、事前事後同一性、Worker終了、採取結果 | Supervisorが検証し4JSON内に格納してCOMMIT |

S0は最大64KiB（history本体は別journal参照/凍結1MiB）。Bundle全体はManifest/COMMIT込み10MiB。32KiB event/120秒/1000event/1MiB historyの先着上限で打ち切り理由を必ず記録する。
Node上限は`CaptureRequest.max_nodes`を**S1+S2の合計**として適用。標準1000、最大2000、S1へ最大200を予約し残りをS2へ。S1で対象がなければその分はS2へ使える。
Probe探索もvisited node/deadline上限を持つ。上限で探索が止まった場合、単にmatch=0と断定せず`probe_complete=false`とする。

既存`timeout_seconds`（0.1–20秒、標準10秒）はWorker spawnからguard/S1/S2/postcheckまでの全体期限として引き継ぐ。phaseごとに20秒へリセットしない。
Supervisorはpoll最大0.25秒で期限を監督し、超過時はWorker jobをkill、2秒まで終了確認、保存可能なら5秒目標でS3を確定する。
**10+2+5秒はOS/storage停止時も守れる総遅延保証ではない。** 硬い意味はBrowser処理が期限後に別Workerへ継続・retryされないこと。OS応答不能、kill失敗、disk I/Oハングは明示障害で、保存できたとは報告しない。

S1/S2のどちらかがFAILED又はNOT_RUN、identity/lease/URL/loader/page集合が変化、Workerが異常終了した場合は、最終事後証明のない構造を全て破棄する。STRUCTURE.jsonを空にして残すだけではなく、構造なしのS0/S3だけでPARTIALを確定する。
例外は**正常に終了し前後identityが成立したbounded truncation又は正しく記録した履歴不足**。bounded truncationではS1/S2各stageがCOMPLETE/reason=null又はPARTIAL/TRUNCATEDであり、STRUCTURE.truncated=true・coverage_complete=false・最終PARTIALを要する。S1/S2のFAILED/NOT_RUNはこの例外ではない。履歴だけが不足し構造を切り詰めていない場合、S1/S2はCOMPLETE/reason=nullのままhistory.complete=falseを記録し、HISTORY_INCOMPLETEのPARTIALとする。どちらも正しく検証した範囲だけを保存し、完全互換性判定はUNKNOWN。全条件は[Evidence契約7.3の許容表](02_Evidence_And_Report_Contract.md#73-fix003-履歴とstageの意味検証)に従う。

## 6. WindowsでのWorker停止要件

L07はWorker専用Job Objectを作成し、KILL_ON_JOB_CLOSEを設定、breakawayを許可しない。Workerをsuspendedで作成→jobへ割当→resumeの順とし、割当失敗なら実行しない。
Supervisorだけがjob handleを保持し、Workerへjobの最後のhandleを継承させない。Supervisor異常終了時も孤児Workerを残さない。
期限超過時は保存済みの所有handleでTerminateJobObjectし、終了を確認する。**Chrome/Consumer/別Pythonをjobへ含めず、process名によるtaskkillは使わない。**

Job Objectsの関連プロセス一括終了、KILL_ON_JOB_CLOSEの仕様は[EXT-L02-02/03](../evidence/technical_references.json)で確認した。実装・Windowsでの効果は未検証。
Job Objectは同一ユーザー内の悪意あるコード隔離や情報流出防止sandboxではなく、所有Workerのライフサイクル境界である。
kill後も終了未確認ならWORKER_TERMINATION_UNCONFIRMEDを記録し、次のWorkerを開始しない。Humanによる確認まで診断の新規入場を止める。ブラウザ再起動で回避しない。

Supervisorのheartbeat/kill監督loopはDOM読み出しや長い同期fsyncで塞がない。単一writer journalは別threadで直列処理可能だが、kill監督はその完了待ちをしない。thread追加はprocess責務の追加ではない。

## 7. 三系統の故障と復旧

| 故障 | 条件 | 許可される採取と最終状態 |
|---|---|---|
| Consumerだけ停止 | Supervisor keeper継続、record/current整合、lease有効、既知CDP断なし | 元Bindingで新Workerが再検証。成功ならCOMPLETEも可。WORK成功にはしない |
| Consumer停止＋既知CDP断/失効 | 失効recordまたは不明状態 | DOM不可。保存可能なS0/S3のPARTIAL |
| Chrome停止 | Target/epochを回収できない | 再起動/別タブ探索なし。PARTIALメタデータ |
| Worker停止/ハング | S0は先に永続化済み | Workerのみ停止、最終postcheckのないDOMを破棄、PARTIAL |
| Supervisor停止 | clock keeper喪失、job handle close | Workerも停止。再起動時はS0/journalのみでPARTIAL。自動capture replay不可 |
| Supervisor＋Consumer両停止 | 旧recordファイルのみ | 旧BindingでDOM取得不可。明示Preflightが次の入口 |
| disk full/ACL拒否 | S0またはfinal保存不能 | STORAGE_FAILED。bundle_pathを捏造しない。以前のS0があればその存在だけ報告 |

queueの古い要求は新Bindingへ付け替えない。attempt.jsonのspawn前記録から、crash後に未確定spawnが残っても同じcapture_idを再spawnしない。
COMMIT済みならverifyして既存参照を返す。COMMITなしは未公開のstagingであり、S0からメタデータのみのfinalを一度作れる場合だけ復旧する。未検証DOMを組み入れない。
FINALIZING中に失効/秘匿違反/Worker異常が到着した場合の正本は[状態・ラッチ契約](02_Diagnostics_State_And_Triggers.md#61-finalizingの失敗ラッチf-02)。S1/S2の構造はCOMMITまで未確定であり、失敗ラッチ後の正常通知で復活させない。COMMIT済みの証拠は不変とし、遅着異常は別メタデータで追跡する。

Workerの正常detach/socket closeと運用制御接続の異常CDP断は区別する。正常detachで常に全Bindingを失効させる実装にはしない。

## 8. CLI/SDK接続とScheduler境界

既存`CaptureRequest`本文は変更しない。ChatGPT版CLI captureは、別引数`--binding-record`と`--state-dir`を必須にし、profile supervisorへローカル要求を公開する設計。
現行v0.2.1 CLIにこれらの引数が既に存在するという意味ではない。L06/L07で実装する。レコードのみで裸collectorへ抜ける経路は配布版で禁止する。
SDKの`DiagnosticService.capture(request)`は設定済みprofile coordinatorへ委譲し、最終CaptureOutcomeを返す。同名署名は維持するが、guard/keeperの無い弱い動作は非対応。
内部`submit_capture`はCAPTURE_ACCEPTEDを返せるが、それをCaptureOutcomeに代用しない。timeout/storage errorには存在しないpathを返さない。

定期採取はMANUAL/SCHEDULED_CAPTUREとして同じqueue・同じguardへ入る。既存Binding有効＋Supervisor生存＋Consumer idleの時のみ。新規ログイン/新Binding/期限延長/ブラウザ起動は行わない。
稼働中の作業があればSKIPPED_BUSY、有効Bindingが無ければNO_ACTIVE_BINDINGのメタデータとUNKNOWNレポート。4時間leaseを無視して夜間自動で継続監視しない。
Schedulerは既定無効、登録はL08のスクリプトをHumanが実行する。L02では登録しない。
**C-03:** v1の定期監視は明示Preflightで開始した有効な運用セッション中のみ。夜間・翌日に人の再Preflightなしで継続する無人24時間監視ではない。4時間leaseを延長する新機能や、新規自動Bindingは追加しない。設計Gateではこの制約を明示的に確認する。

## FIX002の結合責務

Report用の元Bundle検証をL08だけに後付けしない。L06は[Bundle入力契約](../contracts/bundle_input_contract.json)を保存前と読戻しに適用し、全file型とCOMPLETE/worker/failure/stage/構造の内部整合が成立してから公開する。L08も同じ前段を再実行する。schema/hash/profile整合の合成検査を実Windows fsync/ACL/Job ObjectのPASSへ読み替えない。

Provider由来ACTION_FAILED/DISCONNECTEDはEVT-26。L05最小Coordinatorが当該Binding失効・new mutation停止を単一writerで先行させ、既存incidentはKNOWN_CDP_LOSSでS0/S3 metadata-only最終化する。S0未保存・storage異常では成功したBundleを捏造しない。実Worker/consumer停止の順序はL07とL11の責務であり、12Ticket/2Gate/DAGを変えずL04/L06のFake利用境界を維持する。

## FIX003の結合責務

L05は既存Envelopeのstream内累積lossと不明状態を保持し、L06はtrigger/保持history/RESULT集計とS1/S2/構造保持を[同じ必須前段](../tests/bundle_input_model.py)で保存前・読戻しに照合する。L08もReport生成・受信時に同じ前段を再実行する。counterをwindow外へ自動除外せず、1つの不適合BundleをsourceにもBaselineにも採用しない。未公開の入力を破棄した場合、元bytesを修理して成功扱いにせず固定codeで記録する。

FLT-63〜74はこの責務を後続製品のSynthetic/Mock・Windows注入へ接続する試験定義であり、現時点では全NOT_RUN。[FIX003自己検証](../evidence/fix003/README.md)や[23観点replay](../tests/replay_assessment_fix003.py)は設計のExecution証拠であり、実Worker/Job Object/fsync/ACLの成功、独立Assessment閉鎖、Human Gate承認を意味しない。
