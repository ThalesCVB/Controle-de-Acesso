"""Regras de negócio: fila de sincronização, simulação de acessos, emergência."""
import os
import random
from datetime import timedelta

import config
from devices import DeviceError, get_driver
from models import (Dispositivo, EventoAcesso, Grupo, SyncJob, Usuario, Visitante,
                    agora, db)

_driver = None


def driver():
    global _driver
    if _driver is None:
        _driver = get_driver(config.DEVICE_DRIVER)
    return _driver


def _pessoa_dict(tipo, pessoa):
    foto = os.path.join(config.FOTOS_DIR, pessoa.foto) if pessoa.foto else None
    return {
        "tipo": tipo, "id": pessoa.id, "nome": pessoa.nome, "foto_path": foto,
        "valido_de": getattr(pessoa, "valido_de", None),
        "valido_ate": getattr(pessoa, "valido_ate", None),
    }


def _carregar(tipo, pessoa_id):
    return db.session.get(Usuario if tipo == "usuario" else Visitante, pessoa_id)


def dispositivos_da_pessoa(tipo, pessoa):
    """Usuário: dispositivos liberados pro grupo. Visitante: todos."""
    if tipo == "usuario":
        return list(pessoa.grupo.dispositivos)
    return Dispositivo.query.all()


def enfileirar(tipo, pessoa, acao="upsert", dispositivos=None):
    """Cria um SyncJob por catraca (sem commit — quem chama commita)."""
    alvos = dispositivos if dispositivos is not None else dispositivos_da_pessoa(tipo, pessoa)
    for d in alvos:
        # evita acumular upserts repetidos ainda pendentes
        db.session.query(SyncJob).filter_by(
            dispositivo_id=d.id, pessoa_tipo=tipo, pessoa_id=pessoa.id, status="pendente"
        ).delete()
        db.session.add(SyncJob(dispositivo_id=d.id, pessoa_tipo=tipo,
                               pessoa_id=pessoa.id, acao=acao))


def processar_fila(dispositivo_id=None):
    """Tenta enviar os jobs pendentes. Devolve (ok, falhas)."""
    q = SyncJob.query.filter_by(status="pendente").order_by(SyncJob.id)
    if dispositivo_id:
        q = q.filter_by(dispositivo_id=dispositivo_id)
    ok = falhas = 0
    for job in q.all():
        d = job.dispositivo
        job.tentativas += 1
        try:
            if job.acao == "remover":
                driver().remover_pessoa(d, {"tipo": job.pessoa_tipo, "id": job.pessoa_id})
            else:
                p = _carregar(job.pessoa_tipo, job.pessoa_id)
                if p is None:
                    raise DeviceError("pessoa não existe mais")
                driver().enviar_pessoa(d, _pessoa_dict(job.pessoa_tipo, p))
            job.status, job.done_at, job.erro = "ok", agora(), None
            d.online, d.ultimo_contato = True, agora()
            ok += 1
        except (DeviceError, NotImplementedError) as e:
            job.erro = str(e)[:255]
            d.online = False
            falhas += 1
    db.session.commit()
    return ok, falhas


def atualizar_status_dispositivos():
    for d in Dispositivo.query.all():
        try:
            d.online = bool(driver().ping(d))
        except (DeviceError, NotImplementedError):
            d.online = False
        if d.online:
            d.ultimo_contato = agora()
    db.session.commit()


def pendentes_por_dispositivo():
    linhas = db.session.query(SyncJob.dispositivo_id, db.func.count(SyncJob.id)) \
        .filter_by(status="pendente").group_by(SyncJob.dispositivo_id).all()
    return dict(linhas)


def liberacao_emergencia():
    """Abre todas as catracas. Devolve (abertas, falhas[nomes])."""
    abertas, falhas = 0, []
    for d in Dispositivo.query.all():
        try:
            driver().liberar_porta(d)
            abertas += 1
            db.session.add(EventoAcesso(dispositivo_id=d.id, pessoa_nome="Liberação de emergência",
                                        permitido=True, motivo="Emergência"))
        except (DeviceError, NotImplementedError):
            falhas.append(d.nome)
    db.session.commit()
    return abertas, falhas


def decidir_acesso(tipo, pessoa, dispositivo, quando):
    """(permitido, motivo) — a mesma regra que as catracas aplicam, usada na simulação."""
    if tipo == "usuario":
        if pessoa.status != "ativo":
            return False, "Usuário %s" % pessoa.status
        if dispositivo not in pessoa.grupo.dispositivos:
            return False, "Dispositivo não liberado para o grupo"
        if not pessoa.grupo.permite_horario(quando):
            return False, "Fora do horário do grupo"
        return True, None
    if pessoa.situacao != "em_visita":
        return False, "Credencial de visitante fora da validade"
    return True, None


