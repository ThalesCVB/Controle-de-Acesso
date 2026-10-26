from datetime import datetime

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


def agora():
    return datetime.now()


class Operador(db.Model):
    """Quem usa o sistema. Perfis: 'ti' (acesso total) e 'portaria'."""
    __tablename__ = "operadores"
    id = db.Column(db.Integer, primary_key=True)
    login = db.Column(db.String(60), unique=True, nullable=False)
    nome = db.Column(db.String(120), nullable=False)
    perfil = db.Column(db.String(20), nullable=False, default="portaria")
    senha_hash = db.Column(db.String(255), nullable=False)
    ativo = db.Column(db.Boolean, nullable=False, default=True)
    trocar_senha = db.Column(db.Boolean, nullable=False, default=True)

    def set_senha(self, senha):
        self.senha_hash = generate_password_hash(senha)

    def confere_senha(self, senha):
        return check_password_hash(self.senha_hash, senha)

    @property
    def eh_ti(self):
        return self.perfil == "ti"


class LoginTentativa(db.Model):
    __tablename__ = "login_tentativas"
    id = db.Column(db.Integer, primary_key=True)
    login = db.Column(db.String(60), unique=True, nullable=False)
    falhas = db.Column(db.Integer, nullable=False, default=0)
    bloqueado_ate = db.Column(db.DateTime)


grupo_dispositivo = db.Table(
    "grupo_dispositivo",
    db.Column("grupo_id", db.Integer, db.ForeignKey("grupos.id"), primary_key=True),
    db.Column("dispositivo_id", db.Integer, db.ForeignKey("dispositivos.id"), primary_key=True),
)


class Grupo(db.Model):
    """Departamento + regra de horário. dias = '0,1,2,3,4' (0 = segunda)."""
    __tablename__ = "grupos"
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(80), unique=True, nullable=False)
    acesso_24h = db.Column(db.Boolean, nullable=False, default=False)
    hora_inicio = db.Column(db.String(5), nullable=False, default="07:00")
    hora_fim = db.Column(db.String(5), nullable=False, default="19:00")
    dias = db.Column(db.String(20), nullable=False, default="0,1,2,3,4")
    dispositivos = db.relationship("Dispositivo", secondary=grupo_dispositivo, backref="grupos")
    usuarios = db.relationship("Usuario", backref="grupo")

    @property
    def zona_label(self):
        if self.acesso_24h:
            return "24h"
        nomes = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
        dias = [nomes[int(d)] for d in self.dias.split(",") if d != ""]
        return "%s–%s · %s" % (self.hora_inicio, self.hora_fim, ", ".join(dias))

    def permite_horario(self, quando):
        if self.acesso_24h:
            return True
        if str(quando.weekday()) not in self.dias.split(","):
            return False
        return self.hora_inicio <= quando.strftime("%H:%M") <= self.hora_fim


class Dispositivo(db.Model):
    __tablename__ = "dispositivos"
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(80), unique=True, nullable=False)
    modelo = db.Column(db.String(40), nullable=False)
    ip = db.Column(db.String(45), nullable=False)
    local = db.Column(db.String(120), nullable=False, default="")
    usuario_api = db.Column(db.String(60), nullable=False, default="admin")
    senha_api = db.Column(db.String(120), nullable=False, default="")
    online = db.Column(db.Boolean, nullable=False, default=False)
    ultimo_contato = db.Column(db.DateTime)


class Usuario(db.Model):
    """Colaborador/voluntário com credencial facial."""
    __tablename__ = "usuarios"
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(120), nullable=False)
    cpf = db.Column(db.String(11), unique=True)
    cargo = db.Column(db.String(80), nullable=False, default="")
    grupo_id = db.Column(db.Integer, db.ForeignKey("grupos.id"), nullable=False)
    foto = db.Column(db.String(120))
    status = db.Column(db.String(12), nullable=False, default="ativo")  # ativo/inativo/bloqueado
    created_at = db.Column(db.DateTime, nullable=False, default=agora)


class Visitante(db.Model):
    """Credencial temporária vinculada a um anfitrião (Usuario)."""
    __tablename__ = "visitantes"
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(120), nullable=False)
    documento = db.Column(db.String(30), nullable=False)
    anfitriao_id = db.Column(db.Integer, db.ForeignKey("usuarios.id"), nullable=False)
    anfitriao = db.relationship("Usuario")
    valido_de = db.Column(db.DateTime, nullable=False, default=agora)
    valido_ate = db.Column(db.DateTime, nullable=False)
    foto = db.Column(db.String(120))
    encerrado = db.Column(db.Boolean, nullable=False, default=False)

    @property
    def situacao(self):
        n = agora()
        if self.encerrado or n > self.valido_ate:
            return "encerrado"
        return "em_visita" if n >= self.valido_de else "agendado"


class SyncJob(db.Model):
    """Fila de sincronização de credenciais com as catracas."""
    __tablename__ = "sync_jobs"
    id = db.Column(db.Integer, primary_key=True)
    dispositivo_id = db.Column(db.Integer, db.ForeignKey("dispositivos.id"), nullable=False)
    dispositivo = db.relationship("Dispositivo")
    pessoa_tipo = db.Column(db.String(12), nullable=False)  # usuario/visitante
    pessoa_id = db.Column(db.Integer, nullable=False)
    acao = db.Column(db.String(10), nullable=False)  # upsert/remover
    status = db.Column(db.String(10), nullable=False, default="pendente")  # pendente/ok/erro
    tentativas = db.Column(db.Integer, nullable=False, default=0)
    erro = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, nullable=False, default=agora)
    done_at = db.Column(db.DateTime)


class EventoAcesso(db.Model):
    __tablename__ = "eventos_acesso"
    id = db.Column(db.Integer, primary_key=True)
    quando = db.Column(db.DateTime, nullable=False, default=agora, index=True)
    dispositivo_id = db.Column(db.Integer, db.ForeignKey("dispositivos.id"), nullable=False)
    dispositivo = db.relationship("Dispositivo")
    pessoa_tipo = db.Column(db.String(12))  # usuario/visitante/None (rosto desconhecido)
    pessoa_id = db.Column(db.Integer)
    pessoa_nome = db.Column(db.String(120), nullable=False, default="Desconhecido")
    grupo_nome = db.Column(db.String(80))
    sentido = db.Column(db.String(10), nullable=False, default="Entrada")
    permitido = db.Column(db.Boolean, nullable=False)
    motivo = db.Column(db.String(120))

    @property
    def tipo_label(self):
        return {"usuario": "Colaborador", "visitante": "Visitante"}.get(self.pessoa_tipo, "—")


class LogAuditoria(db.Model):
    __tablename__ = "log_auditoria"
    id = db.Column(db.Integer, primary_key=True)
    quando = db.Column(db.DateTime, nullable=False, default=agora)
    operador = db.Column(db.String(60))
    acao = db.Column(db.String(60), nullable=False)
    detalhe = db.Column(db.String(255))
