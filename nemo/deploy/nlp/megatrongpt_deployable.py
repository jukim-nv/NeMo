import logging
from pathlib import Path

import numpy as np
import torch
import wrapt
from pytorch_lightning.trainer.trainer import Trainer

from nemo.collections.nlp.models.language_modeling.megatron_gpt_model import MegatronGPTModel
from nemo.collections.nlp.modules.common.text_generation_utils import (
    OutputType,
    get_default_length_params,
    get_default_sampling_params,
)
from nemo.collections.nlp.modules.common.transformer.text_generation import LengthParam, SamplingParam
from nemo.collections.nlp.parts.nlp_overrides import NLPDDPStrategy, NLPSaveRestoreConnector
from nemo.deploy import ITritonDeployable
from nemo.deploy.utils import cast_output, str_ndarray2list, typedict2tensor


@wrapt.decorator
def noop_decorator(func):
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)

    return wrapper


use_pytriton = True
batch = noop_decorator
try:
    from pytriton.decorators import batch
    from pytriton.model_config import Tensor
except Exception:
    use_pytriton = False

LOGGER = logging.getLogger("NeMo")

# utility funciton to get Triton Tensor shape from a python value
# assume that lists are shape -1 and all others are scalars with shape 1
def GetTensorShape(pyvalue):
    return (-1 if type(pyvalue) == list else 1,)


# utility function to get numpy dtype of a python value
# e.g. bool -> np.bool_
def GetNumpyDtype(pyvalue):
    # manually defining the mapping of python type -> numpy type for now
    # is there a better way to do it?  tried np.array(pyvalue).dtype, but that doesn't seem to work
    py_to_numpy_mapping = {str: bytes, bool: np.bool_, float: np.single, int: np.int_}
    python_type = type(pyvalue)
    # for lists, return the type of the internal elements
    if python_type == list:
        python_type = type(pyvalue[0])
    numpy_type = py_to_numpy_mapping[python_type]
    return numpy_type


