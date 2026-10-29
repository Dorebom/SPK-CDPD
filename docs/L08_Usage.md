# L08 利用手順 — オフライン比較と任意Scheduler定義

このCandidateは、確定済みEvidence Bundleから構造特徴を比較し、独立した`report.json`／`report.md`を生成します。Baseline候補は作成できますが、この操作だけでは採用されません。Schedulerのスクリプトを実行する例は、登録しない定義確認に限定しています。

現在の`SCHEDULED_CAPTURE`は、信頼されたlive idle境界が未統合のためBLOCKED/UNKNOWNです。新しいDOM採取やChrome起動を行う機能として扱わないでください。既存のEvidenceを再評価する`OFFLINE_REPORT`は実装済みです。

## 1. Windowsでの前提

このZIPを新しいCandidateディレクトリへ展開し、元ZIP・receipt・Manifestを保持します。Python実行環境には既存`requirements-l06.txt`の依存を用意します。CLI入口は`scripts/run_l08_report.py`と`scripts/run_l08_monitor.py`です。`-B`を付けるとbytecode書込みも抑止できます。

Evidence入力は`C-`＋32桁小文字hexのcapture IDをディレクトリ名とする確定Bundleです。中にはTRIGGER/EVENTS/RESULT/MANIFEST/COMMITと、ある場合はSTRUCTUREがあります。sourceとBaselineの双方が同じ厳密検証を受けます。

Windowsの書込みには、次の条件を満たす**既存の**出力rootと専用Chrome profileが必要です。

- ローカル固定ドライブ上のNTFS。UNC、ネットワーク共有、同期root、cloud/offline、junction/reparse pointを使用しない。
- 出力rootとprofileは別ディレクトリとし、一方を他方の内部に置かない。
- 両rootは実行ユーザー所有、保護されたprivate DACL。許容SIDは実行ユーザー、SYSTEM、Administratorsで、実行ユーザーに必要な制御権があること。
- 出力rootは入力Bundleや承認recordの内部に置かない。既存の承認済みBaseline/history用rootと候補作成rootも区別する。

CLIはACL・所有者・profileを変更しません。条件を確認できなければBLOCKEDで終了します。Pythonのmode値や合成試験PASSをnative Windowsの保証へ読み替えません。

## 2. 例で使用するパスとpin

以下は**置換が必要な例**です。`<...>`を実際の絶対パス・capture ID・64桁小文字SHA-256へ置き換えてください。例の文字列をそのまま実行しても有効入力にはなりません。

```powershell
$L08Python = 'C:\<PythonEnvironment>\Scripts\python.exe'
$L08ProjectRoot = 'C:\CDPD\CDPD-L08_CANDIDATE_v0.3.0_IMPL001'
$L08SourceBundle = 'C:\CDPD\Evidence\C-<32hex-source-capture-id>'
$L08SourceCommitSha256 = '<source-COMMIT-raw-bytes-sha256>'
$L08ReportRoot = 'C:\CDPD\L08Reports'
$L08CandidateRoot = 'C:\CDPD\L08BaselineCandidates'
$L08BrowserProfile = 'C:\CDPD\DedicatedBrowserProfile'
```

`--source-commit-sha256`は**COMMIT.json原bytes**のSHAです。MANIFEST.jsonのSHAやCOMMIT内の`manifest_sha256`値とは異なります。単発CLIではpin引数を省略できますが、固定入力としての受渡しでは指定してください。monitorのOFFLINE_REPORT configでは必須です。

## 3. Baseline未採用のままレポートを作る

```powershell
& $L08Python -B "$L08ProjectRoot\scripts\run_l08_report.py" report `
  --source $L08SourceBundle `
  --source-commit-sha256 $L08SourceCommitSha256 `
  --output-root $L08ReportRoot `
  --profile-directory $L08BrowserProfile
$LASTEXITCODE
```

適切に読めるsourceでもBaselineがないため、評価は`UNKNOWN`、外側の理由は`BASELINE_UNAPPROVED`、終了コードは12です。coreの元契約では同じ理由を`BASELINE_NOT_APPROVED`と表記します。両者は明示的な境界マッピングで、誤ったPASSへ補正しません。

標準出力は固定形状の安全なJSONです。出力artifactは`$L08ReportRoot\R-<artifact-Manifest-SHA>`に保存され、`report.json`、`report.md`、独立した`MANIFEST.json`、`COMMIT.json`を持ちます。

## 4. 未採用Baseline候補を作る

```powershell
& $L08Python -B "$L08ProjectRoot\scripts\run_l08_report.py" baseline-candidate `
  --source $L08SourceBundle `
  --source-commit-sha256 $L08SourceCommitSha256 `
  --output-root $L08CandidateRoot `
  --profile-directory $L08BrowserProfile
$LASTEXITCODE
```

適格な完全観測だけを候補にできます。成功時の終了コード0は**候補作成の成功**であり、レポート評価はUNKNOWN、BaselineはBASELINE_UNAPPROVEDのままです。PARTIAL、必要probe不備、hash不正などは候補の採用根拠になりません。

