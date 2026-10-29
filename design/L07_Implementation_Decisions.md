# CDPD-L07 実装判断

Role: EXECUTION / Authority: Candidate only

本書は新Candidate `CDPD-L07_CANDIDATE_v0.3.0_IMPL001` の実装判断を記録する。独立Assessment、管理COMPLETE反映、Gate承認、Stable昇格ではない。元L07の5手順、AC01〜04、WD-01〜05は `inputs/state004_records/tickets/CDPD-L07.md` の原bytesを規範として保持する。

## DD-L07-01 — 入力と所有範囲

COMPACT001は閲覧・配送用であり、現行実装や正式source gateの代用品にしない。完全版STATE004、外添receipt、元15外部原本を別領域へ取得し、外添receipt、ZIP SHA-256/bytes、安全かつ一意なmember、full manifestのexact coverageと全digestを検証した。未変更の `verify_state004.py` をfull STATE004に対して実行し、191,164 PASS / 0 FAIL / 0 BLOCKEDを得た後に製品コードを使用した。

製品基点はexact L06 FIX002である。STATE004 rootの歴史L03やCOMPACT snapshotから製品を作らない。既存L03/L04/L05/L06 code、tests、契約は変更せず、L07を追加moduleとして接続する。`SOURCE_LOCK.json` はfull STATE004の2原本＋元15原本の計17個を必須外部入力とする。欠落BLOCKED、異なるbytesはFAIL。保存audit・snapshot・内包copy・再構成ZIPの代用禁止を保持する。

## DD-L07-02 — 三つの独立した処理

| 処理 | 責務 | 長いI/Oとの関係 |
| --- | --- | --- |
| Supervisorの監視loop | 自身のmonotonic、検証済みイベント受取、Heartbeat期限、登録process handleの観測、局所的な操作停止 | journal fsyncやDOM呼出しを行わない |
| Journal writer / capture finalizer | accepted L05の唯一writer、永続claim、L07 S0、accepted L06 S0・保存・COMMIT | 別thread。停止中もWorker期限監督を所有しない |
| Owned Workerの監督 | one-shot processの通算deadline、専用Job/groupの終了と確認 | permit通知、pipe、永続化が止まっても別の監督threadが継続 |

SupervisorはConsumerより先に独立コンソールで起動する。Windowsで既存Job所属が観測された場合、独立性を推定せず起動を拒否する。OS排他lockでrootとprofileの二重所有を拒否し、lockファイルの有無だけで生存を判断しない。起動準備がtimeoutしてもwriterが生存中なら所有lockを解放しない。

accepted L05のquota lock取得失敗は `CURRENT_NOT_DURABLE` として返り、短いpublisher競合でも同じcodeとなる。L07 writerはfaultedでないwriterの同codeに限って、同一metadata eventの受理を最大2秒の範囲で保留する。ACKを捏造せず、期限を越えればSTORAGE_UNAVAILABLEで停止する。これはevent-idでdedupeされるmetadataの再確認であり、Worker起動やWeb操作の再試行ではない。実fsyncでwriterがfaultedになった場合は直ちに失敗する。

## DD-L07-03 — Heartbeatの意味と時計

Consumerの標準更新周期は2秒。受信側が初めて受理した新seqの自身のmonotonicを `t_rx` とし、期限は `t_rx + 10 seconds`。監視pollは最大0.25秒。同seq、古いseq、scope不一致、producer UTC、未来UTC、mtimeで期限を延ばさない。

run_idは起動・登録時の明示入力である。correlationにもrun_idがある場合は一致必須で、producer instanceから暗黙生成しない。operation、Binding、generation、Supervisor instance、producer、producer instanceを完全一致で束縛し、Binding raw hashも登録値と照合する。Heartbeat seqは診断イベントとは別domainとして扱う。

