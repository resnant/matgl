"""
Implementation of MEGNet model.
"""
from __future__ import annotations

import logging
import os

import dgl
import torch
import torch.nn as nn
from dgl.nn import Set2Set
from torch.nn import Dropout, Identity, Module, ModuleList

from matgl.layers.activations import SoftPlus2
from matgl.layers.core import MLP, EdgeSet2Set
from matgl.layers.graph_conv import MEGNetBlock

logger = logging.getLogger(__file__)
CWD = os.path.dirname(os.path.abspath(__file__))

MODEL_NAME = "megnet"

MODEL_PATHS = {
    "MP-2018.6.1-Eform": os.path.join(CWD, "..", "..", "pretrained", "MP-2018.6.1-Eform"),
    "MP-2019.4.1-BandGap": os.path.join(CWD, "..", "..", "pretrained", "MP-2019.4.1-BandGap"),
}


class MEGNet(Module):
    """
    DGL implementation of MEGNet.
    """

    def __init__(
        self,
        node_embedding_dim: int,
        edge_embedding_dim: int,
        attr_embedding_dim: int,
        num_blocks: int,
        hiddens: list[int],
        conv_hiddens: list[int],
        s2s_num_layers: int,
        s2s_num_iters: int,
        output_hiddens: list[int],
        act: str = "swish",
        is_classification: bool = True,
        node_embed: Module | None = None,
        edge_embed: Module | None = None,
        attr_embed: Module | None = None,
        include_states: bool = False,
        dropout: float | None = None,
        graph_transformations: list | None = None,
        element_types: list | None = None,
        cutoff: float | None = None,
        data_mean: torch.tensor | None = None,
        data_std: torch.tensor | None = None,
        device: torch.device | None = None,
        **kwargs,
    ) -> None:
        """
        TODO: Add docs.
        :param in_dim:
        :param num_blocks:
        :param hiddens:
        :param conv_hiddens:
        :param s2s_num_layers:
        :param s2s_num_iters:
        :param output_hiddens:
        :param act:
        :param is_classification:
        :param node_embed:
        :param edge_embed:
        :param attr_embed:
        :param dropout:
        :param graph_transform: Perform a graph transformation, e.g., incorporate three-body interactions, prior to
            performing the GCL updates.
        """

        # Store MEGNet model args for loading trained model
        self.model_args = {k: v for k, v in locals().items() if k not in ["self", "__class__", "kwargs"]}
        self.model_args.update(kwargs)

        super().__init__()

        if element_types is not None:
            self.element_types = element_types
        if cutoff is not None:
            self.cutoff = cutoff
        if data_mean is not None:
            self.data_mean = data_mean
        if data_std is not None:
            self.data_std = data_std

        self.edge_embed = edge_embed if edge_embed else Identity()
        self.node_embed = node_embed if node_embed else Identity()
        self.attr_embed = attr_embed if attr_embed else Identity()

        node_dims = [node_embedding_dim, *hiddens]
        edge_dims = [edge_embedding_dim, *hiddens]
        attr_dims = [attr_embedding_dim, *hiddens]

        if act == "swish":
            activation = nn.SiLU()  # type: ignore
        elif act == "sigmoid":
            activation = nn.Sigmoid()  # type: ignore
        elif act == "tanh":
            activation = nn.Tanh()  # type: ignore
        elif act == "softplus2":
            activation = SoftPlus2()  # type: ignore
        else:
            raise Exception("Undefined activation type, please try using swish, sigmoid, tanh, softplus2")

        self.edge_encoder = MLP(edge_dims, activation, activate_last=True, device=device)
        self.node_encoder = MLP(node_dims, activation, activate_last=True, device=device)
        self.attr_encoder = MLP(attr_dims, activation, activate_last=True, device=device)

        blocks_in_dim = hiddens[-1]
        block_out_dim = conv_hiddens[-1]
        block_args = {
            "conv_hiddens": conv_hiddens,
            "dropout": dropout,
            "act": activation,
            "skip": True,
            "device": device,
        }
        blocks = []

        # first block
        blocks.append(MEGNetBlock(dims=[blocks_in_dim], **block_args))  # type: ignore
        # other blocks
        for _ in range(num_blocks - 1):
            blocks.append(MEGNetBlock(dims=[block_out_dim, *hiddens], **block_args))  # type: ignore
        self.blocks = ModuleList(blocks)

        s2s_kwargs = {"n_iters": s2s_num_iters, "n_layers": s2s_num_layers}
        self.edge_s2s = EdgeSet2Set(block_out_dim, **s2s_kwargs)
        self.node_s2s = Set2Set(block_out_dim, **s2s_kwargs)

        self.output_proj = MLP(
            # S2S cats q_star to output producing double the dim
            dims=[2 * 2 * block_out_dim + block_out_dim, *output_hiddens, 1],
            activation=activation,
            activate_last=False,
            device=device,
        )

        self.dropout = Dropout(dropout) if dropout else None
        # TODO(marcel): should this be an 1D dropout

        self.is_classification = is_classification
        self.graph_transformations = graph_transformations or [Identity()] * num_blocks
        self.include_states = include_states

    def as_dict(self):
        out = {"state_dict": self.state_dict(), "model_args": self.model_args}
        return out

    @classmethod
    def from_dict(cls, dict, **kwargs):
        """
        build a MEGNet from a saved dictionary
        """
        # check if cuda is available
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        # check if mps is available
        device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
        dict["model_args"]["device"] = device
        model = MEGNet(**dict["model_args"])
        model.load_state_dict(dict["state_dict"], **kwargs)
        return model

    @classmethod
    def from_dir(cls, path, **kwargs):
        """
        build a MEGNet from a saved directory
        """
        file_name = os.path.join(path, MODEL_NAME + ".pt")
        if torch.cuda.is_available() is False:
            state = torch.load(file_name, map_location=torch.device("cpu"))
        else:
            state = torch.load(file_name)
        print(state["model"])
        model = MEGNet.from_dict(state["model"], **kwargs)
        return model

    @classmethod
    def load(cls, model_dir: str = "MP-2018.6.1-Eform") -> MEGNet:
        """
        Load the model weights from pre-trained model (megnet.pt)
        Args:
            model_dir (str): directory for saved model. Defaults to "MP-2018.6.1-Eform".

        Returns: MEGNet object.
        """
        if model_dir in MODEL_PATHS:
            return cls.from_dir(MODEL_PATHS[model_dir])

        if os.path.isdir(model_dir) and "megnet.pt" in os.listdir(model_dir):
            return cls.from_dir(model_dir)

        raise ValueError(f"{model_dir} not found in available pretrained {list(MODEL_PATHS.keys())}")

    def forward(
        self,
        graph: dgl.DGLGraph,
        edge_feat: torch.Tensor,
        node_feat: torch.Tensor,
        graph_attr: torch.Tensor,
    ):
        """
        TODO: Add docs.
        :param graph: Input graph
        :param edge_feat: Edge features
        :param node_feat: Node features
        :param graph_attr: Graph attributes / state features.
        :return: Prediction
        """
        graph_transformations = self.graph_transformations
        edge_feat = self.edge_encoder(self.edge_embed(edge_feat))
        node_feat = self.node_encoder(self.node_embed(node_feat))
        if self.include_states:
            graph_attr = self.attr_embed(graph_attr)
        else:
            graph_attr = self.attr_encoder(self.attr_embed(graph_attr))

        for i, block in enumerate(self.blocks):
            graph = graph_transformations[i](graph)
            output = block(graph, edge_feat, node_feat, graph_attr)
            edge_feat, node_feat, graph_attr = output

        node_vec = self.node_s2s(graph, node_feat)
        edge_vec = self.edge_s2s(graph, edge_feat)

        node_vec = torch.squeeze(node_vec)
        edge_vec = torch.squeeze(edge_vec)
        graph_attr = torch.squeeze(graph_attr)

        vec = torch.hstack([node_vec, edge_vec, graph_attr])

        if self.dropout:
            vec = self.dropout(vec)  # pylint: disable=E1102

        output = self.output_proj(vec)
        if self.is_classification:
            output = torch.sigmoid(output)

        return output


class MEGNetCalculator(nn.Module):
    """
    MEGNet Calculator
    Params:
    model (MEGNet): MEGNet model
    data_mean (torch.tensor): the average of training data
    data_std (torch.tensor): the standard deviation of training data
    """

    def __init__(self, model: MEGNet, data_std: torch.tensor | None = None, data_mean: torch.tensor | None = None):
        super().__init__()
        self.model = model
        self.data_mean = data_mean
        self.data_std = data_std

    def forward(self, g: dgl.DGLGraph, attrs: torch.tensor):
        """
        Args:
        g (dgl.DGLGraph): dgl graph
        attrs (torch.tensor): graph attributes
        Returns:
        output (torch.tensor): output property
        """
        output = (
            self.data_std * self.model(g, g.edata["edge_attr"].float(), g.ndata["node_type"].long(), attrs)
            + self.data_mean
        )
        return output
