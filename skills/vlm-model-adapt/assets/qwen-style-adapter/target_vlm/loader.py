"""Loader template for the target VLM Adapter."""

from msmodelslim.model.plugin_factory.base_loader import BaseModelAdapterLoader


class TargetVLMAdapterLoader(BaseModelAdapterLoader):
    """Resolve the Adapter only after dependency validation succeeds."""

    ADAPTER_CLASS_PATH = (
        "msmodelslim.model.target_vlm.model_adapter:TargetVLMModelAdapter"
    )