def registrar_evento(tipo, pessoa, dispositivo, sentido="Entrada", quando=None):
    quando = quando or agora()
    permitido, motivo = (False, "Rosto não reconhecido") if pessoa is None \
        else decidir_acesso(tipo, pessoa, dispositivo, quando)
    ev = EventoAcesso(
        quando=quando, dispositivo_id=dispositivo.id, sentido=sentido,
        permitido=permitido, motivo=motivo,
        pessoa_tipo=tipo if pessoa else None, pessoa_id=pessoa.id if pessoa else None,
        pessoa_nome=pessoa.nome if pessoa else "Desconhecido",
        grupo_nome=(pessoa.grupo.nome if tipo == "usuario" else "Visitante") if pessoa else None,
    )
    db.session.add(ev)
    db.session.commit()
    return ev


def sincronizar_eventos():
    """Busca nas catracas os eventos novos (polling) e grava em EventoAcesso. Devolve quantos entraram."""
    novos = 0
    for d in Dispositivo.query.filter_by(online=True).all():
        ultimo = db.session.query(db.func.max(EventoAcesso.quando)).filter_by(dispositivo_id=d.id).scalar()
        desde = ultimo or (agora() - timedelta(hours=1))
        try:
            eventos = driver().buscar_eventos(d, desde)
        except (DeviceError, NotImplementedError):
            continue
        for e in eventos:
            pessoa = _carregar(e["pessoa_tipo"], e["pessoa_id"]) if e["pessoa_tipo"] else None
            tipo = e["pessoa_tipo"] if pessoa else None
            db.session.add(EventoAcesso(
                quando=e["quando"], dispositivo_id=d.id, sentido=e["sentido"],
                permitido=e["permitido"], motivo=e["motivo"],
                pessoa_tipo=tipo, pessoa_id=pessoa.id if pessoa else None,
                pessoa_nome=pessoa.nome if pessoa else (e["pessoa_nome"] or "Desconhecido"),
                grupo_nome=(pessoa.grupo.nome if tipo == "usuario" else "Visitante") if pessoa else None,
            ))
            novos += 1
    db.session.commit()
    return novos


def simular_acesso():
    """Gera um evento aleatório (só pra dev/demonstração com o driver mock)."""
    disp = Dispositivo.query.filter_by(online=True).all()
    if not disp:
        return None
    candidatos = [("usuario", u) for u in Usuario.query.all()] + \
                 [("visitante", v) for v in Visitante.query.all()]
    d = random.choice(disp)
    if not candidatos or random.random() < 0.1:
        return registrar_evento(None, None, d)
    tipo, p = random.choice(candidatos)
    return registrar_evento(tipo, p, d, sentido=random.choice(["Entrada", "Saída"]))


def indicadores():
    hoje = agora().replace(hour=0, minute=0, second=0, microsecond=0)
    eventos_hoje = EventoAcesso.query.filter(EventoAcesso.quando >= hoje).count()
    ontem = EventoAcesso.query.filter(EventoAcesso.quando >= hoje - timedelta(days=1),
                                      EventoAcesso.quando < hoje).count()
    visitas = [v for v in Visitante.query.filter(Visitante.valido_ate >= hoje).all()
               if v.situacao != "encerrado" or v.valido_de >= hoje]
    devs = Dispositivo.query.all()
    offline = [d.nome for d in devs if not d.online]
    return {
        "usuarios_ativos": Usuario.query.filter_by(status="ativo").count(),
        "novos_semana": Usuario.query.filter(Usuario.created_at >= agora() - timedelta(days=7)).count(),
        "visitas_hoje": len(visitas),
        "visitas_andamento": [v for v in visitas if v.situacao == "em_visita"],
        "disp_total": len(devs), "disp_online": len(devs) - len(offline), "disp_offline": offline,
        "acessos_hoje": eventos_hoje,
        "variacao": round((eventos_hoje - ontem) * 100 / ontem) if ontem else None,
    }


def seed():
    """Dados iniciais: grupos e as 7 catracas do protótipo."""
    if Grupo.query.count() == 0:
        modelos = [
            ("Portaria Principal", "SS 3532 MF W", "192.168.10.11", "Entrada principal"),
            ("Portaria Fundos", "SS 3532 MF", "192.168.10.12", "Acesso fundos"),
            ("Entrada Enfermaria", "SS 3532 MF W", "192.168.10.13", "Ala de enfermagem"),
            ("Bloco Administrativo", "SS 3532 MF W", "192.168.10.14", "Recepção administrativa"),
            ("Almoxarifado", "SS 3532 MF", "192.168.10.15", "Depósito"),
            ("Sala de Treinamento", "SS 3532 MF W", "192.168.10.16", "Auditório"),
            ("Garagem", "SS 3532 MF", "192.168.10.17", "Frota"),
        ]
        ds = [Dispositivo(nome=n, modelo=m, ip=ip, local=l) for n, m, ip, l in modelos]
        db.session.add_all(ds)
        db.session.flush()
        for nome, h24 in [("Enfermagem", True), ("Diretoria", True), ("TI", True),
                          ("Voluntariado", False), ("Administrativo", False)]:
            db.session.add(Grupo(nome=nome, acesso_24h=h24, dispositivos=list(ds)))
        db.session.commit()
