# L07 Windows起動・保管条件

対象は新L07 CandidateのSupervisorです。Windows Job Object、実NTFS、実ACL、実Chrome停止の受入は本資料の存在で成立しません。Windows実機QAは別記録が必要です。

## 1. 独立起動の手順

1. Consumer（SHIRABE）を開始する前に、Windowsのスタートメニュー又はWin+Rから専用の`powershell.exe -NoProfile`コンソールを開きます。Consumerの子、Consumerが管理するターミナル、終了連鎖を持つJob Objectの中で起動しないでください。
2. 同一Windowsユーザーが所有する専用state rootと、既存の専用Chrome profileディレクトリを指定します。両ディレクトリは既存で、互いに内包しない配置とします。
3. Pythonと依存関係はCandidateの固定requirementsに合わせます。`PythonExe`はその環境の絶対パスです。
4. 下記を専用コンソールから実行し、Supervisorが起動を受理した後にConsumerを開始します。Supervisor自身が既存Job Object所属を検出した場合はBLOCKEDで止まります。Jobの制約を解除して強行するスイッチはありません。

```powershell
$cdpdCandidate = 'C:\Work\CDPD-L07_CANDIDATE_v0.3.0_IMPL001'
$cdpdPython = 'C:\Work\cdpd-venv\Scripts\python.exe'
$cdpdState = Join-Path $env:LOCALAPPDATA 'CDPD\L07\State'
$cdpdProfile = Join-Path $env:LOCALAPPDATA 'CDPD\L03-Example\BrowserProfile'

& (Join-Path $cdpdCandidate 'scripts\start_watchdog.ps1') `
  -StateRoot $cdpdState `
  -ProfileDirectory $cdpdProfile `
  -RunId 'explicit-operational-run-001' `
  -PythonExe $cdpdPython `
  -PollSeconds 0.25
```

スクリプトは前景でSupervisorを起動します。Chrome起動、ACL修正、フォルダー生成、Task Scheduler登録、Web操作を行いません。専用コンソールを閉じればSupervisorも停止し、所有WorkerのJob handle closeによる終了が必要です。この効果のWindows実機確認は別試験です。

## 2. Run・Bindingとbootstrap

`RunId`は明示的なopaque IDです。同じ運用runのSupervisor再起動では同じ値を維持します。同じrun/operationはproducer、Binding世代、Supervisor instanceを変えても再採取できません。古いattemptを回避するためにrun IDを付け替えないでください。

Supervisor再起動では時計keeperの連続性を失います。保存したrecord、current、heartbeat、receiptからlive permitを復活させず、既存S0からmetadataのみを復旧します。新しい構造採取には明示Preflightに接続した信頼済みruntime登録が必要です。

必要な場合は`-Bootstrap 'trusted_module:register_runtime'`を明示します。これは同一ユーザーがレビューした実行コードを登録する入口です。bootstrapの具体的な引数と登録APIは`code/cdpd_chatgpt_profile/supervisor.py`を参照してください。保存recordの読戻しだけでguard/keeperを作るbootstrapは不適合です。未接続時はmetadataだけを扱い、SHIRABEの新API結合完了やL09受入を意味しません。

## 3. ACL・保管領域

`WindowsStoragePolicy`は次を読み取り検査し、不明な場合も拒否します。

- root/profileはローカル固定NTFS上の絶対パス。UNC、device path、ADS、`..`、曖昧な末尾空白/ピリオド、予約device名を拒否します。
- 全既存祖先にreparse pointやcloud/offline属性がないことを確認します。
- root/profile owner SIDは実行プロセスのTokenUser SIDと一致し、ルートDACLは継承から保護されている必要があります。
- allow ACEは当該ユーザー、SYSTEM、Administratorsのみに限定します。当該ユーザーには有効なFull Controlが必要です。null DACL、広いUsers/Everyone/Authenticated Users権限、未知のACE型は拒否します。SYSTEM/AdministratorsはOS管理権限の範囲であり、同一ユーザー内の敵対コードを隔離するものではありません。
- 出力先はstate root配下だけです。既存の各子についてowner/ACLを再検査し、存在しない出力は検証済み祖先の下に限って許可します。
- 登録済みCloud Files同期root、既知同期フォルダー名・OneDrive環境rootを拒否します。Cloud Files APIの未知エラーを「同期なし」と解釈しません。

ACLを自動修正しません。拒否された場合は管理者又は所有者が原因を確認し、専用ディレクトリに必要な限定ACLを設定してから再度確認してください。製品ファイルやChrome profileの既存ACLを無断で一括変更する手順は提供していません。

第三者の未登録同期/バックアップソフトが任意のディレクトリをコピーしていないことは、これらのpath APIだけでは証明できません。state/profileは同期対象外として運用管理してください。ACL照合は名前に対する検査であり、同一ユーザー又は管理者による敵対的な同時path差替えを隔離するsandboxではありません。

## 4. 監視間隔と故障時の動作

Consumer Heartbeatの設計周期は2秒、受信側の新しいseqから10秒で期限超過、Supervisor pollは最大0.25秒です。UTC、mtime、同じseqの再読込みで延命しません。Heartbeatが戻ってもDIAGNOSTIC_ONLYから自動復帰しません。

Worker期限はspawn起点の通算期限です。Worker専用Jobだけを終了し、Chrome/ConsumerはJobへ入れません。終了未確認なら次Workerを起動しません。DOM/fsyncが遅くてもkill監督をその完了待ちにしませんが、OS全体の停止・無応答や保存syscall自体の停止まで含む遅延保証ではありません。保存不能時に成功したBundle pathを返しません。

## 5. 実装根拠と未検証範囲

以下のMicrosoft一次資料でAPIの宣言・意味を確認しました（2026-10-25）。これらは実機測定の代用にはなりません。

- [GetNamedSecurityInfoW](https://learn.microsoft.com/en-us/windows/win32/api/aclapi/nf-aclapi-getnamedsecurityinfow)：owner/DACL取得とバッファ解放、競合時の制限。
- [GetAce](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-getace)：ACL内ACE取得。
- [GetTokenInformation](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-gettokeninformation)：現在ユーザーTokenUser取得。
- [GetDriveTypeW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getdrivetypew)：固定/ネットワーク等のdrive分類。
- [CfGetSyncRootInfoByPath](https://learn.microsoft.com/en-us/windows/win32/api/cfapi/nf-cfapi-cfgetsyncrootinfobypath)：pathが属するCloud Files同期root照会。
- [CF_SYNC_ROOT_INFO_CLASS](https://learn.microsoft.com/en-us/windows/win32/api/cfapi/ne-cfapi-cf_sync_root_info_class)：BASIC=0。

CFAPIの「同期root外」は`HRESULT_FROM_WIN32(ERROR_CLOUD_FILE_NOT_UNDER_SYNC_ROOT=390)`だけを肯定的な非同期判定として使います。その他の未対応HRESULTは`CLOUD_SCOPE_UNKNOWN`として停止します。実Windowsで返る結果とcompatibilityはnative QAで確認が必要です。Linux上のsynthetic probe、単体テスト、静的コード検査からWindowsのACL・CFAPI・Job Object動作をPASSにはしません。
