"""
An LSTM that use gate to compute distance
"""
import torch.nn.functional as F
import torch.nn as nn
import torch

from locked_dropout import LockedDropout


class LayerNorm(nn.Module):

    def __init__(self, features, eps=1e-6):
        super(LayerNorm, self).__init__()
        self.gamma = nn.Parameter(torch.ones(features))
        self.beta = nn.Parameter(torch.zeros(features))
        self.eps = eps

    def forward(self, x):
        mean = x.mean(-1, keepdim=True)
        std = x.std(-1, keepdim=True)
        return self.gamma * (x - mean) / (std + self.eps) + self.beta


class LinearDropConnect(nn.Linear):
    def __init__(self, in_features, out_features, bias=True, dropout=0.):
        super(LinearDropConnect, self).__init__(
            in_features=in_features,
            out_features=out_features,
            bias=bias
        )
        self.dropout = dropout

    def sample_mask(self):
        if self.dropout == 0.:
            self._weight = self.weight
        else:
            mask = self.weight.new_empty(
                self.weight.size(),
                dtype=torch.uint8
            )
            mask.bernoulli_(self.dropout)
            self._weight = self.weight.masked_fill(mask, 0.)

    def forward(self, input, sample_mask=False):
        if self.training:
            if sample_mask:
                self.sample_mask()
            return F.linear(input, self._weight, self.bias)
        else:
            return F.linear(input, self.weight * (1 - self.dropout),
                            self.bias)


def cumsoftmax(x, dim=-1):
    return torch.cumsum(F.softmax(x, dim=dim), dim=dim)


class LSTMCell(nn.Module):

    def __init__(self, input_size, hidden_size, dropconnect=0.):
        super(LSTMCell, self).__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size

        self.ih = nn.Sequential(
            nn.Linear(input_size, 4 * hidden_size, bias=True),
            # LayerNorm(3 * hidden_size)
        )
        self.hh = LinearDropConnect(hidden_size, hidden_size*4, bias=True, dropout=dropconnect)

        # self.c_norm = LayerNorm(hidden_size)

        self.drop_weight_modules = [self.hh]

    def forward(self, input, hidden,
                transformed_input=None):
        hx, cx = hidden

        if transformed_input is None:
            transformed_input = self.ih(input)

        gates = transformed_input + self.hh(hx)
        outgate, cell, ingate, forgetgate = gates.chunk(4, 1)

        ingate = F.sigmoid(ingate)
        forgetgate = F.sigmoid(forgetgate)
        cell = F.tanh(cell)
        outgate = F.sigmoid(outgate)

        distance_cforget = 1. - forgetgate.mean(dim=-1)
        distance_cin = ingate.mean(dim=-1)

        cy = forgetgate * cx + ingate * cell
        hy = outgate * F.tanh(cy)
        return hy.view(-1, self.hidden_size), cy, (distance_cforget, distance_cin)

    def init_hidden(self, bsz):
        weight = next(self.parameters()).data
        return (weight.new(bsz, self.hidden_size).zero_(),
                weight.new(bsz, self.hidden_size).zero_())

    def sample_masks(self):
        for m in self.drop_weight_modules:
            m.sample_mask()


class LSTMCellStack(nn.Module):
    def __init__(self, layer_sizes, dropout=0., dropconnect=0.):
        super(LSTMCellStack, self).__init__()
        self.cells = nn.ModuleList([LSTMCell(layer_sizes[i],
                                             layer_sizes[i+1],
                                             dropconnect=dropconnect)
                                    for i in range(len(layer_sizes) - 1)])
        self.lockdrop = LockedDropout()
        self.dropout = dropout
        self.sizes = layer_sizes

    def init_hidden(self, bsz):
        return [c.init_hidden(bsz) for c in self.cells]

    def forward(self, input, hidden):
        length, batch_size, _ = input.size()

        if self.training:
            for c in self.cells:
                c.sample_masks()

        prev_state = list(hidden)
        prev_layer = input

        raw_outputs = []
        outputs = []
        distances_forget = []
        distances_in = []
        for l in range(len(self.cells)):
            curr_layer = [None] * length
            dist = [None] * length
            t_input = self.cells[l].ih(prev_layer)

            for t in range(length):
                hidden, cell, d = self.cells[l](
                    None, prev_state[l],
                    transformed_input=t_input[t]
                )
                prev_state[l] = hidden, cell  # overwritten every timestep
                curr_layer[t] = hidden
                dist[t] = d

            prev_layer = torch.stack(curr_layer)
            dist_cforget, dist_cin = zip(*dist)
            dist_layer_cforget = torch.stack(dist_cforget)
            dist_layer_cin = torch.stack(dist_cin)
            raw_outputs.append(prev_layer)
            if l < len(self.cells) - 1:
                prev_layer = self.lockdrop(prev_layer, self.dropout)
            outputs.append(prev_layer)
            distances_forget.append(dist_layer_cforget)
            distances_in.append(dist_layer_cin)
        output = prev_layer

        return output, prev_state, raw_outputs, outputs, (torch.stack(distances_forget), torch.stack(distances_in))