# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import argparse
import importlib
from typing import Union, Dict
import os

from contextlib import ExitStack

from fairseq_signals.dataclass import Dataclass
from fairseq_signals.dataclass.utils import merge_with_parent, populate_dataclass
from fairseq_signals.utils import checkpoint_utils
from hydra.core.config_store import ConfigStore
from omegaconf import open_dict, OmegaConf

from .distributed_model import DistributedModel
from .model import (
    BaseModel,
)

MODEL_REGISTRY = {}
MODEL_DATACLASS_REGISTRY = {}
ARCH_MODEL_REGISTRY = {}
ARCH_MODEL_NAME_REGISTRY = {}
ARCH_MODEL_INV_REGISTRY = {}
ARCH_CONFIG_REGISTRY = {}

__all__ = [
    "BaseModel",
    "DistributedModel"
]

def build_model(
    cfg: Union[Dataclass, Dict],
    task,
    from_checkpoint=False,
    checkpoint_path=None,
) -> BaseModel:
    model = None
    if isinstance(cfg, Dict):
        cfg = OmegaConf.create(cfg)
    model_type = getattr(cfg, "_name", None) or getattr(cfg, "arch", None)

    if not model_type and len(cfg) == 1:
        # this is hit if config object is nested in directory that is named after model type

        model_type = next(iter(cfg))
        if model_type in MODEL_DATACLASS_REGISTRY:
            cfg = cfg[model_type]
        else:
            raise Exception(
                "Could not infer model type from directory. Please add _name field to indicate model type. "
                "Available models: "
                + str(MODEL_DATACLASS_REGISTRY.keys())
                + " Requested model type: "
                + model_type
            )
    
    if model_type in ARCH_MODEL_REGISTRY:
        # case 1: legacy models
        model = ARCH_MODEL_REGISTRY[model_type]
    elif model_type in MODEL_DATACLASS_REGISTRY:
        # case 2: config-driven models
        model = MODEL_REGISTRY[model_type]
    
    if model_type in MODEL_DATACLASS_REGISTRY:
        # set defaults from dataclass. note that arch name and model name can be the same
        dc = MODEL_DATACLASS_REGISTRY[model_type]
        if isinstance(cfg, argparse.Namespace):
            cfg = populate_dataclass(dc(), cfg)
        else:
            cfg = merge_with_parent(dc(), cfg, from_checkpoint)
    else:
        if model_type in ARCH_CONFIG_REGISTRY:
            with open_dict(cfg) if OmegaConf.is_config(cfg) else ExitStack():
                # this calls the different "arch" functions (like base_architecture()) that you indicate
                # if you specify --arch on the command line. this is only applicable to the old argparse based models
                # hydra models should expose different architectures via different config files
                # it will modify the cfg object and default parameters according to the arch
                ARCH_CONFIG_REGISTRY[model_type](cfg)

    assert model is not None, (
        f"Could not infer model type from {cfg}. "
        f"Available models: "
        + str(MODEL_DATACLASS_REGISTRY.keys())
        + " Requested model type: "
        + model_type
    )

    model_instance = model.build_model(cfg, task)
    if checkpoint_path is not None and from_checkpoint:
        state = checkpoint_utils.load_checkpoint_to_cpu(checkpoint_path)
        model_instance.load_state_dict(state["model"], strict=True)

    return model_instance

def build_model_from_checkpoint(checkpoint_path):
    state = checkpoint_utils.load_checkpoint_to_cpu(checkpoint_path)
    model_cfg = state["cfg"]["model"]
    # set `no_pretrained_weights` to True as we will load the whole model weights eventually
    if hasattr(model_cfg, "no_pretrained_weights") and not model_cfg.no_pretrained_weights:
        model_cfg.no_pretrained_weights = True

    return build_model(model_cfg, task=None, from_checkpoint=True, checkpoint_path=checkpoint_path)

