"""
Módulo de abstração para providers de storage.
Suporta Cloudflare R2, AWS S3 e armazenamento local.
"""
import os
import shutil
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

import boto3
import pytz
from botocore.exceptions import ClientError, NoCredentialsError
import logging

log = logging.getLogger("backup.storage")


class StorageProvider(ABC):
    """Classe abstrata para providers de storage."""

    def __init__(self, destination_folder: Optional[str] = None):
        self.destination_folder = destination_folder or "backups/"

    @abstractmethod
    def upload_file(
        self,
        local_filepath: str,
        filename: str,
        metadata: Optional[dict] = None,
    ) -> str:
        """
        Armazena o arquivo de backup no destino configurado.

        Args:
            local_filepath: Caminho local do arquivo.
            filename: Nome do arquivo de destino.
            metadata: Metadados adicionais (suportado por providers S3-compatible).

        Returns:
            str: Caminho completo do arquivo no destino.
        """

    @abstractmethod
    def test_connection(self) -> bool:
        """Testa se o destino de storage está acessível."""

    def _build_destination_path(self, filename: str) -> str:
        """
        Constrói o caminho de destino com organização por data.

        Formato: destination_folder/YYYYMMDD/filename
        """
        base_folder = self.destination_folder.rstrip("/") + "/" if self.destination_folder else ""

        timezone_name = os.getenv("TIMEZONE", "America/Sao_Paulo")
        try:
            timezone = pytz.timezone(timezone_name)
        except pytz.UnknownTimeZoneError:
            timezone = pytz.timezone("America/Sao_Paulo")

        local_time = datetime.now(timezone)
        date_folder = local_time.strftime("%Y%m%d")

        return f"{base_folder}{date_folder}/{filename}"


# ── Base S3-compatible ───────────────────────────────────────────────────────


class S3StorageProvider(StorageProvider):
    """Base para providers de object storage compatíveis com S3."""

    def __init__(
        self,
        access_key_id: str,
        secret_access_key: str,
        bucket_name: str,
        destination_folder: Optional[str] = None,
    ):
        if not all([access_key_id, secret_access_key, bucket_name]):
            raise ValueError(
                "Configurações de storage incompletas. "
                "Verifique STORAGE_ACCESS_KEY_ID, STORAGE_SECRET_ACCESS_KEY e STORAGE_BUCKET_NAME."
            )

        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.bucket_name = bucket_name
        self._client = None
        super().__init__(destination_folder)

    @property
    def client(self):
        """Cliente S3 lazy-loaded — implementado pelas subclasses."""
        if self._client is None:
            self._client = self._create_client()
        return self._client

    @abstractmethod
    def _create_client(self):
        """Cria e retorna o client boto3 configurado para o provider."""

    def upload_file(
        self,
        local_filepath: str,
        filename: str,
        metadata: Optional[dict] = None,
    ) -> str:
        """
        Faz upload do arquivo para o storage S3-compatible.

        Args:
            local_filepath: Caminho local do arquivo.
            filename: Nome do arquivo no bucket.
            metadata: Metadados adicionais para o arquivo.

        Returns:
            str: Caminho completo do arquivo no storage.

        Raises:
            FileNotFoundError: Arquivo local não encontrado.
            ClientError: Erro na comunicação com o storage.
            NoCredentialsError: Credenciais inválidas ou ausentes.
        """
        if not os.path.exists(local_filepath):
            raise FileNotFoundError(f"Arquivo não encontrado: {local_filepath}")

        destination_path = self._build_destination_path(filename)

        extra_args = {}
        if metadata:
            extra_args["Metadata"] = metadata

        log.info(f"📤 Iniciando upload: {destination_path}")

        try:
            self.client.upload_file(
                local_filepath,
                self.bucket_name,
                destination_path,
                ExtraArgs=extra_args,
            )
            log.info("✅ Upload concluído com sucesso")
            return destination_path

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            log.error(f"❌ Erro do cliente storage: {error_code}")
            raise
        except NoCredentialsError:
            log.error("❌ Credenciais de storage inválidas ou ausentes")
            raise
        except Exception as e:
            log.error(f"❌ Erro inesperado no upload: {e}")
            raise

    def test_connection(self) -> bool:
        """Testa a conexão com o storage."""
        try:
            self.client.list_objects_v2(Bucket=self.bucket_name, MaxKeys=1)
            log.info("✅ Conexão com storage testada com sucesso")
            return True
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            log.error(f"❌ Falha no teste de conexão: {error_code}")
            return False
        except Exception as e:
            log.error(f"❌ Erro inesperado no teste de conexão: {e}")
            return False


# ── Implementações S3-compatible ─────────────────────────────────────────────


class R2Storage(S3StorageProvider):
    """Storage provider para Cloudflare R2."""

    def __init__(
        self,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        bucket_name: str,
        destination_folder: Optional[str] = None,
    ):
        if not endpoint_url:
            raise ValueError(
                "STORAGE_ENDPOINT_URL é obrigatório para Cloudflare R2."
            )
        self.endpoint_url = endpoint_url
        super().__init__(access_key_id, secret_access_key, bucket_name, destination_folder)

    def _create_client(self):
        return boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
            region_name="auto",
        )


