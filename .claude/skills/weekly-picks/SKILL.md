---
name: weekly-picks
description: Run the weekly agent committee end to end and produce a validated allocation and trade list. Recommendation only.
disable-model-invocation: true
---

Run the weekly committee. Recommendation only: never place trades, log in anywhere, or edit memos or the PM
response. Add no investment opinions of your own. Use `.venv/bin/python` for every Python command.

1. Run `.venv/bin/python -m src.run_pipeline --agents`. Note the run folder it prints (`runs/<run_id>`). It
   writes `runs/.current_run`, which the agents' guard hook uses. If it fails, stop and report the error.
2. Read `runs/<run_id>/agent_inputs/manifest.json` for `equity_batches`.
3. Launch the **macro-strategist** subagent with the run folder. Wait for it.
4. In parallel, launch:
   - one **equity-research-analyst** subagent per entry in `equity_batches`, passing the run folder and that
     batch's tickers
   - the **nba-scout** subagent
   - the **politician-tracker** subagent
   Wait for all of them.
5. Launch the **risk-manager** subagent. Wait for it.
6. Launch the **lebron** subagent. Wait for it.
7. Run `.venv/bin/python -m src.agents.assemble runs/<run_id>`.
8. Launch the **portfolio-manager** subagent with the run folder.
9. Run `.venv/bin/python -m src.validate_allocation runs/<run_id>/recommendation_reason.txt`.
10. If validation fails and fewer than 2 retries have been used, run
    `.venv/bin/python -m src.agents.retry runs/<run_id>`. It saves the attempt in `pm_attempts/` and appends
    the validator table to `weight_prompt.txt` as VALIDATOR FEEDBACK. Then go back to step 8. Stop after 2
    retries.
11. If validation passed, run `.venv/bin/python -m src.trade_list runs/<run_id>`.
12. Report, using only what the files say:
    - final allocation (or the validator failure)
    - the trade list, including any compliance buys and any buys moved ahead of sales for the 5-stock floor
    - month-to-date buy count
    - realized alpha to date (the Contest Metric section of the latest `reports/` file)
    - memos that were unavailable, and why (`agents/committee.json`)
    - the captain's call and whether the PM accepted it
    - any "strategy changed since last form" reminder from step 1
    - "Recommendation only. Place trades manually in the class game."