候補は`B-<artifact-Manifest-SHA>`に保存されます。`baseline-candidate.json`と、元bytesを保つ`source_bundle\C-<capture-id>\...`が含まれます。承認recordやactive pointerは作成・更新されません。

## 5. 別途採用済みのBaselineで比較する

この手順は、Humanの別採用手続により、対象hashと承認recordが既に確定している場合だけ使用します。このCandidateの試験用recordは使用しません。

```powershell
$L08BaselineBundle = 'C:\CDPD\ApprovedBaseline\C-<32hex-baseline-capture-id>'
$L08BaselineCommitSha256 = '<baseline-COMMIT-raw-bytes-sha256>'
$L08ApprovalRecord = 'C:\CDPD\ApprovedBaseline\<trusted-approval-record>.json'
$L08TrustedApprovalSha256 = '<independently-approved-record-raw-bytes-sha256>'

& $L08Python -B "$L08ProjectRoot\scripts\run_l08_report.py" report `
  --source $L08SourceBundle `
  --source-commit-sha256 $L08SourceCommitSha256 `
  --baseline $L08BaselineBundle `
  --baseline-commit-sha256 $L08BaselineCommitSha256 `
  --approval-record $L08ApprovalRecord `
  --trusted-approval-sha256 $L08TrustedApprovalSha256 `
  --output-root $L08ReportRoot `
  --profile-directory $L08BrowserProfile
$LASTEXITCODE
```

trusted approval SHAは、承認recordとは別に信頼できる採用情報から選びます。**未承認recordをその場でhash化した値を、承認済みpinと見なしてはいけません。** hashが一致しても、人の承認や観測者の誠実性が暗号学的に証明されるわけではありません。

承認recordの閉Schemaは`contracts/baseline_approval_v1.schema.json`です。対象Manifest、normalized profile、recipe/catalog/normalization/policy/locale/viewport/browser profileと、承認者ID・日時・旧Manifest pinを束縛します。recordが不正、別Baselineに対するもの、trust pin不一致、Baseline自体が不適格であれば、APPROVEDにはなりません。recordが読めない場合はBLOCKED/2です。

## 6. 生成物の再検証

```powershell
$L08ReportPackage = 'C:\CDPD\L08Reports\R-<artifact-manifest-sha256>'

& $L08Python -B "$L08ProjectRoot\scripts\run_l08_report.py" verify `
  --source $L08SourceBundle `
  --source-commit-sha256 $L08SourceCommitSha256 `
  --baseline $L08BaselineBundle `
  --baseline-commit-sha256 $L08BaselineCommitSha256 `
  --approval-record $L08ApprovalRecord `
  --trusted-approval-sha256 $L08TrustedApprovalSha256 `
  --report-package $L08ReportPackage
$LASTEXITCODE
```

Baselineなしで生成したものを検証するときは、Baseline/approval関連引数を全て省略します。検証は元の入力条件を再現してください。verifyは保存物のhashだけでなく、source/Baselineからの意味再計算とMarkdown原bytesも照合します。終了コード0は整合性と意味検証の成功で、内包する評価がPASSとは限りません。

## 7. Report内のhashと判定

| 項目 | 対象 |
|---|---|
| `before_sha256` | Baseline MANIFEST.json原bytes |
| `after_sha256` | source MANIFEST.json原bytes |
| `source_commit_sha256` | source COMMIT.json原bytes |
| `before_normalized_sha256` / `after_normalized_sha256` | 固定規則で正規化した適用feature/context |
| 保存先`R-...` / `B-...` / `M-...` | 新しい独立artifactのMANIFEST.json原bytes |

hashを取り違えないでください。正規化hashの差だけでBREAKINGを決めません。UNKNOWNが優先し、前提成立後に必須機能違反をBREAKING、安全な非破壊差分をREVIEW、全条件を満たすものをPASSとします。送信・応答内容・業務成功の認定ではありません。

## 8. monitor configの厳密な形

configはUTF-8 JSON、最大32768 bytes、未知field・欠落field拒否です。実行時はconfig原bytesのSHAを`--expected-config-sha256`へ渡します。

### OFFLINE_REPORT: 固定した既存Evidenceを再評価

以下の**10項目全て**が必要です。省略せず、使用しないoptional値はJSON nullにします。

```json
{
  "schema": "cdpd.l08.monitor-config/v1",
  "mode": "OFFLINE_REPORT",
  "output_directory": "C:\\CDPD\\L08Reports",
  "profile_directory": "C:\\CDPD\\DedicatedBrowserProfile",
  "source_bundle": "C:\\CDPD\\Evidence\\C-<32hex-source-capture-id>",
  "source_commit_sha256": "<source-COMMIT-raw-bytes-sha256>",
  "baseline_bundle": null,
  "baseline_commit_sha256": null,
  "approval_record": null,
  "trusted_approval_sha256": null
}
```

`baseline_bundle`と`baseline_commit_sha256`、`approval_record`と`trusted_approval_sha256`はそれぞれ両方nullまたは両方指定します。Windowsの書込みでは`profile_directory`を実在する適格なパスにします。採用済みBaselineを使う場合のtrusted approval pinの意味は5節と同じです。

このmodeは、同じ古いEvidenceを繰り返し再評価する処理です。Chromeの現時点の存在・現UIの変更を検出したことにはなりません。標準出力の`scheduled_invocation`は`OFFLINE_REPLAY_ONLY`です。

### SCHEDULED_CAPTURE: 現Candidateでは安全側に拒否

このmodeで許可されるfieldは、次の**4項目だけ**です。

```json
{
  "schema": "cdpd.l08.monitor-config/v1",
  "mode": "SCHEDULED_CAPTURE",
  "output_directory": "C:\\CDPD\\L08MonitorStatus",
  "profile_directory": "C:\\CDPD\\DedicatedBrowserProfile"
}
```

`idle=true`、`chrome_running=true`、provider名などを追加しても権限にはならず、config自体が拒否されます。通常CLIは`BLOCKED / UNKNOWN / TRUSTED_LIVE_IDLE_BOUNDARY_UNAVAILABLE`を出力し、保存できれば終了コード12です。capture・送信・再送・Binding更新は0です。保存条件も満たさなければ終了コード2になります。

製品内部の信頼されたnegative observerがChrome不在を確定した場合の契約は`SKIPPED / UNKNOWN / CHROME_NOT_RUNNING`ですが、現在のconfig/CLIからobserverを注入する機能はありません。これは今回のlive Chrome実機確認を意味しません。

### configを明示して単発実行

```powershell
$L08MonitorConfig = 'C:\CDPD\Config\<reviewed-monitor-config>.json'
$L08MonitorConfigSha256 = '<reviewed-config-raw-bytes-sha256>'

