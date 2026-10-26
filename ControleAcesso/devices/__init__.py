"""Camada de integração com as catracas. O resto do app só fala com `get_driver()`."""
from .base import DeviceDriver, DeviceError


def get_driver(nome):
    if nome == "mock":
        from .mock import MockDriver
        return MockDriver()
    if nome == "intelbras":
        from .intelbras import IntelbrasDriver
        return IntelbrasDriver()
    raise RuntimeError("DEVICE_DRIVER inválido: %r" % nome)


__all__ = ["DeviceDriver", "DeviceError", "get_driver"]
