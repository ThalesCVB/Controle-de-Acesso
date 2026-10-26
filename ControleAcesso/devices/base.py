class DeviceError(Exception):
    """Falha de comunicação ou recusa do dispositivo."""


class DeviceDriver:
    """Contrato que qualquer driver de catraca precisa cumprir.

    `dispositivo` é o model Dispositivo (ip, usuario_api, senha_api).
    `pessoa` é um dict simples, sem ligação com o banco:
        {"tipo": "usuario"|"visitante", "id": int, "nome": str,
         "foto_path": str|None, "valido_de": datetime|None, "valido_ate": datetime|None}
    """

    def ping(self, dispositivo):
        """True se a catraca responde."""
        raise NotImplementedError

    def enviar_pessoa(self, dispositivo, pessoa):
        """Cadastra/atualiza a pessoa (e a foto facial) na catraca."""
        raise NotImplementedError

    def remover_pessoa(self, dispositivo, pessoa):
        raise NotImplementedError

    def liberar_porta(self, dispositivo):
        """Abre a catraca remotamente (liberação de emergência)."""
        raise NotImplementedError

    def buscar_eventos(self, dispositivo, desde):
        """Eventos de acesso novos desde `desde` (datetime). Lista de dicts:
        {"quando", "pessoa_tipo", "pessoa_id", "pessoa_nome", "sentido", "permitido", "motivo"}
        Drivers que não fazem polling (modo push/callback) podem devolver []."""
        return []
