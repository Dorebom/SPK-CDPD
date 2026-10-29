# L07 FIX010: 起動処理と metadata-only 公開契約

## 位置付け

FIX010 は FIX009 を親とする実装 Candidate である。管理上の承認、Finding Closure、Gate、L07_COMPLETE、Stable、Release、L09 を変更しない。既存 1,519 ID のテスト、fixture、承認済み L02 schema/matrix、既存 deadline は変更しない。

今回受領した Windows V5 REV001 は 26 回中 21 PASS / 5 FAIL だった。記録完了を示す DIAGNOSTIC_RECORDED は全件合格を意味しない。元 Watchdog Prepare の boundary 2 回は認証済み Terminal と Prepare の到達前に deadline に達していた。軽量の認証済み Prepare 対照は 4 回とも PASS だった。Terminal Binding の off 1 回の直接の失敗位置は記録されておらず、起動遅延と断定しない。

## 1. 起動処理の変更

### 公開 export の遅延読込み

cdpd と chatgpt_web_api の package initializer は、PEP 562 の module __getattr__ を使い、最初の公開属性アクセスで定義元 module を import する。__all__、公開名、version、返す class/function の identity を維持し、解決後は package globals に cache する。__dir__ も公開名を返す。worker が target_guard や collector だけを必要とする場合に、未使用の session/service 等を先行 import しない。

collector の http.client は transport constructor 内で必要になった時点で import する。deadline の設定は import より前の元の位置に保つ。依存 module の import 時間も同じ timeout を消費し、timeout を延ばしたり、開始点を後ろへ移したりしない。

### 既知 schema 自体の再検査の省略

bundle_validation._contracts は schema の raw bytes を一度読み、SHA-256 が次の既知値と完全一致する場合だけ schema 自体の check_schema を省略する。

    2bb2483725d3042c3f53e7a44d616c4f949c0f3fe7868e716b6d74ca2eeca996

この bytes が元の完全な check_schema を通ることを追加試験で無条件に検証する。空白だけを含むどの変更でも元の check_schema 経路へ戻る。JSON decode、厳密な整数型、FormatChecker、入力 envelope/payload 検証、capture rule の matrix 一意性、Bundle 検証は維持する。任意の schema を信頼する変更ではない。

## 2. 元 WD02 と公開禁止の整合

起動処理の 4 ファイルを修正した段階の Linux 全件試験では、既存 WD02 だけが失敗した。未変更 FIX009 の別コピーでも同じ失敗を確認した。Chrome 終了による PublicationGate の失効が、元々 METADATA_ONLY と分類された CHROME_EXITED の診断 Bundle まで拒否していた。

承認済み L02 diagnostic_event_matrix の EVT14 は CHROME_EXITED に対し METADATA_ONLY、INVALIDATE、STOP を同時に要求する。元 WD02 は、worker 起動 0、STRUCTURE 不在、S0 保存、metadata PARTIAL の commit を期待する。一方、FIX002/FIX003 の公開線形化契約は通常の publication に失効 veto を設けている。

FIX010 はこの両者の適用範囲を明確にする。既存 PublicationGate.publish と invalidate の本文を変更せず、最初から METADATA_ONLY の job の診断記録だけに別の限定 capability を発行する。後から失敗して metadata へ降格した content-origin の job には適用しない。既存 R2/R3 の公開前失効・atomic rename・公開後障害の否定条件を維持する。

## 3. metadata-only capability の条件

coordinator は次のすべてを満たす場合だけ専用 view を要求する。

- 正確な PublicationGate 型である。
- 元の immutable WorkerRequest の matrix mode が METADATA_ONLY である。
- recovery ではなく、先行 terminal 記録がなく、worker を開始していない。
- permit session の失敗を、この経路で回避していない。

capability 発行時に WorkerRequest を再構成して canonical raw bytes が一致することと元の分類を確認する。encode した Bundle を通常の完全検証へ通し、次の metadata-only 制約を追加する。

- payload は TRIGGER / EVENTS / RESULT のみで、STRUCTURE は存在しない。
- capture_status は PARTIAL または REJECTED、assessment は NOT_PERFORMED、有効な失敗理由がある。
- capture ID、trigger、binding raw/hash、generation、supervisor、policy が元 job と一致する。
- worker は NOT_STARTED、capture 開始時刻と latency は null、S1/S2 は NOT_RUN かつ duration 0。
- identity、sanitization 成功、coverage complete、検証済み report context といった content 成功の主張がない。document marker も null である。

capability は job の全 raw bytes、capture ID、Bundle manifest hash、全 encode 済み blob の fingerprint、保存先の正規絶対パス、発行元 gate に束縛する。同じ capture の再発行は拒否する。shallow copy しても親 gate の同じ消費 state を共有する。

## 4. commit の検証と線形化

EvidenceStore は exact 専用 view 型の場合だけ metadata 専用経路を選ぶ。単なる bool、同じ名前の method を持つ wrapper、通常 gate の metadata_request 指定ではこの経路へ入れない。view.publish(action) 単独は必ず拒否する。

store は通常どおり payload を保存し、保存された actual bytes を読み戻し、完全な Bundle 検証を行う。その actual bytes と元 job、実際の destination が capability の値に一致した場合だけ publish_verified を使う。拒否・不正証明・例外・atomic rename の失敗も 1 attempt を消費し、別 job、別 store、別 blob への試し直しを認めない。

ファイル I/O、fsync、schema/Bundle 検証、hash 計算は親 gate lock の外で行う。lock 内の処理は発行/消費のメモリ更新と、従来の最後の atomic COMMIT rename に限定する。既存の公開後 readback/cleanup 契約を保つ。

診断記録が commit されても、親 gate の invalidated、失効理由、BindingGuard terminal state、stop、worker 起動制限は戻さない。同じ gate に対する通常 publish は引き続き拒否される。metadata の公開はブラウザ操作やコンテンツ取得の許可ではない。

## 5. 検証と適用限界

元のテストと fixture は FIX009 と byte 単位で同一である。追加試験は fix010_validation に置き、既存 pytest.ini の testpaths に追加しない。1519 ID の全件試験と追加試験は別々に記録する。

起動の API 互換性、deadline 消費、schema fallback と不正入力、metadata-only 正例、content-origin/別 job/別 destination/再発行/再使用/不正 blob/rename 障害の否定例、既存 R2/R3 の公開禁止を確認する。実際の件数と結果は TEST_SUMMARY.json と evidence/fix010 の記録を参照する。

Linux の時間測定は同一環境の開発上の比較であり、Windows のプロセス起動、ウイルス対策ソフト、Job Object、native storage の挙動を保証しない。Windows 固定環境での境界診断と uninstrumented 全件実行は引き続き必要である。実ブラウザの製品受入れは NOT_RUN のままとする。

## 6. provenance

親 FIX009 ZIP と外添 Receipt を固定し、親の全ファイルと FIX010 を比較する。変更した 7 製品ファイルを before/after hash と patch で列挙する。元 root metadata は evidence/fix010/baseline_metadata にそのまま保存し、新 root metadata は FIX010 の状態を表す。

INHERITED_FILES は FIX009 相対の変更していない active source を表す。旧 L06 mapping は歴史記録として保存し、現在の全ソースが L06 と同一という主張はしない。継承された全 67 外部原本の再検証や、外部原本の代用をしたという主張もしない。既存 verify_l07_sources.py は歴史的な検証ツールとして保持し、FIX010 の新しい archive/manifest integrity gate と混同しない。