監視loopが検証済みqueueから受け取った時刻を受信時刻として使う。I/O threadの時刻と監視threadのpoll時刻を混ぜて、正常なqueue遅延を時計逆行にしない。期限を過ぎたfresh Heartbeatでも期限超過を消さない。Heartbeat復帰は自動的な操作再開を意味しない。欠落だけからConsumer死亡を断定しない。

## DD-L07-04 — 即時の局所停止と共有通知

Heartbeat欠落や認証済み失敗を観測すると、exact L04 guardの局所modeを直ちにDIAGNOSTIC_ONLYへ単調に制限する。既存RECONCILIATION_REQUIREDとterminal first-causeを保持する。共有authorityのcallbackは監視loopで待たず、別threadからexact Binding/generationへ1回通知する。制限の確認は共有snapshotの同一scope読戻しを必要とする。

0.2秒の期限内に適用確認できなければUNCONFIRMEDを固定する。遅着callbackによるCONFIRMEDへの昇格や自動再試行は行わない。すでに異なる世代のauthorityへ通知を振り替えない。共有authorityが実際には変わっていない場合に、他peerまで停止したと主張しない。詳細は `L07_Supervisor_Authority.md`。

COORDINATOR_START、各Worker permit、最終COMMIT前のpermitで、停止要求、終了未確認block、exact live contextを再確認する。S0のfsync中に停止指示が届いても、後から戻った処理が新Workerを開始しない。terminal noticeは許可ではなくEvidenceなので、既存L06のfirst-cause・UNKNOWN receipt経路を保持する。

## DD-L07-05 — S0と元Consumer履歴の出所

accepted L06 Bundleはtrigger producerの単一streamに束縛された閉schemaである。Supervisorが生成した `CONSUMER_EXITED` 等のeventをConsumerになりすまして発行したり、別producerの履歴を同じ母集団として混ぜたりしない。

L07は、Worker起動前に追加の `operations/<operation-key>/L07_S0.json` とそのreceiptをfsyncする。ここにはaccepted L05 `read_history`で読んだ元Consumerのexact stream、元envelope、source cutoff、loss lower bound、sticky unknown、run/operation、exact job hashを保存する。その後accepted L06が従来どおり自身のS0とattemptを永続化し、Workerを起動する。

Supervisor由来eventのloss counterへConsumerの累積値を転記しない。Supervisor streamは保守的にunknownとし、元Consumerの値はL07 S0に別の出所として残す。`CONFIRMED`・`POSSIBLE`・`UNKNOWN`等のdispatch観測は同scopeに限って単調に集約し、既知の送信をNOT_DISPATCHEDへ戻さない。原イベントbytes自体は変更しない。

L07 S0はL06 Bundleのhistory coverageを置き換えるものではない。両方の出所とhashをOUTCOMEで参照する。S0の保存失敗はWorker開始前に失敗させ、存在しないBundle pathを作らない。

## DD-L07-06 — 一回限りの採取と再起動

永続dedupe keyはcanonical JSONの `[run_id, operation_id]` に対するSHA-256である。producerやBinding/generationを変更して同operationを再採取する抜け道を作らない。

`CLAIM.json`をexclusive createし、fileとdirectoryをfsyncした後だけ次へ進む。CLAIMはimmutableなWorkerRequestとhashを含む。さらにL07 S0、L06 S0、L06 attemptをWorkerより前に保存する。COMMIT済みBundleは不変で、遅着異常は別のmetadataとして記録する。

再起動時にlive keeperは復元しない。旧sessionに記録されたBindingを、保存rawだけからlive contextとして再登録できない。未確定claimは新Workerを禁止し、既存S0がある場合だけ旧jobを用いてaccepted L06のmetadata-only recoveryへ渡す。S0がなければclaim metadataの不足を明示し、元履歴や成功Bundleを再構成しない。

OUTCOMEの存在だけで完了とは見なさず、内容をstrictに読む。終了未確認・worker blockはOUTCOMEからも再構築し、global block markerの書込み失敗で制限を消さない。自動clear・自動replay・自動再送は実装しない。