def register_model(name, dataclass=None):
    """
    New model types can be added to fairseq_signals with the :func:`register_model`
    function decorator.

    For example::

        @register_model('lstm')
        class LSTM(EncoderDecoderModel):
            (...)
    
    .. note:: All models must implement the :class:`BaseModel` interface.
        Typically you will extend :class:`EncoderDecoderModel` for
        sequence-to-sequence tasks or :class:`LanguageModel` for
        language modeling tasks

    Args:
        name (str): the name of the model
    """

    def register_model_cls(cls):
        if name in MODEL_REGISTRY:
            raise ValueError("Cannot register duplicate model ({})".format(name))
        if not issubclass(cls, BaseModel):
            raise ValueError(
                "Model ({}: {}) must extend BaseModel".format(name, cls.__name__)
            )
        MODEL_REGISTRY[name] = cls
        if dataclass is not None and not issubclass(dataclass, Dataclass):
            raise ValueError(
                "Dataclass {} must extend Dataclass".format(dataclass)
            )
        
        cls.__dataclass = dataclass
        if dataclass is not None:
            MODEL_DATACLASS_REGISTRY[name] = dataclass

            cs = ConfigStore.instance()
            node = dataclass()
            node._name = name
            cs.store(name=name, group="model", node=node, provider="fairseq-signals")
        
            @register_model_architecture(name, name)
            def noop(_):
                pass
        
        return cls
    
    return register_model_cls

def register_model_architecture(model_name, arch_name):
    """
    New model architectures can be added to fairseq_ecg with the
    :func:`register_model_architecture` function decorator. After registration,
    model architectures can be selected with the ``--arch`` command-line
    argument.

    For example::

        @register_model_architecture('lstm', 'lstm_luong_wmt_en_de')
        def lstm_luong_wmt_en_de(cfg):
            cfg.encoder_embed_dim = getattr(cfg.model, 'encoder_embed_dim', 1000)
            (...)
    
    The decorated function should take a single argument *cfg*, which is a
    :clss:`omegaconf.DictConfig`. The decorated function should modify these
    arguments in-place to match the desired architecture.
    
    Args:
        model_name (str): the name of the Model (Model must already be registered)
        arch_name (str): the name of the model architecture (``--arch``)
    """

    def register_model_arch_fn(fn):
        if model_name not in MODEL_REGISTRY:
                raise ValueError(
                    "Cannot register model architecture for unknown model type ({})".format(
                        model_name
                    )
                )
        if arch_name in ARCH_MODEL_REGISTRY:
            raise ValueError(
                "Cannot register duplicate model architecture ({})".format(arch_name)
            )
        if not callable(fn):
            raise ValueError(
                "Model architecture must be callable ({})".format(arch_name)
            )
        ARCH_MODEL_REGISTRY[arch_name] = MODEL_REGISTRY[model_name]
        ARCH_MODEL_NAME_REGISTRY[arch_name] = model_name
        ARCH_MODEL_INV_REGISTRY.setdefault(model_name, []).append(arch_name)
        ARCH_CONFIG_REGISTRY[arch_name] = fn
        return fn

    return register_model_arch_fn

def import_models(models_dir, namespace):
    for file in os.listdir(models_dir):
        path = os.path.join(models_dir, file)
        if (
            not file.startswith("_")
            and not file.startswith(".")
            and (file.endswith(".py") or os.path.isdir(path))
        ):
            model_name = file[: file.find(".py")] if file.endswith(".py") else file
            importlib.import_module(namespace + "." + model_name)

            # extra `model_parser` for sphinx
            if model_name in MODEL_REGISTRY:
                parser = argparse.ArgumentParser(add_help=False)
                group_archs = parser.add_argument_group("Named architectures")
                group_archs.add_argument(
                    "--arch", choices=ARCH_MODEL_INV_REGISTRY[model_name]
                )
                group_args = parser.add_argument_group(
                    "Additional command-line arguments"
                )
                MODEL_REGISTRY[model_name].add_args(group_args)
                globals()[model_name + "_parser"] = parser


# automatically import any Python files in the models/ directory
models_dir = os.path.dirname(__file__)
import_models(models_dir, "fairseq_signals.models")
