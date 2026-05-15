-- pell_runtime — the single thin runtime package referenced by emitted code.
--
-- v0 contents:
--   1. The pell_err application context.
--   2. set_err / clear_err helpers that wrap DBMS_SESSION.SET_CONTEXT.
--   3. EXCEPTION declarations per error variant — these are appended to
--      pell_runtime by the compiler. Collate the `-- Additions to pell_runtime`
--      comment blocks from every emitted module's .sql file and merge them
--      into the package below.
--
-- Real (multi-module) builds will regenerate this file from a single
-- compiler pass over the whole project. For v0 you maintain it by hand.

CREATE OR REPLACE CONTEXT pell_err USING pell_runtime ACCESSED LOCALLY;

CREATE OR REPLACE PACKAGE pell_runtime AS
  -- Helpers
  PROCEDURE set_err(p_key IN VARCHAR2, p_payload IN VARCHAR2);
  PROCEDURE clear_err(p_key IN VARCHAR2);
  FUNCTION  get_err(p_key IN VARCHAR2) RETURN VARCHAR2;

  -- Compiler-injected EXCEPTION decls go here, e.g.:
  --   hr_employees_notfound EXCEPTION;
  --   PRAGMA EXCEPTION_INIT(hr_employees_notfound, -20100);
  --   hr_employees_policyviolation EXCEPTION;
  --   PRAGMA EXCEPTION_INIT(hr_employees_policyviolation, -20101);
END pell_runtime;
/

CREATE OR REPLACE PACKAGE BODY pell_runtime AS
  PROCEDURE set_err(p_key IN VARCHAR2, p_payload IN VARCHAR2) IS
  BEGIN
    DBMS_SESSION.SET_CONTEXT('pell_err', p_key, p_payload);
  END set_err;

  PROCEDURE clear_err(p_key IN VARCHAR2) IS
  BEGIN
    DBMS_SESSION.CLEAR_CONTEXT('pell_err', p_key);
  END clear_err;

  FUNCTION get_err(p_key IN VARCHAR2) RETURN VARCHAR2 IS
  BEGIN
    RETURN SYS_CONTEXT('pell_err', p_key);
  END get_err;
END pell_runtime;
/
