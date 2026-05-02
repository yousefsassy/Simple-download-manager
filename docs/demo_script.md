# Demo Video Script

Target length: 5 to 10 minutes.

## 1. Introduction

- Introduce Simple Download Manager.
- Mention the goal: segmented downloads using multithreading and HTTP Range requests.

## 2. Architecture Walkthrough

- Show the README architecture diagram.
- Explain the UI layer, manager core, workers, assembler, and SQLite storage.
- Explain that each segment is downloaded by a separate worker thread.

## 3. Core Demo

- Launch the app with `python -m sdm.app`.
- Paste a valid file URL.
- Use 4 segments and 3 retries.
- Set the active download limit to 1 or 2.
- Start the download.
- Show progress percentage, speed, ETA, and status.

## 4. Pause And Resume

- Start a larger download.
- Pause it while progress is visible.
- Show that the status becomes paused.
- Resume the download and show that it continues.

## 5. Error And Retry Behavior

- Add an invalid URL or temporarily interrupt a local test server.
- Show the failed state.
- Use retry after fixing the problem.

## 6. History

- Show completed and failed downloads in the History tab.
- Point out that state is stored in SQLite.

## 6.1 Queue Management

- Set active downloads to 1.
- Add two large downloads.
- Show that the first starts and the second waits in queued state.
- Show that the queued download starts automatically when the first leaves the active slot.

## 7. Performance Comparison

- Run `PYTHONPATH=src python3 scripts/performance_compare.py`.
- Show the measured single-threaded and multithreaded times in the technical report table.

## 8. Conclusion

- Summarize what was implemented.
- Mention the main distributed systems concepts: concurrency, network requests, partitioning, recovery, and monitoring.
