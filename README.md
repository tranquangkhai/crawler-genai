# crawler-chatgpt

Python からブラウザを直接制御し、人間に近い挙動で ChatGPT にプロンプトを投げて回答を取得、目視タイミングと DevTools 互換のネットワーク Timing (Request sent / Waiting / Content Download) を CSV に記録するクローラ。

## 特徴

- **Playwright / Selenium を使わない**: `websockets` + `aiohttp` だけで Chrome の CDP / Firefox の WebDriver BiDi を直接叩く
- **Chrome / Firefox 両対応**: `--browser` フラグで切り替え。同じ CSV スキーマに正規化
- **人間らしい入力**: 1 文字ずつ + ランダムジッター (50〜200ms、句読点後はさらに +150〜300ms)
- **目視と一致するタイミング**: `first_byte` / `answer_done` は DOM 観測ベース (人間が画面で見る瞬間と一致)
- **DevTools 互換のネットワーク値**: 各ブラウザの timing オブジェクトから DevTools の "Timing" タブと同じ値を計算
- **OKTA SSO 対応**: ブラウザ別の専用プロファイルに初回手動ログイン → 以降セッション再利用
- **Bot 検知回避**: 起動フラグに警告を出さず、`navigator.webdriver` を `undefined` に上書き
- **クロスプラットフォーム**: macOS / Windows / Linux で動作

## セットアップ

```bash
pip install -r requirements.txt

# 初回のみ: Chrome を起動して OKTA 手動ログイン (default)
python crawler.py --setup

# Firefox を使う場合
python crawler.py --setup --browser firefox
```

`--setup` は専用プロファイルを作成し (`~/chrome-chatgpt-profile` または `~/firefox-chatgpt-profile`)、ブラウザを起動して終了します。ブラウザ自体は起動したまま残るので、OKTA で手動ログインしてください。Cookie がプロファイルに保存され、次回以降は自動でログイン済み状態になります。

## 使い方

```bash
# Chrome (default)
python crawler.py "日本の首都はどこですか？"

# Firefox
python crawler.py --browser firefox "日本の首都はどこですか？"
```

毎回 `https://chatgpt.com/` (新規チャット) に遷移してからプロンプトを送るので、過去の会話に追記されることはありません。

- 既に `--setup` で起動したブラウザがあればそのタブを再利用
- 無ければ自動で起動
- 結果は `results.csv` に 1 行追記 (`browser` 列で chrome/firefox を識別)

## 出力 (`results.csv`)

### 識別・メタデータ

| 列 | 意味 |
|---|---|
| `run_id` | 実行ごとの UUID4 |
| `timestamp_iso` | 実行起点の時刻 (ISO 8601, μs 精度) |
| `browser` | `chrome` または `firefox` |
| `prompt` | 投入したプロンプト (改行は `\n` にエスケープ) |

### 入力 (人間操作) のタイミング — 実時計

| 列 | 計測点 |
|---|---|
| `typing_start` | 1 文字目を `Input.insertText` する直前 |
| `typing_end` | 最後の文字を入力し終えた直後 |
| `enter_pressed` | Enter キーイベント発火直前 |

### 可視タイミング — 目視ベース (DOM 観測)

| 列 | 計測点 |
|---|---|
| `first_byte` | assistant バブルの `textContent` が初めて空でなくなった瞬間 (画面に最初の文字が出た時) |
| `answer_done` | stop indicator (stop ボタン / `.result-streaming` クラス / `aria-busy` のいずれか) が消えた瞬間 (画面で「回答終了」と分かる時) |

### ネットワークタイミング — ブラウザイベント時刻 (参考)

| 列 | Chrome (CDP) 由来 | Firefox (BiDi) 由来 |
|---|---|---|
| `net_response_received` | `Network.responseReceived` | `network.responseStarted` |
| `net_loading_finished` | `Network.loadingFinished` (届かなければ最後の `dataReceived`) | `network.responseCompleted` |

### DevTools "Timing" タブ互換のメトリクス (ms)

| 列 | Chrome (CDP) | Firefox (BiDi) |
|---|---|---|
| `request_sent_ms` | `sendEnd - sendStart` | **常に 0** (BiDi に send 専用フェーズが無い) |
| `waiting_ms` | `receiveHeadersEnd - sendEnd` | `responseStart - requestStart` (TTFB) |
| `content_download_ms` | `(loadingFinished or last dataReceived) - (requestTime + receiveHeadersEnd)` | `responseEnd - responseStart` |
| `total_ms` | `(loadingFinished or last dataReceived) - (requestTime + sendStart)` | `responseEnd - requestStart` |

Firefox は BiDi 仕様上 `request_sent_ms` を独立計測できません (送信フェーズの値が `waiting_ms` 側に含まれる)。Chrome の方が DevTools の "Timing" 表示と完全一致します。

### HTTP・補足

