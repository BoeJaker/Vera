"""Portable model lifecycle contracts."""

from .onnx_import import ONNXImportReceipt, inspect_and_register_onnx

__all__ = ["ONNXImportReceipt", "inspect_and_register_onnx"]
