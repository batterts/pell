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

-- 04_inventory needs its table (seq-backed id, per house style —
-- no IDENTITY). Created here in DBA context so the DEFAULT works.
DECLARE
    PROCEDURE try(p_sql VARCHAR2) IS
    BEGIN
        EXECUTE IMMEDIATE p_sql;
    EXCEPTION WHEN OTHERS THEN
        IF SQLCODE != -955 THEN RAISE; END IF;
    END;
BEGIN
    try('CREATE SEQUENCE inventory.skus_seq');
    try('CREATE TABLE inventory.skus (
           id NUMBER DEFAULT inventory.skus_seq.NEXTVAL PRIMARY KEY,
           code VARCHAR2(40), qty NUMBER, expires_at DATE)');
END;
/

-- Demo tables the schema-qualified examples query. Owner context so
-- defaults work; idempotent via try(). Sequence-backed ids per house
-- style (no IDENTITY).
DECLARE
    PROCEDURE try(p_sql VARCHAR2) IS
    BEGIN
        EXECUTE IMMEDIATE p_sql;
    EXCEPTION WHEN OTHERS THEN
        IF SQLCODE != -955 THEN RAISE; END IF;
    END;
BEGIN
    -- Modules renamed (hr::staffing, inventory::stock) — retire the old
    -- package names so the same-named TABLES can exist (one namespace).
    BEGIN EXECUTE IMMEDIATE 'DROP PACKAGE hr.employees';
    EXCEPTION WHEN OTHERS THEN NULL; END;
    BEGIN EXECUTE IMMEDIATE 'DROP PACKAGE inventory.skus';
    EXCEPTION WHEN OTHERS THEN NULL; END;
    try('CREATE TABLE hr.employees (
           id NUMBER PRIMARY KEY, name VARCHAR2(100),
           email VARCHAR2(100), grade NUMBER)');
    try('CREATE TABLE billing.accounts (
           id NUMBER PRIMARY KEY, balance NUMBER, frozen NUMBER DEFAULT 0)');
    try('CREATE TABLE reports.orders (
           order_id NUMBER, total NUMBER, created_at DATE)');
    try('CREATE TABLE lookups.countries (
           code VARCHAR2(8), name VARCHAR2(80))');
    try('CREATE TABLE lookups.audit_tbl (event VARCHAR2(200), ts DATE)');
    try('CREATE TABLE orders.orders (
           id NUMBER PRIMARY KEY, customer_id NUMBER,
           status VARCHAR2(20), total NUMBER, created_at DATE)');
    try('CREATE SEQUENCE orders.order_lines_seq');
    try('CREATE TABLE orders.order_lines (
           id NUMBER DEFAULT orders.order_lines_seq.NEXTVAL,
           order_id NUMBER, sku_id NUMBER, qty NUMBER)');
    try('CREATE TABLE orders.skus (id NUMBER PRIMARY KEY, qty NUMBER)');
    try('CREATE SEQUENCE orders.audit_log_seq');
    try('CREATE TABLE orders.audit_log (
           id NUMBER DEFAULT orders.audit_log_seq.NEXTVAL,
           event VARCHAR2(200), order_id NUMBER, occurred_at DATE)');
    try('CREATE SEQUENCE auditing.ledger_seq');
    try('CREATE TABLE auditing.ledger (
           entry_id NUMBER DEFAULT auditing.ledger_seq.NEXTVAL,
           account_id NUMBER, amount NUMBER, ts DATE)');
    try('CREATE TABLE bulk.num_table (n NUMBER)');
    try('CREATE TABLE market.stocktable (sym VARCHAR2(10), price NUMBER)');
    -- pell_runtime / logger / pell_re live in PELL_TEST; packages in
    -- the example schemas reference them unqualified — public synonyms
    -- make that resolve (EXECUTE is granted by pell deploy itself).
    try('CREATE PUBLIC SYNONYM pell_runtime FOR pell_test.pell_runtime');
    try('CREATE PUBLIC SYNONYM logger FOR pell_test.logger');
    try('CREATE PUBLIC SYNONYM pell_re FOR pell_test.pell_re');
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