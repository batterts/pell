#!/bin/sh
# Install utPLSQL v3 into the docker harness's Oracle and expose it to
# every schema (public synonyms + grants). Idempotent. Run once after
# the container is up:
#
#     ./docker/install_utplsql.sh
#
# For a remote instance, run install_headless.sql + the grant block in
# this script as SYSDBA there instead.
set -eu

VERSION="${UTPLSQL_VERSION:-v3.1.14}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE="docker compose -f $SCRIPT_DIR/docker-compose.yml"
WORK=$(mktemp -d)

echo "utPLSQL $VERSION → downloading"
curl -sL -o "$WORK/utPLSQL.tar.gz" \
  "https://github.com/utPLSQL/utPLSQL/releases/download/$VERSION/utPLSQL.tar.gz"
tar xzf "$WORK/utPLSQL.tar.gz" -C "$WORK"

echo "→ copying into container"
$COMPOSE cp "$WORK/utPLSQL" oracle:/tmp/utPLSQL

echo "→ install_headless (schema UT3)"
$COMPOSE exec -T oracle bash -c \
  "cd /tmp/utPLSQL/source && sqlplus -S sys/pellsys@FREEPDB1 as sysdba @install_headless.sql UT3 UT3pass USERS" \
  || true   # re-running reports ORA-01920 (UT3 exists); grants below still apply

echo "→ public synonyms + grants"
$COMPOSE exec -T oracle sqlplus -S sys/pellsys@FREEPDB1 as sysdba <<'SQL'
SET FEEDBACK OFF SERVEROUTPUT ON
BEGIN
  FOR o IN (SELECT object_name, object_type FROM dba_objects
             WHERE owner = 'UT3'
               AND object_type IN ('PACKAGE','TYPE','FUNCTION','PROCEDURE','SYNONYM')
               AND object_name NOT LIKE 'SYS%'
               AND generated = 'N') LOOP
    BEGIN
      EXECUTE IMMEDIATE 'GRANT EXECUTE ON UT3."'||o.object_name||'" TO PUBLIC';
    EXCEPTION WHEN OTHERS THEN NULL; END;
    BEGIN
      EXECUTE IMMEDIATE 'CREATE PUBLIC SYNONYM "'||o.object_name||'" FOR UT3."'||o.object_name||'"';
    EXCEPTION WHEN OTHERS THEN NULL; END;
  END LOOP;
  FOR o IN (SELECT object_name FROM dba_objects
             WHERE owner='UT3' AND object_type IN ('TABLE','VIEW') AND generated='N') LOOP
    BEGIN
      EXECUTE IMMEDIATE 'GRANT SELECT ON UT3."'||o.object_name||'" TO PUBLIC';
    EXCEPTION WHEN OTHERS THEN NULL; END;
  END LOOP;
  DBMS_OUTPUT.PUT_LINE('utPLSQL public access configured');
END;
/
SQL

rm -rf "$WORK"
echo "utPLSQL ready — ut.expect/ut.run callable from any schema."
