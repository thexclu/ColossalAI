from typing import Any, List, Optional, Tuple

import torch
import torch.distributed as dist
from torch import Tensor
from torch.cuda.amp import custom_bwd, custom_fwd
from torch.distributed import ProcessGroup

MOE_KERNEL = None


def load_moe():
    global MOE_KERNEL
    from colossalai.kernel.kernel_loader import MoeLoader

    MOE_KERNEL = MoeLoader().load()


class AllGather(torch.autograd.Function):
    @staticmethod
    def forward(
        ctx: Any,
        inputs: Tensor,
        group: Optional[ProcessGroup] = None,
        overlap: bool = False,
    ) -> Tuple[Tensor, Any]:
        """
        Returns:
            outputs: Tensor
            handle: Optional[Work], if overlap is True
        """
        assert ctx is not None or not overlap

        if ctx is not None:
            ctx.comm_grp = group

        comm_size = dist.get_world_size(group)
        if comm_size == 1:
            return inputs.unsqueeze(0), None

        buffer_shape = (comm_size,) + inputs.shape
        outputs = torch.empty(buffer_shape, dtype=inputs.dtype, device=inputs.device)
        buffer_list = list(torch.chunk(outputs, comm_size, dim=0))
        if not overlap:
            dist.all_gather(buffer_list, inputs, group=group)
            return outputs, None
        else:
            handle = dist.all_gather(buffer_list, inputs, group=group, async_op=True)
            return outputs, handle

    @staticmethod
    def backward(ctx: Any, *grad_outputs) -> Tuple[Tensor, None, None]:
        return (
            ReduceScatter.forward(None, grad_outputs[0], ctx.comm_grp, False)[0],
            None,
            None,
        )


class ReduceScatter(torch.autograd.Function):
    @staticmethod
    def forward(
        ctx: Any,
        inputs: Tensor,
        group: ProcessGroup,
        overlap: bool = False,
    ) -> Tuple[Tensor, Any]:
        """
        Returns:
            outputs: Tensor
            handle: Optional[Work], if overlap is True
        """
        assert ctx is not None or not overlap

        if ctx is not None:
            ctx.comm_grp = group

        comm_size = dist.get_world_size(group)
        if comm_size == 1:
            return inputs.squeeze(0), None

        if not inputs.is_contiguous():
            inputs = inputs.contiguous()

        output_shape = inputs.shape[1:]
        outputs = torch.empty(output_shape, dtype=inputs.dtype, device=inputs.device)
        buffer_list = list(torch.chunk(inputs, comm_size, dim=0))
        if not overlap:
            dist.reduce_scatter(outputs, buffer_list, group=group)
            return outputs, None
        else:
            handle = dist.reduce_scatter(outputs, buffer_list, group=group, async_op=True)
            return outputs, handle

    @staticmethod
    def backward(ctx: Any, *grad_outputs) -> Tuple[Tensor, None, None]:
        # TODO: support async backward
        return (
            AllGather.forward(None, grad_outputs[0], ctx.comm_grp, False)[0],
            None,
            None,
        )


class AllToAll(torch.autograd.Function):
    """Dispatches input tensor [e, c, h] to all experts by all_to_all_single
    operation in torch.distributed.
    """

    @staticmethod
    def forward(
        ctx: Any,
        inputs: Tensor,
        group: ProcessGroup,
        overlap: bool = False,
    ) -> Tuple[Tensor, Any]:
        """
        Returns:
            outputs: Tensor
            handle: Optional[Work], if overlap is True
        """
        assert ctx is not None or not overlap

        if ctx is not None:
            ctx.comm_grp = group
        if not inputs.is_contiguous():
            inputs = inputs.contiguous()
        if dist.get_world_size(group) == 1:
            return inputs, None
        output = torch.empty_like(inputs)
        if not overlap:
            dist.all_to_all_single(output, inputs, group=group)
            return output, None
        else:
            handle = dist.all_to_all_single(output, inputs, group=group, async_op=True)
            return output, handle

    @staticmethod
    def backward(ctx: Any, *grad_outputs) -> Tuple[Tensor, None, None]:
        return (
            AllToAll.forward(None, grad_outputs[0], ctx.comm_grp, False)[0],
            None,
            None,
        )


