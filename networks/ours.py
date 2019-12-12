from typing import Tuple

import numpy as np
import torch
from torch import nn, Tensor
from torch.nn import functional as F


class GraphConv(nn.Module):
    def __init__(self, c_in: int, c_out: int, edge_dim: int, order: int, dropout: float):
        super(GraphConv, self).__init__()
        self.order = order
        c_in = (order * edge_dim + 1) * c_in
        self.mlp = nn.Conv2d(c_in, c_out, kernel_size=(1, 1), padding=(0, 0), stride=(1, 1), bias=True)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x: Tensor, supports: Tensor):
        """
        :param x: tensor, [B, *, N, c_in]
        :param supports: tensor, [n_edge, N, N] or [n_edge, B, N, N]
        :return: tensor, [B, *, N, c_out]
        """
        out = [x]
        for support in supports:
            x1 = self.nconv(x, support)
            out.append(x1)
            for k in range(2, self.order + 1):
                x2 = self.nconv(x1, support)
                out.append(x2)
                x1 = x2

        h = torch.cat(out, 1)
        h = self.mlp(h)
        h = self.dropout(h)
        return h

    @staticmethod
    def nconv(x: Tensor, adj: Tensor):
        assert len(adj.shape) in [2, 3] and len(x.shape) in [3, 4], f'x of {x.shape} or adj of {adj.shape} is wrong.'
        x_, r_ = ('btvc', 'btwc') if len(x.shape) == 4 else ('bvc', 'bwc')
        a_ = 'vw' if len(adj.shape) == 2 else 'bvw'
        x = torch.einsum(f'{x_},{a_}->{r_}', [x, adj])
        return x.contiguous()


class STLayer(nn.Module):
    def __init__(self, n_residuals: int, n_dilations: int, kernel_size: int, dilation: int,
                 n_skip: int, edge_dim: int, order: int, dropout: float):
        super(STLayer, self).__init__()
        # dilated convolutions
        self.filter_conv = nn.Conv2d(n_residuals, n_dilations, kernel_size=(1, kernel_size), dilation=dilation)

        self.gate_conv = nn.Conv1d(n_residuals, n_dilations, kernel_size=(1, kernel_size), dilation=dilation)

        # 1x1 convolution for residual connection
        self.gconv = GraphConv(n_dilations, n_residuals, edge_dim, order, dropout)

        # 1x1 convolution for skip connection
        self.skip_conv = nn.Conv1d(n_dilations, n_skip, kernel_size=(1, 1))
        self.bn = nn.BatchNorm2d(n_residuals)

    def forward(self, x: Tensor, skip: Tensor, supports: Tensor):
        residual = x
        # dilated convolution
        _filter = self.filter_conv(residual)
        _filter = torch.tanh(_filter)
        _gate = self.gate_conv(residual)
        _gate = torch.sigmoid(_gate)
        x = _filter * _gate

        # parametrized skip connection
        s = x
        s = self.skip_conv(s)
        skip = skip[:, :, :, -s.size(3):]
        skip = s + skip

        x = self.gconv(x, supports)

        x = x + residual[:, :, :, -x.size(3):]

        x = self.bn(x)
        return x, skip


class STBlock(nn.ModuleList):
    def __init__(self, n_layers: int, kernel_size: int, n_residuals: int, n_dilations: int,
                 n_skips: int, edge_dim: int, order: int, dropout: float):
        super(STBlock, self).__init__()
        for i in range(n_layers):
            self.append(STLayer(n_residuals, n_dilations, kernel_size, 2 ** i, n_skips, edge_dim, order, dropout))

    def forward(self, x: Tensor, skip: Tensor, supports: Tensor):
        for layer in self:
            x, skip = layer(x, skip, supports)

        return x, skip


class StackedSTBlocks(nn.ModuleList):
    def __init__(self, n_blocks, n_layers: int, kernel_size: int, n_residuals: int, n_dilations: int,
                 n_skips: int, edge_dim: int, order: int, dropout: float):
        self.n_skips = n_skips
        super(StackedSTBlocks, self).__init__()
        for _ in range(n_blocks):
            self.append(STBlock(n_layers, kernel_size, n_residuals, n_dilations, n_skips, edge_dim, order, dropout))

    def forward(self, x: Tensor, supports: Tensor):
        b, f, n, t = x.shape
        skip = torch.zeros(b, self.n_skips, n, t, dtype=torch.float32, device=x.device)
        for block in self:
            x, skip = block(x, skip, supports)
        return x, skip