& $L08Python -B "$L08ProjectRoot\scripts\run_l08_monitor.py" `
  --config $L08MonitorConfig `
  --expected-config-sha256 $L08MonitorConfigSha256
$LASTEXITCODE
```

configが変更されれば、元pinでは次回実行を拒否します。configのpin固定はBaseline採用や新規captureの承認ではありません。

## 9. Scheduler定義を確認する — 登録しない

Windows PowerShellで、次のように**`-Register`なし**で実行します。日時は利用時点から1分以上先のローカル時刻へ置き換えてください。

```powershell
$L08FirstRun = [datetime]'2026-11-29T09:00:00'

& "$L08ProjectRoot\scripts\register_monitor_task.ps1" `
  -PythonExe $L08Python `
  -ConfigPath $L08MonitorConfig `
  -ConfigSha256 $L08MonitorConfigSha256 `
  -FirstRunAt $L08FirstRun `
  -DaysInterval 30 `
  -TaskName 'CDPD-L08-Monitor'
```

出力は`DEFINITION_ONLY / scheduler_registered=false`です。monitor起動、Task Scheduler登録・有効化はしません。30は「30日ごと」で、暦月ごとではありません。

| オプション | 効果 |
|---|---|
| 指定なしの既定 | 定義JSONだけを出力 |
| `-Register` | 別途明示した登録操作。既定はdisabledで登録。既存taskは上書き拒否 |
| `-Register -EnableAutomaticStart` | 将来の自動実行を明示した登録操作。今回の実行対象外 |
| `-WhatIf` | 登録指定があってもTask Scheduler APIを呼ばない |

登録・自動起動は、別の明示要求に基づく操作です。この作業では実施していません。scriptに即時起動機能はなく、登録してもその場でmonitorは実行しません。taskは現在ユーザーInteractive/Limited、重複実行IgnoreNew、失敗時restart・catch-up・wakeなし、実行上限5分を指定します。PowerShell/Task Schedulerのnative実行確認はNOT_RUNです。

## 10. 終了コードと保存異常の扱い

| 終了コード | report / OFFLINE_REPORT | その他 |
|---:|---|---|
| 0 | PASS | baseline-candidate作成成功、またはverify成功。評価PASSとは別 |
| 10 | REVIEW | 要確認の非破壊差分 |
| 11 | BREAKING | 完全観測の必須機能違反 |
| 12 | UNKNOWN | Baseline未採用・証拠不足、またはSCHEDULED_CAPTUREの未接続境界 |
| 2 | BLOCKED/UNKNOWN | 引数・読取・pin・保存拒否、verify不一致 |

`POST_PUBLICATION_UNCONFIRMED`では、COMMIT公開後のdurability/readbackが確定できていません。既存COMMITやpayloadを削除・変更しないでください。`STORE_BUSY`、不完全artifact、容量不足でも自動修理・削除・capture再試行はしません。原bytesと固定理由を保持して別途確認します。

report/monitor rootは1GiB/250件、Baseline候補rootは64MiBの上限です。七日retentionは運用側の責務で、このCandidateは承認版や履歴を自動削除しません。native保存・retention consumer・trusted idle統合・real Chrome/CDP/ChatGPT/SHIRABEの受入は、引き続き各後続工程の範囲です。
