# GitHub公開版：整理・削除結果

作成日：2026-10-30（JST）。元STATE007ファイルは変更していません。

## 再構成方針

- 最新ソース：STATE007内 `references/L08_IMPL001_FIX001.zip` のソース/テスト/契約を展開、必要部分を採用（原productコードのbyte改変なし）。
- `code/` と `packages/` は**旧STATE007直下のL03コードではなく** L08 FIX001の製品ソース。
- 過去の評価ZIP、ログ、履歴、管理Evidence、source原本入出力、Manifest/Receiptチェーン、`.pyc`/cacheを除去。
- `approved_l01`/`references/approved_l02`から設計・Schema・テストのみ保持。追加した `inputs/` は合成fixtureの2ファイルに限定。
- パッケージREADMEの**リンク行のみ**公開版に調整。元の製品実装（Python、PowerShell、C#、JS）、契約、テスト、設計本文のbytesは保存。
- 直下の元管理JSON群はGitHub版には複製せず、限定的に `PROJECT_STATUS.md`へ要約。
- 人名を含む過去の意思決定記録（`docs/adr/`）も、今回の一般公開用ソースから除外。

## 検証と制限

- 元STATE007 ZIP SHA-256: `fdaa982717e4f0f0a86f0a44274c0a5aaeea5fe7029c30c7ffe74639684eff04`。
- 元STATE007 root Manifest **1,795ファイル**の完全一致・全digestを確認。
- 元内包L08 FIX001のroot Manifest **17,332ファイル**の完全一致・全digestを確認。
- 公開用には新しい `MANIFEST_SHA256.txt`と外添の`DELIVERY_RECEIPT.json`を作成。元receipt／source gateと同等ではない。
- 独立原本33/37のsource gateや本番/native受入は、この再包装で実行・継承しない。
- 元ZIP全体の完全な機密検査と、本公開選択ファイルだけの機械走査は別検査。
- 動的テスト結果がある場合は本ファイルの末尾のテスト節を参照（後から追記する場合はManifest/ZIPを再生成）。

## 組織・権利

GitHub公開権限、ライセンス・第三者コードの再配布許諾、およびサイト利用条件は、機械的削除だけで解決されません。公開には権利者の確認が必要です。**Licenseをこちらで推定して付与していません。**

## GitHub公開用の追加静的検査（公開ファイルのみ）

- 実在する`C:\Users\<username>`・`/home/<username>`型パス、実際の個人名と考えられる決定者表記、主要クラウドAPI秘密・PRIVATE KEYブロック：検出なし。
- テストコードに含まれる`TOP_SECRET_QUERY_TOKEN`、`SYNTHETIC_CANARY`、`user:pass@127.0.0.1`は**意図した合成テスト値**であり、実認証情報ではない。
- Windowsのサンプルパス`C:\CDPD\...`は作業手順の例であり、実ユーザーディレクトリを含まない。
- 未知形式の機密、著作権侵害、安全性・脆弱性の不存在を保証する検査ではない。

## 公開版に対する独立した動作点検（元Assessmentとは別）

実施環境は本作成環境（Linux / Python / Node）で、ブラウザ実接続・Windows nativeは試していません。

| テスト集合 | 結果 | 備考 |
|---|---|---|
| `packages/chatgpt-web-api/tests` | **184 PASS / 7 SKIP** | syntheticのみ。サイトへの実接続なし |
| `tests/test_l08_report_engine.py` と `tests/test_l08_scheduler_script.py` | **121 PASS** | offline/synthetic |
| `pytest -x`で先頭から検証（全体） | **452 PASS / 1 FAILの時点で停止** | `tests/test_l06_boundaries.py::test_exact_fixed_script_traverses_main_open_shadow_and_stays_bounded[normal-1000]` で `KeyError: 'profile'`。欠落ファイルが原因か、元からの非整合かは**未断定**。 |

全体テストは**未PASS**であり、公開版のコードをテストに合わせて改変していません。元のSTATE007/L10限定受入とは異なる、新規抽出スナップショットの検査結果です。

## 公開版Manifest

`tools/verify_public_manifest.py` は、展開した本公開版の配布ファイル全件（ハッシュ/過不足）をstdlibで検証するための追加Toolです。元のSTATE007 verifierやSOURCE_LOCKに対する代用品ではありません。

```powershell
python -I -B tools/verify_public_manifest.py --root .
```