class MegatronGPTDeployable(ITritonDeployable):
    def __init__(self, nemo_checkpoint_filepath: str, num_devices: int = 1, num_nodes: int = 1):
        self.nemo_checkpoint_filepath = nemo_checkpoint_filepath
        self._load(nemo_checkpoint_filepath, num_devices, num_nodes)

    def _load(self, nemo_checkpoint_filepath: str, num_devices: int, num_nodes: int):
        if Path(nemo_checkpoint_filepath).exists():
            trainer = Trainer(
                strategy=NLPDDPStrategy(),
                accelerator="gpu",
                precision="bf16",
                devices=num_devices,
                num_nodes=num_nodes,
            )
            # self.model = MegatronGPTModel.restore_from(nemo_checkpoint_filepath, trainer=trainer)

            custom_config = MegatronGPTModel.restore_from(nemo_checkpoint_filepath, trainer=trainer, return_config=True)
            # transformner_engine should always be true according to EricH
            custom_config.transformer_engine = True
            # using multi-gpu for tensor parallelism directly for now, could do pipeline parallel instead or a combination
            custom_config.tensor_model_parallel_size = num_devices

            self.model = MegatronGPTModel.restore_from(nemo_checkpoint_filepath, trainer=trainer, override_config_path=custom_config)

            self.model.eval()

    _INPUT_PARAMETER_FIELDS = {
        "prompts": (-1, bytes, False),
    }

    # there is no get_default equivalent for OutputType like there is for SamplingParameters and LengthParameters
    # but we still want to generate output using a real OutputType TypedDict for static type checking
    _BLANK_OUTPUTTYPE: OutputType = {
        'sentences': [""],
        'tokens': [[""]],
        'logprob': [[0.0]],
        'full_logprob': [[0.0]],
        'token_ids': [[0]],
        'offsets': [[0]],
    }

    @property
    def get_triton_input(self):
        input_parameters = tuple(
            Tensor(name=name, shape=(shape,), dtype=dtype, optional=optional)
            for name, (shape, dtype, optional) in self._INPUT_PARAMETER_FIELDS.items()
        )
        # TODO: in theory, would like to use typedict2tensor() function to generate Tensors, but it purposely ignores 1D arrays
        # need to find out why that is, asked Jakub Kosek on 2024-04-26, but he doesn't know who owns it
        # sampling_parameters = typedict2tensor(SamplingParam)
        default_sampling_params: SamplingParam = get_default_sampling_params()
        sampling_parameters = tuple(
            Tensor(
                name=parameter_name,
                shape=GetTensorShape(parameter_value),
                dtype=GetNumpyDtype(parameter_value),
                optional=True,
            )
            for parameter_name, parameter_value in default_sampling_params.items()
        )
        # length_parameters = typedict2tensor(LengthParam)
        default_length_params: LengthParam = get_default_length_params()
        length_parameters = tuple(
            Tensor(
                name=parameter_name,
                shape=GetTensorShape(parameter_value),
                dtype=GetNumpyDtype(parameter_value),
                optional=True,
            )
            for parameter_name, parameter_value in default_length_params.items()
        )

        inputs = input_parameters + sampling_parameters + length_parameters
        return inputs

    @property
    def get_triton_output(self):
        # outputs are defined by the fields of OutputType
        outputs = [
            Tensor(
                name=parameter_name, shape=GetTensorShape(parameter_value), dtype=GetNumpyDtype(parameter_value[0]),
            )
            for parameter_name, parameter_value in MegatronGPTDeployable._BLANK_OUTPUTTYPE.items()
        ]
        return outputs

    @staticmethod
    def _sampling_params_from_triton_inputs(**inputs: np.ndarray):
        sampling_params: SamplingParam = get_default_sampling_params()
        for sampling_param_field in sampling_params.keys():
            if sampling_param_field in inputs:
                sampling_params[sampling_param_field] = inputs.pop(sampling_param_field)[0][0]
        return sampling_params

    @staticmethod
    def _length_params_from_triton_inputs(**inputs: np.ndarray):
        length_params: LengthParam = get_default_length_params()
        for length_param_field in length_params.keys():
            if length_param_field in inputs:
                length_params[length_param_field] = inputs.pop(length_param_field)[0][0]
        return length_params

    @batch
    def triton_infer_fn(self, **inputs: np.ndarray):
        input_strings = str_ndarray2list(inputs.pop("prompts"))
        sampling_params = self._sampling_params_from_triton_inputs(**inputs)
        length_params = self._length_params_from_triton_inputs(**inputs)

        model_output = self.model.generate(
            inputs=input_strings, length_params=length_params, sampling_params=sampling_params
        )
        # sentences will be a list of strings (one per prompts)
        # other fields will either be a list of lists (tokens, for example)
        # or a list of pytorch Tensor
        # we expect all lists to be the same length
        # TODO: add an error check for when they aren't, like if you feed in prompts of varying lengths to compute logprob

        num_prompts = len(input_strings)
        triton_output = {}
        for model_output_field, value in model_output.items():
            field_dtype = GetNumpyDtype(MegatronGPTDeployable._BLANK_OUTPUTTYPE[model_output_field][0])
            if value is None:
                # triton does not allow for optional output parameters, so need to populate them if they don't exist
                # 'sentences' should always have a valid value, so use that for the output shape
                triton_output[model_output_field] = np.full(
                    np.shape(model_output['sentences']),
                    MegatronGPTDeployable._BLANK_OUTPUTTYPE[model_output_field][0],
                    dtype=field_dtype,
                )
            elif field_dtype == bytes:
                # strings are cast to bytes
                triton_output[model_output_field] = cast_output(value, field_dtype)
            elif isinstance(value[0], torch.Tensor):
                if value[0].dtype == torch.bfloat16:
                    # numpy currently does not support bfloat16, so need to manually convert it
                    triton_output[model_output_field] = np.array([tensor.cpu().float().numpy() for tensor in value])
                else:
                    triton_output[model_output_field] = np.array([cast_output(entry, field_dtype) for entry in value])
            else:
                # non-strings are output as-is (in numpy format)
                triton_output[model_output_field] = np.array(value)
        return triton_output
