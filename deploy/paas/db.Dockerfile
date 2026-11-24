# The database for a platform with no shared volume (Render, Railway).
#
# The same Postgres image as docker-compose.yml, with the engine's first-start
# script built in, because these platforms cannot mount a file from the
# repository. Build it with the repository root as the context:
#
#   docker build -f deploy/paas/db.Dockerfile .
#
# Set POSTGRES_PASSWORD (required) and mount the disk at
# /var/lib/postgresql/data. The data lives in the `pgdata` folder under it,
# so the `lost+found` folder a new disk comes with does not stop the first start.
FROM ghcr.io/supabase/postgres:15.8.1.060

# Only the engine's script runs at first start, as in docker-compose.yml,
# which mounts deploy/local/db-init over this folder.
RUN rm -rf /docker-entrypoint-initdb.d/*
COPY deploy/local/db-init/ /docker-entrypoint-initdb.d/

ENV POSTGRES_HOST=/var/run/postgresql \
    POSTGRES_DB=postgres \
    POSTGRES_USER=postgres \
    POSTGRES_PORT=5432 \
    PGDATA=/var/lib/postgresql/data/pgdata

EXPOSE 5432
CMD ["postgres", "-c", "listen_addresses=*"]
