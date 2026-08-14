# Manus Research Pipeline

「人生のレシピ」の台本・サムネイル分類を、Manus API v2とBigQueryで校正・蓄積するためのパイプラインです。

## 主要ファイル

- `TAXONOMY_V1.md`: 分類方針
- `classification_definitions_v1.json`: P型・Rカードの判定定義
- `story_schema_v1.json`: 台本分類の出力スキーマ
- `thumbnail_schema_v1.json`: サムネ分類の出力スキーマ
- `title_batch_schema_v1.json`: 校正25本のタイトル盲検一括分類スキーマ
- `title_thumbnail_alignment_schema_v1.json`: タイトルとサムネイルの約束整合性スキーマ
- `build_calibration_set.py`: 再生実績5分位から校正25本を作成
- `analyze_calibrated_performance.py`: 人間校正済み分類へYouTube Analyticsとリーチ指標を結合し、構造別実績をCSV・JSON・BigQueryへ保存
- `analyze_thumbnail_performance.py`: Manusのサムネイル視覚分類を校正25本の実績へ結合し、P03B・P06B別に集計
- `classify_titles_batch.py`: タイトル25本の盲検分類と、サムネイルとの一致・補完・食い違い判定を2段階で実行
- `analyze_title_alignment_performance.py`: タイトル分類・サムネイル整合性・台本構造・YouTube実績を結合して集計
- `fetch_related_video_sources.py`: 校正25本ごとに関連動画の参照元をYouTube Analyticsから取得し、現在表と履歴表へ保存
- `analyze_related_video_network.py`: 関連動画総流入・開示参照元・自動画回遊・外部チャンネル橋渡しを台本構造と実績へ結合
- `import_own_scripts_to_bq.py`: 完成台本・既存分類・校正セットをBigQueryへ接続
- `manus_pipeline.py`: Manusへ順次送信し、最新ターンのJSONを品質検証して保存
- `manus_autopilot.py`: Freeの同時実行1枠を止めず、全タスク種別からpriority最高の1件を順次処理
- `fetch_pending_transcripts.py`: Manusを使わずYouTube字幕を取得し、BigQuery・処理キュー・Vaultアーカイブへ反映
- `sync_thumbnail_text_overrides_to_bq.py`: 既存サムネ文字overrideをBigQuery OCR表へ統合し、再OCRを防止

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
& $python .\manus_research\classify_titles_batch.py
& $python .\manus_research\analyze_title_alignment_performance.py
& $python .\manus_research\fetch_related_video_sources.py
& $python .\manus_research\analyze_related_video_network.py
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

## 自動連続運転

Freeプランは同時実行1件のため、並列送信せず、現在の実行を回収してから次のpriority最高案件を送ります。台本とサムネイルを横断して選ぶため、現在は「人生のレシピ」台本（priority 95）→同サムネイル（90）→競合台本（70）→競合サムネイル（65）の順です。

```powershell
& $python .\manus_research\manus_autopilot.py --profile manus-1.6
```

- `manus_research/STOP_MANUS_AUTOPILOT` を作ると、実行中の1件を回収してから停止する。
- `logs/manus_autopilot_status.json` に現在状態、`logs/manus_autopilot.jsonl` にイベント履歴を保存する。
- OSロックにより、手動実行とタスクスケジューラの二重起動を拒否する。
- 一時的なAPIエラーや日次制限は指数バックオフし、BigQueryで `running` の案件を先に回収する。
- 全必須項目を含む完成JSONの後に2分以上イベントがなく `running` が残る場合は、Structured Output抽出停滞とみなし、タスクを安全停止して本文JSONを回収する。

## 文字起こし欠損の回収

自チャンネルの欠損を先に回収する例:

```powershell
& $python .\manus_research\fetch_pending_transcripts.py --self-only --limit 20
```

取得結果はBigQueryへMERGEし、`research_data_coverage` とキューを更新する。根拠字幕は `02_Channels/シニア朗読/analysis/transcript_archive/YYYYMMDD/` にJSON・TXTで保存する。

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
- `manus_title_calibration_v1`: 校正25本のタイトル分類
- `manus_title_thumbnail_alignment_v1`: 校正25本のタイトル・サムネイル整合性判定
- `manus_title_thumbnail_performance_v1`: タイトル・サムネイル整合性と台本・実績の結合表
- `youtube_related_video_edges_current_v1`: 最新取得分の対象動画→参照元動画エッジ
- `youtube_related_video_edges_history`: 時点別に追記する関連動画エッジ履歴
- `youtube_related_target_summary_v1`: 動画別の関連流入比率と開示ネットワーク型
- `youtube_related_source_centrality_v1`: 参照元動画の送客中心性
- `youtube_related_external_channels_v1`: 外部参照元チャンネル集計
- `youtube_related_structure_transitions_v1`: 校正済み自動画間のP型遷移
- `youtube_related_video_edges_enriched_v1`: 台本・タイトル構造を付与した参照元エッジ

## 注意

- 校正25本を一括投入しない。1件完了・品質確認後に次を送る。
- 処理順はキューのpriorityを最優先し、同一priority内で完了数の少ない既存P型・Rカードを優先する。既存分類そのものはManusへ送らない。
- 再生数、CTR、維持率、既存分類はManusの盲検入力へ入れない。
- 公開サイト生成処理は `sync_target=self` を対象外のままにし、自チャンネル内部情報を公開しない。

## 重点チャンネルの保全

- `priority_archive_channels.json`: 3時間監視の対象設定。初期対象は「人生は贈り物」。
- `archive_priority_channels.py`: 公開状態・メタデータ・サムネイル・文字起こしをローカルとBigQueryへ保全する。
- `run_priority_archive.ps1`: Windowsタスクスケジューラ用の実行・ログ保存ラッパー。

BigQuery出力:

- `priority_channel_videos_current_v1`
- `priority_channel_video_snapshots_history`
- `priority_channel_availability_events`
- `priority_channel_videos_latest_v1`
- `priority_channel_availability_changes_v1`