class S3Storage(S3StorageProvider):
    """Storage provider para AWS S3."""

    def __init__(
        self,
        access_key_id: str,
        secret_access_key: str,
        bucket_name: str,
        region: str,
        endpoint_url: Optional[str] = None,
        destination_folder: Optional[str] = None,
    ):
        if not region:
            raise ValueError(
                "STORAGE_REGION é obrigatório para AWS S3 (ex: us-east-1)."
            )
        self.region = region
        self.endpoint_url = endpoint_url
        super().__init__(access_key_id, secret_access_key, bucket_name, destination_folder)

    def _create_client(self):
        kwargs = {
            "aws_access_key_id": self.access_key_id,
            "aws_secret_access_key": self.secret_access_key,
            "region_name": self.region,
        }
        if self.endpoint_url:
            kwargs["endpoint_url"] = self.endpoint_url

        return boto3.client("s3", **kwargs)


# ── Armazenamento local ──────────────────────────────────────────────────────


class LocalStorage(StorageProvider):
    """
    Storage provider para armazenamento local em disco.
    Organiza os backups em subpastas por data (YYYYMMDD) dentro do caminho base.

    O `base_path` é mapeado para `destination_folder` da classe pai, o que permite
    reutilizar `_build_destination_path()` para gerar caminhos absolutos no formato
    `base_path/YYYYMMDD/filename`.
    """

    def __init__(
        self,
        base_path: str,
    ):
        if not base_path:
            raise ValueError(
                "STORAGE_LOCAL_PATH é obrigatório para armazenamento local."
            )
        super().__init__(destination_folder=base_path)

    def upload_file(
        self,
        local_filepath: str,
        filename: str,
        metadata: Optional[dict] = None,
    ) -> str:
        """
        Copia o arquivo de backup para o diretório local configurado.

        Args:
            local_filepath: Caminho local do arquivo de origem.
            filename: Nome do arquivo de destino.
            metadata: Ignorado no storage local.

        Returns:
            str: Caminho completo do arquivo salvo.

        Raises:
            FileNotFoundError: Arquivo de origem não encontrado.
            OSError: Erro ao criar diretório ou copiar o arquivo.
        """
        if not os.path.exists(local_filepath):
            raise FileNotFoundError(f"Arquivo não encontrado: {local_filepath}")

        destination_path = self._build_destination_path(filename)
        destination_dir = os.path.dirname(destination_path)

        os.makedirs(destination_dir, exist_ok=True)

        log.info(f"💾 Salvando backup localmente: {destination_path}")
        shutil.copy2(local_filepath, destination_path)
        log.info("✅ Backup salvo localmente com sucesso")

        return destination_path

    def test_connection(self) -> bool:
        """Verifica se o diretório base existe e tem permissão de escrita."""
        base_path = self.destination_folder
        try:
            os.makedirs(base_path, exist_ok=True)
            if os.access(base_path, os.W_OK):
                log.info("✅ Diretório de storage local acessível")
                return True
            log.error(f"❌ Sem permissão de escrita em: {base_path}")
            return False
        except OSError as e:
            log.error(f"❌ Erro ao acessar diretório de storage local: {e}")
            return False


# ── Factory ──────────────────────────────────────────────────────────────────

SUPPORTED_STORAGE_TYPES = ["r2", "s3", "local"]


def create_storage_from_env() -> StorageProvider:
    """
    Cria um StorageProvider a partir das variáveis de ambiente.

    Variáveis utilizadas:
        STORAGE_TYPE: Tipo de storage (r2, s3, local)
        STORAGE_ENDPOINT_URL: URL do endpoint (obrigatório para R2, opcional para S3)
        STORAGE_ACCESS_KEY_ID: Chave de acesso (r2, s3)
        STORAGE_SECRET_ACCESS_KEY: Chave secreta (r2, s3)
        STORAGE_BUCKET_NAME: Nome do bucket (r2, s3)
        STORAGE_DESTINATION_FOLDER: Pasta de destino (padrão: backups/) — r2, s3
        STORAGE_REGION: Região AWS (obrigatório para S3)
        STORAGE_LOCAL_PATH: Caminho base para backups locais (padrão: /app/backups) — local

    Returns:
        StorageProvider: Instância configurada do provider.

    Raises:
        ValueError: Se STORAGE_TYPE não for suportado ou variáveis obrigatórias faltarem.
    """
    storage_type = os.getenv("STORAGE_TYPE", "").lower().strip()

    if storage_type not in SUPPORTED_STORAGE_TYPES:
        raise ValueError(
            f"STORAGE_TYPE '{storage_type}' não suportado. "
            f"Valores aceitos: {', '.join(SUPPORTED_STORAGE_TYPES)}"
        )

    if storage_type == "local":
        base_path = os.getenv("STORAGE_LOCAL_PATH", "/app/backups")
        return LocalStorage(base_path=base_path)

    access_key_id = os.getenv("STORAGE_ACCESS_KEY_ID")
    secret_access_key = os.getenv("STORAGE_SECRET_ACCESS_KEY")
    bucket_name = os.getenv("STORAGE_BUCKET_NAME")
    destination_folder = os.getenv("STORAGE_DESTINATION_FOLDER", "backups/")
    endpoint_url = os.getenv("STORAGE_ENDPOINT_URL")
    region = os.getenv("STORAGE_REGION")

    if storage_type == "r2":
        return R2Storage(
            endpoint_url=endpoint_url,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            bucket_name=bucket_name,
            destination_folder=destination_folder,
        )

    # storage_type == "s3"
    return S3Storage(
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        bucket_name=bucket_name,
        region=region,
        endpoint_url=endpoint_url,
        destination_folder=destination_folder,
    )
