# Auto Deploy

This portal can refresh from BigQuery and publish to GitHub Pages with:

```powershell
E:\Data\ObsidianVault\04_Tools\senior-kikaku-research\deploy_from_bq.ps1
```

The script runs:

```powershell
E:\Data\ObsidianVault\04_Tools\envs\senior_reading\Scripts\python.exe deploy_pages.py --from-bq
```

It writes logs under `logs/`, which is ignored by git.

Scheduled task:

- Name: `SeniorReadingKikakuResearchDeploy`
- Schedule: daily 03:30 local time
- Purpose: run after the 03:00 YouTube metadata batch and 03:10 thumbnail OCR batch.

The task uses the local machine's existing Google Cloud and GitHub authentication.
