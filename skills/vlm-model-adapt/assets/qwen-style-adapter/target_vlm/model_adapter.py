"""Complete Qwen-style Dense image-text VLM Adapter template.

This file demonstrates msModelSlim integration structure only. Replace every
target-model placeholder and re-derive processor, module, forward, configuration
and weight semantics from the exact target model source.
"""

from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Generator, Iterable, List, Tuple, Type
from unittest.mock import patch

import torch
from safetensors import safe_open
from torch import nn
from transformers import AutoProcessor

from msmodelslim.core.base.protocol import ProcessRequest
from msmodelslim.core.const import DeviceType
from msmodelslim.model.common.layer_wise_forward import (
    generated_decoder_layer_visit_func,
)
from msmodelslim.model.common.vlm_base import VLMBaseModelAdapter
from msmodelslim.model.interface_hub import (
    ModelInfoInterface,
    ModelSlimPipelineInterfaceV1,
)
from msmodelslim.utils.logging import logger_setter
from msmodelslim.utils.security import (
    MAX_READ_FILE_SIZE_32G,
    get_valid_read_path,
    json_safe_load,
)


ConfigSnapshot = List[Tuple[Any, str, Any]]


def _cast_floating_state_dict(
    state_dict: Dict[str, torch.Tensor],
    dtype: torch.dtype,
) -> Dict[str, torch.Tensor]:
    """Cast floating tensors while preserving integer and boolean state."""
    return {
        key: value.to(dtype) if value.is_floating_point() else value
        for key, value in state_dict.items()
    }


