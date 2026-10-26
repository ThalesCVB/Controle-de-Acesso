"""Driver simulado: guarda as pessoas em memória e deixa uma catraca 'offline' de propósito
quando o IP termina em .12 (pra exercitar a fila de sincronização em dev)."""
from .base import DeviceDriver, DeviceError

_pessoas = {}  # (dispositivo_id, tipo, id) -> dict


class MockDriver(DeviceDriver):
    def _checa(self, dispositivo):
        if dispositivo.ip.endswith(".12"):
            raise DeviceError("catraca %s não responde (simulado)" % dispositivo.ip)

    def ping(self, dispositivo):
        return not dispositivo.ip.endswith(".12")

    def enviar_pessoa(self, dispositivo, pessoa):
        self._checa(dispositivo)
        _pessoas[(dispositivo.id, pessoa["tipo"], pessoa["id"])] = pessoa

    def remover_pessoa(self, dispositivo, pessoa):
        self._checa(dispositivo)
        _pessoas.pop((dispositivo.id, pessoa["tipo"], pessoa["id"]), None)

    def liberar_porta(self, dispositivo):
        self._checa(dispositivo)
