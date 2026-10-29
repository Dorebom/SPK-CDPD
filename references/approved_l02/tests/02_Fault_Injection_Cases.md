---
id: CDPD-L02-FAULT-TESTS
title: "L02 障害注入試験定義"
project: CDPD-CHATGPT-ONLY
ticket: CDPD-L02
role: EXECUTION
status: CANDIDATE
version: 0.3.0-l02-candidate.4-fix003
updated: '2026-10-18'
---

# L02 障害注入試験定義

## 実行範囲

現行74件（FIX002までの62件＋FIX003追加12件）は後工程の製品試験仕様であり、L02では実行しない。`validate_l02_design.py`が実行するのはSchema・有限状態/判定モデル・文書・入力hashの静的自己検証である。
同じ番号の期待値をモデルで検査しても、実ブラウザ/Windows kill/永続化試験のPASSへ昇格しない。
原計画FLT-01〜FLT-06を維持し、AC01〜AC05へtraceする。試験matrixは[機械定義](fixtures/fault_injection_cases.json)、[Source根拠](../evidence/source_inspection.json)を参照する。

評価に必要な固定入力はL01 Binding原bytes/clock、L02 matrix/limits/policyのhash、候補製品source/wheel hash、OS/browser適合profile、合成DOM、注入点である。
全caseに`expected/actual/status/command/exit_code/log_ref/source_sha`を最終記録する。ここではactual/command/exit_code/log_refがnull、status=NOT_RUNである。
Humanの実サービス利用許可なしにChatGPTへ送信しない。通常はSynthetic/Mockで先行し、Windows専用項目はL11へ残す。

## FLT-01 — 操作対象不一致

**State: NOT_RUN** / 実装: CDPD-L04, CDPD-L06 / 独立評価: CDPD-L10

環境: Mock CDP＋合成UI。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: bind A後に採取対象をBへ置換する

期待結果: **PARTIAL、DOMなし、別Target commandゼロ、旧B失効。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC04, AC05。

## FLT-02 — 状態遷移Timeout

**State: NOT_RUN** / 実装: CDPD-L05, CDPD-L06 / 独立評価: CDPD-L10

環境: 合成UI。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: provider送出後にcompletion観測だけをTimeoutさせる

期待結果: **元operation_id/POSSIBLE保持、S0先行、採取1回、再送0回。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC03, AC05。

## FLT-03 — Consumer強制停止

**State: NOT_RUN** / 実装: CDPD-L05, CDPD-L07 / 独立評価: CDPD-L11

環境: Windows実機＋合成UI。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: 開始ACK後にConsumerだけを終了、Supervisor/Chromeは維持

期待結果: **keeper有効なら固定Bで診断、履歴はdurable分だけ。WORK成功にしない。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC02, AC03, AC05。

## FLT-04 — CDP/Worker障害

**State: NOT_RUN** / 実装: CDPD-L06, CDPD-L07 / 独立評価: CDPD-L11

環境: Windows実機＋合成UI。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: A:制御CDP切断、B:WorkerをS1直後に終了

期待結果: **Aは旧Binding失効・S0のみ。Bは未commit DOM破棄・PARTIAL。再spawn0。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC02, AC05。

## FLT-05 — Evidence改ざん

**State: NOT_RUN** / 実装: CDPD-L06 / 独立評価: CDPD-L10

環境: 合成ファイルfixture。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: STRUCTUREを1byte変更/COMMIT削除/余剰file追加の3変種

期待結果: **verify_bundle失敗、report UNKNOWN、原本を書き換えない。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC05。

## FLT-06 — 初回Baseline未承認

**State: NOT_RUN** / 実装: CDPD-L08 / 独立評価: CDPD-L10

環境: 合成DOM fixture。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: approved Baselineなしで最初の比較を要求

期待結果: **Candidateのみ、UNKNOWN/BASELINE_NOT_APPROVED、正規pointer不変。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC05。

## FLT-07 — memory受理直後のcrash

**State: NOT_RUN** / 実装: CDPD-L05 / 独立評価: CDPD-L10

環境: Mock writer。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: try_emit True後、ready作成前にConsumer終了

期待結果: **ACK_DURABLEなし、lost total unknown、メモリ受理を永続扱いしない。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC02, AC03, AC05。

## FLT-08 — journal後ACK前のcrash

**State: NOT_RUN** / 実装: CDPD-L05, CDPD-L07 / 独立評価: CDPD-L10

環境: Mock filesystem。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: fsync完了後、receipt公開前にSupervisor終了

期待結果: **journal行はrecover、同じevent再配送は同じreceipt、旧Binding DOMは不可。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC02, AC03, AC05。

## FLT-09 — 同一eventの重複配送

**State: NOT_RUN** / 実装: CDPD-L05 / 独立評価: CDPD-L10

環境: Mock inbox。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: 同一event_id/hash/seqのreadyを2回読む

