-- ===========================================================================
-- setup_example_schemas.sql
--
-- Creates the schemas that the schema-qualified pell examples deploy into,
-- and grants the PELL_TEST user the rights to install packages / types /
-- tables into them.
--
-- Why this is needed: several examples declare a schema-qualified module —
-- e.g. `module audit.charges;` lowers to `CREATE OR REPLACE PACKAGE
-- audit.charges`. Deploying that as PELL_TEST fails with ORA-01031 /
-- ORA-04050 unless (a) the `audit` schema exists and (b) PELL_TEST has the
-- CREATE ANY PROCEDURE / TYPE rights to write into it.
--
-- Run as a DBA (SYSTEM or SYS) against the target PDB:
--     sqlplus system/<pwd>@host:1521/FREEPDB1 @setup_example_schemas.sql
-- or:
--     pell sql scripts/setup_example_schemas.sql   (connected as a DBA)
--
-- Idempotent: re-running is safe (existing users are skipped).
-- ===========================================================================

DECLARE
    TYPE name_list IS TABLE OF VARCHAR2(30);
    -- The distinct schema prefixes across examples/*.pell. Keep in sync if
    -- a new schema-qualified example lands.
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
                -- ORA-01920: user name conflicts (already exists) — fine.
                IF SQLCODE != -1920 THEN RAISE; END IF;
        END;
        -- Minimal object-creation rights so the schema can own pell packages.
        EXECUTE IMMEDIATE
            'GRANT CREATE SESSION, CREATE PROCEDURE, CREATE TYPE,' ||
            ' CREATE TABLE, CREATE SEQUENCE, CREATE VIEW, CREATE TRIGGER' ||
            ' TO ' || schemas(i);
    END LOOP;
END;
/

-- Let PELL_TEST install objects INTO the schemas above
-- (`CREATE OR REPLACE PACKAGE audit.charges`, the schema-qualified form).
-- The ANY-privileges are what the cross-schema CREATE requires.
GRANT CREATE ANY PROCEDURE, ALTER ANY PROCEDURE, DROP ANY PROCEDURE,
      CREATE ANY TYPE,      DROP ANY TYPE,
      CREATE ANY TABLE,     DROP ANY TABLE,
      CREATE ANY SEQUENCE,
      CREATE ANY VIEW,
      CREATE ANY TRIGGER,
      EXECUTE ANY PROCEDURE
  TO PELL_TEST;

-- Debugger support. The pell debugger uses DBMS_DEBUG_JDWP: the database
-- session connects OUT to the IDE's JDWP listener (the SQL Developer
-- mechanism). DEBUG CONNECT SESSION enables it; DEBUG ANY PROCEDURE lets
-- the debugger step into units owned by the example schemas; the ACE
-- permits the jdwp network egress (without it CONNECT_TCP raises
-- ORA-24247).
GRANT DEBUG CONNECT SESSION TO PELL_TEST;
GRANT DEBUG ANY PROCEDURE TO PELL_TEST;
BEGIN
    DBMS_NETWORK_ACL_ADMIN.APPEND_HOST_ACE(
        host => '*',
        ace  => xs$ace_type(
                    privilege_list => xs$name_list('JDWP'),
                    principal_name => 'PELL_TEST',
                    principal_type => xs_acl.ptype_db));
END;
/

-- Performance-introspection examples (stat_diff, table-stats reports,
-- execution-plan visualizers) read the V$ dynamic views.
GRANT SELECT ON SYS.V_$MYSTAT   TO PELL_TEST;
GRANT SELECT ON SYS.V_$STATNAME TO PELL_TEST;
GRANT SELECT ON SYS.V_$SESSTAT  TO PELL_TEST;
GRANT SELECT ON SYS.V_$SESSION  TO PELL_TEST;
GRANT SELECT ON SYS.V_$SQL      TO PELL_TEST;
GRANT SELECT ON SYS.V_$SQL_PLAN TO PELL_TEST;

-- Verify.
PROMPT
PROMPT Schemas created/confirmed. PELL_TEST can now deploy schema-qualified
PROMPT examples (auditing.charges, signups.validate, hr.employees, ...).