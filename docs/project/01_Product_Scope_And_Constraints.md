---
id: CDPD-LITE-SCOPE
title: "ChatGPT専用スコープ"
project: CDPD-CHATGPT-ONLY
type: scope
status: PLANNED
version: 0.3.0-plan
updated: 2026-10-17
---


# 製品スコープ・非目標

**診断対象:** Windows 11の専用Chrome/ChromiumプロファイルからCDPで参照可能な`https://chatgpt.com`のメインページ。CDPDはSHIRABE非依存、対象サイトはChatGPT Webに固定。

## 運用上の制約（簡素化の要）
1. Chromeは1インスタンス、専用 `--user-data-dir`。普段使いのChromeプロファイルへ接続しない。
2. 1運用時点でChatGPTタブ1枚。対象0枚・2枚以上では `TARGET_AMBIGUOUS/UNBOUND` を返し、操作も採取もしない。
3. loopbackのみ（`http://127.0.0.1:9222`）。`--remote-debugging-address`によるLAN公開は禁止。
4. 操作前に `target_id + browser_epoch + expected_origin` を固定し、フォーカス/URL/末尾タブによる再選択は禁止。
5. タブ切替・再生成、Chrome再起動では現在Bindingを失効させる。明示的な次のPreflightまで採取しない。
6. 読取範囲はv0.2.1の**main frame + open shadow**の制限を出発点とする。closed shadow、cross-origin iframe、全frameは保証しない。
7. 収集はタグ・階層・許可状態などの構造情報に限定。本文・入力値・Cookie・トークン・Networkレスポンス本文・Raw DOM全体は収集しない。
8. 自動送信・再送・自動修復・Baseline自動承認・本番登録は実施しない。

## 非目標
Firefox、Safari、他サイト、複数Target、複数OS、REST/ネットワークdaemon、汎用Plugin SDK、任意JS実行API、HAR/スクリーンショット標準採取、全ブラウザバージョン保証、SHIRABEのWORK/Stable管理の改修。

## 品質上の限界
Chromeが停止すれば停止時点のDOMは新規取得不能。S0のみの部分証拠として扱う。`capture_status=COMPLETE`は証拠採取の成功であり、ChatGPT送信・SHIRABE WORK成功を意味しない。

## 利用条件
ChatGPT Webへのプログラム操作・自動取得はサービス条件と利用権限の確認が必要。開発とCIは自作合成Web UIを標準とする。許可未確認の実サービス送信は実行しない。

## 外部技術の参照
- https://developer.chrome.com/blog/remote-debugging-port （Chrome136+の専用user-data-dir）
- https://chromedevtools.github.io/devtools-protocol/tot/Target/ （Target ID/Discovery）
- https://playwright.dev/python/docs/api/class-browsertype （CDP既存Chromeへの接続。Playwright本来の接続より制限あり）