期待結果: **journal採用1回、capture attempt最大1、元receipt再利用。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC03, AC05。

## FLT-10 — 同一event IDの内容衝突

**State: NOT_RUN** / 実装: CDPD-L05 / 独立評価: CDPD-L10

環境: Mock inbox。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: 同じIDで異なるsafe payload、別IDで同じproducer_seqも試す

期待結果: **EVENT_ID_CONFLICT、原行不変、browser commandなし。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC03, AC05。

## FLT-11 — normal queue満杯

**State: NOT_RUN** / 実装: CDPD-L05 / 独立評価: CDPD-L10

環境: Mock queue。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: 通常slot240件を占有し次のnormal/criticalを投入

期待結果: **normal拒否/欠落増加、critical予備16を使用可、FalseをACK扱いしない。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC03, AC05。

## FLT-12 — critical予備も枯渇

**State: NOT_RUN** / 実装: CDPD-L05 / 独立評価: CDPD-L10

環境: Mock queue/spool。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: memory256とdisk予備枠を使い切る

期待結果: **受理拒否、新規mutation停止、既存ACK済みS0を追出さない。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC03, AC05。

## FLT-13 — 順序逆転と遅着

**State: NOT_RUN** / 実装: CDPD-L05 / 独立評価: CDPD-L10

環境: Mock clock/inbox。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: seq1,3を先に、seq2を2秒後に投入

期待結果: **gap range記録、late arrival保持、元cutoff/Bundle不変。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC03, AC05。

## FLT-14 — 欠落counter未flush

**State: NOT_RUN** / 実装: CDPD-L05 / 独立評価: CDPD-L10

環境: Mock producer。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: queue拒否後counter永続化前に終了

期待結果: **loss lower boundだけ、loss_count_exact=false。ゼロと推定しない。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC02, AC03, AC05。

## FLT-15 — history上限

**State: NOT_RUN** / 実装: CDPD-L05, CDPD-L06 / 独立評価: CDPD-L10

環境: Mock clock。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: 120秒/1000件/1MiB各上限を越える

期待結果: **境界と切詰め理由記録、必要履歴不足はPARTIAL/UNKNOWN。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC03, AC05。

## FLT-16 — 旧Binding/別producerの履歴

**State: NOT_RUN** / 実装: CDPD-L05, CDPD-L06 / 独立評価: CDPD-L10

環境: Mock journal。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: 複数世代のeventを同じ120秒窓に入れる

期待結果: **元producer instance＋binding hash＋cutoffに一致する履歴のみ採用。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC04, AC05。

## FLT-17 — clock keeperなし

**State: NOT_RUN** / 実装: CDPD-L07 / 独立評価: CDPD-L10

環境: Mock process registry。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: Consumer消失＋Supervisor発行時anchor未保持

期待結果: **既存recordだけでは採取不可、S0/S3のPARTIAL。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC02, AC05。

## FLT-18 — Supervisor再起動

**State: NOT_RUN** / 実装: CDPD-L07 / 独立評価: CDPD-L11

環境: Windows実機＋合成UI。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: Worker採取中にSupervisor終了し明示的に再起動

期待結果: **Worker job終了、旧recordでDOM再開なし、S0のみ復旧。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC02, AC05。

## FLT-19 — lease/時計違反

**State: NOT_RUN** / 実装: CDPD-L06, CDPD-L07 / 独立評価: CDPD-L10

環境: Mock clock。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: 4時間境界またはUTC/monotonic差5秒超を注入

期待結果: **期限失効/不連続で停止、Heartbeat延長不可、構造未commitなら破棄。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC02, AC05。

## FLT-20 — Chrome再起動同port

**State: NOT_RUN** / 実装: CDPD-L03, CDPD-L06, CDPD-L07 / 独立評価: CDPD-L11

環境: Windows実機＋合成UI。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: 同じ9222でChromeを入れ替える

期待結果: **epoch mismatch、旧Bindingで再接続/代替選択不可、S0のみ。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC02, AC05。

## FLT-21 — 採取中document遷移

**State: NOT_RUN** / 実装: CDPD-L06 / 独立評価: CDPD-L10

環境: 合成UI。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: S1とS2間でloader/SPA URLを変える

期待結果: **PARTIAL、未commit構造全破棄、NAVIGATION_DURING_CAPTURE。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC04, AC05。

## FLT-22 — 採取中2枚目のpage

**State: NOT_RUN** / 実装: CDPD-L06 / 独立評価: CDPD-L10

環境: 合成UI。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: postcheck前に2枚目のpageを追加

期待結果: **Binding失効、no DOM commit、pageを自動closeしない。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC04, AC05。

## FLT-23 — Workerハング

**State: NOT_RUN** / 実装: CDPD-L07 / 独立評価: CDPD-L11

環境: Windows実機＋合成UI。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: Workerをevaluate待ちに固定する