| 列 | 意味 |
|---|---|
| `http_status` | HTTP ステータス (通常 200。429 等で異常検知) |
| `request_id` | CDP requestId (`responseReceived` / `dataReceived` / `loadingFinished` の相関キー) |
| `notes` | セミコロン区切りの補足: `url=...`, `done-by=stop-button` 等, `finish-source=last-dataReceived`, `failed=...`, `diag={...}` |

## 終了条件

| 種類 | 条件 |
|---|---|
| **正常完了** | stop indicator (stop ボタン / `.result-streaming` / `aria-busy`) が消えた瞬間 |
| 強制終了 | 90 秒タイムアウト (`ANSWER_TIMEOUT_SEC`) |

stop indicator は **broad heuristic** で検出 (button の `aria-label` / `data-testid` に "stop" / "止め" / "停止" / "streaming" / "ストリーミング" を含む、または `.result-streaming` クラス、`aria-busy="true"` 属性)。検出できないバージョンでは `notes` 列の `diag={...}` JSON を見て selector を `crawler.py` の `ASSISTANT_SELECTORS` 等に追加してください。

## 対応 OS と実行ファイルの検索パス

OS 自動判定でブラウザ実行ファイルを自動検索します。

### Chrome

| OS | 検索順 |
|---|---|
| **macOS** | `/Applications/Google Chrome.app/...` → `~/Applications/Google Chrome.app/...` |
| **Windows** | `%ProgramFiles%\Google\Chrome\Application\chrome.exe` → `%ProgramFiles(x86)%\...` → `%LOCALAPPDATA%\...` |
| **Linux** | `which google-chrome` / `chromium` → `/usr/bin/google-chrome*`, `/usr/bin/chromium*`, `/snap/bin/chromium` |

### Firefox

| OS | 検索順 |
|---|---|
| **macOS** | `/Applications/Firefox.app/Contents/MacOS/firefox` → `~/Applications/Firefox.app/...` |
| **Windows** | `%ProgramFiles%\Mozilla Firefox\firefox.exe` → `%ProgramFiles(x86)%\...` |
| **Linux** | `which firefox` / `firefox-esr` → `/usr/bin/firefox*`, `/snap/bin/firefox` |

プロファイル保存先は `~/chrome-chatgpt-profile` または `~/firefox-chatgpt-profile`、全 OS 共通で `Path.home()` 配下です。子プロセスは Python 終了後も残るよう OS ごとにデタッチ (Windows: `DETACHED_PROCESS`, Unix: `start_new_session`)。

### Firefox の前提

- **Firefox 113+** が必要 (WebDriver BiDi のサーバ機能)
- 現行 Firefox (129+) は CDP をサポートしないので BiDi が必須
- `--remote-debugging-port=9222` で BiDi WebSocket サーバを起動 → `ws://localhost:9222/session` に接続

## トラブルシュート

| 症状 | 対処 |
|---|---|
| ログインページに飛ばされる | OKTA セッション失効。`python crawler.py --setup` で再ログイン |
| `prompt 入力欄が見つかりません` | ChatGPT の UI 変更。`crawler.py` の `focus_prompt_input` 内のセレクタを更新 |
| `done-by=` が `notes` に出ない / 90 秒待つ | stop indicator が検出できていない。`notes` の `diag={...}` を見て selector を追加 |
| `failed=loadingFinished not seen (using last dataReceived)` | SSE が長く繋がっているだけ。値は `Network.dataReceived` 最終チャンクで代用済み (実用上問題なし) |
| ポート 9222 使用中 | 既存 Chrome に attach するので通常は問題なし。別プロファイルで占有している場合はそちらを終了 |
| Windows でコマンドプロンプトが別ウィンドウに残る | `DETACHED_PROCESS` で意図的に分離している (Chrome が独立して残るための設計) |

## 動作の検証

実行後、ブラウザの DevTools (F12) を開いて Network タブで `f/conversation` を選択 → "Timing" を見ると、`request_sent_ms` / `waiting_ms` / `content_download_ms` の値と CSV の値が ±数 ms 以内で一致することを確認できます。これは両者が同じ CDP `response.timing` を参照しているためです。

## ファイル構成

```
crawler-chatgpt/
├── crawler.py             # CLI、orchestration、CSV 出力
├── browser.py             # Browser 抽象 + Chrome (CDP) + Firefox (BiDi) 実装
├── requirements.txt       # websockets, aiohttp
├── results.csv            # 実行ログ (初回実行時に生成、以降追記)
└── README.md              # このファイル

~/chrome-chatgpt-profile/  # Chrome 用 OKTA ログイン済みプロファイル
~/firefox-chatgpt-profile/ # Firefox 用 OKTA ログイン済みプロファイル
```

## 前提

- Python 3.10+
- Google Chrome (Chromium / Edge も CDP 互換だが未検証) **または** Firefox 113+
