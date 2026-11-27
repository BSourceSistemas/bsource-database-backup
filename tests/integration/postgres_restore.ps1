param(
    [Parameter(Mandatory = $false)]
    [string]$PostgresImage = "postgres:17.10-bookworm"
)

$ErrorActionPreference = "Stop"
$suffix = "local-$PID"
$network = "bsource-it-$suffix"
$sourceContainer = "bsource-pg-source-$suffix"
$targetContainer = "bsource-pg-target-$suffix"
$minioContainer = "bsource-minio-$suffix"
$artifactVolume = "bsource-artifact-$suffix"

function Invoke-Docker {
    & docker @args
    if ($LASTEXITCODE -ne 0) {
        throw "docker command failed with exit code $LASTEXITCODE"
    }
}

try {
    Invoke-Docker network create $network | Out-Null
    Invoke-Docker volume create $artifactVolume | Out-Null
    Invoke-Docker run -d --name $minioContainer --network $network --network-alias minio `
        -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin `
        minio/minio:RELEASE.2025-04-22T22-12-26Z server /data | Out-Null
    Invoke-Docker run -d --name $sourceContainer --network $network --network-alias pg-source `
        -e POSTGRES_PASSWORD=test-password -e POSTGRES_DB=source_db $PostgresImage | Out-Null
    Invoke-Docker run -d --name $targetContainer --network $network --network-alias pg-target `
        -e POSTGRES_PASSWORD=test-password -e POSTGRES_DB=target_db $PostgresImage | Out-Null

    foreach ($container in @($sourceContainer, $targetContainer)) {
        $ready = $false
        foreach ($attempt in 1..60) {
            & docker exec $container pg_isready -U postgres 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { $ready = $true; break }
            Start-Sleep -Seconds 1
        }
        if (-not $ready) { throw "$container did not become ready" }
    }

    $minioReady = $false
    foreach ($attempt in 1..60) {
        & docker run --rm --network $network --entrypoint /bin/sh minio/mc:RELEASE.2025-04-16T18-13-26Z `
            -c "mc alias set local http://minio:9000 minioadmin minioadmin >/dev/null && mc ready local" `
            2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { $minioReady = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $minioReady) { throw "MinIO did not become ready" }

    Invoke-Docker exec -e PGPASSWORD=test-password $sourceContainer psql -U postgres -d source_db `
        -v ON_ERROR_STOP=1 -c "CREATE TABLE restore_probe(id integer PRIMARY KEY, value text); INSERT INTO restore_probe VALUES (1,'alpha'),(2,'beta'),(3,'gamma');" | Out-Null
    Invoke-Docker run --rm --network $network --entrypoint /bin/sh `
        minio/mc:RELEASE.2025-04-16T18-13-26Z -c `
        "mc alias set local http://minio:9000 minioadmin minioadmin >/dev/null && mc mb local/backups" | Out-Null
    Invoke-Docker run --rm --network $network `
        -e RUN_MODE=once -e BACKUP_TIMEZONE=UTC -e APP_ENV=integration `
        -e DB_TYPE=postgres -e DB_HOST=pg-source -e DB_PORT=5432 -e DB_USER=postgres `
        -e DB_PASSWORD=test-password -e DB_DATABASE=source_db `
        -e STORAGE_TYPE=s3 -e STORAGE_ENDPOINT_URL=http://minio:9000 -e STORAGE_REGION=us-east-1 `
        -e STORAGE_ACCESS_KEY_ID=minioadmin -e STORAGE_SECRET_ACCESS_KEY=minioadmin `
        -e STORAGE_BUCKET_NAME=backups -e STORAGE_DESTINATION_FOLDER=integration `
        bsource-database-backup:test run | Out-Host

    Invoke-Docker run --rm --network $network -v "${artifactVolume}:/out" `
        --entrypoint /bin/sh minio/mc:RELEASE.2025-04-16T18-13-26Z -c `
        "mc alias set local http://minio:9000 minioadmin minioadmin >/dev/null && mc find local/backups --name '*.dump' --exec 'mc cp {} /out/backup.dump'" | Out-Null
    Invoke-Docker run --rm --network $network -v "${artifactVolume}:/restore:ro" `
        -e PGPASSWORD=test-password --entrypoint pg_restore bsource-database-backup:test `
        -h pg-target -p 5432 -U postgres -d target_db --clean --if-exists /restore/backup.dump | Out-Null

    $restoredCount = (& docker exec -e PGPASSWORD=test-password $targetContainer `
        psql -U postgres -d target_db -Atqc "SELECT count(*) FROM restore_probe").Trim()
    if ($restoredCount -ne "3") { throw "Expected 3 restored rows, got $restoredCount" }
    Write-Host "PostgreSQL dump/upload/restore verified for $PostgresImage"
}
finally {
    & docker rm -f $sourceContainer $targetContainer $minioContainer 2>$null | Out-Null
    & docker network rm $network 2>$null | Out-Null
    & docker volume rm $artifactVolume 2>$null | Out-Null
}