期待結果: **spawn基準10秒期限＋pollでkill要求、job終了確認。Chrome/Consumer生存。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC02, AC05。

## FLT-24 — kill/終了確認失敗

**State: NOT_RUN** / 実装: CDPD-L07 / 独立評価: CDPD-L11

環境: Mock Windows process API。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: TerminateJobObject失敗/終了handle未signal

期待結果: **UNCONFIRMED明記、次Worker禁止、Chrome killなし。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC02, AC05。

## FLT-25 — S0/final保存不能

**State: NOT_RUN** / 実装: CDPD-L05, CDPD-L06 / 独立評価: CDPD-L10

環境: Mock filesystem。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: disk full/ACL拒否をS0前とCOMMIT前へ注入

期待結果: **STORAGE_FAILED、新bundle_pathなし、存在したS0だけ別記。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC02, AC03, AC05。

## FLT-26 — COMMIT前後crash

**State: NOT_RUN** / 実装: CDPD-L06, CDPD-L07 / 独立評価: CDPD-L10

環境: Mock filesystem。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: 各payload/Manifest/COMMIT/fsync直後でSupervisor終了

期待結果: **COMMITなしは未公開、完成分はverify後に既存参照、未検証DOM採用なし。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC02, AC05。

## FLT-27 — 秘匿canary

**State: NOT_RUN** / 実装: CDPD-L05, CDPD-L06 / 独立評価: CDPD-L10

環境: 合成UI。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: 本文/input/aria-label/URL/console/例外strに異なるcanaryを入れる

期待結果: **全disk/output/Bundleにcanaryなし、固定codeのみ。correlationは安全なfixture ID。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC04, AC05。

## FLT-28 — PID再利用

**State: NOT_RUN** / 実装: CDPD-L07 / 独立評価: CDPD-L11

環境: Mock Windows process handle。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: 保存PIDを別processに再利用した状態を模擬

期待結果: **所有handle/jobでのみ終了、別process/Chrome/Consumerへkillゼロ。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC02, AC05。

## FLT-29 — 任意JS/CDP要求

**State: NOT_RUN** / 実装: CDPD-L06 / 独立評価: CDPD-L10

環境: Mock CDP。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: Input.*、Page.navigate、任意Runtime.evaluateを要求

期待結果: **COMMAND/SCRIPT_NOT_ALLOWED、実UI作用ゼロ。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC04, AC05。

## FLT-30 — 壊れた入力

**State: NOT_RUN** / 実装: CDPD-L05, CDPD-L06 / 独立評価: CDPD-L10

環境: 合成JSON fixture。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: unknown key/duplicate key/NaN/32KiB超/raw exception field

期待結果: **INPUT_REJECTED、payload保存なし、固定エラー。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC04, AC05。

## FLT-31 — 未完成journal tail

**State: NOT_RUN** / 実装: CDPD-L05, CDPD-L07 / 独立評価: CDPD-L10

環境: Mock filesystem。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: 最後のJSON行を途中で切り、内部行のhash違いも別途注入

期待結果: **末尾未完は未ACKとして除外、内部破損はfail-closed、虚偽ACKなし。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC02, AC03, AC05。

## FLT-32 — Baseline hashすり替え

**State: NOT_RUN** / 実装: CDPD-L08 / 独立評価: CDPD-L10

環境: 合成Baseline fixture。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: 承認対象と別hashのCandidateを昇格要求

期待結果: **拒否、approved pointer不変、勝手な再承認なし。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC05。

## FLT-33 — 異なるcontext/catalog

**State: NOT_RUN** / 実装: CDPD-L08 / 独立評価: CDPD-L10

環境: 合成DOM fixture。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: IDLE_EMPTY baseline対GENERATING、新matcher版を比較

期待結果: **UNKNOWN、BREAKING/PASSへ誤判定しない。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC05。

## FLT-34 — composer欠落

**State: NOT_RUN** / 実装: CDPD-L08 / 独立評価: CDPD-L10

環境: 完全coverageの合成UI。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: 承認済みIDLE_EMPTY baselineからcomposerだけを除去

期待結果: **BREAKING、source pointer/expected vs observedあり、selector修復なし。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC05。

## FLT-35 — 非機能的構造差分

**State: NOT_RUN** / 実装: CDPD-L08 / 独立評価: CDPD-L10

環境: 完全coverageの合成UI。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: 必須probeは同じ、装飾wrapperだけ変更

期待結果: **REVIEWまたは承認policyの許容差ならPASS。HTMLhashだけでBREAKING禁止。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC05。

## FLT-36 — 空入力時send disabled

**State: NOT_RUN** / 実装: CDPD-L08 / 独立評価: CDPD-L10

環境: IDLE_EMPTY合成UI。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: sendをdisabled、composerは正常

期待結果: **recipeどおりならPASS。他条件を満たすために入力操作しない。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC05。

## FLT-37 — 定期実行のbusy/unbound

