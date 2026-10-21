"""Progress ticks for any Lightning Trainer, attached through Lightning's callback entry point (no code edit)."""
import faulthandler
import signal

import lightning as L
import progress_watch as pw


class Tick(L.pytorch.callbacks.Callback):
    def on_train_batch_end(self, trainer, *_):
        pw.tick('train', trainer.global_step, trainer.global_step, trainer.global_rank)          # E1

    def on_validation_start(self, trainer, *_):
        pw.tick('eval', 0, trainer.global_step, trainer.global_rank)                             # E2 begins

    def on_validation_batch_end(self, trainer, pl_module, outputs, batch, batch_idx, *_):
        pw.tick('eval', batch_idx + 1, trainer.global_step, trainer.global_rank)                 # E2 batches

    def on_save_checkpoint(self, trainer, *_):
        pw.tick('save', 0, trainer.global_step, trainer.global_rank)                             # E3, before the write

    def on_train_end(self, trainer, *_):
        pw.tick('final', 0, trainer.global_step, trainer.global_rank)                            # E4


def factory():
    faulthandler.register(signal.SIGUSR2, all_threads=True)
    return [Tick()]
