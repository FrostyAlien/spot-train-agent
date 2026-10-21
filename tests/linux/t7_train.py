"""A plain DDP training script with no hooks (rank-mode test); run under torchrun, CPU and gloo."""
import sys
import time

import torch
import torch.distributed as dist
from torch.utils.data import DataLoader, Dataset


class Sums(Dataset):                  # defined in the script: spawned loader workers find it through __main__
    def __len__(self):
        return 100000

    def __getitem__(self, i):
        x = torch.randn(32)
        return x, x.sum(dim=0, keepdim=True)


def main():
    mode = sys.argv[1]                 # stall | hidden-eval
    dist.init_process_group('gloo')
    rank = dist.get_rank()
    model = torch.nn.parallel.DistributedDataParallel(torch.nn.Linear(32, 1))
    opt = torch.optim.SGD(model.parameters(), lr=0.01)
    loader = DataLoader(Sums(), batch_size=64, num_workers=2, multiprocessing_context='spawn')
    for step, (x, y) in enumerate(loader, 1):
        loss = torch.nn.functional.mse_loss(model(x), y)
        opt.zero_grad()
        loss.backward()
        opt.step()
        time.sleep(0.05)
        if mode == 'stall' and step == 40 and rank == 0:
            print(f'T7-STALL {time.time():.0f}', flush=True)
            time.sleep(600)            # rank 1 then waits in the next all-reduce
        if mode == 'hidden-eval' and step % 30 == 0:
            t, acc = time.time(), 0.0
            while time.time() - t < 20:   # an evaluation no hook announces: compute only, no bytes
                with torch.no_grad():
                    acc += model(torch.randn(256, 32)).sum().item()
            print(f'T7-EVAL step={step} seconds={time.time() - t:.0f}', flush=True)
        if step == 100:
            break
    dist.destroy_process_group()


if __name__ == '__main__':
    main()
