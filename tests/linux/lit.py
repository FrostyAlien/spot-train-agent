"""A plain Lightning script (no watchdog code): ticks come from the wdtick package's callback entry point."""
import time

import lightning as L
import torch
from torch.utils.data import DataLoader, TensorDataset


class Model(L.LightningModule):
    def __init__(self):
        super().__init__()
        self.layer = torch.nn.Linear(32, 1)
        self.register_buffer('big', torch.zeros(64 * 1024 * 1024))     # 256 MB in every checkpoint

    def training_step(self, batch, i):
        x, y = batch
        time.sleep(0.3)                                                # a step long enough for the 5 s polls
        return torch.nn.functional.mse_loss(self.layer(x), y)

    def validation_step(self, batch, i):
        x, y = batch
        self.log('val_loss', torch.nn.functional.mse_loss(self.layer(x), y), sync_dist=True)

    def configure_optimizers(self):
        return torch.optim.SGD(self.parameters(), lr=0.01)


def data(n):
    x = torch.randn(n, 32)
    return DataLoader(TensorDataset(x, x.sum(1, keepdim=True)), batch_size=16)


if __name__ == '__main__':
    every = L.pytorch.callbacks.ModelCheckpoint(dirpath='/tmp/lit-ckpt', every_n_train_steps=20, save_top_k=-1)
    trainer = L.Trainer(accelerator='cpu', devices=2, strategy='ddp', max_steps=60, val_check_interval=20,
                        callbacks=[every], logger=False, enable_progress_bar=False, enable_model_summary=False)
    trainer.fit(Model(), data(2000), data(33))
