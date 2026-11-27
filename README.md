# BSource Database Backup

Serviço de backup em que cada contêiner protege exatamente um banco, com execução segura dos clientes, validação do dump, upload verificável, scheduler, healthchecks e métricas.

## Bancos e formatos

| `DB_TYPE` | Versões de servidor | Formato | Validação antes do upload |
|---|---|---|---|
| `postgres` | 15–17 | custom `.dump` | `pg_restore --list` |
| `mysql` | 8.0/8.4 | `.sql` | validação estrutural |
| `mariadb` | 10.11/11.4 | `.sql` | validação estrutural |
| `mongodb` | 7/8 | `.archive.gz` | `mongorestore --dryRun` |
| `mssql` | 2019/2022 | `.bak` | volume compartilhado e `RESTORE VERIFYONLY` |

A imagem usa clientes PostgreSQL 17. Dumps das origens PostgreSQL 15–17 são
validados por restauração em PostgreSQL 17; restauração em versões anteriores
ao cliente de dump não é garantida (por exemplo, `transaction_timeout` não existe
no PostgreSQL 15/16).

MySQL e MariaDB usam clientes distintos. SQL Server remoto sem filesystem compartilhado não é suportado.

## Execução

```bash
backup config check
backup database check
backup storage check
backup run
backup status --json
backup scheduler
```

`RUN_MODE=once` executa uma vez e encerra. `RUN_MODE=scheduler` inicia o scheduler e a API em `:8080`. Os códigos de `backup run` são:

| Código | Resultado |
|---:|---|
| 0 | backup e verificação concluídos |
| 1 | configuração inválida |
| 2 | preflight ou conexão |
| 3 | dump |
| 4 | validação |
| 5 | upload |
| 6 | verificação do upload |
| 7 | cancelamento |

Falha exclusiva de notificação mantém código `0`.

## Endpoints

- `GET /health/live`: processo e scheduler ativos.
- `GET /health/ready`: configuração e cliente local aptos.
- `GET /status`: último resultado, próxima execução e degradação.
- `GET /metrics`: métricas Prometheus.

Os endpoints não possuem autenticação e devem permanecer em rede interna.

## SQL Server com volume compartilhado

O caminho usado por `BACKUP DATABASE ... TO DISK` pertence ao SQL Server. Para o serviço conseguir validar e enviar o `.bak`, os dois contêineres devem montar o mesmo volume:

```yaml
services:
  sqlserver:
    image: mcr.microsoft.com/mssql/server:2022-CU20-ubuntu-22.04
    volumes:
      - mssql-backups:/var/opt/mssql/backup

  backup:
    image: bsourcedev/bsource-database-backup:sha-COMMIT
    environment:
      DB_TYPE: mssql
      DB_HOST: sqlserver
      DB_PORT: "1433"
      MSSQL_BACKUP_MODE: shared-volume
      MSSQL_SERVER_BACKUP_DIR: /var/opt/mssql/backup
      MSSQL_LOCAL_BACKUP_DIR: /shared/mssql
    volumes:
      - mssql-backups:/shared/mssql

volumes:
  mssql-backups:
```

O deployment precisa conceder ao usuário do backup leitura e remoção dos arquivos criados pelo usuário `mssql`. Cada arquivo recebe um nome UUID novo; se o SQL Server gravar em outro filesystem, a execução falha porque o arquivo não aparece em `MSSQL_LOCAL_BACKUP_DIR`.

O backup usa `COPY_ONLY`, `CHECKSUM` e `RESTORE VERIFYONLY`. A CI também deve realizar restore real, pois `VERIFYONLY` não valida semanticamente todo o banco.

## Segredos

Cada segredo aceita valor direto ou arquivo, nunca ambos:

- `DB_PASSWORD_FILE`
- `AWS_ACCESS_KEY_ID_FILE`
- `AWS_SECRET_ACCESS_KEY_FILE`
- `SMTP_PASSWORD_FILE`
- `SEQ_API_KEY_FILE`

