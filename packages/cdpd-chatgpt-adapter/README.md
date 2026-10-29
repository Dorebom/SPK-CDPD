---
id: CDPD-PACKAGE-CDPD-CHATGPT-ADAPTER
title: Optional metadata instrumentation
project: CDPD
type: readme
status: candidate
version: 0.2.1
updated: '2026-10-17'
tags:
- cdpd
- readme
---

# Optional metadata instrumentation

`InstrumentedFacade`と`BoundedEventQueue`。操作本文/結果本文を診断側へコピーせず、呼出結果を変えない。自動capture workerは未実装。利用者がqueueをdrainしてCDPDへ渡す。

[公開版README](../../README.md) / [公開版の進捗と限界](../../PROJECT_STATUS.md) / [設計・利用資料](../../docs/README.md)
