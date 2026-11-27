#!/usr/bin/env bash
set -euo pipefail

postgres_image="${1:?postgres image tag is required}"
target_image="${2:-postgres:17.10-bookworm}"
app_image="${BACKUP_TEST_IMAGE:-bsource-database-backup:test}"
suffix="${GITHUB_RUN_ID:-local}-${RANDOM}"
network="bsource-it-${suffix}"
source_container="bsource-pg-source-${suffix}"
target_container="bsource-pg-target-${suffix}"
minio_container="bsource-minio-${suffix}"
artifact_volume="bsource-artifact-${suffix}"

cleanup() {
  docker rm -f "$source_container" "$target_container" "$minio_container" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
  docker volume rm "$artifact_volume" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker network create "$network" >/dev/null
docker volume create "$artifact_volume" >/dev/null
docker run -d --name "$minio_container" --network "$network" --network-alias minio \
  -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin \
  minio/minio:RELEASE.2025-04-22T22-12-26Z server /data >/dev/null
docker run -d --name "$source_container" --network "$network" --network-alias pg-source \
  -e POSTGRES_PASSWORD=test-password -e POSTGRES_DB=source_db "$postgres_image" >/dev/null
docker run -d --name "$target_container" --network "$network" --network-alias pg-target \
  -e POSTGRES_PASSWORD=test-password -e POSTGRES_DB=target_db "$target_image" >/dev/null

for container in "$source_container" "$target_container"; do
  for _ in $(seq 1 60); do
    docker exec "$container" pg_isready -U postgres >/dev/null 2>&1 && break
    sleep 1
  done
done
for _ in $(seq 1 60); do
  docker run --rm --network "$network" --entrypoint /bin/sh minio/mc:RELEASE.2025-04-16T18-13-26Z \
    -c "mc alias set local http://minio:9000 minioadmin minioadmin >/dev/null && mc ready local" \
    >/dev/null 2>&1 && break
  sleep 1
done

docker exec -e PGPASSWORD=test-password "$source_container" psql -U postgres -d source_db -v ON_ERROR_STOP=1 \
  -c "CREATE TABLE restore_probe(id integer PRIMARY KEY, value text); INSERT INTO restore_probe VALUES (1,'alpha'),(2,'beta'),(3,'gamma');" >/dev/null
docker run --rm --network "$network" --entrypoint /bin/sh minio/mc:RELEASE.2025-04-16T18-13-26Z \
  -c "mc alias set local http://minio:9000 minioadmin minioadmin >/dev/null && mc mb local/backups" >/dev/null

docker run --rm --network "$network" \
  -e RUN_MODE=once -e BACKUP_TIMEZONE=UTC -e APP_ENV=integration \
  -e DB_TYPE=postgres -e DB_HOST=pg-source -e DB_PORT=5432 -e DB_USER=postgres \
  -e DB_PASSWORD=test-password -e DB_DATABASE=source_db \
  -e STORAGE_TYPE=s3 -e STORAGE_ENDPOINT_URL=http://minio:9000 -e STORAGE_REGION=us-east-1 \
  -e STORAGE_ACCESS_KEY_ID=minioadmin -e STORAGE_SECRET_ACCESS_KEY=minioadmin \
  -e STORAGE_BUCKET_NAME=backups -e STORAGE_DESTINATION_FOLDER=integration \
  "$app_image" run

docker run --rm --network "$network" -v "${artifact_volume}:/out" \
  --entrypoint /bin/sh minio/mc:RELEASE.2025-04-16T18-13-26Z -c \
  "mc alias set local http://minio:9000 minioadmin minioadmin >/dev/null && mc find local/backups --name '*.dump' --exec 'mc cp {} /out/backup.dump'"

docker run --rm --network "$network" -v "${artifact_volume}:/restore:ro" \
  -e PGPASSWORD=test-password --entrypoint pg_restore "$app_image" \
  -h pg-target -p 5432 -U postgres -d target_db --clean --if-exists /restore/backup.dump

restored_count="$(docker exec -e PGPASSWORD=test-password "$target_container" \
  psql -U postgres -d target_db -Atqc "SELECT count(*) FROM restore_probe")"
test "$restored_count" = "3"
