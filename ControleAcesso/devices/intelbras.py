"""Driver real da linha Bio-T / faciais Intelbras (SS 3532 MF / MF W).

Baseado no Portal de Integração (Linha de Faciais): HTTP Digest, chamadas CGI em /cgi-bin/.
  - ping:            GET  global.cgi?action=getCurrentTime
  - usuário:         POST AccessUser.cgi?action=insertMulti|updateMulti, GET removeMulti
  - foto facial:     POST AccessFace.cgi?action=insertMulti|updateMulti (JPEG base64)
  - abrir porta:     GET  accessControl.cgi?action=openDoor&channel=1
  - eventos:         GET  recordFinder.cgi?action=find&name=AccessControlCardRec (polling)
"""
import base64
import io
import json
import re
from datetime import datetime

import requests
from requests.auth import HTTPDigestAuth

from .base import DeviceDriver, DeviceError

TIMEOUT = 15
PORTA = 0           # índice da porta nos cadastros (Doors)
CANAL = 1           # canal da porta no openDoor
VALIDO_DE_PADRAO = "2000-01-01 00:00:00"
VALIDO_ATE_PADRAO = "2037-12-31 23:59:59"
# Faixa exclusiva deste sistema na catraca (12 dígitos, acima de qualquer CPF numérico), pra não colidir com
# usuários cadastrados por outros programas (ex.: InControl): usuário = BASE + id, visitante = BASE*2 + id.
USUARIO_BASE = 100_000_000_000
VISITANTE_BASE = 2 * USUARIO_BASE

# Limites da foto (doc "Cadastrar Faces")
FOTO_MAX_BYTES = 100 * 1024
FOTO_MAX_LARGURA, FOTO_MAX_ALTURA = 600, 1200
FOTO_MIN_LARGURA = 150

ERROS_ACESSO = {  # "Códigos de Erro Eventos" da documentação
    16: "Sem autorização", 17: "Cartão perdido ou cancelado", 18: "Sem permissão nesta catraca",
    19: "Modo de abertura incorreto", 20: "Credencial expirada", 21: "Anti-passback",
    32: "Fora do horário", 33: "Horário inválido em feriado", 51: "Catraca: usuário não passou",
    97: "Rosto não confere", 98: "Tempo esgotado no reconhecimento", 99: "Entrada repetida",
    101: "Temperatura alta", 102: "Sem máscara", 164: "Usuário bloqueado", 165: "Limite de acessos do visitante",
    169: "Credencial expirada",
}


def user_id(tipo, pessoa_id):
    return str((VISITANTE_BASE if tipo == "visitante" else USUARIO_BASE) + pessoa_id)


def pessoa_de_user_id(uid):
    """Inverso de user_id(): (tipo, id), ou (None, None) se o ID não é deste sistema (ex.: InControl)."""
    try:
        n = int(uid)
    except (TypeError, ValueError):
        return None, None
    if n >= VISITANTE_BASE:
        return "visitante", n - VISITANTE_BASE
    if n >= USUARIO_BASE:
        return "usuario", n - USUARIO_BASE
    return None, None


def _fmt(dt, padrao):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else padrao


def foto_jpeg_base64(caminho):
    """Converte a foto para JPEG dentro dos limites da catraca e devolve em base64."""
    try:
        from PIL import Image
    except ImportError:
        raise DeviceError("Pillow não instalado (pip install pillow)")
    try:
        img = Image.open(caminho).convert("RGB")
    except Exception as e:
        raise DeviceError("foto ilegível: %s" % e)
    img.thumbnail((FOTO_MAX_LARGURA, FOTO_MAX_ALTURA))
    if img.width < FOTO_MIN_LARGURA or img.height < FOTO_MIN_LARGURA or img.height > 2 * img.width:
        raise DeviceError("foto fora das dimensões aceitas (%dx%d)" % img.size)
    for qualidade in (90, 80, 70, 60, 50, 40):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=qualidade)
        if buf.tell() <= FOTO_MAX_BYTES:
            return base64.b64encode(buf.getvalue()).decode()
    raise DeviceError("foto não coube em 100KB")


