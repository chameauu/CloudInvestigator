# Postgres 18 + pgvector — matches PLAN.md Phase 1 pins (PG 18.6, pgvector v0.8.7).
#
# The official postgres:18-alpine image builds Postgres from source under
# /usr/local, so unlike a distro package there is no postgresql-dev to install:
#   - headers already live in /usr/local/include/postgresql/server
#   - PG_CONFIG must point at /usr/local/bin/pg_config
#   - `make install` must land in /usr/local/share/postgresql/extension (where
#     the other built-in extensions already live) — asserted at build time.
#
# with_llvm=no: this image's PGXS is configured --with-llvm and its bitcode
# rule hard-requires `clang-21` (Makefile.global: CLANG = clang-21), which the
# runtime image does not ship. Building the extension without bitcode only
# forgoes LLVM-JIT inlining of extension C functions; pgvector itself is fully
# functional. pgxs.mk gates every bitcode step behind `ifeq ($(with_llvm), yes)`.
#
# Build deps (build-base, git) are added and purged in a single RUN layer.
FROM postgres:18-alpine

RUN apk add --no-cache --virtual .pgvector-build-deps build-base git \
    && git clone --branch v0.8.7 --depth 1 \
        https://github.com/pgvector/pgvector.git /tmp/pgvector \
    && make -C /tmp/pgvector PG_CONFIG=/usr/local/bin/pg_config with_llvm=no \
    && make -C /tmp/pgvector PG_CONFIG=/usr/local/bin/pg_config with_llvm=no install \
    && ls /usr/local/share/postgresql/extension/vector.control \
    && ls /usr/local/lib/postgresql/vector.so \
    && rm -rf /tmp/pgvector \
    && apk del .pgvector-build-deps

# On a fresh (empty) volume the entrypoint runs this once, so `make up` yields a
# database with the vector extension already usable. Warm volumes are untouched.
RUN mkdir -p /docker-entrypoint-initdb.d \
    && printf 'CREATE EXTENSION IF NOT EXISTS vector;\n' \
        > /docker-entrypoint-initdb.d/00-vector.sql
