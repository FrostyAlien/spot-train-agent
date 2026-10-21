# Recovery runbook

Recovery itself needs no runbook step: SkyPilot replaces the lost worker, the entrypoint selects the newest
verified checkpoint and the guard notifies `recovering` and `running again`. Check only after `running again` (the
queue shows `RUNNING` with a higher recovery count): before that the log can still be the old attempt's, so a wait
for the next commit could match the old one. Then check the observe runbook's milestone (the resumed
attempt's `CHECKPOINT_JOURNAL` selection line shows `resume` from the newest committed step that verifies; the metrics continue) and record which VM SkyPilot used
(`hook_instance_ids` through the provider hooks: the stopped one restarted, or a new one).
