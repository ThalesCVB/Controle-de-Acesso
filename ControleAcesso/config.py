import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY não definida (ver .env.example)")

os.makedirs(os.path.join(BASE_DIR, "instance"), exist_ok=True)
SQLALCHEMY_DATABASE_URI = os.environ.get(
    "DATABASE_URL",
    "sqlite:///" + os.path.join(BASE_DIR, "instance", "controle_acesso.db"),
)
SQLALCHEMY_TRACK_MODIFICATIONS = False

FOTOS_DIR = os.path.join(BASE_DIR, "uploads", "fotos")
os.makedirs(FOTOS_DIR, exist_ok=True)
MAX_CONTENT_LENGTH = 8 * 1024 * 1024

DEVICE_DRIVER = os.environ.get("DEVICE_DRIVER", "mock")
HTTPS_ENABLED = os.environ.get("HTTPS_ENABLED") == "1"
SESSION_HORAS = 8
LOGIN_MAX_TENTATIVAS = 5
LOGIN_BLOQUEIO_MINUTOS = 10
