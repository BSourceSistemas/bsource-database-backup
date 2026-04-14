# Arquitetura

## 📂 Estrutura do Projeto

```
├── app/
│   ├── __init__.py              # Pacote Python
│   ├── main.py                  # Aplicação principal (scheduler + orquestração)
│   ├── db_dumper.py             # Abstração de dump de bases de dados
│   ├── storage_provider.py      # Abstração de storage providers
│   ├── email_helper.py          # Auxiliar para envio de emails
│   ├── requirements.txt         # Dependências Python
│   └── backups/                 # Backups locais temporários
├── docker/
│   ├── docker-compose.yaml      # Configuração Docker Compose
│   └── Dockerfile               # Imagem Docker
├── .env.example                 # Exemplo de configuração
└── .gitignore                   # Arquivos ignorados pelo Git
```

## 🏗️ Diagrama de Arquitetura

```
                    ┌───────────────────┐
                    │      main.py      │  (scheduler + orquestração)
                    └─────┬───────┬─────┘
                          │       │
              ┌───────────┘       └───────────┐
              ▼                               ▼
   ┌─────────────────┐              ┌─────────────────┐
   │  DatabaseDumper  │ (ABC)       │ StorageProvider  │ (ABC)
   ├─────────────────┤              ├─────────────────┤
   │ PostgresDumper   │              │ S3StorageProvider│ (ABC)
   │ MySQLDumper      │              │  ├─ R2Storage    │
   │ MSSQLDumper      │              │  └─ S3Storage    │
   │ MongoDumper      │              │ LocalStorage     │
   └─────────────────┘              └─────────────────┘
```

## 🔄 Fluxo de Execução

1. **Scheduler** dispara `gerar_backup()` via expressão CRON (também executa imediatamente ao iniciar)
2. **DatabaseDumper** executa a ferramenta CLI correspondente ao `DB_TYPE` e gera o arquivo de backup em `/tmp`
3. **StorageProvider** faz upload do arquivo para o bucket configurado, organizando em pastas por data (`YYYYMMDD`)
4. **Email** envia notificação de sucesso ou erro
5. **Cleanup** remove o arquivo de backup local

## 🗄️ Bases de Dados Suportadas

| DB_TYPE | Engine | Ferramenta CLI | Extensão | Notas |
|---------|--------|---------------|----------|-------|
| `postgres` | PostgreSQL | `pg_dump` | `.sql` | Formato custom (`-F c`) com blobs |
| `mysql` | MySQL | `mysqldump` | `.sql` | Single-transaction, routines, triggers |
| `mariadb` | MariaDB | `mysqldump` | `.sql` | Mesma ferramenta que MySQL — retrocompatível |
| `mssql` | SQL Server | `sqlcmd` | `.bak` | `BACKUP DATABASE` com compressão |
| `mongodb` | MongoDB | `mongodump` | `.gz` | Archive comprimido com gzip; `DB_AUTH_SOURCE` configurável |

### DatabaseDumper (ABC)

Cada implementação encapsula:
- O comando CLI e suas flags específicas
- A gestão de credenciais (ex: `PGPASSWORD` para PostgreSQL)
- A extensão do arquivo de backup
- Metadados identificadores (`backup-type`, `database`)

A factory `create_dumper_from_env()` seleciona a implementação com base na variável `DB_TYPE`.

## ☁️ Storage Providers

| STORAGE_TYPE | Provider | Região | Endpoint |
|--------------|----------|--------|----------|
| `r2` | Cloudflare R2 | `auto` (fixo) | Obrigatório |
| `s3` | AWS S3 | Configurável (`STORAGE_REGION`) | Opcional |
| `local` | Disco local | — | — |

### StorageProvider (ABC)

`S3StorageProvider` (subclasse de `StorageProvider`) é a base para providers S3-compatible (`R2Storage` e `S3Storage`), utilizando `boto3` com:
- **Lazy loading** do client S3 — criado apenas no primeiro uso
- **Organização por data** — arquivos agrupados em `destination_folder/YYYYMMDD/filename`
- **Metadados** — cada upload inclui tipo de backup, database, timestamp e timezone

`LocalStorage` (subclasse de `StorageProvider`) grava os backups diretamente no disco:
- **Organização por data** — arquivos agrupados em `STORAGE_LOCAL_PATH/YYYYMMDD/filename`
- **Metadados** — ignorados (não aplicável para armazenamento local)

A factory `create_storage_from_env()` seleciona a implementação com base na variável `STORAGE_TYPE`.

## 📊 Monitoramento

- **SEQ** (opcional): Logging estruturado enviado para SEQ se `SEQ_URL` estiver configurado
- **Console**: Logs no stdout sempre ativos, independente do SEQ
- **Email**: Notificações SMTP de sucesso/erro com timestamp local
- **Metadados**: Informações de auditoria armazenadas junto ao arquivo de backup no storage

## 🛠️ Tecnologias

| Componente | Tecnologia |
|------------|------------|
| Linguagem | Python 3.11 |
| Scheduler | APScheduler (CronTrigger) |
| Storage SDK | boto3 (S3-compatible) |
| Logging | seqlog (opcional) + logging stdlib |
| Email | smtplib (SMTP + TLS) |
| Configuração | python-dotenv (.env) |
| Container | Docker (python:3.11-slim) |
| DB Clients | pg_dump, mysqldump, sqlcmd, mongodump |

## 🐳 Docker

A imagem Docker (`python:3.11-slim`) inclui todos os clientes de base de dados:

- `postgresql-client` — para `pg_dump`
- `default-mysql-client` — para `mysqldump` (compatível com MySQL e MariaDB)
- `mssql-tools18` + `msodbcsql18` — para `sqlcmd` (SQL Server)
- `mongodb-database-tools` — para `mongodump` (MongoDB)

A imagem é executada com um usuário não-root (`backup`) por segurança. O tipo de base de dados a utilizar é selecionado via `DB_TYPE` no `.env` — a mesma imagem serve para qualquer engine.
