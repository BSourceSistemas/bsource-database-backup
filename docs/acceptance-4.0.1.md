# Aceite operacional da versão 4.0.1

A publicação e os testes locais não comprovam recebimento pelo relay de produção
nem restauração de um objeto do R2 real. Estes dois aceites são executados pelo
operador na VPS e registrados separadamente. Execute os comandos a partir
de um checkout da tag `v4.0.1`, para disponibilizar os roteiros em `scripts/`.

## Imagem e configuração

Obtenha o digest no resumo do workflow **Publicar imagem assinada**. Use a mesma
referência imutável em todos os comandos:

```bash
export BACKUP_IMAGE='bsourcedev/bsource-database-backup@sha256:DIGEST_PUBLICADO'
docker pull "$BACKUP_IMAGE"
docker buildx imagetools inspect "$BACKUP_IMAGE"
```

No arquivo de ambiente usado pelo serviço, configure o mesmo host e porta do
relay existente e os remetentes/destinatários autorizados:

```dotenv
NOTIFICATION_PROVIDER=email
EMAIL_AUTH_MODE=none
EMAIL_STARTTLS=true
EMAIL_SMTP=HOST_DO_RELAY
EMAIL_PORT=587
EMAIL_FROM=REMETENTE_AUTORIZADO
EMAIL_TO=DESTINATARIO_DO_TESTE
```

`none` não envia AUTH, mesmo se usuário/senha estiverem presentes. Para SMTP
com autenticação, use `EMAIL_AUTH_MODE=credentials`, `EMAIL_USER` e uma única
fonte de senha: `EMAIL_PASSWORD` ou `SMTP_PASSWORD_FILE`. STARTTLS valida cadeia
CA e hostname. Para CA privada, monte o bundle confiável e configure
`SSL_CERT_FILE`; não desative a verificação para contornar erros de certificado.
`EMAIL_STARTTLS=false` é a opção explícita para relay que não oferece TLS.

Os exemplos abaixo usam `/caminho/backup.env` e `REDE_DO_RELAY`: substitua pelos
recursos existentes da VPS. Se usar variáveis `*_FILE`, acrescente
`-v /caminho/secrets:/run/secrets:ro` aos comandos, preservando os caminhos
configurados. O arquivo de ambiente não é um script: não execute `source` nele.

## Relay na VPS

Este comando envia uma mensagem de teste e não executa backup:

```bash
docker run --rm --network REDE_DO_RELAY \
  --env-file /caminho/backup.env \
  -e NOTIFICATION_PROVIDER=email \
  -v "$PWD/scripts/check_email.py:/checks/email.py:ro" \
  --entrypoint python "$BACKUP_IMAGE" /checks/email.py
```

Aceite: código `0`, `notificationStatus=sent` e recebimento confirmado no
endereço de teste, correlacionado pelo `runId`. `sent` significa aceite SMTP;
não comprova entrega na caixa postal. Em falha, o roteiro retorna código `1`
e somente a categoria do erro, sem a resposta SMTP bruta.

Para validar o fluxo completo, execute uma vez usando a configuração da VPS,
com estado separado do scheduler:

```bash
umask 077
docker run --rm --network REDE_DO_RELAY \
  --env-file /caminho/backup.env \
  -e NOTIFICATION_PROVIDER=email -e NOTIFY_ON_SUCCESS=true \
  -e STATE_FILE=/tmp/acceptance-state.json \
  "$BACKUP_IMAGE" run > acceptance-run.jsonl
python3 - <<'PY'
import json
from pathlib import Path
records = [json.loads(line) for line in Path('acceptance-run.jsonl').read_text().splitlines() if line.strip()]
result = next(record for record in reversed(records) if 'runId' in record and 'success' in record)
assert result['success'], 'Backup did not succeed'
Path('backup-result.json').write_text(json.dumps(result))
print(json.dumps(result))
PY
```

O teste cria um novo objeto de backup no destino configurado. Verifique
`success=true`, `notificationStatus=sent` e recebimento. Em caso de falha apenas
no e-mail, o código do backup continua `0`, `error=null` e
`notificationStatus=failed`; registre `notificationErrorType`, `runId`,
`objectKey` e checksum. O objeto permanece disponível para o restore.

Falhas controladas de conexão/TLS/envio são testadas localmente; não altere o
relay compartilhado para provocá-las em produção. Em scheduler, consulte
`/status`, `backup status --json`, o evento `NotificationFailed` e a métrica
`backup_notifications_total{provider="email",status="failed"}`.

## R2 → PostgreSQL 17 isolado

Use `backup-result.json` da execução específica acima, com `success=true`,
`objectKey`, `fileSizeBytes` e `checksum`. Não selecione um objeto por nome parcial
ou por "mais recente". Prepare consultas determinísticas de verificação de
esquema e dados e a saída esperada, coerentes com o snapshot do backup.

Exemplo para um banco sintético contendo `restore_probe(id, value)`:

```bash
cat > verify.sql <<'SQL'
SELECT column_name || ':' || data_type FROM information_schema.columns
WHERE table_schema='public' AND table_name='restore_probe' ORDER BY ordinal_position;
SELECT id || ':' || value FROM public.restore_probe ORDER BY id;
SQL
cat > expected.txt <<'EXPECTED'
id:integer
value:text
1:alpha
2:beta
3:gamma
EXPECTED

python3 scripts/verify_r2_restore.py \
  --image "$BACKUP_IMAGE" \
  --env-file /caminho/backup.env \
  --result backup-result.json \
  --verify-sql verify.sql \
  --expected-results expected.txt
```

Substitua as consultas/expectativas pelo esquema e dados do banco escolhido.
Se necessário, acrescente `--secrets-dir /caminho/secrets`. A origem deve ser
PostgreSQL 17; o destino padrão é `postgres:17.10-bookworm`, substituível por
outra imagem PostgreSQL 17 compatível via `--postgres-image`.

O roteiro verifica metadata, baixa apenas o objeto indicado, recalcula tamanho
e SHA-256 e restaura usando `pg_restore --exit-on-error --no-owner --no-acl`.
Compara a saída das consultas com o arquivo esperado. Portanto, não valida a
recriação de owners/ACLs de produção. Extensões presentes no dump precisam
estar disponíveis na imagem PostgreSQL de destino.

O destino usa rede Docker interna, sem portas publicadas, senha aleatória e
volumes novos. As credenciais de produção são usadas somente no contêiner de
download. O roteiro não se conecta ao banco de origem e não remove objetos R2.
Contêiner, rede e volumes de teste são removidos no encerramento normal ou por
erro. Após interrupção forçada do processo/host, confira recursos com prefixo
`bsource-restore-` antes de removê-los manualmente.

Aceite: saída `restoreVerified=true`, chave e checksum correspondentes ao
backup, referência da imagem por digest e comparação de esquema/dados aprovada.
Guarde essa saída e a evidência de recebimento do relay no registro da versão.

## Publicação

A versão do pacote, label Docker e tag devem corresponder a `4.0.1`/`v4.0.1`.
O workflow aceita push da tag ou acionamento manual nessa tag; não publique a
mesma tag novamente depois de distribuir seu digest. Se o push já iniciou a
publicação, acompanhe essa execução em vez de disparar outra concorrente.
O resumo registra imagem, versão, commit e digest após assinatura, SBOM e
proveniência. Execute os aceites acima usando exatamente esse digest.
