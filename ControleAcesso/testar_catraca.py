"""Teste manual de uma catraca real (a senha é digitada aqui, nunca fica em arquivo).

    python testar_catraca.py 192.168.0.9 [caminho_da_foto.jpg]

Passos: ping -> cadastro de pessoa de teste (e foto, se informada) -> eventos recentes ->
abertura da porta (pede confirmação) -> remoção da pessoa de teste (opcionalmente depois de você testar o rosto na catraca).
"""
import getpass
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

from devices import DeviceError
from devices.intelbras import IntelbrasDriver

ip = sys.argv[1] if len(sys.argv) > 1 else input("IP da catraca: ")
foto = sys.argv[2] if len(sys.argv) > 2 else None
usuario = input("Usuário da API [admin]: ").strip() or "admin"
senha = getpass.getpass("Senha da API: ")
disp = SimpleNamespace(ip=ip, usuario_api=usuario, senha_api=senha)
drv = IntelbrasDriver()
teste = {"tipo": "usuario", "id": 999001, "nome": "TESTE SISTEMA", "foto_path": foto,
         "valido_de": datetime.now(), "valido_ate": datetime.now() + timedelta(days=1)}


def passo(nome, fn):
    try:
        r = fn()
        print("OK    %s %s" % (nome, "" if r in (None, True) else r))
        return True
    except DeviceError as e:
        print("ERRO  %s: %s" % (nome, e))
        return False


if passo("ping/autenticação", lambda: drv.ping(disp) or (_ for _ in ()).throw(DeviceError("sem resposta ou senha recusada"))):
    passo("cadastro da pessoa de teste" + (" + foto" if foto else ""), lambda: drv.enviar_pessoa(disp, teste))
    passo("eventos da última hora", lambda: "%d evento(s)" % len(drv.buscar_eventos(disp, datetime.now() - timedelta(hours=1))))
    if input("Abrir a porta agora? (s/N) ").strip().lower() == "s":
        passo("abrir porta", lambda: drv.liberar_porta(disp))
    if foto and input("Manter cadastrado para testar o rosto na catraca agora? (s/N) ").strip().lower() == "s":
        input("Passe o rosto na catraca e depois aperte Enter para ver os eventos e remover o cadastro de teste...")
        for e in drv.buscar_eventos(disp, datetime.now() - timedelta(minutes=10)):
            print("  %s  %-8s %-20s %s %s" % (e["quando"].strftime("%H:%M:%S"), e["sentido"], e["pessoa_nome"] or "(sem nome)",
                                              "LIBERADO" if e["permitido"] else "NEGADO", e["motivo"] or ""))
    passo("remover pessoa de teste", lambda: drv.remover_pessoa(disp, teste))
