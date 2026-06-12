-- Runs once on first container boot (gvenzl image executes
-- /container-entrypoint-initdb.d/*.sql as SYSDBA).
--
-- Creates the schemas the schema-qualified examples deploy into and
-- grants pell_test the rights to install packages/types across them —
-- the containerized equivalent of compiler/scripts/setup_example_schemas.sql.

ALTER SESSION SET CONTAINER = FREEPDB1;

DECLARE
    TYPE name_list IS TABLE OF VARCHAR2(30);
    -- Distinct schema prefixes across examples/*.pell. AUDIT is
    -- Oracle-reserved, hence AUDITING.
    schemas name_list := name_list(
        'AUDITING', 'BILLING', 'BULK', 'DATA', 'HR', 'INVENTORY',
        'LOOKUPS', 'MARKET', 'ORDERS', 'REPORTS', 'SIGNUPS', 'STD'
    );
BEGIN
    FOR i IN 1 .. schemas.COUNT LOOP
        BEGIN
            EXECUTE IMMEDIATE
                'CREATE USER ' || schemas(i) ||
                ' IDENTIFIED BY pell_demo' ||
                ' DEFAULT TABLESPACE USERS QUOTA UNLIMITED ON USERS';
        EXCEPTION
            WHEN OTHERS THEN
                IF SQLCODE != -1920 THEN RAISE; END IF;  -- exists: fine
        END;
        EXECUTE IMMEDIATE
            'GRANT CREATE SESSION, CREATE PROCEDURE, CREATE TYPE,' ||
            ' CREATE TABLE, CREATE SEQUENCE, CREATE VIEW, CREATE TRIGGER' ||
            ' TO ' || schemas(i);
    END LOOP;
END;
/

-- pell_test (created by the image's APP_USER mechanism) needs:
--   * cross-schema CREATE rights for the schema-qualified examples,
--   * CREATE ANY CONTEXT for pell_runtime's SYS_CONTEXT error channel.
GRANT CREATE ANY PROCEDURE, ALTER ANY PROCEDURE, DROP ANY PROCEDURE,
      CREATE ANY TYPE,      DROP ANY TYPE,
      CREATE ANY TABLE,     DROP ANY TABLE,
      CREATE ANY SEQUENCE,
      CREATE ANY VIEW,
      CREATE ANY TRIGGER,
      EXECUTE ANY PROCEDURE,
      CREATE ANY CONTEXT,   DROP ANY CONTEXT
  TO pell_test;

-- A couple of examples reference DBMS_LOCK (retry/backoff sleeps).
GRANT EXECUTE ON SYS.DBMS_LOCK TO pell_test;

-- Performance-introspection examples (stat_diff, table-stats reports,
-- execution-plan visualizers) read the V$ dynamic views.
GRANT SELECT ON SYS.V_$MYSTAT   TO pell_test;
GRANT SELECT ON SYS.V_$STATNAME TO pell_test;
GRANT SELECT ON SYS.V_$SESSTAT  TO pell_test;
GRANT SELECT ON SYS.V_$SESSION  TO pell_test;
GRANT SELECT ON SYS.V_$SQL      TO pell_test;
GRANT SELECT ON SYS.V_$SQL_PLAN TO pell_test;

-- Debugger support: the pell debugger has the database session connect
-- OUT to the IDE's JDWP listener (DBMS_DEBUG_JDWP.CONNECT_TCP — same
-- mechanism SQL Developer uses). That needs the session privilege plus
-- a network ACE permitting jdwp egress, and DEBUG ANY PROCEDURE to step
-- into units owned by the example schemas.
GRANT DEBUG CONNECT SESSION TO pell_test;
GRANT DEBUG ANY PROCEDURE TO pell_test;
BEGIN
    DBMS_NETWORK_ACL_ADMIN.APPEND_HOST_ACE(
        host => '*',
        ace  => xs$ace_type(
                    privilege_list => xs$name_list('JDWP'),
                    principal_name => 'PELL_TEST',
                    principal_type => xs_acl.ptype_db));
END;
/