**State: NOT_RUN** / 実装: CDPD-L08 / 独立評価: CDPD-L10

環境: Mock scheduler。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: Consumer動作中または4時間lease期限切れ

期待結果: **SKIPPED_BUSY/NO_ACTIVE_BINDING、UNKNOWN、再Binding/Chrome起動なし。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC05。

## FLT-38 — 本番へtest-only origin混入

**State: NOT_RUN** / 実装: CDPD-L03, CDPD-L06 / 独立評価: CDPD-L10

環境: Mock production configuration。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: 合成origin/TEST_ONLY入りBindingを本番loaderへ渡す

期待結果: **拒否。L01 schemaやexact originを緩和しない。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC04, AC05。

## FLT-39 — ACLとreparse

**State: NOT_RUN** / 実装: CDPD-L05, CDPD-L06 / 独立評価: CDPD-L11

環境: Windows実機ファイルシステム。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: 別ユーザーACL/保存root親junction/readonlyを用意

期待結果: **不正rootへ書かず保存拒否、権限不足を成功にしない。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC04, AC05。

## FLT-40 — retentionと保護対象

**State: NOT_RUN** / 実装: CDPD-L06, CDPD-L08 / 独立評価: CDPD-L10

環境: Mock filesystem/clock。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: 7日/1GiB到達、active S0/approved baselineを混在

期待結果: **terminal期限切れだけ削除、保護だけで満杯なら入場拒否。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC03, AC05。

## FLT-41 — 遅いfsyncとkill監督

**State: NOT_RUN** / 実装: CDPD-L05, CDPD-L07 / 独立評価: CDPD-L11

環境: Windows実機＋fault shim。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: journal writerだけをblockしWorkerもhangさせる

期待結果: **ACK出ないが監督loopはkill要求可能。OS停止なら保証外と明記。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC02, AC03, AC05。

## FLT-42 — 診断例外とprovider結果

**State: NOT_RUN** / 実装: CDPD-L04, CDPD-L05 / 独立評価: CDPD-L10

環境: Fake provider。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: 送出後にevent sink例外、provider success/raise両方を試す

期待結果: **元result/元例外維持、diagnostic文字列記録なし、provider呼出最大1回。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC04, AC05。

## FLT-43 — 古いcontrol generation

**State: NOT_RUN** / 実装: CDPD-L06, CDPD-L07 / 独立評価: CDPD-L10

環境: Mock keeper。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: Worker permit確認後にrevocation/generation更新

期待結果: **postcheck/commitで拒否、旧ref復活なし、DOM破棄。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC02, AC05。

## FLT-44 — heartbeat replayとprogress

**State: NOT_RUN** / 実装: CDPD-L07 / 独立評価: CDPD-L10

環境: Mock clock。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: 同seqを繰返し読み、またはheartbeatのみ進め観測をTimeout

期待結果: **同seqで延命しない。heartbeat進行だけで応答完了にしない。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC02, AC05。

## FLT-45 — report後のBundle不変

**State: NOT_RUN** / 実装: CDPD-L08 / 独立評価: CDPD-L10

環境: 合成committed Bundle。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: 比較reportを生成し元Bundleのhashと比較

期待結果: **Bundle bytes不変、report別Manifest、元assessment_status NOT_PERFORMED維持。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC05。

## FLT-46 — 未知coverageを最優先

**State: NOT_RUN** / 実装: CDPD-L06, CDPD-L08 / 独立評価: CDPD-L10

環境: bounded合成UI。合成UIまたはMock。source/binding/clock/policyを固定し、Web再送を無効にする。

注入: 探索上限でcomposer未到達、別featureの違反も付与

期待結果: **UNKNOWN優先、未到達0件を確定欠落にしない、違反候補はfindingsに残す。**

証拠: source hash、注入箇所/process identity、固定CDP command audit、S0/finalまたは保存エラー、expected/actual比較。実コマンド/ExitCode/actual/log_refは実装後に埋める（現時点null）。

対応AC: AC01, AC04, AC05。

## FIX001で追加した製品試験定義（実行は未実施）

元FLT-01〜46を無変更で維持。以下の8件は**試験定義**の追加であり、開発Ticketを増やすものではない。静的MockチェックのPASSと実製品試験を混同しない。

