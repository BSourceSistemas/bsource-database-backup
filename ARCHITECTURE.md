# Arquitetura

## Invariantes

- Uma instância contém uma configuração e protege um banco.
- O dump só é bem-sucedido depois da validação e da confirmação do objeto.
- Notificação e backup possuem resultados independentes.
- Nenhum cliente é executado por shell e nenhuma senha entra em `argv`.
- Arquivos intermediários são privados, exclusivos por execução e sempre limpos.

## Componentes

```text
CLI / FastAPI / APScheduler
            |
      BackupExecutor
       /    |     \
 Database  Storage  Notifier
  Dumper   Provider
            |
     BackupStateStore
```

`BackupExecutor` é o único dono do pipeline. CLI e scheduler apenas o disparam. `BackupStateStore` usa substituição atômica e permite que `/status` sobreviva a reinicializações quando seu diretório é persistido.

## Pipeline

```text
configuration -> preflight -> connection -> dump -> validation
              -> upload -> upload-verification -> notification -> cleanup
```

Uma etapa obrigatória interrompe as seguintes. Notification e cleanup são best-effort, preservando o erro obrigatório original. Upload e verificação são retentados; dump não é.

## SQL Server

O modo `shared-volume` possui dois nomes para o mesmo diretório: um visto pelo SQL Server e outro pelo serviço. O SQL Server gera `{runId}.bak`; o serviço exige o arquivo na montagem local e o servidor executa `RESTORE VERIFYONLY WITH CHECKSUM`. Isso prova acesso ao artefato sem depender de `localhost`, que não atravessa contêineres.

## Estado e health

O estado contém somente timestamps, contadores e o último `BackupRun` sanitizado. `/health/live` mede processo/scheduler; `/health/ready` mede capacidade local; `/status` avalia idade do último sucesso, falhas consecutivas, scheduler e timeout da execução atual.

## Segurança operacional

- API interna sem autenticação.
- Um worker Uvicorn, uma réplica e `max_instances=1`.
- Imagem não-root e inicialmente `amd64`.
- Retenção é responsabilidade de lifecycle do bucket.
- Object keys preservam case, codificam segmentos inseguros e incluem UUID.
