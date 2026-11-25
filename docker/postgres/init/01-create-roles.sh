#!/bin/sh
set -eu

: "${EVP_WRITER_PASSWORD:?EVP_WRITER_PASSWORD is required}"
: "${EVP_READER_PASSWORD:?EVP_READER_PASSWORD is required}"

psql -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" << 'SQL'

\getenv writer_password EVP_WRITER_PASSWORD
\getenv reader_password EVP_READER_PASSWORD

SELECT format(
  'CREATE ROLE evp_reader LOGIN PASSWORD %L',
  :'reader_password'
) \gexec

SELECT format(
  'CREATE ROLE evp_writer LOGIN PASSWORD %L',
  :'writer_password'
) \gexec

GRANT CONNECT ON DATABASE evp To evp_writer, evp_reader;

GRANT USAGE, CREATE ON SCHEMA public To evp_writer;
GRANT USAGE ON SCHEMA public To evp_reader;

GRANT SELECT ON ALL TABLES IN SCHEMA public To evp_reader;

ALTER DEFAULT PRIVILEGES FOR ROLE evp_writer
IN SCHEMA public
GRANT SELECT ON TABLES To evp_reader;

SQL