| ID | 対象 | 注入と期待結果 | 実装担当 | 状態 |
|---|---|---|---|---|
| FLT-47 | 未分類Provider例外の連結 | 同じBindingでProviderが未知の例外を送出。例外に秘匿canaryを含める。 元例外同一性・呼出1回・再送0回を維持。EVT-25経由でguardし、canaryの保存0件。 | CDPD-L04, CDPD-L05, CDPD-L06 | NOT_RUN |
| FLT-48 | 未知failure resultと観測introspection異常 | 未知failure_kindの戻り値と、診断getterの例外を別ケースで注入する。 元resultを保持。ACTION_FAILED/UNKNOWNはEVT-25、OBSERVATION_FAILED/UNKNOWNはEVT-08へ。dispatch状態の未送出への改変なし。 | CDPD-L04, CDPD-L05 | NOT_RUN |
| FLT-49 | FINALIZING直前・COMMIT直前の失敗 | POSTCHECK後/FINALIZING/COMMIT公開前にrevocation・lease満了・privacy・Worker異常・keeper喪失をそれぞれ注入。 失敗ラッチを保持し未確定構造0件。旧S3/検証を無効化、再保存後にPARTIAL。COMPLETE・再採取0件。storage正常ならS0/S3あり。 | CDPD-L06, CDPD-L07 | NOT_RUN |
| FLT-50 | COMMIT後の遅着異常と保存不能 | COMMIT後の異常を別イベントで発行。またCOMMIT前のS3再保存時にstorage拒否を注入。 既COMMITのbytes/hash不変。遅着は別メタデータ。保存拒否はSTORAGE_FAILED、新Bundle pathなし。 | CDPD-L06, CDPD-L07 | NOT_RUN |
| FLT-51 | 開始ACKのhash・current束縛 | row hash/payload hash/current hashを個別改変。current未fsync/公開失敗・古いgenerationも各ケースで注入。 正しいACKはround-trip可能。各不一致/未永続化ではProvider呼出0回。event ACKだけで開始認可しない。 | CDPD-L04, CDPD-L05 | NOT_RUN |
| FLT-52 | ACK遅着・再読込・自由文 | 2秒境界/取消し後/使用済みACK/別supervisorのACK、failure_codeへの改行と自由文を注入。 全て開始拒否。元のjournal履歴の永続性は保持、開始許可へ格上げしない。自由文の保存なし。 | CDPD-L04, CDPD-L05 | NOT_RUN |
| FLT-53 | Report正負round-tripと観測値 | 正常・未承認Baseline・必須composer0件・UNKNOWN+必須違反の各BundleからReportを導出し、矛盾PASSも投入。 Schema+意味検証で正常PASS/BREAKING/UNKNOWNが一致。矛盾PASS拒否。観測値・期待値とpointerを実証拠から解決。 | CDPD-L06, CDPD-L08 | NOT_RUN |
| FLT-54 | Report欠損・改変証拠と省略finding | Manifest不一致、probe欠損、重複feature、偽pointer、required finding省略、別recipe/catalogを注入。 欠損/不一致からUNKNOWN、宣言Reportの矛盾は拒否。任意count/許可構造だけの差分はREVIEW。自動修復・Baseline更新0回。 | CDPD-L06, CDPD-L08 | NOT_RUN |

## FIX002追加試験（FLT-55〜62、全NOT_RUN）

旧FLT-01〜54の機械定義はバイト内容相当を保持し、新規8件を追補した。以下は製品試験定義であり、tests内の静的oracle実行でPASSにしない。

### FLT-55 — 元Bundle全必須項目・全file型

注入: 全必須fieldの削除、未対応Schema、EVENTS各行の不正をrehashして両Bundleに注入。

期待: 型不正を固定code/UNKNOWNへ閉じ、矛盾PASSを拒否。

実装: CDPD-L06, CDPD-L08。独立実行: CDPD-L10/L11。状態: **NOT_RUN**。

### FLT-56 — COMPLETE内部整合

注入: COMPLETEとfailure_code、worker_termination、stage、policy、coverage、historyを矛盾させる。

期待: 誤PASS 0件。正常/metadata PARTIAL/REJECTEDと上限PARTIALは維持。

実装: CDPD-L06, CDPD-L08。独立実行: CDPD-L10/L11。状態: **NOT_RUN**。

### FLT-57 — Reportネスト型異常

注入: recipe配列/object、bool/float count、NaN/重複key/過深JSONを投入。

期待: 未捕捉例外なし。固定code/UNKNOWN。入力内容をログへ出さない。

実装: CDPD-L06, CDPD-L08。独立実行: CDPD-L10/L11。状態: **NOT_RUN**。

### FLT-58 — Bindingと証拠の相互参照

注入: 元Binding bytes/hash、Target/epoch/generation、trigger envelope/ID/historyを個別に不整合化。

期待: 別Binding・世代の証拠からPASSを生成しない。

実装: CDPD-L06, CDPD-L08。独立実行: CDPD-L10/L11。状態: **NOT_RUN**。

### FLT-59 — Document・Baseline整合

注入: marker不一致、Baseline document relation不明、別policy/catalog/正規化版を投入。

期待: UNKNOWN優先を維持し、未承認/不適合Baselineを採用しない。

実装: CDPD-L06, CDPD-L08。独立実行: CDPD-L10/L11。状態: **NOT_RUN**。

### FLT-60 — Adapter DISCONNECTED全公開操作

注入: 3公開操作で既知DISCONNECTED result。未知例外9caseも同時回帰。

