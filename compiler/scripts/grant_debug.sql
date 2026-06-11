-- ===========================================================================
-- grant_debug.sql — enable the pell debugger for ONE user.
--
-- Run as a DBA:
--     sqlplus system/<pwd>@host:1521/<pdb> @grant_debug.sql <USERNAME>
--
-- This is the same privilege bar SQL Developer's debugger requires —
-- Oracle treats session debugging as a security boundary, so there is
-- no grant-free path (both DBMS_DEBUG_JDWP and the deprecated
-- DBMS_DEBUG check DEBUG CONNECT SESSION). Users without these grants
-- can still use `pell exec --trace` / `pell deploy --trace` — a
-- zero-privilege line-by-line execution trace over DBMS_OUTPUT.
-- ===========================================================================

DEFINE who = &1

-- Required: lets the user's session be debugged at all.
GRANT DEBUG CONNECT SESSION TO &who;

-- Optional but recommended: step into units owned by OTHER schemas
-- (cross-schema examples, shared runtime packages). Omit on hardened
-- instances; debugging then covers only the user's own objects.
GRANT DEBUG ANY PROCEDURE TO &who;

-- Required: lets the user's session open the outbound JDWP connection
-- to the IDE (without it CONNECT_TCP raises ORA-24247). Tighten
-- host => '*' to the developer workstation's address/subnet if policy
-- requires.
BEGIN
    DBMS_NETWORK_ACL_ADMIN.APPEND_HOST_ACE(
        host => '*',
        ace  => xs$ace_type(
                    privilege_list => xs$name_list('JDWP'),
                    principal_name => UPPER('&who'),
                    principal_type => xs_acl.ptype_db));
END;
/

PROMPT Debug grants applied for &who