class HierarchicalAllToAll(torch.autograd.Function):
    @staticmethod
    def forward(ctx: Any, inputs: Tensor, groups: Tuple[ProcessGroup, ProcessGroup], src_rank: int) -> Tensor:
        """
        Returns:
            outputs: Tensor
        """
        # TODO: we can reduce comm volume by removing empty capacity
        if ctx is not None:
            ctx.comm_grps = groups
            ctx.src_rank = src_rank
        intra_node_group, inter_node_group = groups

        local_world_size = dist.get_world_size(intra_node_group)
        num_group = dist.get_world_size(inter_node_group) if inter_node_group is not None else 1
        world_size = local_world_size * num_group
        outputs = torch.empty_like(inputs)

        if dist.get_rank() == src_rank:
            # intra-node gather
            intra_output = [torch.empty_like(inputs) for _ in range(local_world_size)]
            dist.gather(inputs, intra_output, dst=src_rank, group=intra_node_group)

            intra_output = [v.chunk(world_size, dim=0) for v in intra_output]
            intra_output = torch.cat(sum(zip(*intra_output), ()))

            # inter-node all-to-all
            if inter_node_group is not None:
                inter_output = torch.empty_like(intra_output)
                dist.all_to_all_single(inter_output, intra_output, group=inter_node_group)

                # layout transform
                inter_output = inter_output.chunk(num_group, dim=0)
                inter_output = [v.chunk(local_world_size, dim=0) for v in inter_output]
                intra_output = torch.cat(sum(zip(*inter_output), ()))

            # intra-node scatter
            intra_output = list(intra_output.chunk(local_world_size, dim=0))
            dist.scatter(outputs, intra_output, src=src_rank, group=intra_node_group)

        else:
            dist.gather(inputs, dst=src_rank, group=intra_node_group)
            dist.scatter(outputs, src=src_rank, group=intra_node_group)

        return outputs

    @staticmethod
    def backward(ctx: Any, *grad_outputs) -> Tuple[Tensor, None, None]:
        return (
            HierarchicalAllToAll.forward(None, grad_outputs[0], ctx.comm_grps, ctx.src_rank),
            None,
            None,
        )


class MoeDispatch(torch.autograd.Function):
    @staticmethod
    @custom_fwd
    def forward(ctx, tokens, mask, dest_idx, ec):
        s = tokens.size(0)
        h = tokens.size(1)
        dtype = tokens.dtype

        if MOE_KERNEL is None:
            load_moe()
        if tokens.dtype != torch.float32:
            tokens = tokens.to(torch.float32)
        expert_input = MOE_KERNEL.dispatch_forward(s, ec, h, tokens, mask, dest_idx)
        if expert_input.dtype != dtype:
            expert_input = expert_input.to(dtype)
        ctx.save_for_backward(mask, dest_idx)
        ctx.s = s
        ctx.h = h
        ctx.ec = ec
        ctx.dtype = dtype

        return expert_input

    @staticmethod
    @custom_bwd
    def backward(ctx, output_grad):
        mask, dest_idx = ctx.saved_tensors
        if output_grad.dtype != torch.float32:
            output_grad = output_grad.to(torch.float32)
        d_tokens = MOE_KERNEL.dispatch_backward(ctx.s, ctx.ec, ctx.h, output_grad, mask, dest_idx)
        if d_tokens.dtype != ctx.dtype:
            d_tokens = d_tokens.to(ctx.dtype)
        return d_tokens, None, None, None


class MoeCombine(torch.autograd.Function):
    @staticmethod
    @custom_fwd
    def forward(ctx, expert_tokens, logits, mask, dest_idx, ec):
        assert logits.dtype == torch.float32

        s = logits.size(0)
        e = logits.size(1)
        c = ec // e
        h = expert_tokens.size(-1)
        dtype = expert_tokens.dtype

        if expert_tokens.dtype != torch.float32:
            expert_tokens = expert_tokens.to(torch.float32)
        if MOE_KERNEL is None:
            load_moe()
        output = MOE_KERNEL.combine_forward(s, e, c, h, expert_tokens, logits, mask, dest_idx)
        if output.dtype != dtype:
            output = output.to(dtype)

        ctx.save_for_backward(expert_tokens, logits, mask, dest_idx)
        ctx.s = s
        ctx.e = e
        ctx.c = c
        ctx.h = h
        ctx.dtype = dtype

        return output

    @staticmethod
    @custom_bwd
    def backward(ctx, tokens_grad):
        expert_tokens, logits, mask, dest_idx = ctx.saved_tensors
        if tokens_grad.dtype != torch.float32:
            tokens_grad = tokens_grad.to(torch.float32)

        d_expert, d_logits = MOE_KERNEL.combine_backward(
            ctx.s, ctx.e, ctx.c, ctx.h, tokens_grad, expert_tokens, logits, mask, dest_idx
        )
        if d_expert.dtype != ctx.dtype:
            d_expert = d_expert.to(ctx.dtype)

        return d_expert, d_logits, None, None, None