期待: ACTION_FAILED/DISCONNECTEDを保持しEVT-26。metadata-only/Binding失効/new mutation停止、provider各1回、再送・再接続0。

実装: CDPD-L05, CDPD-L06, CDPD-L08。独立実行: CDPD-L10/L11。状態: **NOT_RUN**。

### FLT-61 — DISCONNECTED最終化競合

注入: S0直後からCOMMIT直前の断、失効/Worker/storage異常の順序、COMMIT後・旧世代イベントを注入。

期待: 単調ラッチと構造破棄。旧世代は新Bindingを失効させない。COMMIT後は不変。

実装: CDPD-L05, CDPD-L06, CDPD-L08。独立実行: CDPD-L10/L11。状態: **NOT_RUN**。

### FLT-62 — 必須入力前段の実結合

注入: L06保存前/読戻しとL08 Report入口へ同じfront-endを接続し前段を迂回したPASSを投入。

期待: Schemaだけやdeclared-vs-derivedだけの受理禁止。Windows実ファイル/ACL/実プロセス観測と静的Mockを分離。

実装: CDPD-L05, CDPD-L06, CDPD-L08。独立実行: CDPD-L10/L11。状態: **NOT_RUN**。


## FIX003追加試験

FLT-63〜74の12件を追補し、元FLT-01〜62の機械定義は内容不変とする。全74件が**NOT_RUN**。前回F-04の7失敗をsource/Baseline双方と境界正例へ接続するが、ここでは製品Runtimeを実行していない。

各caseは正常入力から1つの不整合を作り、byte長・hash・Manifest・COMMITを正しく再計算する。生成UNKNOWNの検査と、不正な元Bundleの前段拒否及び宣言PASSの拒否を分けて記録する。source/Baseline両側の検査で、実Baseline承認を代用・更新しない。

正本: [履歴規則](../design/02_Diagnostics_State_And_Triggers.md)、[構造保持表](../design/02_Evidence_And_Report_Contract.md)、[入力契約](../contracts/bundle_input_contract.json)。設計自己検証: [23観点Execution replay](replay_assessment_fix003.py)、[追加チェック](fix003_checks.py)。

### FLT-63 — 個別既知lossと履歴COMPLETEの矛盾

注入: source/Baselineの両側で、trigger Envelopeと同一history行のloss_lower_bound=1をそろえて記録し、RESULT集計loss0・exact=true・history.complete=true・capture COMPLETEを残す。各file/Manifest/COMMITを正しくrehashする。

期待: 両側で必須前段がPROFILE_INCONSISTENTを返し、別の型/意味適合UNKNOWNを生成する。同じ不整合Bundleからの宣言PASSを受信者が拒否。Baselineの承認hashが一致しても不整合を免除しない。

証拠: source/Baseline全bytes、変異とrehash記録、前段code、生成ReportのSchema/意味検証、宣言PASS拒否、入力不変と安全なcommand audit。

実装: CDPD-L05, CDPD-L06, CDPD-L08。独立実行: CDPD-L10/L11。対応AC: AC01, AC03, AC05。状態: **NOT_RUN**。actual/command/exit_code/log_refはnull。

### FLT-64 — 個別loss総数不明と集計exactの矛盾

注入: source/Baselineの両側で、1つの保存Envelopeにloss_count_exact=falseを記録し、RESULT集計exact=true・history.complete=trueを残してrehashする。triggerだけが不明を報告する変種も含む。

期待: 個別のunknownを集計で確定へ変換した入力を両側で前段拒否し、Reportは別UNKNOWN。history.complete=falseでも集計exact=trueの矛盾を免除しない。元正常PASSの宣言は不成立。

証拠: source/Baseline全bytes、変異とrehash記録、前段code、生成ReportのSchema/意味検証、宣言PASS拒否、入力不変と安全なcommand audit。

実装: CDPD-L05, CDPD-L06, CDPD-L08。独立実行: CDPD-L10/L11。対応AC: AC01, AC03, AC05。状態: **NOT_RUN**。actual/command/exit_code/log_refはnull。

### FLT-65 — 保持通常sequence gapの両側拒否

注入: 同じproducer/instance/Binding/hash/generation/domainで保持通常seq1/3を並べ、sampled=false・loss0・history.complete=trueのままsource/Baseline双方をrehashする。同じUTCを用いjournal_seqと比較しない。

期待: 内部gap1を集計へ反映しない入力は両側で前段拒否、UNKNOWN、宣言PASS拒否。正常対照seq1/2/3は受理し、他の前提も正常ならPASS。保持prefixを要求する検査へ置換しない。

証拠: source/Baseline全bytes、変異とrehash記録、前段code、生成ReportのSchema/意味検証、宣言PASS拒否、入力不変と安全なcommand audit。

実装: CDPD-L05, CDPD-L06, CDPD-L08。独立実行: CDPD-L10/L11。対応AC: AC01, AC03, AC05。状態: **NOT_RUN**。actual/command/exit_code/log_refはnull。