class IntelbrasDriver(DeviceDriver):
    # ---- infraestrutura ------------------------------------------------------------------
    def _req(self, dispositivo, metodo, caminho, **kw):
        url = "http://%s/cgi-bin/%s" % (dispositivo.ip, caminho)
        auth = HTTPDigestAuth(dispositivo.usuario_api, dispositivo.senha_api)
        try:
            r = requests.request(metodo, url, auth=auth, timeout=TIMEOUT, **kw)
        except requests.RequestException as e:
            raise DeviceError("catraca %s inacessível: %s" % (dispositivo.ip, e))
        if r.status_code == 401:
            raise DeviceError("catraca %s: usuário/senha da API recusados" % dispositivo.ip)
        if r.status_code != 200:
            raise DeviceError("catraca %s respondeu HTTP %s: %s" % (dispositivo.ip, r.status_code, r.text[:120]))
        return r.text

    @staticmethod
    def _ok(texto):
        return texto.strip().upper().startswith("OK")

    @staticmethod
    def _erro_json(texto):
        try:
            return json.loads(texto).get("message") or texto[:120]
        except ValueError:
            return texto[:120]

    # ---- contrato ------------------------------------------------------------------------
    def ping(self, dispositivo):
        try:
            self._req(dispositivo, "GET", "global.cgi?action=getCurrentTime")
            return True
        except DeviceError:
            return False

    def _gravar(self, dispositivo, recurso, acao_nova, corpo_nova, corpo_atual, falha_msg):
        """insertMulti; se já existir, updateMulti. Levanta DeviceError se ambos falharem."""
        txt = self._req(dispositivo, "POST", "%s?action=%s" % (recurso, acao_nova), json=corpo_nova)
        if self._ok(txt):
            return
        txt = self._req(dispositivo, "POST", "%s?action=updateMulti" % recurso, json=corpo_atual)
        if not self._ok(txt):
            raise DeviceError("%s: %s" % (falha_msg, self._erro_json(txt)))

    def enviar_pessoa(self, dispositivo, pessoa):
        uid = user_id(pessoa["tipo"], pessoa["id"])
        usuario = {
            "UserID": uid, "UserName": pessoa["nome"][:32], "UserType": 0, "Authority": 2,
            "Doors": [PORTA], "TimeSections": [255],
            "ValidFrom": _fmt(pessoa.get("valido_de"), VALIDO_DE_PADRAO),
            "ValidTo": _fmt(pessoa.get("valido_ate"), VALIDO_ATE_PADRAO),
        }
        corpo = {"UserList": [usuario]}
        self._gravar(dispositivo, "AccessUser.cgi", "insertMulti", corpo, corpo, "cadastro de usuário recusado")
        if pessoa.get("foto_path"):
            face = {"FaceList": [{"UserID": uid, "PhotoData": [foto_jpeg_base64(pessoa["foto_path"])]}]}
            self._gravar(dispositivo, "AccessFace.cgi", "insertMulti", face, face, "cadastro de face recusado")

    def remover_pessoa(self, dispositivo, pessoa):
        uid = user_id(pessoa["tipo"], pessoa["id"])
        # remover usuário também apaga as credenciais (face) dele
        txt = self._req(dispositivo, "GET", "AccessUser.cgi?action=removeMulti&UserIDList[0]=%s" % uid)
        if not self._ok(txt):
            raise DeviceError("remoção recusada: %s" % self._erro_json(txt))

    def liberar_porta(self, dispositivo):
        txt = self._req(dispositivo, "GET", "accessControl.cgi?action=openDoor&channel=%d&Type=Remote" % CANAL)
        if not self._ok(txt):
            raise DeviceError("abertura recusada: %s" % txt[:120])

    def buscar_eventos(self, dispositivo, desde):
        inicio = int(desde.timestamp())
        fim = int(datetime.now().timestamp()) + 60
        txt = self._req(dispositivo, "GET",
                        "recordFinder.cgi?action=find&name=AccessControlCardRec&StartTime=%d&EndTime=%d" % (inicio, fim))
        registros = {}
        for linha in txt.splitlines():
            m = re.match(r"records\[(\d+)\]\.(\w+)=(.*)$", linha.strip())
            if m:
                registros.setdefault(int(m.group(1)), {})[m.group(2)] = m.group(3)
        eventos = []
        for _, r in sorted(registros.items()):
            try:
                quando = datetime.fromtimestamp(int(r["CreateTime"]))
            except (KeyError, ValueError):
                continue
            if quando <= desde:
                continue
            tipo, pid = pessoa_de_user_id(r.get("UserID"))
            permitido = r.get("Status") == "1" and r.get("ErrorCode", "0") == "0"
            codigo = int(r.get("ErrorCode") or 0)
            eventos.append({
                "quando": quando, "pessoa_tipo": tipo, "pessoa_id": pid,
                "pessoa_nome": r.get("CardName") or "",
                "sentido": "Saída" if r.get("Type") == "Exit" else "Entrada",
                "permitido": permitido,
                "motivo": None if permitido else ERROS_ACESSO.get(codigo, "Negado (código %d)" % codigo),
            })
        return eventos
