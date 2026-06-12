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