class Ours(nn.Module):
    def __init__(self,
                 factors: Tuple[np.ndarray, np.ndarray],
                 num_node: int,
                 n_in: int,
                 n_out: int,
                 n_hist: int,
                 n_pred: int,
                 node_dim: int,
                 edge_dim: int,
                 n_residuals: int,
                 n_dilations: int,
                 n_skips: int,
                 n_ends: int,
                 kernel_size: int,
                 n_blocks: int,
                 n_layers: int,
                 order: int,
                 dropout: float):
        super(Ours, self).__init__()
        self.factors = factors
        self.receptive_field = n_blocks * (kernel_size - 1) * (2 ** n_layers - 1) + 1

        self.enter = nn.Conv2d(n_in * 2, n_residuals, kernel_size=(1, 1))

        self.blocks = StackedSTBlocks(n_blocks, n_layers, kernel_size, n_residuals, n_dilations,
                                      n_skips, edge_dim, order, dropout)

        self.out = nn.Sequential(
            nn.ReLU(),
            nn.Conv2d(n_skips, n_ends, kernel_size=(1, 1)),
            nn.ReLU(),
            nn.Conv2d(n_ends, n_pred, kernel_size=(1, 1))
        )

        # self.vertexes = nn.Parameter(torch.randn(num_node, node_dim), requires_grad=True)
        self.arcs = nn.Sequential(
            nn.Linear(2 * node_dim, 2 * node_dim),
            nn.LeakyReLU(),
            nn.Linear(2 * node_dim, node_dim),
            nn.LeakyReLU(),
            nn.Linear(node_dim, edge_dim),
        )

        # self.dynamics = nn.Sequential(
        #     nn.Linear(n_hist * n_in, 4 * node_dim),
        #     nn.ReLU(),
        #     nn.Linear(4 * node_dim, 4 * node_dim),
        #     nn.ReLU(),
        #     nn.Linear(4 * node_dim, edge_dim),
        # )

    def forward(self, inputs: Tensor):
        """
        : params inputs: tensor, [B, T, N, F]
        """

        t_factor = torch.tensor(self.factors[0], device=inputs.device, dtype=torch.float32)
        s_factor = torch.tensor(self.factors[1], device=inputs.device, dtype=torch.float32)

        supports = self.adaptive_supports(s_factor)
        # supports = self.adaptive_supports(self.vertexes)

        static_x, dynamic_x = self.inputs_split(inputs, t_factor, s_factor)

        inputs = torch.cat([static_x, dynamic_x], -1)

        inputs = inputs.transpose(1, 3)  # [B, F, N, T]
        in_len = inputs.size(3)
        if in_len < self.receptive_field:
            x = nn.functional.pad(inputs, [self.receptive_field - in_len, 0, 0, 0])
        else:
            x = inputs
        x = self.enter(x)

        x, skip = self.blocks(x, supports)

        x = self.out(skip)
        return x

    def adaptive_supports(self, vertexes) -> Tensor:
        num_node, node_dim = vertexes.shape
        src = vertexes.unsqueeze(0).expand([num_node, num_node, node_dim])
        dst = vertexes.unsqueeze(1).expand([num_node, num_node, node_dim])
        adj_mxs = self.arcs(torch.cat([src, dst], -1)).permute([2, 0, 1])

        identity = torch.eye(num_node, dtype=torch.float32, device=vertexes.device)
        adj_mxs = F.normalize(F.relu(adj_mxs.contiguous()), p=1, dim=2)
        adaptive_supports = torch.max(adj_mxs, identity)
        # adaptive_supports = identity.unsqueeze(0).expand(adj_mxs.shape)

        return adaptive_supports

    def inputs_split(self, inputs: Tensor, t_factor: Tensor, s_factor: Tensor) -> Tuple[Tensor, Tensor]:
        static_x = torch.einsum('btnf,tw,nu,wp,uq->bpqf', [inputs, t_factor, s_factor, t_factor.t(), s_factor.t()])
        return static_x, inputs - static_x

    def dynamic_bias(self, x: Tensor) -> Tensor:
        pass


def test():
    factors = (np.random.randn(12, 3), np.random.randn(207, 50))
    m = Ours(factors, 207, 2, 1, 12, 12, 8, 2, 32, 32, 256, 512, 2, 4, 2, 2, 0.3)
    x, y = torch.randn(64, 12, 207, 2), torch.randn(64, 12, 207, 1)
    y_ = m(x)
    print(y_.shape)
