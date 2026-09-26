# 昭和池 Sentinel-2 自動処理パイプライン

初めて設定する場合は、先に [初心者向け設定手順](GETTING_STARTED.md) を読んでください。

`Syouwa_lake.ipynb` のうち、定期運用に必要な処理を非対話型Pythonへ移したものです。

## 実行内容

1. Google Earth EngineからSentinel-2 SRとCloud Probabilityを取得
2. SCLとCloud Probabilityで雲・雲影・薄雲・雪氷を除外
3. 昭和池の固定ROIから水域マスクを生成
4. 水域上の欠損率が30%以下、有効率が30%以上の画像だけを採用
5. NDCI、NDTI、FAI、水域有効面積を計算
6. 観測値CSVとNDCI PNGを生成
7. Supabase StorageへCSV/PNGをupsert
8. `satellite_observations`へ観測値とStorageパスをupsert

採用画像が0枚の場合は障害ではないため、終了コード0で完了し、`satellite_ingestion_runs`へ`no_data`を記録します。

## notebookから変更した点

- `google.colab`、`display()`、手動の`ee.Authenticate()`を削除
- GitHub Actions用サービスアカウント認証へ変更
- 固定期間ではなく、通常は直近21日を再確認
- 同一`scene_id`をupsertし、再実行しても重複させない
- 月2枚の表示用抽出ではなく、条件を満たす観測をすべて保存
- 気象CSVと実験用RandomForest/XGBoost部分は定期取得処理から分離

## 事前準備

### Google Earth Engine

- Google CloudプロジェクトでEarth Engine APIを有効化
- プロジェクトをEarth Engine利用登録
- サービスアカウントへ必要なEarth Engine権限を付与
- JSONキーはGitへ保存せず、GitHub Secret `GEE_SERVICE_ACCOUNT_JSON`へ登録

### Supabase

1. `supabase_schema.sql`をSQL Editorで実行
2. SQL内で次のStorageバケットも作成されます
   - `satellite-images`：Webで画像を一般公開する場合はPublic
   - `satellite-csv`：Private推奨
3. GitHubへ次を登録
   - Repository Variable `SUPABASE_URL`
   - Repository Secret `SUPABASE_SECRET_KEY`

ブラウザ側へ渡すのはPublishable keyだけです。Secret keyはHTML/JavaScriptへ入れません。

### GitHub

Repository Variables:

- `GEE_PROJECT_ID`
- `SUPABASE_URL`

Repository Secrets:

- `GEE_SERVICE_ACCOUNT_JSON`
- `SUPABASE_SECRET_KEY`

ワークフローは3日ごとの12:17（日本時間）に起動します。手動実行時は開始日・終了日を指定できます。

## ローカル実行

Python 3.12を使用します。

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --requirement requirements.txt
python syouwa_lake_pipeline.py --skip-supabase --start-date 2025-01-01 --end-date 2025-01-31
```

環境変数は`.env.example`を参考に設定してください。このスクリプトは`.env`を自動読込しないため、PowerShellまたはGitHub Actionsから渡します。

## 主な調整値

- `CLOUD_OVER_WATER_THRESHOLD`：水域上欠損率の上限。既定30%
- `MIN_VALID_RATIO`：水域の最低有効率。既定0.3
- `MNDWI_THRESHOLD`：水域マスク。既定0.05
- `WATER_MASK_START_DATE` / `WATER_MASK_END_DATE`：水域マスク参照期間
- `MAX_IMAGES_PER_RUN`：1回の最大処理枚数。0は無制限
- `THUMBNAIL_SCALE_METERS`：PNG解像度。既定10m

初回の過去データ登録はActionsの手動実行で期間を小分けにしてください。Earth EngineのクォータとActionsの時間制限を避けるため、最初は1か月、その後も短い期間ごとに進めてください。過去5年の観測値は保存しますが、画像は取得日が実行日から365日以内のものだけを作成します。古い画像の自動削除は未実装です。

