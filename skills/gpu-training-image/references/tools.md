# Checking the image on a GPU workstation the user owns (offline only)

Only for a GPU workstation the user already owns, never a rented one: on rented GPUs the qualification task is the check. The build and push commands are in the [runbook](runbook.md). Verified against primary documentation on 2026-10-01; not run in a live test. Uppercase shell variables come from inspected project configuration.

## Check

```sh
# IMAGE is an already available, verified immutable reference; N and SHM_SIZE are measured/configured.
docker run --rm --pull=never --gpus "$N" --shm-size "$SHM_SIZE" \
  --entrypoint python "$IMAGE" -c 'import torch; assert torch.cuda.is_available(); x=torch.randn(32,32,device="cuda",requires_grad=True); (x@x).sum().backward(); torch.cuda.synchronize(); print(torch.cuda.device_count(), torch.version.cuda)'
```

Overriding the entrypoint prevents accidentally starting training. This checks only one device's basic autograd path; separately run the project's all-rank collective and custom-kernel smoke tests. Do not add `--privileged`, host IPC, host networking or broad host mounts as generic fixes. A missing local image should fail rather than pull unexpectedly.

Sources: [Docker run options](https://docs.docker.com/reference/cli/docker/container/run/), [NVIDIA sample workload](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/sample-workload.html).