def moe_cumsum(inputs: Tensor, use_kernel: bool = False):
    dim0 = inputs.size(0)
    flag = (dim0 <= 1024) or (dim0 <= 2048 and dim0 % 2 == 0) or (dim0 % 4 == 0)
    if flag and use_kernel:
        if MOE_KERNEL is None:
            load_moe()
        return MOE_KERNEL.cumsum_sub_one(inputs)
    else:
        return torch.cumsum(inputs, dim=0) - 1


class EPGradScalerIn(torch.autograd.Function):
    """
    Scale the gradient back by the number of experts
    because the batch size increases in the moe stage
    """

    @staticmethod
    def forward(ctx: Any, inputs: Tensor, ep_size: int) -> Tensor:
        ctx.ep_size = ep_size
        return inputs

    @staticmethod
    def backward(ctx: Any, *grad_outputs: Tensor) -> Tuple[Tensor, None]:
        assert len(grad_outputs) == 1
        grad = grad_outputs[0]
        if ctx.ep_size != 1:
            grad = grad * ctx.ep_size
        return grad, None


class EPGradScalerOut(torch.autograd.Function):
    """
    Scale the gradient by the number of experts
    because the batch size increases in the moe stage
    """

    @staticmethod
    def forward(ctx: Any, inputs: Tensor, ep_size: int) -> Tensor:
        ctx.ep_size = ep_size
        return inputs

    @staticmethod
    def backward(ctx: Any, *grad_outputs: Tensor) -> Tuple[Tensor, None]:
        assert len(grad_outputs) == 1
        grad = grad_outputs[0]
        if ctx.ep_size != 1:
            grad = grad / ctx.ep_size
        return grad, None


class DPGradScalerIn(torch.autograd.Function):
    """
    Scale the gradient back by the number of experts
    because the batch size increases in the moe stage
    """

    @staticmethod
    def forward(ctx: Any, inputs: Tensor, moe_dp_size: int, activated_experts: int) -> Tensor:
        assert activated_experts != 0, f"shouldn't be called when no expert is activated"
        ctx.moe_dp_size = moe_dp_size
        ctx.activated_experts = activated_experts
        return inputs

    @staticmethod
    def backward(ctx: Any, *grad_outputs: Tensor) -> Tuple[Tensor, None, None]:
        assert len(grad_outputs) == 1
        grad = grad_outputs[0]
        if ctx.moe_dp_size != ctx.activated_experts:
            grad.mul_(ctx.activated_experts / ctx.moe_dp_size)
        return grad, None, None


class DPGradScalerOut(torch.autograd.Function):
    """
    Scale the gradient by the number of experts
    because the batch size increases in the moe stage
    """

    @staticmethod
    def forward(ctx: Any, inputs: Tensor, moe_dp_size: int, activated_experts: int) -> Tensor:
        assert activated_experts != 0, f"shouldn't be called when no expert is activated"
        ctx.moe_dp_size = moe_dp_size
        ctx.activated_experts = activated_experts
        return inputs

    @staticmethod
    def backward(ctx: Any, *grad_outputs: Tensor) -> Tuple[Tensor, None, None]:
        assert len(grad_outputs) == 1
        grad = grad_outputs[0]
        if ctx.moe_dp_size != ctx.activated_experts:
            grad.mul_(ctx.moe_dp_size / ctx.activated_experts)
        return grad, None, None


def _all_to_all(
    inputs: torch.Tensor,
    input_split_sizes: Optional[List[int]] = None,
    output_split_sizes: Optional[List[int]] = None,
    group=None,
    async_op: bool = False,
):
    """
    Returns:
        outputs: Tensor
        handle: Optional[Work], if overlap is True
    """
    outputs_shape = list(inputs.shape)
    if output_split_sizes is not None:
        outputs_shape[0] = sum(output_split_sizes)
    outputs = torch.empty(outputs_shape, dtype=inputs.dtype, device=inputs.device)
    inputs = inputs.contiguous()
    outputs = outputs.contiguous()
    handle = dist.all_to_all_single(
        outputs, inputs, output_split_sizes, input_split_sizes, group=group, async_op=async_op
    )
    return outputs, handle