@logger_setter()
class TargetVLMModelAdapter(
    VLMBaseModelAdapter,
    ModelInfoInterface,
    ModelSlimPipelineInterfaceV1,
):
    """Replace all placeholders with behavior proven by the target model."""

    TARGET_FORWARD_KEYS = (
        "input_ids",
        "attention_mask",
        "pixel_values",
    )
    TARGET_FORWARD_DEFAULTS: Dict[str, Any] = {}

    def get_model_pedigree(self) -> str:
        """Return the stable practice group for the target model family."""
        raise NotImplementedError("Replace with the target-model pedigree key")

    def get_model_type(self) -> str:
        """Preserve the concrete registration name passed by the factory."""
        return self.model_type

    def _build_messages_for_target_model(self, sample: Any) -> List[dict]:
        """Validate one image-text sample and build target processor messages."""
        return [
            {
                "role": "user",
                "content": [
                    {"type": "image", "image": sample.image},
                    {"type": "text", "text": sample.text},
                ],
            }
        ]

    def handle_dataset(
        self,
        dataset: Any,
        device: DeviceType = DeviceType.NPU,
    ) -> List[Any]:
        """Convert verified image-text samples into target forward inputs."""
        self._processor = AutoProcessor.from_pretrained(
            self.model_path,
            trust_remote_code=self.trust_remote_code,
            local_files_only=True,
        )

        processed_data = []
        for sample in dataset:
            messages = self._build_messages_for_target_model(sample)
            inputs = self._processor.apply_chat_template(
                messages,
                tokenize=True,
                add_generation_prompt=True,
                return_dict=True,
                return_tensors="pt",
            )
            processed_data.append(
                self._collect_inputs_to_device(
                    inputs,
                    device,
                    keys=self.TARGET_FORWARD_KEYS,
                    defaults=self.TARGET_FORWARD_DEFAULTS,
                )
            )
        return processed_data

    def _resolve_target_model_class(self) -> Type[nn.Module]:
        """Import the exact conditional-generation class for the target model."""
        raise NotImplementedError("Resolve the target model class")

    @staticmethod
    def _snapshot_config_attributes(
        attributes: Iterable[Tuple[Any, str]],
    ) -> ConfigSnapshot:
        """Capture every configuration value before temporary mutation."""
        return [
            (config, name, getattr(config, name))
            for config, name in attributes
        ]

    @staticmethod
    def _restore_config_attributes(snapshot: ConfigSnapshot) -> None:
        """Restore parent configs before child configs to undo recursive setters."""
        for config, name, value in snapshot:
            setattr(config, name, value)

    def init_model(self, device: DeviceType = DeviceType.NPU) -> nn.Module:
        """Load the complete vision path and only the first text layer."""
        del device  # Initialization stays on CPU; Runner controls execution devices.
        model_class = self._resolve_target_model_class()
        global_torch_dtype = self.get_global_model_torch_dtype()

        layer_config = self.config.text_config
        attention_configs = (
            self.config,
            self.config.text_config,
            self.config.vision_config,
        )
        origin_layers = layer_config.num_hidden_layers
        config_snapshot = self._snapshot_config_attributes(
            [
                (self.config, "use_cache"),
                *((config, "_attn_implementation") for config in attention_configs),
                (layer_config, "num_hidden_layers"),
            ]
        )
        init_succeeded = False

        try:
            layer_config.num_hidden_layers = 1
            self.config.use_cache = False
            self.config._attn_implementation = "eager"

            model = model_class.from_pretrained(
                self.model_path,
                config=self.config,
                trust_remote_code=self.trust_remote_code,
                torch_dtype=global_torch_dtype,
                local_files_only=True,
                device_map="cpu",
                attn_implementation="eager",
            ).eval()

            layer_config.num_hidden_layers = origin_layers
            self.config._attn_implementation = "eager"

            state_dict = self._get_state_dict(model)
            state_dict = _cast_floating_state_dict(
                state_dict,
                global_torch_dtype,
            )
            missing, unexpected = model.load_state_dict(state_dict, strict=False)
            self._validate_init_state_dict_result(missing, unexpected)

            init_succeeded = True
            return model
        finally:
            if init_succeeded:
                layer_config.num_hidden_layers = origin_layers
            else:
                self._restore_config_attributes(config_snapshot)

    def _validate_init_state_dict_result(
        self,
        missing: List[str],
        unexpected: List[str],
    ) -> None:
        """Allow only weights intentionally deferred to layer-wise loading."""
        raise NotImplementedError(
            "Validate target-specific missing and unexpected keys"
        )

    @lru_cache(maxsize=1)
    def _get_weight_map(self) -> Dict[str, str]:
        """Map each checkpoint tensor name to its safetensors shard."""
        index_path = Path(self.model_path) / "model.safetensors.index.json"
        if index_path.exists():
            index_data = json_safe_load(str(index_path))
            weight_map = index_data.get("weight_map")
            if not isinstance(weight_map, dict) or not weight_map:
                raise RuntimeError(f"Invalid or empty weight_map: {index_path}")
            return weight_map

        weight_map: Dict[str, str] = {}
        for shard_path in sorted(Path(self.model_path).glob("*.safetensors")):
            safe_path = get_valid_read_path(
                str(shard_path),
                extensions="safetensors",
                size_max=MAX_READ_FILE_SIZE_32G,
            )
            with safe_open(safe_path, framework="pt", device="cpu") as shard:
                for tensor_name in shard.keys():
                    weight_map[tensor_name] = shard_path.name

        if not weight_map:
            raise RuntimeError(
                f"No safetensors weights found under: {self.model_path}"
            )
        return weight_map

    def _get_state_dict(
        self,
        module: nn.Module,
        prefix: str = "",
    ) -> Dict[str, torch.Tensor]:
        """Load one module's parameters and persistent buffers shard-wise."""
        weight_map = self._get_weight_map()
        shard_to_tensors = defaultdict(list)

        for tensor_name in module.state_dict().keys():
            full_name = f"{prefix}.{tensor_name}" if prefix else tensor_name
            shard_name = weight_map.get(full_name)
            if shard_name is not None:
                shard_to_tensors[shard_name].append(tensor_name)

        state_dict: Dict[str, torch.Tensor] = {}
        for shard_name, tensor_names in shard_to_tensors.items():
            shard_path = get_valid_read_path(
                str(Path(self.model_path) / shard_name),
                extensions="safetensors",
                size_max=MAX_READ_FILE_SIZE_32G,
            )
            with safe_open(shard_path, framework="pt", device="cpu") as shard:
                for tensor_name in tensor_names:
                    full_name = (
                        f"{prefix}.{tensor_name}" if prefix else tensor_name
                    )
                    state_dict[tensor_name] = shard.get_tensor(full_name)
        return state_dict

    def _build_target_decoder(self, layer_idx: int) -> nn.Module:
        """Construct the target decoder with its exact configuration."""
        raise NotImplementedError(
            f"Build the target decoder layer at index {layer_idx}"
        )

    def _get_target_text_layers(self, model: nn.Module) -> nn.ModuleList:
        """Return the target model's real text-decoder ModuleList."""
        raise NotImplementedError("Locate the target text layer container")

    @staticmethod
    def _is_materialized(module: nn.Module) -> bool:
        """Return whether all parameters and buffers exist outside meta."""
        tensors = list(module.parameters()) + list(module.buffers())
        return bool(tensors) and all(not tensor.is_meta for tensor in tensors)

    def _load_decoder_if_not_exist(
        self,
        model: nn.Module,
        name: str,
        layer_idx: int,
    ) -> nn.Module:
        """Reuse a materialized decoder or construct and load it on demand."""
        try:
            decoder = model.get_submodule(name)
            if self._is_materialized(decoder):
                return decoder
        except AttributeError:
            pass

        with patch.object(nn.Linear, "reset_parameters", lambda _self: None):
            decoder = self._build_target_decoder(layer_idx)

        state_dict = self._get_state_dict(decoder, prefix=name)
        if not state_dict:
            raise RuntimeError(
                f"No checkpoint weights matched decoder prefix: {name}"
            )

        model_dtype = self.get_global_model_torch_dtype()
        state_dict = _cast_floating_state_dict(state_dict, model_dtype)
        decoder.load_state_dict(state_dict, strict=True)
        decoder.eval()

        layers = self._get_target_text_layers(model)
        if len(layers) <= layer_idx:
            layers.append(decoder)
        else:
            layers[layer_idx] = decoder
        return decoder

    def generate_decoder_layer(
        self,
        model: nn.Module,
    ) -> Generator[Tuple[str, nn.Module], None, None]:
        """Yield each target decoder in checkpoint order."""
        num_layers = self.config.text_config.num_hidden_layers
        for layer_idx in range(num_layers):
            name = f"model.language_model.layers.{layer_idx}"
            yield name, self._load_decoder_if_not_exist(
                model,
                name,
                layer_idx,
            )

    def generate_model_visit(
        self,
        model: nn.Module,
    ) -> Generator[ProcessRequest, Any, None]:
        """Visit the complete vision path, then each text decoder."""
        yield ProcessRequest(
            name="model.visual",
            module=model.model.visual,
            args=(),
            kwargs={},
        )
        yield from generated_decoder_layer_visit_func(
            model,
            transformer_blocks=self.generate_decoder_layer(model),
        )

    def _prepare_text_layer_inputs(
        self,
        model: nn.Module,
        sample: Dict[str, Any],
        inputs_embeds: Any,
        attention_mask: Any,
    ) -> Dict[str, Any]:
        """Reproduce the target position, mask, rotary and cache inputs."""
        raise NotImplementedError("Derive decoder inputs from the target forward")

    def generate_model_forward(
        self,
        model: nn.Module,
        inputs: Any,
    ) -> Generator[ProcessRequest, Any, None]:
        """Run a Qwen-style vision, fusion and layer-wise forward."""
        sample = inputs[0] if isinstance(inputs, list) else inputs

        pixel_values = sample["pixel_values"]
        image_grid_thw = sample["image_grid_thw"]
        image_embeds, deepstack_image_embeds = yield ProcessRequest(
            name="model.visual",
            module=model.model.visual,
            args=(pixel_values, image_grid_thw),
            kwargs={},
        )

        input_ids = sample["input_ids"]
        attention_mask = sample["attention_mask"]
        inputs_embeds = model.model.language_model.embed_tokens(input_ids)

        if isinstance(image_embeds, (list, tuple)):
            image_embeds = torch.cat(image_embeds, dim=0)
        image_embeds = image_embeds.to(
            inputs_embeds.device,
            inputs_embeds.dtype,
        )

        image_mask = (
            (input_ids == model.config.image_token_id)
            .unsqueeze(-1)
            .expand_as(inputs_embeds)
        )
        inputs_embeds = inputs_embeds.masked_scatter(
            image_mask,
            image_embeds,
        )
        visual_pos_masks = image_mask[..., 0]

        layer_inputs = self._prepare_text_layer_inputs(
            model=model,
            sample=sample,
            inputs_embeds=inputs_embeds,
            attention_mask=attention_mask,
        )

        hidden_states = inputs_embeds
        for layer_idx, (name, layer) in enumerate(
            self.generate_decoder_layer(model)
        ):
            hidden_states = yield ProcessRequest(
                name=name,
                module=layer,
                args=(hidden_states,),
                kwargs=layer_inputs,
            )

            if (
                deepstack_image_embeds is not None
                and layer_idx < len(deepstack_image_embeds)
            ):
                visual_embeds = deepstack_image_embeds[layer_idx].to(
                    hidden_states.device,
                    hidden_states.dtype,
                )
                hidden_states = hidden_states.clone()
                hidden_states[visual_pos_masks, :] += visual_embeds

    def enable_kv_cache(
        self,
        model: nn.Module,
        need_kv_cache: bool,
    ) -> None:
        """Set the cache flag read by the verified target implementation."""
        model.config.use_cache = need_kv_cache
