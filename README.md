# TQQQ（iPhone用ウェブアプリ）

TQQQのチャート（日足・週足・月足、通常／対数、指数近似）、月次騰落率の分布、保有年数別のリターン（QQQ・QLDと比較）を表示します。
GitHub Pages で公開し、GitHub Actions が米国市場の取引時間中に約15分ごとに Nasdaq.com からデータを更新します（Nasdaq.com は直近10年分のため、それより前の履歴は初期データとして data/tqqq.json に入っており、新しい日が毎回追記されます）。

| ファイル | 役割 |
|---|---|
| `index.html` | アプリ本体 |
| `manifest.webmanifest` / `sw.js` / `icons/` | ホーム画面アプリ化とオフライン表示 |
| `d3.min.js` | 描画ライブラリ |
| `data/tqqq.json` | 価格データ（初期データ入り。Actions が毎回追記・更新します。手で上書きしないでください） |
| `data/status.json` | 最後の更新の成否 |
| `scripts/update_tqqq.py` | Nasdaq.com からデータを取得して追記するスクリプト |
| `.github/workflows/update-data.yml` | 15分ごとの自動実行の設定 |

URL：`https://limelightle-debug.github.io/tqqq/`