### FLT-66 — 累積counterの重複・減少・不明解除

注入: 同じ基本streamの通常event損失counterの累積loss2を通常event/heartbeatへ繰返す正例と、比較可能な同domainのseq順のloss2→1、exact=false→true、同domain/sequenceに異なるevent又はcounterを割り当てる負例を両側へ投入する。triggerの同内容複製も含む。

期待: 同じcounterの累積2 snapshotを加算せず集計下限2で受理し、正しい履歴不足PARTIAL/UNKNOWNへ。同domainで順序を比較できるcounter減少・不明解除・相反する同sequenceは前段拒否。異domain間の時系列は推定せず全carrierのmaxとunknownを伝播し、同じ既知gapを二重加算しない。個別exactだけで集計exactを自己証明しない。

証拠: source/Baseline全bytes、変異とrehash記録、前段code、生成ReportのSchema/意味検証、宣言PASS拒否、入力不変と安全なcommand audit。

実装: CDPD-L05, CDPD-L06, CDPD-L08。独立実行: CDPD-L10/L11。対応AC: AC01, AC03, AC05。状態: **NOT_RUN**。actual/command/exit_code/log_refはnull。

### FLT-67 — stream境界とresetの限定

注入: 1つのBundleへ異なるproducer/instance/Binding/hash/generationを混在させる負例と、別の正規streamを別Bundleにした正例を用意する。旧eventを新streamへ付け替える変種、同producer instanceでBinding変更を理由にseqをresetする操作を監査する。

期待: 混在/付替えを前段又は生成側契約で拒否する。新instance又は正規のBinding/世代変更は別streamで扱い、同一instanceのseqはdomain別に継続する。単一Bundleから過去のreset権限を証明せず、単にcapture_idやUTCを変えてlossを消さない。

証拠: source/Baseline全bytes、変異とrehash記録、前段code、生成ReportのSchema/意味検証、宣言PASS拒否、入力不変と安全なcommand audit。

実装: CDPD-L05, CDPD-L06, CDPD-L08。独立実行: CDPD-L10/L11。対応AC: AC01, AC03, AC05。状態: **NOT_RUN**。actual/command/exit_code/log_refはnull。

### FLT-68 — window・UTC・heartbeat domain境界

注入: 保持通常seq41/42、保持history外のtrigger、UTCがseq順と異なる正例を用意する。累積loss>0をwindow外と自己申告して集計0にする負例、通常1/heartbeat2/通常3、通常1/heartbeat100/通常2/通常3をsource/Baselineへ投入する。

期待: prefix/suffixやhistory外triggerまでの距離、UTC逆順だけを欠落にしない。窓外免除の証明fieldがない累積lossの抹消は拒否する。heartbeat2は通常gapを埋めず前者を拒否し、heartbeat100のcoalesce gapは通常event欠落と数えず後者を受理する。heartbeatも基本stream共通の通常event損失counterを運び、そのloss/unknownは集計へ伝播。domainごとに別母集団を作らない。

証拠: source/Baseline全bytes、変異とrehash記録、前段code、生成ReportのSchema/意味検証、宣言PASS拒否、入力不変と安全なcommand audit。

実装: CDPD-L05, CDPD-L06, CDPD-L08。独立実行: CDPD-L10/L11。対応AC: AC01, AC03, AC05。状態: **NOT_RUN**。actual/command/exit_code/log_refはnull。

### FLT-69 — sampling・必要履歴切詰めの集計

注入: sampling又は必要履歴の件数/byte上限による省略を起こし、sampled=true/complete=falseと既知loss下限・exactnessを正しく記録する正例を用意する。sampled=trueのままcomplete=true、省略をloss0/exact=trueで隠す負例も両側へ投入する。

期待: samplingは保持gapに見えなくてもCOMPLETE/PASS不可。適合する履歴不足PARTIALは受理してUNKNOWNとし、相互矛盾は前段拒否。knownとunknownを区別し、同じ省略の累積報告を無条件に加算しない。

証拠: source/Baseline全bytes、変異とrehash記録、前段code、生成ReportのSchema/意味検証、宣言PASS拒否、入力不変と安全なcommand audit。

実装: CDPD-L05, CDPD-L06, CDPD-L08。独立実行: CDPD-L10/L11。対応AC: AC01, AC03, AC05。状態: **NOT_RUN**。actual/command/exit_code/log_refはnull。

### FLT-70 — S1/S2失敗・未実行と構造保持の禁止

注入: 正常bounded PARTIALから、S1又はS2をFAILED/CDP_COMMAND_FAILED、又はNOT_RUN/固定reason/duration0へ変え、構造fileを保持してsource/Baseline双方をrehashする。nodes=[]の回避、COMPLETE stageへの非null reasonも含む。

