# SkyPilot launch commands
Checked against official current documentation on 2026-10-01. Run with the SkyPilot version chosen at the start. Example AWS/A100 values are illustrative; replace them with the approved manifest values. Installed-version help takes precedence over examples.

## Inspect version and existing access
```bash
python -c 'import sky; print(sky.__version__)'
sky --help
sky jobs launch --help
sky gpus list --help
sky api info
sky check aws
```
`sky check` probes already-configured credentials and updates enabled-cloud cache; restrict it to the selected provider. It does not create credentials or prove quota/capacity. Inspect configuration without dumping credential files into logs. Confirm endpoint/account/project matches the campaign. Normal commands may start a local API server when none is configured; do not accidentally substitute that for the campaign controller.

Only with authentication permission, connect to the exact verified server recorded in the controller plan:
```bash
sky api login -e https://verified-control.example.org
sky api info
```
The domain is a placeholder. Substitute the actual verified HTTPS/private endpoint, not a guessed URL. Compare reported version/commit to the lock. Do not pass real tokens in command arguments or invent login credentials.

## Render, start checks and submit
The [runbook](runbook.md) has these commands, in the order they run; this file keeps the diagnostics only.

## Diagnostic commands (offline only)
A managed job name is not an idempotency guarantee. If a submission's response is lost, inspect existing
requests and jobs before retrying:
```bash
sky api status REQUEST_ID
sky jobs queue -v -o json
sky jobs logs JOB_ID --tail 100 --no-follow
sky jobs logs --controller JOB_ID --no-follow
```
Use structured queue details and recent task/controller logs when the installed version supports these flags; consult the official skill’s job-investigation reference instead of parsing human tables. Replace uppercase placeholders with recorded IDs. No provider/region/purchase-model CLI override should conflict with the reviewed YAML.

Sources: https://docs.skypilot.ai/en/latest/reference/cli.html ; https://docs.skypilot.ai/en/latest/reference/api.html#sky.Task.from_yaml ; https://docs.skypilot.ai/en/latest/reference/api-server/api-server.html

## Covered elsewhere
- Offer discovery and prices: `gpu-rental-select`.
- Fixed project entrypoint: `gpu-training-lifecycle` → `references/entrypoint.md`.
- Using the official SkyPilot skill: `gpu-training-lifecycle` → `references/official-skypilot.md`.
- Access preparation: `gpu-training-lifecycle` → `references/credentials.md`.
