import torch
from torch import nn, Tensor
from typing import List

from torch.nn.parallel.parallel_apply import parallel_apply
from torch.nn.parallel.replicate import replicate
from torch.nn.parallel.scatter_gather import gather, scatter
import apex
import sparse_embedding_cuda
from torch.nn.parallel import DistributedDataParallel as DDP
import numpy as np
import amp_C
import apex
import horovod.torch as hvd

class LookupFunction(torch.autograd.Function):
    @staticmethod
    def forward(ctx, weights, indices):
        ctx.save_for_backward(weights, indices)
        return sparse_embedding_cuda.forward_fast_single(weights, indices)

    @staticmethod
    def backward(ctx, grad_output):
        weights, indices = ctx.saved_tensors
        # TODO: obvious hack
        LR = 0.05
        sparse_embedding_cuda.backward_update_fast_single(
            grad_output, weights, indices, LR)
        return (torch.cuda.sparse.FloatTensor(*weights.size()), None)


class UniformShardedEmbeddingBags(nn.Module):
    def __init__(self, num_tables, num_embeddings, embedding_dim):
        super(UniformShardedEmbeddingBags, self).__init__()
        # Whole tables (i.e. all rows for a table) are partitioned uniformly across devices
        self.embedding_weights = nn.Parameter(
            torch.randn(num_embeddings, num_tables, embedding_dim))

    def forward(self, sharded_sparse_features):
        return LookupFunction.apply(self.embedding_weights,
                                    sharded_sparse_features)


class ReduceScatterFunction(torch.autograd.Function):
    @staticmethod
    def forward(ctx, sharded_embeddings):
        return sparse_embedding_cuda.forward_reducescatter(sharded_embeddings)

    @staticmethod
    def backward(ctx, grad_output):
        return sparse_embedding_cuda.forward_allgather(grad_output)


class All2AllFunction(torch.autograd.Function):
    @staticmethod
    def forward(ctx, partitioned_embeddings):
        (B, T, D) = partitioned_embeddings.size()
        assert B % hvd.size() == 0
        butterfly_embeddings = torch.empty(B // hvd.size(),
                                           T * hvd.size(),
                                           D,
                                           device=torch.cuda.current_device())
        sparse_embedding_cuda.forward_all2all(partitioned_embeddings,
                                              butterfly_embeddings)
        return butterfly_embeddings

    @staticmethod
    def backward(ctx, grad_output):
        # in: (B // hvd.size(), T * hvd.size(), D)
        # out: (B, T, D)
        # solution: transpose to (T * hvd.size(), B // hvd.size(), D), make contiguous
        # All2All to get (T, B, D)
        # Transpose to get (B, T, D)
        (B_div_world_size, T_mul_world_size, D) = grad_output.size()
        B = B_div_world_size * hvd.size()
        T = T_mul_world_size // hvd.size()
        grad_input = torch.empty(T, B, D, device=torch.cuda.current_device())
        sparse_embedding_cuda.forward_all2all(
            grad_output.transpose(1, 0).contiguous(), grad_input)
        return grad_input.transpose(1, 0)


class FastZeroFusedSGD(apex.optimizers.FusedSGD):
    def __init__(self, *args, **kwargs):
        super(FastZeroFusedSGD, self).__init__(*args, **kwargs)
        self._overflow_buf = torch.cuda.IntTensor([0])

    def zero_grad(self):
        r"""Clears the gradients of all optimized :class:`torch.Tensor` s."""
        grads = [
            p.grad for group in self.param_groups for p in group['params']
            if p.grad is not None
        ]
        if not grads:
            return
        for grad in grads:
            grad.detach_()
        apex.multi_tensor_apply.multi_tensor_applier(amp_C.multi_tensor_scale,
                                                     self._overflow_buf,
                                                     [grads, grads], 0.0)
