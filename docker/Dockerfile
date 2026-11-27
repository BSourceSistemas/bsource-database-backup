# amd64 manifest of python:3.11.11-slim-bookworm
FROM python:3.11.11-slim-bookworm@sha256:a8e0a3090316aed0b11037aac613aef32fb1747dcc1dcb5c0f6c727a0113a07f

ARG MYSQL_VERSION=8.4.9
ARG MONGODB_TOOLS_VERSION=100.17.0

LABEL org.opencontainers.image.title="BSource Database Backup" \
      org.opencontainers.image.description="Secure single-database backup service" \
      org.opencontainers.image.version="4.0.1"

ENV DEBIAN_FRONTEND=noninteractive \
    PATH="/opt/mysql84/bin:/opt/mssql-tools18/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN test "$(dpkg --print-architecture)" = "amd64" && \
    apt-get update && apt-get upgrade -y --no-install-recommends && \
    apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg xz-utils libaio1 libnuma1 libtinfo6 && \
    install -d -m 0755 /etc/apt/keyrings && \
    curl --retry 3 --retry-all-errors --connect-timeout 15 --max-time 300 -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor -o /etc/apt/keyrings/postgresql.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/postgresql.gpg] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" > /etc/apt/sources.list.d/postgresql.list && \
    curl --retry 3 --retry-all-errors --connect-timeout 15 --max-time 300 -fsSL https://mariadb.org/mariadb_release_signing_key.asc | gpg --dearmor -o /etc/apt/keyrings/mariadb.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/mariadb.gpg] https://dlm.mariadb.com/repo/mariadb-server/11.4.9/repo/debian bookworm main" > /etc/apt/sources.list.d/mariadb.list && \
    curl --retry 3 --retry-all-errors --connect-timeout 15 --max-time 300 -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /etc/apt/keyrings/microsoft.gpg && \
    echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/microsoft.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" > /etc/apt/sources.list.d/microsoft.list && \
    curl --retry 3 --retry-all-errors --connect-timeout 15 --max-time 300 -fsSL https://www.mongodb.org/static/pgp/server-8.0.asc | gpg --dearmor -o /etc/apt/keyrings/mongodb.gpg && \
    echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/mongodb.gpg] https://repo.mongodb.org/apt/debian bookworm/mongodb-org/8.0 main" > /etc/apt/sources.list.d/mongodb.list && \
    apt-get update && ACCEPT_EULA=Y apt-get install -y --no-install-recommends \
      postgresql-client-17=17.11-1.pgdg12+2 \
      mariadb-client=1:11.4.9+maria~deb12 \
      mssql-tools18=18.6.2.1-1 unixodbc=2.3.11-2+deb12u1 \
      mongodb-database-tools=${MONGODB_TOOLS_VERSION} && \
    rm -rf /var/lib/apt/lists/*

# Oracle's generic minimal distribution is kept separate from MariaDB binaries.
RUN curl --retry 3 --retry-all-errors --connect-timeout 15 --max-time 300 -fsSL -o /tmp/mysql.tar.xz \
      "https://cdn.mysql.com/archives/mysql-8.4/mysql-${MYSQL_VERSION}-linux-glibc2.28-x86_64-minimal.tar.xz" && \
    curl --retry 3 --retry-all-errors --connect-timeout 15 --max-time 300 -fsSL -o /tmp/mysql.tar.xz.asc \
      "https://cdn.mysql.com/archives/mysql-8.4/mysql-${MYSQL_VERSION}-linux-glibc2.28-x86_64-minimal.tar.xz.asc" && \
    curl --retry 3 --retry-all-errors --connect-timeout 15 --max-time 300 -fsSL https://repo.mysql.com/RPM-GPG-KEY-mysql-2025 | gpg --import && \
    gpg --batch --verify /tmp/mysql.tar.xz.asc /tmp/mysql.tar.xz && \
    mkdir -p /opt/mysql84 && \
    tar -xJf /tmp/mysql.tar.xz --strip-components=1 -C /opt/mysql84 && \
    rm -f /tmp/mysql.tar.xz /tmp/mysql.tar.xz.asc && \
    pg_dump --version | grep -E ' 17\.' && \
    /opt/mysql84/bin/mysqldump --version | grep -F '8.4.' && \
    mariadb-dump --version | grep -F '11.4.' && \
    mongodump --version | grep -F "${MONGODB_TOOLS_VERSION}" && \
    sqlcmd -? 2>&1 | grep -F '18.'

WORKDIR /src
COPY pyproject.toml requirements.lock README.md ./
COPY app ./app
RUN pip install --no-cache-dir --disable-pip-version-check -c requirements.lock . && \
    groupadd --force --system backup && \
    (id -u backup >/dev/null 2>&1 || useradd --system --gid backup --home /app backup) && \
    install -d -o backup -g backup -m 0700 /tmp/bsource-backup && \
    install -d -o backup -g backup -m 0750 /var/lib/bsource-backup /app/backups /shared/mssql

USER backup
WORKDIR /app
EXPOSE 8080
VOLUME ["/var/lib/bsource-backup"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/live', timeout=3)"]

ENTRYPOINT ["backup"]
CMD ["scheduler"]
