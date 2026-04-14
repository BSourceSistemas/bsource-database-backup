FROM python:3.11-slim

# Metadados da imagem
LABEL maintainer="BSource Team"
LABEL description="Sistema automatizado de backup de bases de dados com suporte a múltiplos engines e storage providers"
LABEL version="3.0.0"

WORKDIR /app

# Instalar dependências do sistema: clientes de PostgreSQL, MySQL/MariaDB, SQL Server e MongoDB
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        gnupg \
        ca-certificates \
        postgresql-client \
        default-mysql-client && \
    # Repositório Microsoft para mssql-tools e ODBC driver
    curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" > /etc/apt/sources.list.d/mssql-release.list && \
    apt-get update && \
    ACCEPT_EULA=Y apt-get install -y --no-install-recommends \
        msodbcsql18 \
        mssql-tools18 && \
    # Repositório MongoDB para mongodb-database-tools (mongodump)
    curl -fsSL https://www.mongodb.org/static/pgp/server-8.0.asc | gpg --dearmor -o /usr/share/keyrings/mongodb-server-8.0.gpg && \
    echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-8.0.gpg ] https://repo.mongodb.org/apt/debian bookworm/mongodb-org/8.0 main" > /etc/apt/sources.list.d/mongodb-org-8.0.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        mongodb-database-tools && \
    rm -rf /var/lib/apt/lists/*

# Adicionar mssql-tools ao PATH
ENV PATH="$PATH:/opt/mssql-tools18/bin"

# Copiar arquivos da aplicação
COPY app/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY app/ .

# Criar usuário não-root para segurança
RUN groupadd -f backup && \
    id -u backup 2>/dev/null || useradd -r -g backup backup && \
    chown -R backup:backup /app && \
    mkdir -p /tmp && \
    chown backup:backup /tmp

USER backup

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import boto3; import os; print('OK')" || exit 1

# Comando padrão
CMD ["python", "-u", "main.py"]