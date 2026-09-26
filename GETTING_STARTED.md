# はじめての設定手順：昭和池の衛星データ自動更新

この文書は、GitHubの公開リポジトリ `Katsu-Yama/minamisakae_estonia-lake_monitoring` と既存のSupabaseプロジェクト `yczkemhgmibupcdzcabz` を使う場合の手順です。Pythonを自分のパソコンへ入れなくても、GitHub Actionsがクラウド上でスクリプトを実行します。

## まず全体像

1. Google Earth Engineが衛星データを提供します。
2. GitHub Actionsが数日ごとに `syouwa_lake_pipeline.py` を実行します。
3. スクリプトがCSVと直近1年のPNG画像を作り、Supabaseへ保存します。
4. 後で作るWebアプリがSupabaseから表と画像を読みます。**このリポジトリにはWeb画面はまだありません。**

採用できる晴天の観測がない場合は、何も追加せず正常終了します。古いデータは残ります。

## 1. Supabaseを確認

1. [Supabaseダッシュボード](https://supabase.com/dashboard)へログインします。
2. `Katsu-Yama's Project`（ID: `yczkemhgmibupcdzcabz`）を開きます。停止中なら `Resume project` を押し、起動完了まで待ちます。
3. 左側の `Table Editor` で `satellite_observations` と `satellite_ingestion_runs` があることを確認します。
4. 左側の `Storage` で `satellite-images`（Public）と `satellite-csv`（Private）があることを確認します。
5. まだ無い場合は、リポジトリの `supabase_schema.sql` 全文をコピーし、Supabaseの `SQL Editor` → `New query` に貼り付けて `Run` を押します。再度3・4を確認します。

`satellite_observations` はWebから読み取れる観測値、`satellite_ingestion_runs` は管理用の実行記録です。後者は一般公開しません。画像のPublicバケットはURLを知る人が画像を見られる設定です。

## 2. Google Earth Engineの利用資格を確認

1. [Google Cloud Console](https://console.cloud.google.com/)へ、notebookでEarth Engineを利用したGoogleアカウントでログインします。
2. 上部のプロジェクト選択欄で、Earth Engine用のCloudプロジェクトを選びます。表示された**プロジェクトID**を控えます（プロジェクト名や番号ではありません）。元notebookには `fleet-breaker-464111-h5` がありましたが、今も使えるか必ずご自身で確認してください。
3. [Earth Engine登録ページ](https://code.earthengine.google.com/register)で、そのCloudプロジェクトが利用登録済みか確認します。非商用の無料利用が適用できるかどうかはGoogleの審査・条件によります。商用用途なら無料と決めつけないでください。
4. Cloud Consoleの `APIとサービス` → `ライブラリ` で `Earth Engine API` が有効か確認します。
5. `IAMと管理` → `サービス アカウント` → `サービス アカウントを作成` で、この自動実行専用のアカウントを作ります。Earth Engineが必要とする権限（通常はEarth Engine Resource Viewer、必要に応じてService Usage Consumer）をそのアカウントに与えます。詳しくは[Google公式のサービスアカウント案内](https://developers.google.com/earth-engine/guides/service_account)を参照してください。
6. 作成したサービスアカウントの `キー` → `鍵を追加` → `新しい鍵を作成` → `JSON` で鍵ファイルをダウンロードします。このJSONはパスワード同様に扱い、**リポジトリ・Issue・チャットへ貼らないでください**。

Googleの組織設定でJSON鍵の作成が禁止されている場合は、ここで止めて相談してください。別の認証方式に変更する必要があります。

## 3. GitHubへ4つの設定値を登録

1. [このリポジトリ](https://github.com/Katsu-Yama/minamisakae_estonia-lake_monitoring)を開きます。
2. `Settings` → 左側の `Secrets and variables` → `Actions` を開きます。
3. `Variables` タブで `New repository variable` を使い、下表の2件を登録します。

| Name | Value |
| --- | --- |
| `GEE_PROJECT_ID` | 手順2で確認したGoogle CloudプロジェクトID |
| `SUPABASE_URL` | `https://yczkemhgmibupcdzcabz.supabase.co` |

4. `Secrets` タブで `New repository secret` を使い、下表の2件を登録します。

| Name | Value |
| --- | --- |
| `GEE_SERVICE_ACCOUNT_JSON` | 手順2でダウンロードしたJSONファイルの**中身を全文**（最初の `{` から最後の `}` まで） |
| `SUPABASE_SECRET_KEY` | Supabaseプロジェクトの `Settings` → `API Keys` にあるサーバー用の **secret key** (`sb_secret_...`)。無い場合は作成してください |

「ファイル名」や「ファイルのパス」を `GEE_SERVICE_ACCOUNT_JSON` に入れても動きません。JSON本文を入れます。Supabaseの **publishable key** (`sb_publishable_...`) は書き込み用ではないため、`SUPABASE_SECRET_KEY` へ入れません。逆にsecret keyをWebのHTML/JavaScriptへ入れないでください。

GitHubのSecretsは登録後に値を表示し直せません。間違えた場合は同じ名前で更新します。

## 4. まず手動で1回実行して確認

1. GitHubリポジトリの `Actions` タブを開き、必要ならActionsの利用を有効にします。
2. 左側の `Update Syouwa Lake satellite data` を選び、`Run workflow` を押します。
3. 最初は開始日と終了日を**直近1か月程度**にして実行します。日付は `YYYY-MM-DD` 形式です。両方空欄なら直近21日を確認します。
4. 開始された実行（丸いアイコンの行）を開き、`update` → `Process and upload satellite data` のログを確認します。緑色のチェックなら処理は成功です。
5. Supabaseの `Table Editor` → `satellite_ingestion_runs` で、`success` または `no_data` の記録を確認します。`no_data` は雲などで採用画像が無かったことを意味し、失敗ではありません。
6. `success` の場合は `satellite_observations` に行が増え、`Storage` の2バケットにCSVと新しい画像があることも確認します。

赤色の×ならログの最後のエラーを確認します。`GEE_PROJECT_ID`、Earth Engineの登録・権限、JSON全文、Supabaseのsecret key、プロジェクト停止を順に見直してください。秘密鍵の中身をスクリーンショットや質問文に含めないでください。

## 5. 自動実行と過去データ

GitHub Actionsは日本時間の**3日おき相当、12:17**に起動する設定です。実際の開始時刻はGitHubの混雑などで遅れることがあります。空振りの日も既存データは保持されます。同じ衛星シーンは `scene_id` で上書きされ、二重登録されません。

過去5年のグラフを作るには、過去の各期間について手順4の手動実行を繰り返します。最初から5年を一度に指定せず、まず1か月、問題がなければ数か月ずつ実行してください。古い観測値はCSV/DBへ保存しますが、**実行日から365日より古い画像は作りません**。これで画像容量を抑えます。

現在のコードは古くなった画像のStorageからの自動削除は行いません。Webアプリ側では画像を直近1年だけ検索し、CSVのグラフは過去5年だけ検索する予定です。

## 無料運用で注意すること

- Earth Engineの無料利用は、Googleの**非商用資格**と利用枠の範囲内に限られます。
- Supabase無料プロジェクトは利用が少ないと停止する場合があります。停止するとWebの読み込みもGitHub Actionsからの保存もできません。停止通知メールとプロジェクト状態を時々確認してください。
- GitHubの公開リポジトリでは、長期間リポジトリ活動がないと定期ワークフローが自動停止されることがあります。`Actions` タブとGitHub通知を時々確認してください。
- 無料枠・サービス仕様は変更され得るため、**費用ゼロと無停止を保証する構成ではありません**。

現在は衛星データの取得・保存までです。カレンダー、最寄り画像選択、5年グラフを並べるWeb画面は次の作業で実装します。