class AllToAllUneven(torch.autograd.Function):
    @staticmethod
    def forward(
        ctx,
        inputs,
        input_split_sizes=None,
        output_split_sizes=None,
        group=None,
        overlap: bool = False,
    ):
        """
        Returns:
            outputs: Tensor
            handle: Optional[Work], if overlap is True
        """
        ctx.input_split_sizes = input_split_sizes
        ctx.output_split_sizes = output_split_sizes
        ctx.group = group
        return _all_to_all(inputs, input_split_sizes, output_split_sizes, group, overlap)

    @staticmethod
    def backward(ctx: Any, *grad_outputs):
        return (
            _all_to_all(grad_outputs[0], ctx.output_split_sizes, ctx.input_split_sizes, ctx.group, False)[0],
            None,
            None,
            None,
            None,
        )


def all_to_all_uneven(
    inputs: torch.Tensor,
    input_split_sizes: Optional[List[int]] = None,
    output_split_sizes: Optional[List[int]] = None,
    group=None,
    overlap: bool = False,
):
    assert (
        inputs.requires_grad
    ), "Input must require grad to assure that backward is executed, otherwise it might hang the program."
    return AllToAllUneven.apply(inputs, input_split_sizes, output_split_sizes, group, overlap)


# ===========================================================
# This code section was modified from 
# https://github.com/microsoft/DeepSpeed/blob/3d347276ce80e1a29e777c839d1d7fabe8e5f034/deepspeed/moe/mappings.py

# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: Apache-2.0

# DeepSpeed Team

# The file has been adapted from the following Megatron-LM file:
# https://github.com/NVIDIA/Megatron-LM/blob/main/megatron/mpu/mappings.py
# Git commit hash: 9dc3c42a84aa656f583703cf8b6b4f79f712b796
# We retain the following copyright from the original files:

# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


def _gather_tokens(input_, dim: int, tp_group: ProcessGroup):
    """Gather tensors and concatenate them along a dimension"""

    input_ = input_.contiguous()
    # Size and dimension.
    rank = tp_group.rank()

    tensor_list = [torch.empty_like(input_) for _ in range(tp_group.size())]
    tensor_list[rank] = input_
    dist.all_gather(tensor_list, input_, group=tp_group)

    # Note: torch.cat already creates a contiguous tensor.
    output = torch.cat(tensor_list, dim=dim).contiguous()

    return output


def _drop_tokens(input_, dim: int, tp_group: ProcessGroup):
    """Divide a tensor among the tensor parallel ranks"""

    total_chunks = tp_group.size()
    this_chunk = tp_group.rank()
    assert input_.shape[
        dim] % total_chunks == 0, f"input dimension {dim} ({input_.shape[dim]}) is not divisible by tensor parallel world size ({total_chunks})"
    chunk_size = input_.shape[dim] // total_chunks

    return torch.narrow(input_, dim, this_chunk * chunk_size, chunk_size)


class _GatherTokens(torch.autograd.Function):
    """All gather tokens among the tensor parallel ranks"""

    @staticmethod
    def forward(ctx, input_: torch.Tensor, dim: int, tp_group: ProcessGroup) -> torch.Tensor:
        ctx.dim = dim
        ctx.tp_group = tp_group
        return _gather_tokens(input_, dim, tp_group)

    @staticmethod
    def backward(ctx, grad_output):
        return _drop_tokens(grad_output, ctx.dim, ctx.tp_group), None, None


class _DropTokens(torch.autograd.Function):
    "Divide tokens equally among the tensor parallel ranks"

    @staticmethod
    def forward(ctx, input_: torch.Tensor, dim: int, tp_group: ProcessGroup) -> torch.Tensor:
        ctx.dim = dim
        ctx.tp_group = tp_group
        return _drop_tokens(input_, dim, tp_group)

    @staticmethod
    def backward(ctx, input_: torch.Tensor) -> Tuple[torch.Tensor, None]:
        return _gather_tokens(input_, ctx.dim, ctx.tp_group), None, None


def gather_tokens(input_, dim: int, tp_group: ProcessGroup):
    if tp_group.size() == 1:
        # no tensor parallelism for non-experts
        return input_
    assert input_.requires_grad, "Input must require grad to assure that backward is executed, otherwise it might hang the program."
    return _GatherTokens.apply(input_, dim)


def drop_tokens(input_, dim: int, tp_group: ProcessGroup):
    if tp_group.size() == 1:
        # no tensor parallelism for non-experts
        return input_
    assert input_.requires_grad, "Input must require grad to assure that backward is executed, otherwise it might hang the program."
    return _DropTokens.apply(input_, dim, tp_group)

# ===========================================================
