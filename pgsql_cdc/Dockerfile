# ---------- Stage 1: Build ----------
FROM postgres:13.12 AS builder

# Install build dependencies.
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    build-essential \
    postgresql-server-dev-13 \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

# Copy the extension source into the container.
COPY request_id_logger.c /tmp/request_id_logger.c
COPY request_id_logger.control /tmp/request_id_logger.control
COPY request_id_logger--1.0.sql /tmp/request_id_logger--1.0.sql

WORKDIR /tmp

# Build the extension.
RUN pg_config --includedir-server && \
    gcc -Wall -Werror -fPIC -I$(pg_config --includedir-server) \
        -I$(pg_config --includedir) \
        -shared -o request_id_logger.so request_id_logger.c

# ---------- Stage 2: Runtime ----------
FROM postgres:13.12

COPY wal2json.so /usr/lib/postgresql/13/lib/wal2json.so

COPY --from=builder /tmp/request_id_logger.so /usr/lib/postgresql/13/lib/
COPY --chmod=0644 --from=builder /tmp/request_id_logger.control /usr/share/postgresql/13/extension/
COPY --chmod=0644 --from=builder /tmp/request_id_logger--1.0.sql /usr/share/postgresql/13/extension/
COPY --chmod=0644 init-request-id-logger.sql /docker-entrypoint-initdb.d/10-request-id-logger.sql
