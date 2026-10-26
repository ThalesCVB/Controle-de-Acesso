"""Fluxo ponta a ponta contra um banco temporário. Rodar: python test_e2e.py"""
import base64
import io
import os
import re
import sys
import tempfile

tmp = tempfile.mkdtemp()
os.environ.update(SECRET_KEY="teste", NO_WORKER="1",
                  DATABASE_URL="sqlite:///" + os.path.join(tmp, "t.db").replace("\\", "/"))
import app as A  # noqa: E402
from models import Operador, SyncJob, Usuario  # noqa: E402

JPEG = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0" + b"0" * 64).decode()
ok = True


def check(nome, cond):
    global ok
    ok &= bool(cond)
    print(("OK   " if cond else "FAIL ") + nome)


def cliente(login, senha):
    c = A.app.test_client()
    html = c.get("/login").get_data(as_text=True)
    tok = re.search(r'name="_csrf" value="(\w+)"', html).group(1)
    r = c.post("/login", data={"login": login, "senha": senha, "_csrf": tok})
    return c, r


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


with A.app.app_context():
    for login, perfil in (("ti", "ti"), ("port", "portaria")):
        o = Operador(login=login, nome=login.upper(), perfil=perfil, trocar_senha=False)
        o.set_senha("Senha123")
        A.db.session.add(o)
    A.db.session.commit()

ti, r = cliente("ti", "Senha123")
check("login TI", r.status_code == 302)
check("POST sem CSRF barrado", ti.post("/emergencia").status_code == 400)
check("dashboard", b"Usu\xc3\xa1rios ativos" in ti.get("/").data)
for p in ("/monitoramento", "/usuarios", "/usuarios/novo", "/visitantes", "/visitantes/novo", "/grupos",
          "/grupos/novo", "/dispositivos", "/dispositivos/novo", "/operadores", "/relatorios"):
    check("GET " + p, ti.get(p).status_code == 200)

# usuário com foto -> fila -> envio (uma catraca fica offline no mock: .12)
r = ti.post("/usuarios/novo", data={"_csrf": csrf(ti), "nome": "Carla Menezes", "cpf": "529.982.247-25",
                                    "cargo": "Enfermeira", "grupo_id": 1, "foto_data": JPEG})
check("cria usuário", r.status_code == 302)
with A.app.app_context():
    jobs = SyncJob.query.all()
    check("6 enviados + 1 pendente (catraca offline)",
          sum(j.status == "ok" for j in jobs) == 6 and sum(j.status == "pendente" for j in jobs) == 1)
check("CPF inválido rejeitado", b"CPF inv" in ti.post("/usuarios/novo", data={
    "_csrf": csrf(ti), "nome": "X", "cpf": "111.111.111-11", "grupo_id": 1}).data)
check("foto falsa rejeitada", b"Foto inv" in ti.post("/usuarios/novo", data={
    "_csrf": csrf(ti), "nome": "Y", "cpf": "168.995.350-09", "grupo_id": 1,
    "foto_data": "data:image/png;base64," + base64.b64encode(b"nao-e-png").decode()}).data)

# visitante
r = ti.post("/visitantes/novo", data={"_csrf": csrf(ti), "nome": "Renata Dias", "documento": "MG-1",
                                      "anfitriao_id": 1, "valido_de": "2020-01-01T08:00",
                                      "valido_ate": "2099-01-01T18:00", "foto_data": JPEG})
check("cria visitante", r.status_code == 302 and b"Renata" in ti.get("/visitantes").data)

# simulação, monitoramento e relatório
for _ in range(15):
    ti.post("/api/simular", headers={"X-CSRF": csrf(ti)})
evs = ti.get("/api/eventos?depois=0").get_json()
check("eventos gerados", len(evs) > 0)
check("relatório html", ti.get("/relatorios?gerar=1&formato=html").status_code == 200)
csv_r = ti.get("/relatorios?gerar=1&formato=csv")
check("csv", csv_r.status_code == 200 and csv_r.data.startswith(b"\xef\xbb\xbfPessoa;"))
pdf_r = ti.get("/relatorios?gerar=1&formato=pdf")
check("pdf", pdf_r.data.startswith(b"%PDF"))

# emergência
ti.post("/emergencia", data={"_csrf": csrf(ti)})
check("emergência registrada", any(e["nome"] == "Liberação de emergência" for e in ti.get("/api/eventos?depois=0").get_json()))

# portaria: só cadastros e relatórios
po, _ = cliente("port", "Senha123")
check("portaria vai pra usuários", po.get("/").headers["Location"].endswith("/usuarios"))
for p in ("/grupos", "/dispositivos", "/monitoramento", "/operadores"):
    check("portaria 403 em " + p, po.get(p).status_code == 403)
check("portaria emergência 403", po.post("/emergencia", data={"_csrf": csrf(po)}).status_code == 403)
for p in ("/usuarios", "/visitantes", "/relatorios"):
    check("portaria acessa " + p, po.get(p).status_code == 200)

# inativar remove credencial
r = ti.post("/usuarios/1/editar", data={"_csrf": csrf(ti), "nome": "Carla Menezes", "cpf": "52998224725",
                                        "grupo_id": 1, "status": "bloqueado"})
with A.app.app_context():
    check("bloqueio enfileira remoção", SyncJob.query.filter_by(acao="remover").count() > 0)

# nomes: padronização, busca tolerante e aviso de parecidos
r = ti.post("/usuarios/novo", data={"_csrf": csrf(ti), "nome": "  jhonatan   PEREIRA da silva ", "cpf": "111.444.777-35",
                                    "cargo": "", "grupo_id": 1})
with A.app.app_context():
    check("nome padronizado ao salvar", Usuario.query.filter_by(cpf="11144477735").first().nome == "Jhonatan Pereira da Silva")
check("busca acha com acento/grafia diferente", b"Jhonatan Pereira" in ti.get("/usuarios?q=jonatan+pereyra").data)
check("busca não traz quem não tem a ver", b"Carla Menezes" not in ti.get("/usuarios?q=jonatan").data)
ap = ti.get("/api/nomes-parecidos?nome=Jonatan+Pereira+Silva").get_json()
check("aviso de nome parecido", any(x["nome"] == "Jhonatan Pereira da Silva" for x in ap))
check("sem aviso pra nome diferente", ti.get("/api/nomes-parecidos?nome=Pedro+Alves").get_json() == [])
check("busca em relatório tolerante", ti.get("/relatorios?quem=carla+menesez").status_code == 200)

from datetime import datetime, timedelta  # noqa: E402
ti.post("/visitantes/novo", data={"_csrf": csrf(ti), "nome": "Rafaela Albuquerque", "documento": "12.345.678-9", "anfitriao_id": 1,
                                  "valido_de": datetime.now().strftime("%Y-%m-%dT%H:%M"),
                                  "valido_ate": (datetime.now() + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M"), "foto_data": JPEG})
sug = ti.get("/api/visitantes-anteriores?q=rafaella+albukerke").get_json()
check("sugere visitante anterior pelo nome errado", sug and sug[0]["documento"] == "12.345.678-9")
check("sugere visitante anterior pelo documento", ti.get("/api/visitantes-anteriores?q=123456").get_json()[0]["nome"] == "Rafaela Albuquerque")

# lockout de login
for _ in range(5):
    cliente("ti", "errada")
_, r = cliente("ti", "Senha123")
check("login bloqueado após 5 falhas", r.status_code == 200)

print("\nTUDO OK" if ok else "\nHÁ FALHAS")
sys.exit(0 if ok else 1)