期待: 各不整合を必須前段で拒否。最終PARTIAL/TRUNCATED又はReport UNKNOWNという文字列だけでは構造保持を許さない。失敗stageのSTRUCTUREは空fileで残さず破棄する。両側の不整合からの宣言PASSを拒否。

証拠: source/Baseline全bytes、変異とrehash記録、前段code、生成ReportのSchema/意味検証、宣言PASS拒否、入力不変と安全なcommand audit。

実装: CDPD-L06, CDPD-L08。独立実行: CDPD-L10/L11。対応AC: AC01, AC02, AC04, AC05。状態: **NOT_RUN**。actual/command/exit_code/log_refはnull。

### FLT-71 — 検証済みbounded PARTIALの許容表

注入: 正常Worker・全identity/policy/sanitization成立、STRUCTURE.truncated=true・coverage_complete=false・最終PARTIALで、S1/S2をCOMPLETE/null又はPARTIAL/TRUNCATEDの許容組合せにする。実truncatedなし、PARTIAL stageの他reason、最終failure=TRUNCATEDのみを足す負例も両側に用意。

期待: 正当なbounded構造を両側で前段受理しReport UNKNOWN。stage又は最終codeだけで実切詰めを偽る入力、coverage_complete=falseだけの保持根拠は拒否する。正常4recipe COMPLETEを回帰維持。

証拠: source/Baseline全bytes、変異とrehash記録、前段code、生成ReportのSchema/意味検証、宣言PASS拒否、入力不変と安全なcommand audit。

実装: CDPD-L06, CDPD-L08。独立実行: CDPD-L10/L11。対応AC: AC01, AC02, AC05。状態: **NOT_RUN**。actual/command/exit_code/log_refはnull。

### FLT-72 — 正しい履歴不足の構造付きPARTIAL

注入: S1/S2 COMPLETE/null・Worker正常・identity等成立で、既知loss、unknown loss、保持gap又はsamplingを正しく集計しhistory.complete=false・最終PARTIAL/HISTORY_INCOMPLETEとする。DOM非truncatedの正例と、HISTORY_INCOMPLETEなのにhistory.complete=trueの負例を両側に用意。

期待: 適格な構造付きPARTIALは前段受理し、実DOMが完全でもReport UNKNOWNを維持。最終codeと履歴事実の矛盾は前段拒否。履歴不足をWorker失敗へ変更せず、適合しないBaselineを承認だけで正規化しない。

証拠: source/Baseline全bytes、変異とrehash記録、前段code、生成ReportのSchema/意味検証、宣言PASS拒否、入力不変と安全なcommand audit。

実装: CDPD-L05, CDPD-L06, CDPD-L08。独立実行: CDPD-L10/L11。対応AC: AC01, AC02, AC03, AC05。状態: **NOT_RUN**。actual/command/exit_code/log_refはnull。

### FLT-73 — 構造なしPARTIALとpolicy REJECTEDの保持

注入: 既存の構造なしPARTIALと、policy=false・Worker NOT_STARTED・S1/S2 NOT_RUN・S0/S3 COMPLETEのREJECTEDを正常対照にする。BOUND/lease/Worker失敗等は固定codeで記録し、source/Baselineの両側へ渡す。

期待: 正当なmetadata-only Bundleを前段受理しReport UNKNOWN。新stage保持規則が必要なS0/S3まで拒否しない。UNKNOWNをBaseline適格/業務成功へ読み替えない。入力や既COMMITを修理・再採取しない。

証拠: source/Baseline全bytes、変異とrehash記録、前段code、生成ReportのSchema/意味検証、宣言PASS拒否、入力不変と安全なcommand audit。

実装: CDPD-L06, CDPD-L08。独立実行: CDPD-L10/L11。対応AC: AC01, AC02, AC04, AC05。状態: **NOT_RUN**。actual/command/exit_code/log_refはnull。

### FLT-74 — 履歴・stage契約の4入口実結合

注入: FLT-63〜73の入力をL06保存前・読戻し、L08 Report生成・受信の4入口へ投入する。前回23観点の7残存反例と既知5反例×source/Baselineを保持し、前段を迂回する呼出側bool又は正常PASS宣言を与える。

期待: 不整合BundleのCOMMIT/受理を拒否し、必要なら元入力とは別の型/意味適合UNKNOWNを生成する。Schemaだけ・hashだけ・declared-vs-derivedだけの受理は禁止。入力bytes不変、修正なし、F-01〜03/F-05回帰保持。静的Execution結果を独立Assessment閉鎖やWindows Runtime PASSへしない。

証拠: source/Baseline全bytes、変異とrehash記録、前段code、生成ReportのSchema/意味検証、宣言PASS拒否、入力不変と安全なcommand audit。

実装: CDPD-L05, CDPD-L06, CDPD-L08。独立実行: CDPD-L10/L11。対応AC: AC01, AC02, AC03, AC04, AC05。状態: **NOT_RUN**。actual/command/exit_code/log_refはnull。

