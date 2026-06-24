# Copy Fallback Search 2026-06-24

## Scope

対象は、サムネイル未取得として残っていた以下6チャンネルの45動画。

- 人生の温もり: 16
- 人生は七色: 12
- 人生は宝物: 11
- 人生の縁側: 4
- 人生のヒカリ: 1
- 人生の糸: 1

過去の「2ch馴れ初め」著作権侵害申告フローに合わせ、既存DB内の類似検索ではなく、YouTube上で新規検索した。

## Method

- 参照手順:
  - `E:\Data\ObsidianVault\02_Channels\馴れ初めシネマ\context\COPYRIGHT_REMOVAL_FAST_START_20260624.md`
  - `E:\Data\ObsidianVault\02_Channels\馴れ初めシネマ\context\YOUTUBE_COPYRIGHT_REMOVAL_WORKFLOW_20260621.md`
- 検索方法:
  - BigQueryから対象45件のタイトルを取得
  - `yt-dlp ytsearch` でYouTubeを新規検索
  - タイトル正規化後の完全一致・包含・類似一致を候補化
- 実行Python:
  - `E:\Data\ObsidianVault\04_Tools\envs\senior_reading\Scripts\python.exe`

## Outputs

強め検索:

- `reports\copy_fallback_search_20260624_145522\copy_fallback_source_targets.csv`
- `reports\copy_fallback_search_20260624_145522\copy_fallback_candidates.csv`
- `reports\copy_fallback_search_20260624_145522\copy_fallback_candidate_channels.csv`
- `reports\copy_fallback_search_20260624_145522\copy_fallback_search_log.csv`

広め検索:

- `reports\copy_fallback_search_20260624_145727\copy_fallback_source_targets.csv`
- `reports\copy_fallback_search_20260624_145727\copy_fallback_candidates.csv`
- `reports\copy_fallback_search_20260624_145727\copy_fallback_candidate_channels.csv`
- `reports\copy_fallback_search_20260624_145727\copy_fallback_search_log.csv`

## Result

強め検索では、45件中13動画に候補が見つかった。候補行は28件、候補チャンネルは11件。

広め検索では、候補行は35件、候補チャンネルは15件。ただし追加分は低スコア候補が中心で、採用前レビューが必要。

強い候補のカバー状況:

| channel | target videos | strong covered | missing |
|---|---:|---:|---:|
| 人生のヒカリ | 1 | 0 | 1 |
| 人生の温もり | 16 | 0 | 16 |
| 人生の糸 | 1 | 0 | 1 |
| 人生の縁側 | 4 | 3 | 1 |
| 人生は七色 | 12 | 11 | 1 |
| 人生は宝物 | 11 | 0 | 11 |

主な候補チャンネル:

| candidate channel | matched source videos | exact rows |
|---|---:|---:|
| 人生の運命劇場 | 9 | 8 |
| 人生100年スタイル | 5 | 4 |
| 人生100年ノート | 5 | 4 |
| 人生の物語 | 3 | 3 |
| 心を癒す物語 | 2 | 2 |

## Interpretation

「人生は七色」「人生の縁側」は、コピー候補からサムネイルを補完できる可能性が高い。

「人生の温もり」「人生は宝物」「人生のヒカリ」「人生の糸」は、今回のタイトル検索だけではコピー候補が見つからなかった。次は以下のいずれかが必要。

- タイトルの短縮検索
- キーフレーズ検索
- チャンネル名・コンセプト単位の周辺チャンネル探索
- Google/YouTube検索結果ページ側の探索

## Next Gate

候補をDBやポータルへ採用する前に、候補動画ごとに以下を確認する。

- 候補動画が現在公開されている
- サムネイルURLまたは画像が取得できる
- 可能なら動画尺が近い
- 台本・字幕・文字起こしが取得できる
- タイトル完全一致でも、別内容の再利用ではないことを確認する
