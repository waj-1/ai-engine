# The gateway for a platform with no shared volume (Render, Railway).
#
# The same Kong image as docker-compose.yml with deploy/paas/kong.yml built
# in. Build it with the repository root as the context:
#
#   docker build -f deploy/paas/gateway.Dockerfile .
#
# Set REST_HOST and AUTH_HOST to the private addresses of the REST layer and
# the auth service (REST_PORT and AUTH_PORT default to 3000 and 9999).
# Keep this service private: it has no API key check (see kong.yml).
FROM kong:2.8.1

COPY deploy/paas/kong.yml /home/kong/temp.yml

ENV KONG_DATABASE=off \
    KONG_DECLARATIVE_CONFIG=/home/kong/kong.yml \
    KONG_PROXY_LISTEN=0.0.0.0:8000 \
    KONG_ADMIN_LISTEN=off \
    KONG_DNS_ORDER=LAST,A,CNAME \
    KONG_NGINX_WORKER_PROCESSES=1 \
    KONG_NGINX_PROXY_PROXY_BUFFER_SIZE=160k \
    KONG_NGINX_PROXY_PROXY_BUFFERS="64 160k"

EXPOSE 8000
ENTRYPOINT ["bash", "-c", "sed -e \"s|__REST_UPSTREAM__|${REST_HOST:?set REST_HOST}:${REST_PORT:-3000}|\" -e \"s|__AUTH_UPSTREAM__|${AUTH_HOST:?set AUTH_HOST}:${AUTH_PORT:-9999}|\" /home/kong/temp.yml > /home/kong/kong.yml && exec /docker-entrypoint.sh kong docker-start"]