process metadata、spawn count、固定PID、termination、boolean flagsを検証し、欠落や不正な型は起動拒否とする。生成の有無が不明、termination factsが未確認、active process数が0と確認できない等の矛盾は、上位worker_blockedがfalseでも制限を復元する。

## DD-L07-07 — OS境界のdeadline

`D = t_spawn_attempt + timeout_seconds`。timeoutは0.1〜20秒、標準10秒。permit待ち、S1、S2、postcheckごとにリセットしない。期限超過時には保存済みowned handleからWorkerの専用Job/groupだけに終了要求を出し、終了確認待ちは最大2秒とする。Chrome、Consumer、他Pythonを名前やPID検索でkillしない。

Windowsは作成時Job List割当＋CREATE_SUSPENDED、membership確認、deadline/cancel確認、resumeの順とする。これによりsuspended作成と後付けJob割当の間の孤立窓を避ける。JobはSupervisorだけが持ち、KILL_ON_JOB_CLOSE、breakawayなし、限定handle継承を使う。正常leader終了でもtree全体の終了を確認する。詳細は `L07_Process_References.md`。

終了未確認はWORKER_TERMINATION_UNCONFIRMEDとして固定し、新Workerを禁止する。OSのCreateProcessやdisk syscallそのものをPythonから強制cancelできるとは主張しない。遅着した作成結果は期限/取消を再検査してresumeしない。要求送信、permit callback、terminal永続化の停止は独立監督を止めない。

保存可能な場合のS3確定5秒は目標であり、OS/storageの全面停止も含む総遅延保証ではない。Linux process groupはsynthetic試験用で、Windows Jobのnative受入や、setsidで脱出する非協調的子孫の隔離を意味しない。

Linuxでは別interpreterのexec wrapperで `PR_SET_PDEATHSIG(SIGKILL)` とexpected parent PIDを確認してからexact argvをexecする。これによりSupervisor強制終了時のWorker leader停止を試験できる。parent-death signalは生成threadに束縛され、forkした子孫には継承されないため、全descendantの終了保証は主張しない。multi-threaded Supervisorからの `preexec_fn` は使用しない。

## DD-L07-08 — 起動と信頼境界

`scripts/start_watchdog.ps1` は独立コンソールでSupervisorを前景起動する。専用のlocal NTFS、単一profile、同一ユーザーACL、同期folder禁止、poll上限を検査する。ACLを自動変更せず、ConsumerやChromeを起動しない。

明示されたtrusted bootstrapだけが同一process内のactual L04 BindingGuardを登録できる。JSON、saved record、Heartbeat、EVENT_DURABILITY ACKをlive permitに読み替える入口はない。bootstrapなしの起動はjournalとmetadataの監視であり、DOM許可を作らない。現SHIRABEとの接続・新たなpreflight統合はL09/L11の別作業である。

Consumer/Chromeの観測対象はexact `RegisteredProcess` とcreation identityを持つ固定handleに限定し、`is_alive()`だけを模した任意objectをCONFIRMED_PROCESS_EXITの証拠には使わない。登録時にclosed/invalid handleを拒否し、実行中の観測失敗は死亡断定にせずkeeper lossとして扱う。

## 維持する評価・管理境界

- 原L05の5 CLOSED / 15 PASSとDD-L05-01 EVENT_DURABILITY only。
- ADR001 sticky unknown、loss lower bound、Report UNKNOWN。
- N01/N02は元LOW/OPEN、技術的支持は元IMPL001の固定範囲のみ。
- 元の7 real-browser test identityはNOT_RUNのまま。
- Windows native Job、ACL/NTFS/CFAPI、PowerShell、Winsock、電源断は未実行。
- retention consumer、trusted idle-proof、L08/L09/L11、Gate/Stable/Releaseは別のEvidenceと権限を必要とする。
- 本CandidateのPASSはExecutionの試験結果であり、L07管理COMPLETEや独立AssessmentのPASSを意味しない。
