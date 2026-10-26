import base64
import csv
import io
import os
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template,
                   request, send_from_directory, session, url_for)

import config
import nomes
import services
from models import (Dispositivo, EventoAcesso, Grupo, LogAuditoria, LoginTentativa, Operador,
                    SyncJob, Usuario, Visitante, agora, db)

app = Flask(__name__)
app.config.from_object(config)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=config.HTTPS_ENABLED,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=config.SESSION_HORAS),
)
db.init_app(app)

DIAS = [("0", "Seg"), ("1", "Ter"), ("2", "Qua"), ("3", "Qui"), ("4", "Sex"), ("5", "Sáb"), ("6", "Dom")]


# ---------------------------------------------------------------- helpers
def cpf_normalizado(v):
    return re.sub(r"\D", "", v or "")


def cpf_valido(v):
    c = cpf_normalizado(v)
    if len(c) != 11 or c == c[0] * 11:
        return False
    for n in (9, 10):
        soma = sum(int(c[i]) * (n + 1 - i) for i in range(n))
        if int(c[n]) != (soma * 10 % 11) % 10:
            return False
    return True


def formatar_cpf(c):
    return "%s.%s.%s-%s" % (c[:3], c[3:6], c[6:9], c[9:]) if c and len(c) == 11 else (c or "")


def senha_erros(s):
    erros = []
    if len(s) < 8:
        erros.append("mínimo 8 caracteres")
    if not re.search(r"[A-Z]", s) or not re.search(r"[a-z]", s) or not re.search(r"\d", s):
        erros.append("precisa de maiúscula, minúscula e número")
    return erros


def auditar(acao, detalhe=""):
    op = getattr(g, "op", None)
    db.session.add(LogAuditoria(operador=op.login if op else None, acao=acao, detalhe=detalhe[:255]))


def salvar_foto(data_url):
    """Recebe um data URL PNG/JPEG da webcam, valida pelo conteúdo e salva. Devolve o nome do arquivo."""
    m = re.match(r"data:image/(png|jpeg);base64,(.+)$", data_url or "", re.S)
    if not m:
        return None
    raw = base64.b64decode(m.group(2), validate=False)
    if len(raw) > 4 * 1024 * 1024:
        return None
    if m.group(1) == "png" and not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    if m.group(1) == "jpeg" and not raw.startswith(b"\xff\xd8\xff"):
        return None
    nome = "%s.%s" % (uuid.uuid4().hex, "png" if m.group(1) == "png" else "jpg")
    with open(os.path.join(config.FOTOS_DIR, nome), "wb") as f:
        f.write(raw)
    return nome


def apagar_foto(nome):
    if nome:
        try:
            os.remove(os.path.join(config.FOTOS_DIR, nome))
        except OSError:
            pass


def parse_dt(v):
    try:
        return datetime.strptime(v, "%Y-%m-%dT%H:%M")
    except (TypeError, ValueError):
        return None


