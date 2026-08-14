# Manus Research Pipeline

「人生のレシピ」の台本・サムネイル分類を、Manus API v2とBigQueryで校正・蓄積するためのパイプラインです。

## 主要ファイル

- `TAXONOMY_V1.md`: 分類方針
- `classification_definitions_v1.json`: P型・Rカードの判定定義
- `story_schema_v1.json`: 台本分類の出力スキーマ
- `thumbnail_schema_v1.json`: サムネ分類の出力スキーマ
- `build_calibration_set.py`: 再生実績5分位から校正25本を作成
- `analyze_calibrated_performance.py`: 人間校正済み分類へYouTube Analyticsとリーチ指標を結合し、構造別実績をCSV・JSON・BigQueryへ保存
- `analyze_thumbnail_performance.py`: Manusのサムネイル視覚分類を校正25本の実績へ結合し、P03B・P06B別に集計
- `import_own_scripts_to_bq.py`: 完成台本・既存分類・校正セットをBigQueryへ接続
- `manus_pipeline.py`: Manusへ順次送信し、最新ターンのJSONを品質検証して保存

## 実行環境

```powershell
$python = 'E:\Data\ObsidianVault\04_Tools\envs\senior_reading\Scripts\python.exe'
```

APIキーはWindowsユーザー環境変数 `MANUS_API_KEY` から読みます。標準出力やBigQueryへキーを書きません。

## 初期化・更新

```powershell
& $python .\manus_research\build_calibration_set.py
& $python .\manus_research\analyze_calibrated_performance.py
& $python .\manus_research\analyze_thumbnail_performance.py
& $python .\manus_research\import_own_scripts_to_bq.py
```

## 1件ずつ実行

現在の無料個人アカウントでは、新規 `task.create` が実体化しない場合があるため、正常動作を確認済みの会話へ `task.sendMessage` で順次送信します。同時実行はしません。

```powershell
& $python .\manus_research\manus_pipeline.py run --task-type classify_story --limit 1 --profile manus-1.6
& $python .\manus_research\manus_pipeline.py run --task-type classify_story_extension --limit 1 --profile manus-1.6
& $python .\manus_research\manus_pipeline.py run --task-type classify_thumbnail --limit 1 --profile manus-1.6
```

無料個人アカウントでは指定プロファイルがLiteへ降格される可能性があります。保存済みの `agent_profile` は要求値であり、実効モデルを保証しません。

## 品質ゲート

- 最新送信時刻より前のイベントを採用しない。
- 結果JSON内の `video_id` が投入対象と一致しない場合は自動的に `needs_review` へ落とし、別動画の応答混入を保存済み正解として扱わない。
- 未回収の `running` キューがある間は新規送信を拒否する。ローカル処理が中断した場合は、再送せず先に `poll` で既存結果を回収する。
- 長尺台本はメッセージ本文へ埋め込まず、UTF-8テキストの `file_data` 添付として渡す。Manusの本文約5,000推定トークン制限を避け、全文を保持する。
- 最新ターンの本文JSONを優先し、必須キーが揃わない場合だけStructured Outputへフォールバックする。
- 4スコアは整数0〜10。confidenceは0〜1へ正規化する。
- evidence、主要配列、スコア、confidenceが空または異常なら `needs_review` にする。
- P型・Rカード・料理役割を人間補正後の既存分類と比較し、不一致を `manus_calibration_review_v1` へ出す。

## BigQuery

- `research_data_coverage`: 動画単位のデータ充足率
- `research_processing_queue`: 文字起こし・台本分類・サムネ分類の優先キュー
- `research_calibration_cases`: 校正25本
- `research_calibration_extension_candidates`: 動画ID未接続の不足P型追加校正候補
- `manus_calibration_extension_review_v1`: 不足P型追加校正の一致・不一致レビュー
- `combined_calibration_metrics.sql`: 動画25本と台本拡張5本の統合集計
- `own_script_structure_classifications`: AI初回値と人間補正後有効値
- `manus_task_runs`: Manus送受信記録
- `manus_classification_results`: 正規化済み分類結果
- `manus_calibration_review_v1`: 既存分類との一致・不一致レビュー
- `manus_calibration_performance_v1`: 人間校正済み25本と最新の再生・視聴維持・CTR・登録転換を結合した実績表
- `manus_calibration_thumbnail_performance_v1`: 校正25本のサムネイル視覚分類と実績の結合表

## 注意

- 校正25本を一括投入しない。1件完了・品質確認後に次を送る。
- 処理順はキューのpriorityを最優先し、同一priority内で完了数の少ない既存P型・Rカードを優先する。既存分類そのものはManusへ送らない。
- 再生数、CTR、維持率、既存分類はManusの盲検入力へ入れない。
- 公開サイト生成処理は `sync_target=self` を対象外のままにし、自チャンネル内部情報を公開しない。
