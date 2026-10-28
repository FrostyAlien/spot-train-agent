# Gates before a launch

The capability gate, the acceptance gates by area and the SkyPilot version rule with its start checks. Where things run and how a stack is qualified: [operating-model.md](operating-model.md).

## Exact recovery capability gate
Before creating the task, lock provider, region, GPU preset/count, the explicitly selected purchase mode and the SkyPilot version chosen at the start (client, API server, provider adapter).
- **Verify the exact tuple** supports the selected purchase mode, Managed Jobs infrastructure reacquisition, and access to the same durable checkpoint service from a replacement. A provider name in a support matrix alone is insufficient. Reject unsupported candidates.
- **Spot runs:** inspect the rendered provider request and current provider semantics to establish the actual spot/preemptible/pricing fields; a legacy use_spot flag alone may not prove billing mode. If those semantics cannot exclude on-demand provisioning, block even the bounded qualification pilot.
- **On-demand runs:** a deliberately selected initial on-demand experiment remains fixed to that mode; it is not fallback permission.
- **Unknown recovery behavior:** mark it unverified and block production; only a bounded explicitly approved qualification pilot may exercise it after the selected purchase-mode billing semantics are established.
- **The created instance:** verify its actual purchase/pricing status before training, and apply the same check on replacement. Never silently strip unsupported fields or broaden purchase mode.

## Acceptance gates by area
The decisive gates (provider lock, Spot-only default, budget approval, no silent fresh start, no rented GPU for CPU work, no resident AI) are in the lifecycle `SKILL.md`; every area below must also hold.
- **Environment:** fixed Linux image and compatible host driver/GPU architecture; avoid rebuilding heavy extensions on every paid 8-GPU allocation.
- **Data:** main copy survives compute deletion; measured pipeline can feed the GPUs; restored data progress has defined scientific semantics.
- **Recovery:** periodic full checkpoints in the selected GPU provider's durable storage, preferably the same region, independent of worker/container deletion.
- **Monitoring:** the chosen metrics SDK (if any) is pinned in the image, configured at deployment with a stable run identity and runtime-only secrets. Checkpoints remain training truth; metrics history is observation, not a recovery store. Read `gpu-training-observe` for replay/history semantics and transmission controls (W&B in detail; the same principles apply to other tools).
- **Cost:** count node-level cost including startup, replay, data movement, storage and control plane.
- **Control plane:** independent of the spot worker; state persisted and restoration checked; authenticated/private access; no permanent availability promise for an assistant session.
- **Completion:** final model and provenance verified before deleting working state; permissions and resource ownership rechecked for every cleanup.

## SkyPilot version: chosen at the start, kept for the run
The skills name no SkyPilot version. At the start of a project use the latest SkyPilot release, or the version the user already runs, and record it (client and API server) with the stack. On a server the user does not run (`controller.mode` `existing` or `provider-managed`) the server's version is the choice: install that exact client and research provider behaviour in that version's source. Run the start checks below once and adapt the project's scripts if a feature changed. Then keep that version for the whole run: the client, API server and jobs controller carry recovery together, so an upgrade mid-run changes the recovery path untested. If an upgrade cannot wait, rerun the start checks before the next launch or recovery. The image digest and framework versions are likewise fixed per run.

Start checks (once per chosen version, not per launch):
1. With the optional guard: the guard's start checks on the guard host, which confirm the installed SkyPilot still exposes the calls the guard makes (`gpu-training-controller` → `references/runbook.md#6-start-checks-once-per-chosen-skypilot-version`).
2. Render a representative task for each tier and variant you will use and check it offline with `render_job.py ... --validate-with-skypilot`: parse, serialize and reload as the client does before sending it to the API server, and no managed-secret references. This catches schema changes, not behaviour changes; the renderer writes the behaviours that matter explicitly (recovery strategy, `api_server_access: false`). Confirm every flag the runbooks use exists in this version's help (the launch runbook's `flags()` block is the list).
3. Skim the release notes since the last version you used (or read the installed source) for changes to managed-job recovery (FAILOVER restarts a stopped VM first, then replaces it and retries indefinitely, which is why the first start's capacity wait is bounded by `job.wait_for_scheduling_timeout` where the version has it, and a wait after a preemption only by the optional guard), credential upload to clusters (`remote_identity`, see the credentials reference) and the chosen provider's spot request fields (capability gate above). Record what you found, with the version and date.

The bundled helpers use only the Python standard library, except the guard and the renderer's start check (they import SkyPilot) and the PyTorch helpers (`gpu_preflight.py`, `torch_rank.py`); no helper imports a provider SDK. At runtime an SDK failure makes the guard "blind" (never "fine"): it notifies the user, and the worker's runtime timeout (and the deadline, if set) stops spending.