def parse_dia(v):
    try:
        return datetime.strptime(v, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- segurança / auth
@app.before_request
def carregar_operador():
    g.op = None
    if session.get("op_id"):
        op = db.session.get(Operador, session["op_id"])
        if op and op.ativo:
            g.op = op
        else:
            session.clear()


@app.before_request
def csrf_protect():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    if request.method == "POST":
        token = request.form.get("_csrf") or request.headers.get("X-CSRF")
        if not token or not secrets.compare_digest(token, session["csrf"]):
            abort(400, "Token CSRF inválido")


@app.before_request
def forcar_troca_senha():
    if g.op and g.op.trocar_senha and request.endpoint not in ("trocar_senha", "logout", "static"):
        return redirect(url_for("trocar_senha"))


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=()"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
    if config.HTTPS_ENABLED:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    return resp


def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not g.op:
            return redirect(url_for("login", next=request.path))
        return f(*a, **k)
    return w


def ti_required(f):
    @wraps(f)
    @login_required
    def w(*a, **k):
        if not g.op.eh_ti:
            abort(403)
        return f(*a, **k)
    return w


@app.context_processor
def inject():
    return {"op": getattr(g, "op", None), "csrf": session.get("csrf", ""),
            "mock": config.DEVICE_DRIVER == "mock", "formatar_cpf": formatar_cpf}


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        login_ = (request.form.get("login") or "").strip().lower()
        senha = request.form.get("senha") or ""
        t = LoginTentativa.query.filter_by(login=login_).first()
        if t and t.bloqueado_ate and t.bloqueado_ate > agora():
            flash("Muitas tentativas. Tente novamente em alguns minutos.", "erro")
            return render_template("login.html")
        op = Operador.query.filter_by(login=login_).first()
        if op and op.ativo and op.confere_senha(senha):
            if t:
                db.session.delete(t)
            session.clear()
            session.permanent = True
            session["op_id"] = op.id
            g.op = op
            auditar("login_sucesso")
            db.session.commit()
            return redirect(request.args.get("next") if (request.args.get("next") or "").startswith("/")
                            and not (request.args.get("next") or "").startswith("//") else url_for("home"))
        t = t or LoginTentativa(login=login_[:60], falhas=0)
        t.falhas += 1
        if t.falhas >= config.LOGIN_MAX_TENTATIVAS:
            t.bloqueado_ate = agora() + timedelta(minutes=config.LOGIN_BLOQUEIO_MINUTOS)
            t.falhas = 0
        db.session.add(t)
        db.session.add(LogAuditoria(operador=login_[:60], acao="login_falhou"))
        db.session.commit()
        flash("Login ou senha incorretos.", "erro")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    auditar("logout")
    db.session.commit()
    session.clear()
    return redirect(url_for("login"))


@app.route("/trocar-senha", methods=["GET", "POST"])
@login_required
def trocar_senha():
    if request.method == "POST":
        atual, nova, conf = (request.form.get(k) or "" for k in ("atual", "nova", "conf"))
        erros = []
        if not g.op.confere_senha(atual):
            erros.append("Senha atual incorreta.")
        if nova != conf:
            erros.append("A confirmação não confere.")
        if nova == atual:
            erros.append("A nova senha deve ser diferente da atual.")
        erros += ["Senha nova: " + e for e in senha_erros(nova)]
        if erros:
            for e in erros:
                flash(e, "erro")
        else:
            g.op.set_senha(nova)
            g.op.trocar_senha = False
            auditar("senha_alterada")
            db.session.commit()
            return redirect(url_for("home"))
    return render_template("trocar_senha.html", titulo="Trocar senha", sub="")


# ---------------------------------------------------------------- páginas
@app.route("/")
@login_required
def home():
    if not g.op.eh_ti:
        return redirect(url_for("usuarios"))
    ind = services.indicadores()
    ultimos = EventoAcesso.query.order_by(EventoAcesso.id.desc()).limit(8).all()
    return render_template("dashboard.html", titulo="Dashboard", sub="Visão geral do acesso",
                           ind=ind, ultimos=ultimos, dispositivos=Dispositivo.query.order_by(Dispositivo.id).all())


@app.route("/monitoramento")
@ti_required
def monitoramento():
    eventos = EventoAcesso.query.order_by(EventoAcesso.id.desc()).limit(50).all()
    return render_template("monitoramento.html", titulo="Monitoramento",
                           sub="Eventos de acesso em tempo real", eventos=eventos,
                           dispositivos=Dispositivo.query.order_by(Dispositivo.nome).all())


def evento_json(e):
    return {"id": e.id, "nome": e.pessoa_nome, "tipo": e.tipo_label, "grupo": e.grupo_nome or "",
            "dispositivo": e.dispositivo.nome, "sentido": e.sentido, "ok": e.permitido,
            "motivo": e.motivo or "", "hora": e.quando.strftime("%d/%m %H:%M:%S")}


@app.route("/api/eventos")
@ti_required
def api_eventos():
    depois = request.args.get("depois", 0, type=int)
    evs = EventoAcesso.query.filter(EventoAcesso.id > depois).order_by(EventoAcesso.id).limit(100).all()
    return jsonify([evento_json(e) for e in evs])


@app.route("/api/simular", methods=["POST"])
@ti_required
def api_simular():
    if config.DEVICE_DRIVER != "mock":
        abort(404)
    ev = services.simular_acesso()
    return jsonify(evento_json(ev) if ev else None)


@app.route("/emergencia", methods=["POST"])
@ti_required
def emergencia():
    abertas, falhas = services.liberacao_emergencia()
    auditar("liberacao_emergencia", "abertas=%d falhas=%s" % (abertas, ", ".join(falhas)))
    db.session.commit()
    if falhas:
        flash("Liberação enviada a %d catraca(s). Sem resposta: %s." % (abertas, ", ".join(falhas)), "erro")
    else:
        flash("Liberação de emergência enviada às %d catracas." % abertas, "ok")
    return redirect(request.referrer or url_for("home"))


# ---- usuários
def _form_usuario(u=None):
    f = request.form
    nome = nomes.padronizar(f.get("nome"))
    cpf = cpf_normalizado(f.get("cpf"))
    erros = []
    if not nome:
        erros.append("Informe o nome.")
    if not cpf_valido(cpf):
        erros.append("CPF inválido.")
    else:
        outro = Usuario.query.filter_by(cpf=cpf).first()
        if outro and (u is None or outro.id != u.id):
            erros.append("Já existe usuário com esse CPF.")
    grupo = db.session.get(Grupo, f.get("grupo_id", type=int) or 0)
    if not grupo:
        erros.append("Escolha o grupo.")
    status = f.get("status", "ativo")
    if status not in ("ativo", "inativo", "bloqueado"):
        status = "ativo"
    return nome, cpf, (f.get("cargo") or "").strip(), grupo, status, erros


@app.route("/usuarios")
@login_required
def usuarios():
    q = (request.args.get("q") or "").strip()
    lista = Usuario.query.order_by(Usuario.nome).all()
    if q:
        cpf_q = cpf_normalizado(q)
        por_cpf = [u for u in lista if cpf_q and u.cpf and cpf_q in u.cpf]
        por_nome = nomes.buscar(q, lista, chave=lambda u: u.nome)
        lista = por_cpf + [u for u in por_nome if u not in por_cpf]
    return render_template("usuarios.html", titulo="Usuários",
                           sub="Colaboradores e voluntários cadastrados", lista=lista, q=q)


@app.route("/usuarios/novo", methods=["GET", "POST"])
@app.route("/usuarios/<int:uid>/editar", methods=["GET", "POST"])
@login_required
def usuario_form(uid=None):
    u = db.get_or_404(Usuario, uid) if uid else None
    grupos = Grupo.query.order_by(Grupo.nome).all()
    if request.method == "POST":
        nome, cpf, cargo, grupo, status, erros = _form_usuario(u)
        foto = salvar_foto(request.form.get("foto_data")) if request.form.get("foto_data") else None
        if request.form.get("foto_data") and not foto:
            erros.append("Foto inválida.")
        if erros:
            apagar_foto(foto)
            for e in erros:
                flash(e, "erro")
            return render_template("usuario_form.html", u=u, grupos=grupos, titulo="Usuário",
                                   sub="", form=request.form)
        novo = u is None
        antigos = [] if novo else list(u.grupo.dispositivos) if u.status == "ativo" else []
        if novo:
            u = Usuario()
            db.session.add(u)
        u.nome, u.cpf, u.cargo, u.grupo, u.status = nome, cpf, cargo, grupo, status
        if foto:
            apagar_foto(u.foto)
            u.foto = foto
        db.session.flush()
        atuais = list(grupo.dispositivos) if status == "ativo" else []
        for d in antigos:
            if d not in atuais:
                services.enfileirar("usuario", u, "remover", [d])
        if u.foto:
            services.enfileirar("usuario", u, "upsert", atuais)
        auditar("usuario_criado" if novo else "usuario_editado", "%s (id %d)" % (u.nome, u.id))
        db.session.commit()
        services.processar_fila()
        flash("Usuário salvo." + ("" if u.foto else " Falta a foto para sincronizar com as catracas."), "ok")
        return redirect(url_for("usuarios"))
    return render_template("usuario_form.html", u=u, grupos=grupos, form={},
                           titulo="Editar usuário" if u else "Novo usuário",
                           sub="Cadastro com captura facial via webcam")


@app.route("/usuarios/<int:uid>/excluir", methods=["POST"])
@ti_required
def usuario_excluir(uid):
    u = db.get_or_404(Usuario, uid)
    if Visitante.query.filter_by(anfitriao_id=u.id).count():
        flash("Usuário é anfitrião de visitantes; não pode ser excluído (inative-o).", "erro")
        return redirect(url_for("usuarios"))
    services.enfileirar("usuario", u, "remover")
    db.session.flush()
    nome, foto = u.nome, u.foto
    db.session.delete(u)
    auditar("usuario_excluido", nome)
    db.session.commit()
    apagar_foto(foto)
    services.processar_fila()
    flash("Usuário excluído.", "ok")
    return redirect(url_for("usuarios"))


@app.route("/fotos/<nome>")
@login_required
def foto(nome):
    if not re.fullmatch(r"[0-9a-f]{32}\.(png|jpg)", nome):
        abort(404)
    return send_from_directory(config.FOTOS_DIR, nome)


@app.route("/api/nomes-parecidos")
@login_required
def api_nomes_parecidos():
    """Aviso no cadastro: pessoas já cadastradas com nome parecido (ou mesmo documento/CPF)."""
    nome = nomes.padronizar(request.args.get("nome"))
    doc = re.sub(r"\W", "", (request.args.get("documento") or "").lower())
    ignorar = request.args.get("ignorar", type=int)
    pessoas = [("Colaborador", u.nome, u.id, u.cpf or "") for u in Usuario.query.all() if u.id != ignorar] +               [("Visitante", v.nome, v.id, v.documento) for v in Visitante.query.all()]
    achados = nomes.parecidos(nome, pessoas, chave=lambda p: p[1])
    if doc:
        mesmos = [p for p in pessoas if re.sub(r"\W", "", p[3].lower()) == doc and p not in achados]
        achados = mesmos + achados
    return jsonify([{"tipo": t, "nome": n, "documento": d} for t, n, _, d in achados[:5]])


@app.route("/api/visitantes-anteriores")
@login_required
def api_visitantes_anteriores():
    """Sugestões ao digitar: visitantes que já vieram (um por documento, nome da visita mais recente)."""
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify([])
    unicos = {}
    for v in Visitante.query.order_by(Visitante.id.desc()).all():
        unicos.setdefault(re.sub(r"\W", "", v.documento.lower()) or str(v.id), v)
    pessoas = list(unicos.values())
    dq = re.sub(r"\W", "", q.lower())
    por_doc = [v for v in pessoas if len(dq) >= 3 and dq in re.sub(r"\W", "", v.documento.lower())]
    por_nome = nomes.buscar(q, pessoas, chave=lambda v: v.nome)
    achados = por_doc + [v for v in por_nome if v not in por_doc]
    return jsonify([{"nome": v.nome, "documento": v.documento} for v in achados[:6]])


# ---- visitantes
@app.route("/visitantes")
@login_required
def visitantes():
    q = (request.args.get("q") or "").strip()
    lista = Visitante.query.order_by(Visitante.valido_ate.desc()).all()
    if q:
        dq = nomes.normalizar(q).replace(" ", "")
        por_doc = [v for v in lista if dq and dq in re.sub(r"\W", "", v.documento.lower())]
        por_nome = nomes.buscar(q, lista, chave=lambda v: v.nome)
        lista = por_doc + [v for v in por_nome if v not in por_doc]
    return render_template("visitantes.html", titulo="Visitantes", sub="Controle de visitas à sede",
                           lista=lista[:300], q=q)


@app.route("/visitantes/novo", methods=["GET", "POST"])
@login_required
def visitante_novo():
    anfitrioes = Usuario.query.filter_by(status="ativo").order_by(Usuario.nome).all()
    if request.method == "POST":
        f = request.form
        nome, doc = nomes.padronizar(f.get("nome")), (f.get("documento") or "").strip()
        de, ate = parse_dt(f.get("valido_de")), parse_dt(f.get("valido_ate"))
        anf = db.session.get(Usuario, f.get("anfitriao_id", type=int) or 0)
        erros = []
        if not nome or not doc:
            erros.append("Informe nome e documento.")
        if not anf:
            erros.append("Escolha o anfitrião.")
        if not de or not ate or ate <= de:
            erros.append("Período inválido (o fim precisa ser depois do início).")
        foto = salvar_foto(f.get("foto_data")) if f.get("foto_data") else None
        if not foto:
            erros.append("Capture a foto do visitante.")
        if erros:
            apagar_foto(foto)
            for e in erros:
                flash(e, "erro")
            return render_template("visitante_form.html", anfitrioes=anfitrioes, form=f,
                                   titulo="Novo visitante", sub="Credencial temporária")
        v = Visitante(nome=nome, documento=doc, anfitriao=anf, valido_de=de, valido_ate=ate, foto=foto)
        db.session.add(v)
        db.session.flush()
        services.enfileirar("visitante", v)
        auditar("visitante_criado", "%s (anfitrião %s)" % (nome, anf.nome))
        db.session.commit()
        services.processar_fila()
        flash("Visitante cadastrado e enviado às catracas.", "ok")
        return redirect(url_for("visitantes"))
    agora_ = agora().replace(second=0, microsecond=0)
    return render_template("visitante_form.html", anfitrioes=anfitrioes, titulo="Novo visitante",
                           sub="Credencial temporária",
                           form={"valido_de": agora_.strftime("%Y-%m-%dT%H:%M"),
                                 "valido_ate": agora_.replace(hour=18, minute=0).strftime("%Y-%m-%dT%H:%M")})


@app.route("/visitantes/<int:vid>/encerrar", methods=["POST"])
@login_required
def visitante_encerrar(vid):
    v = db.get_or_404(Visitante, vid)
    v.encerrado = True
    services.enfileirar("visitante", v, "remover")
    auditar("visitante_encerrado", v.nome)
    db.session.commit()
    services.processar_fila()
    flash("Visita encerrada.", "ok")
    return redirect(url_for("visitantes"))


# ---- grupos
def _dispositivos_selecionados():
    ids = request.form.getlist("dispositivos", type=int)
    return Dispositivo.query.filter(Dispositivo.id.in_(ids)).all() if ids else []


@app.route("/grupos")
@ti_required
def grupos():
    return render_template("grupos.html", titulo="Grupos", sub="Departamentos e regras de acesso",
                           lista=Grupo.query.order_by(Grupo.nome).all(),
                           total=Dispositivo.query.count())


@app.route("/grupos/novo", methods=["GET", "POST"])
@app.route("/grupos/<int:gid>/editar", methods=["GET", "POST"])
@ti_required
def grupo_form(gid=None):
    gr = db.get_or_404(Grupo, gid) if gid else None
    if request.method == "POST":
        f = request.form
        nome = (f.get("nome") or "").strip()
        ini, fim = f.get("hora_inicio", "07:00"), f.get("hora_fim", "19:00")
        h24 = f.get("acesso_24h") == "1"
        dias = ",".join(d for d, _ in DIAS if d in f.getlist("dias"))
        erros = []
        if not nome:
            erros.append("Informe o nome.")
        elif (o := Grupo.query.filter_by(nome=nome).first()) and (gr is None or o.id != gr.id):
            erros.append("Já existe grupo com esse nome.")
        if not h24 and (not re.fullmatch(r"\d\d:\d\d", ini) or not re.fullmatch(r"\d\d:\d\d", fim) or ini >= fim or not dias):
            erros.append("Horário/dias inválidos.")
        if erros:
            for e in erros:
                flash(e, "erro")
            return render_template("grupo_form.html", gr=gr, dispositivos=Dispositivo.query.all(), DIAS=DIAS,
                                   titulo="Grupo", sub="", form=f)
        novo = gr is None
        if novo:
            gr = Grupo()
            db.session.add(gr)
        antigos = set(d.id for d in gr.dispositivos) if not novo else set()
        gr.nome, gr.acesso_24h = nome, h24
        if not h24:
            gr.hora_inicio, gr.hora_fim, gr.dias = ini, fim, dias
        gr.dispositivos = _dispositivos_selecionados()
        db.session.flush()
        novos = set(d.id for d in gr.dispositivos)
        for u in gr.usuarios:
            if u.status != "ativo" or not u.foto:
                continue
            for d in Dispositivo.query.filter(Dispositivo.id.in_(antigos - novos)).all():
                services.enfileirar("usuario", u, "remover", [d])
            for d in Dispositivo.query.filter(Dispositivo.id.in_(novos - antigos)).all():
                services.enfileirar("usuario", u, "upsert", [d])
        auditar("grupo_criado" if novo else "grupo_editado", nome)
        db.session.commit()
        services.processar_fila()
        flash("Grupo salvo.", "ok")
        return redirect(url_for("grupos"))
    return render_template("grupo_form.html", gr=gr, dispositivos=Dispositivo.query.order_by(Dispositivo.nome).all(),
                           DIAS=DIAS, form={}, titulo="Editar grupo" if gr else "Novo grupo",
                           sub="Regra de horário e catracas liberadas")


@app.route("/grupos/<int:gid>/excluir", methods=["POST"])
@ti_required
def grupo_excluir(gid):
    gr = db.get_or_404(Grupo, gid)
    if gr.usuarios:
        flash("Grupo tem usuários; mova-os antes de excluir.", "erro")
    else:
        auditar("grupo_excluido", gr.nome)
        db.session.delete(gr)
        db.session.commit()
        flash("Grupo excluído.", "ok")
    return redirect(url_for("grupos"))


# ---- dispositivos
@app.route("/dispositivos")
@ti_required
def dispositivos():
    return render_template("dispositivos.html", titulo="Dispositivos",
                           sub="Catracas Intelbras Bio-T · Sede BH",
                           lista=Dispositivo.query.order_by(Dispositivo.id).all(),
                           fila=services.pendentes_por_dispositivo(),
                           erros=SyncJob.query.filter_by(status="pendente").filter(SyncJob.erro.isnot(None))
                           .order_by(SyncJob.id.desc()).limit(10).all())


@app.route("/dispositivos/novo", methods=["GET", "POST"])
@ti_required
def dispositivo_novo():
    if request.method == "POST":
        f = request.form
        nome, ip, modelo = (f.get("nome") or "").strip(), (f.get("ip") or "").strip(), f.get("modelo", "")
        if not nome or not re.fullmatch(r"[0-9a-zA-Z.\-]{3,45}", ip) or modelo not in ("SS 3532 MF", "SS 3532 MF W"):
            flash("Preencha nome, IP e modelo corretamente.", "erro")
        elif Dispositivo.query.filter_by(nome=nome).first():
            flash("Já existe dispositivo com esse nome.", "erro")
        else:
            d = Dispositivo(nome=nome, ip=ip, modelo=modelo, local=(f.get("local") or "").strip(),
                            usuario_api=f.get("usuario_api") or "admin", senha_api=f.get("senha_api") or "")
            db.session.add(d)
            auditar("dispositivo_criado", "%s %s" % (nome, ip))
            db.session.commit()
            flash("Catraca adicionada. Associe-a a grupos para enviar as credenciais.", "ok")
            return redirect(url_for("dispositivos"))
    return render_template("dispositivo_form.html", titulo="Adicionar catraca", sub="", form=request.form)


@app.route("/dispositivos/sincronizar", methods=["POST"])
@ti_required
def dispositivos_sincronizar():
    services.atualizar_status_dispositivos()
    ok, falhas = services.processar_fila()
    flash("Sincronização: %d enviado(s), %d com falha." % (ok, falhas), "ok" if not falhas else "erro")
    return redirect(url_for("dispositivos"))


# ---- operadores
@app.route("/operadores", methods=["GET", "POST"])
@ti_required
def operadores():
    if request.method == "POST":
        f = request.form
        login_, nome, perfil = (f.get("login") or "").strip().lower(), (f.get("nome") or "").strip(), f.get("perfil")
        senha = f.get("senha") or ""
        erros = [] if login_ and nome and perfil in ("ti", "portaria") else ["Preencha login, nome e perfil."]
        if Operador.query.filter_by(login=login_).first():
            erros.append("Login já existe.")
        erros += ["Senha: " + e for e in senha_erros(senha)]
        if erros:
            for e in erros:
                flash(e, "erro")
        else:
            o = Operador(login=login_, nome=nome, perfil=perfil, trocar_senha=True)
            o.set_senha(senha)
            db.session.add(o)
            auditar("operador_criado", "%s (%s)" % (login_, perfil))
            db.session.commit()
            flash("Operador criado (trocará a senha no primeiro acesso).", "ok")
        return redirect(url_for("operadores"))
    return render_template("operadores.html", titulo="Operadores", sub="Quem acessa este sistema",
                           lista=Operador.query.order_by(Operador.nome).all())


@app.route("/operadores/<int:oid>/ativo", methods=["POST"])
@ti_required
def operador_ativo(oid):
    o = db.get_or_404(Operador, oid)
    if o.id == g.op.id:
        flash("Você não pode desativar a si mesmo.", "erro")
    else:
        o.ativo = not o.ativo
        auditar("operador_ativado" if o.ativo else "operador_desativado", o.login)
        db.session.commit()
    return redirect(url_for("operadores"))


# ---- relatórios
def _consulta_relatorio(args):
    q = EventoAcesso.query.join(Dispositivo)
    de, ate = parse_dia(args.get("de")), parse_dia(args.get("ate"))
    if de:
        q = q.filter(EventoAcesso.quando >= de)
    if ate:
        q = q.filter(EventoAcesso.quando < ate + timedelta(days=1))
    if (quem := (args.get("quem") or "").strip()):
        existentes = [n for (n,) in db.session.query(EventoAcesso.pessoa_nome).distinct()]
        q = q.filter(EventoAcesso.pessoa_nome.in_(nomes.buscar(quem, existentes) or [""]))
    if (did := args.get("dispositivo", type=int)):
        q = q.filter(EventoAcesso.dispositivo_id == did)
    if args.get("resultado") in ("permitido", "negado"):
        q = q.filter(EventoAcesso.permitido == (args["resultado"] == "permitido"))
    return q.order_by(EventoAcesso.quando.desc())


@app.route("/relatorios")
@login_required
def relatorios():
    args = request.args
    gerar = args.get("gerar") == "1"
    fmt = args.get("formato", "html")
    if gerar and fmt in ("csv", "pdf"):
        evs = _consulta_relatorio(args).limit(20000).all()
        auditar("relatorio_exportado", "%s, %d registros" % (fmt, len(evs)))
        db.session.commit()
        return exportar_csv(evs) if fmt == "csv" else exportar_pdf(evs, args)
    total, evs = 0, []
    if gerar:
        q = _consulta_relatorio(args)
        total = q.count()
        evs = q.limit(500).all()
    return render_template("relatorios.html", titulo="Relatórios", sub="Consulta e exportação de logs de acesso",
                           dispositivos=Dispositivo.query.order_by(Dispositivo.nome).all(),
                           gerar=gerar, eventos=evs, total=total, a=args)


def _linha(e):
    return [e.pessoa_nome, e.tipo_label, e.dispositivo.nome, e.sentido,
            "Permitido" if e.permitido else "Negado", e.motivo or "", e.quando.strftime("%d/%m/%Y %H:%M:%S")]


CAB = ["Pessoa", "Tipo", "Dispositivo", "Sentido", "Resultado", "Motivo", "Horário"]


def exportar_csv(evs):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(CAB)
    for e in evs:
        # evita injeção de fórmula ao abrir no Excel
        w.writerow([("'" + c if c[:1] in "=+-@" and c else c) for c in _linha(e)])
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=acessos.csv"})


def exportar_pdf(evs, args):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=24, rightMargin=24, topMargin=24, bottomMargin=24)
    st = getSampleStyleSheet()
    dados = [CAB] + [_linha(e) for e in evs]
    t = Table(dados, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E2231A")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8), ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8EFEA")])]))
    doc.build([Paragraph("Cruz Vermelha Brasileira — Filial MG · Relatório de acessos", st["Title"]),
               Paragraph("Gerado em %s · %d registro(s)" % (agora().strftime("%d/%m/%Y %H:%M"), len(evs)), st["Normal"]),
               Spacer(1, 10), t])
    return Response(buf.getvalue(), mimetype="application/pdf",
                    headers={"Content-Disposition": "attachment; filename=acessos.pdf"})


# ---------------------------------------------------------------- inicialização
def init_db():
    with app.app_context():
        db.create_all()
        services.seed()
        if Operador.query.count() == 0:
            senha = secrets.token_urlsafe(9)
            op = Operador(login="admin", nome="Administrador TI", perfil="ti", trocar_senha=True)
            op.set_senha(senha)
            db.session.add(op)
            db.session.commit()
            print("=" * 60)
            print("Operador inicial criado -> login: admin | senha: %s" % senha)
            print("(troca obrigatória no primeiro acesso; esta senha só aparece agora)")
            print("=" * 60)
        services.atualizar_status_dispositivos()


def _worker():
    """Reenvia a fila pendente, atualiza o status e busca eventos novos das catracas a cada 30s."""
    while True:
        time.sleep(30)
        try:
            with app.app_context():
                services.atualizar_status_dispositivos()
                services.processar_fila()
                services.sincronizar_eventos()
        except Exception as e:  # o worker nunca pode morrer
            print("worker:", e)


def start_worker():
    threading.Thread(target=_worker, daemon=True).start()


init_db()
if os.environ.get("NO_WORKER") != "1":
    start_worker()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8002)), debug=False)
