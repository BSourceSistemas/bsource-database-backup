"""
Módulo de abstração para dump de bases de dados.
Suporta PostgreSQL, MySQL/MariaDB e SQL Server.
"""
import os
import subprocess
from abc import ABC, abstractmethod
from typing import Optional
import logging

log = logging.getLogger("backup.db")


class DatabaseDumper(ABC):
    """Classe abstrata para dump de bases de dados."""

    def __init__(
        self,
        host: str,
        port: str,
        user: str,
        password: str,
        database: str,
        db_type: str,
    ):
        if not all([host, port, user, password, database]):
            raise ValueError(
                "Configurações de base de dados incompletas. "
                "Verifique DB_HOST, DB_PORT, DB_USER, DB_PASSWORD e DB_DATABASE."
            )

        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.db_type = db_type

    @abstractmethod
    def dump(self, output_path: str) -> str:
        """
        Executa o dump da base de dados.

        Args:
            output_path: Caminho completo do arquivo de saída.

        Returns:
            str: Caminho do arquivo gerado.

        Raises:
            subprocess.CalledProcessError: Se o comando de dump falhar.
        """

    def get_metadata(self) -> dict:
        """Retorna metadados sobre o backup para uso no upload."""
        return {
            "uploaded-by": "bsource-db-backup",
            "database": self.database,
            "backup-type": self.db_type,
        }

    @abstractmethod
    def get_file_extension(self) -> str:
        """Retorna a extensão do arquivo de backup (ex: '.sql', '.bak')."""


class PostgresDumper(DatabaseDumper):
    """Dump de bases de dados PostgreSQL via pg_dump."""

    def __init__(self, host: str, port: str, user: str, password: str, database: str):
        super().__init__(host, port, user, password, database, db_type="postgres")

    def dump(self, output_path: str) -> str:
        os.environ["PGPASSWORD"] = self.password

        command = (
            f"pg_dump -h {self.host} -p {self.port} -U {self.user} "
            f"-F c -b -v -f {output_path} {self.database}"
        )

        log.info(f"Executando: {command}")
        subprocess.run(command, shell=True, check=True)
        return output_path

    def get_file_extension(self) -> str:
        return ".sql"


class MySQLDumper(DatabaseDumper):
    """
    Dump de bases de dados MySQL e MariaDB via mysqldump.
    Compatível com ambos os engines — mysqldump é retrocompatível.
    """

    def __init__(
        self,
        host: str,
        port: str,
        user: str,
        password: str,
        database: str,
        db_type: str = "mysql",
    ):
        super().__init__(host, port, user, password, database, db_type=db_type)

    def dump(self, output_path: str) -> str:
        command = (
            f"mysqldump --host={self.host} --port={self.port} "
            f"--user={self.user} --password={self.password} "
            f"--single-transaction --routines --triggers "
            f"--databases {self.database} "
            f"--result-file={output_path}"
        )

        log.info(f"Executando: mysqldump --host={self.host} --port={self.port} "
                 f"--user={self.user} --password=****** "
                 f"--single-transaction --routines --triggers "
                 f"--databases {self.database} --result-file={output_path}")
        subprocess.run(command, shell=True, check=True)
        return output_path

    def get_file_extension(self) -> str:
        return ".sql"


class MongoDumper(DatabaseDumper):
    """Dump de bases de dados MongoDB via mongodump."""

    def __init__(
        self,
        host: str,
        port: str,
        user: str,
        password: str,
        database: str,
        auth_source: str = "admin",
    ):
        super().__init__(host, port, user, password, database, db_type="mongodb")
        self.auth_source = auth_source

    def dump(self, output_path: str) -> str:
        command = (
            f"mongodump --host={self.host} --port={self.port} "
            f"--username={self.user} --password={self.password} "
            f"--authenticationDatabase={self.auth_source} "
            f"--db={self.database} "
            f"--archive={output_path} --gzip"
        )

        log.info(
            f"Executando: mongodump --host={self.host} --port={self.port} "
            f"--username={self.user} --password=****** "
            f"--authenticationDatabase={self.auth_source} "
            f"--db={self.database} --archive={output_path} --gzip"
        )
        subprocess.run(command, shell=True, check=True)
        return output_path

    def get_file_extension(self) -> str:
        return ".gz"


class MSSQLDumper(DatabaseDumper):
    """Dump de bases de dados SQL Server via sqlcmd."""

    def __init__(self, host: str, port: str, user: str, password: str, database: str):
        super().__init__(host, port, user, password, database, db_type="mssql")

    def dump(self, output_path: str) -> str:
        backup_query = (
            f"BACKUP DATABASE [{self.database}] "
            f"TO DISK = N'{output_path}' "
            f"WITH FORMAT, INIT, COMPRESSION, "
            f"NAME = N'{self.database}-backup'"
        )

        command = (
            f'sqlcmd -S {self.host},{self.port} '
            f'-U {self.user} -P {self.password} '
            f'-Q "{backup_query}" -C'
        )

        log.info(f"Executando: sqlcmd -S {self.host},{self.port} "
                 f"-U {self.user} -P ****** -Q \"BACKUP DATABASE ...\" -C")
        subprocess.run(command, shell=True, check=True)
        return output_path

    def get_file_extension(self) -> str:
        return ".bak"


# ── Factory ──────────────────────────────────────────────────────────────────

_DUMPER_MAP = {
    "postgres": PostgresDumper,
    "mysql": MySQLDumper,
    "mariadb": MySQLDumper,
    "mssql": MSSQLDumper,
    "mongodb": MongoDumper,
}

SUPPORTED_DB_TYPES = list(_DUMPER_MAP.keys())


def create_dumper_from_env() -> DatabaseDumper:
    """
    Cria um DatabaseDumper a partir das variáveis de ambiente.

    Variáveis utilizadas:
        DB_TYPE: Tipo de base (postgres, mysql, mariadb, mssql)
        DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_DATABASE

    Returns:
        DatabaseDumper: Instância configurada do dumper.

    Raises:
        ValueError: Se DB_TYPE não for suportado ou variáveis obrigatórias faltarem.
    """
    db_type = os.getenv("DB_TYPE", "").lower().strip()

    if db_type not in _DUMPER_MAP:
        raise ValueError(
            f"DB_TYPE '{db_type}' não suportado. "
            f"Valores aceitos: {', '.join(SUPPORTED_DB_TYPES)}"
        )

    host = os.getenv("DB_HOST")
    port = os.getenv("DB_PORT")
    user = os.getenv("DB_USER")
    password = os.getenv("DB_PASSWORD")
    database = os.getenv("DB_DATABASE")

    dumper_class = _DUMPER_MAP[db_type]

    # MySQLDumper aceita db_type para diferenciar mysql de mariadb nos metadados
    if db_type in ("mysql", "mariadb"):
        return dumper_class(
            host=host, port=port, user=user,
            password=password, database=database, db_type=db_type,
        )

    # MongoDumper aceita auth_source para configurar a base de autenticação
    if db_type == "mongodb":
        auth_source = os.getenv("DB_AUTH_SOURCE", "admin")
        return dumper_class(
            host=host, port=port, user=user,
            password=password, database=database, auth_source=auth_source,
        )

    return dumper_class(
        host=host, port=port, user=user,
        password=password, database=database,
    )
