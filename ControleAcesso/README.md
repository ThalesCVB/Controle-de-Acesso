# Controle de Acesso — CVBMG

Flask + SQLAlchemy. Catracas Intelbras SS 3532 MF / MF W atrás de uma camada de drivers (`devices/`).

## Rodar em dev
```
pip install -r requirements.txt
$env:SECRET_KEY = "..."      # obrigatória; ver .env.example
python app.py                # http://localhost:8002
```
Primeiro start: cria o banco SQLite (`instance/`), 5 grupos, as 7 catracas do protótipo e o operador
`admin` com senha aleatória **impressa uma única vez no console** (troca obrigatória no 1º login).

Com `DEVICE_DRIVER=mock` (padrão) as catracas são simuladas — a de IP `.12` fica offline de propósito
pra exercitar a fila de sincronização — e o Monitoramento ganha o botão "Simular acesso".

## Teste
`python test_e2e.py` (banco temporário, não toca em `instance/`).

## Perfis
- **TI**: tudo, incl. Dashboard, Monitoramento, Grupos, Dispositivos, Operadores e Liberação de emergência.
- **Portaria**: só Usuários, Visitantes e Relatórios.

## Produção (SQL Server Express)
1. Instalar o *ODBC Driver 18 for SQL Server* e `pip install pyodbc`.
2. `DATABASE_URL=mssql+pyodbc://usuario:senha@SERVIDOR\SQLEXPRESS/ControleAcesso?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes`
3. `waitress-serve --host=0.0.0.0 --port=8002 --threads=4 app:app` (processo único: o worker de
   sincronização roda como thread; não use vários processos).
4. Ligar `HTTPS_ENABLED=1` só com TLS de verdade na frente.

`db.create_all()` só cria tabelas novas — não há migração de colunas (sem Alembic ainda).

## Integração Intelbras (pendente)
Falta a documentação oficial de API/CGI da linha Bio-T. Quando chegar, implementar
`devices/intelbras.py` (a lista do que pedir ao suporte está no docstring) e usar `DEVICE_DRIVER=intelbras`.
Nada fora desse arquivo precisa mudar, exceto: se os eventos vierem por callback/push, adicionar uma rota
que chame `services.registrar_evento`; se por polling, chamar `driver().buscar_eventos` no worker.

## Modelo de dados
`models.py`: Operador, Grupo (horário + catracas liberadas), Dispositivo, Usuario, Visitante,
SyncJob (fila de credenciais), EventoAcesso, LogAuditoria, LoginTentativa. O schema que você mencionou
não estava na pasta, então foi recriado a partir do protótipo — compare com o seu e ajuste se divergir.