Arquivos devem existir, não estar vazios e não ser graváveis por terceiros. Nenhum valor é incluído em argumentos dos processos, estado, endpoints ou logs.

## Object keys e integridade

```text
{prefix}/{environment}/{databaseType}/{databaseName}/YYYY/MM/DD/
{timestampUTC}-{runId}.{extension}
```

O serviço calcula SHA-256, envia tamanho/checksum como metadata e executa `HEAD` depois do upload. ETag não é tratado como checksum de multipart. Storage local realiza cópia atômica e recalcula SHA-256 no destino.

## Compatibilidade de configuração

As variáveis v3 `DB_*`, `STORAGE_*`, `EMAIL_*`, `APP_ENV` e `SEQ_*` continuam aceitas. `BACKUP_CRON` e `BACKUP_TIMEZONE` prevalecem sobre os aliases depreciados `CRON_SCHEDULE` e `TIMEZONE`. Variáveis v2 `PG_*` e `R2_*` não são reintroduzidas.

Consulte [.env.example](.env.example) para a lista completa.

## Desenvolvimento

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python -m pytest
.venv/Scripts/python -m ruff check app tests
.venv/Scripts/python -m mypy app
```

Em Linux, use `.venv/bin/python`. Builds oficiais suportam inicialmente apenas `linux/amd64`.

## Notificações SMTP

`EMAIL_AUTH_MODE=credentials` é o padrão e exige `EMAIL_USER` e senha por
`EMAIL_PASSWORD` ou `SMTP_PASSWORD_FILE`. Para usar o mesmo relay interno sem
AUTH, configure `EMAIL_AUTH_MODE=none`; nesse modo `login()` não é executado e
usuário/senha não são obrigatórios. Nos dois modos, configure `EMAIL_FROM`,
`EMAIL_TO` (separados por vírgulas) e `EMAIL_SMTP`.

Use `NOTIFICATION_PROVIDER=email` explicitamente. Quando omitido, o provedor
é inferido se a configuração do modo escolhido estiver completa; caso contrário
continua `noop`. Um provedor explícito sempre prevalece.

STARTTLS permanece habilitado por padrão (`EMAIL_STARTTLS=true`) e valida
certificado e hostname usando as CAs do sistema. O timeout SMTP é de 30 segundos
por operação de socket. Relay sem TLS exige `EMAIL_STARTTLS=false` explícito.
Não há fallback automático de TLS ou autenticação após falha.

Falhas de conexão, TLS, autenticação, envio e recusa parcial de destinatários
propagam até o executor após até três tentativas, com esperas de 1 e 2 segundos.
Envio parcial ou perda da resposta pode causar mensagens duplicadas nas tentativas.
`sent` indica aceite SMTP, não confirmação de entrega na caixa postal.

O resultado do backup e o da notificação são independentes:

- `backup run` e `/status`: `notificationStatus` (`not-requested`, `suppressed`,
  `sent`, `failed`) e `notificationErrorType` (categoria segura ou `null`).
- `backup status --json`: campos `notification_status` e
  `notification_error_type` em `latestRun`.
- Log `NotificationFailed` em nível ERROR, correlacionado por `BackupRunId`.
- Métrica `backup_notifications_total{provider="email",status="failed"}`:
  um resultado por envio lógico, após as tentativas; envios não solicitados ou
  suprimidos não incrementam o contador. Contadores reiniciam com o processo.

Falha exclusiva de notificação mantém o objeto, o sucesso e o código `0` do
backup, sem degradar sua saúde. O cooldown começa apenas após envio bem-sucedido.
Estados de versões anteriores continuam legíveis.

Consulte o [roteiro de aceite 4.0.1](docs/acceptance-4.0.1.md) para testar o relay
na VPS e restaurar um objeto R2 específico em PostgreSQL 17 isolado.
