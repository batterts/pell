-- Benchmark driver. Run with:
--   cat driver.sql | sqltun
-- Truncates each destination, calls the method 3× (taking the mean of the
-- last two — first run primes the cache), inserts results into bench_results.

SET SERVEROUTPUT ON
SET LINESIZE 140
SET PAGESIZE 100
SET TIMING OFF

PROMPT === Truncating bench_results from previous runs ===
TRUNCATE TABLE bench_results;

DECLARE
  PROCEDURE measure(p_method VARCHAR2, p_dest VARCHAR2, p_call VARCHAR2) IS
    ms NUMBER;
    n  NUMBER;
  BEGIN
    -- Three runs; we record all three so we can see variance.
    FOR i IN 1 .. 3 LOOP
      EXECUTE IMMEDIATE 'TRUNCATE TABLE ' || p_dest;
      EXECUTE IMMEDIATE 'BEGIN :out := ' || p_call || '; END;' USING OUT ms;
      EXECUTE IMMEDIATE 'SELECT COUNT(*) FROM ' || p_dest INTO n;
      INSERT INTO bench_results(method, rows_n, elapsed_ms)
        VALUES (p_method || '#' || i, n, ms);
      COMMIT;
      DBMS_OUTPUT.PUT_LINE(RPAD(p_method || '#' || i, 12)
                       || ' rows=' || LPAD(n, 7)
                       || '  elapsed_ms=' || LPAD(ms, 8));
    END LOOP;
  END;
BEGIN
  DBMS_OUTPUT.PUT_LINE('===  Method            rows         ms');
  DBMS_OUTPUT.PUT_LINE('===  -----------       ----     -------');
  measure('B1_row_by_row',     'ml_ratings_b1', 'bench_b1.run');
  measure('B2_collect_forall', 'ml_ratings_b2', 'bench_b2.run');
  measure('B3_insert_select',  'ml_ratings_b3', 'bench_b3.run');
  measure('B4_pipelined_pt',   'ml_ratings_b4', 'bench_b4.run');
  measure('B5_pipelined_xfm',  'ml_ratings_b5', 'bench_b5.run');
  measure('R1_forall_lim100',  'ml_ratings_r1', 'bench_r1');
  measure('R2_forall_lim1000', 'ml_ratings_r2', 'bench_r2');
  measure('R3_forall_lim10000','ml_ratings_r3', 'bench_r3');
END;
/

PROMPT
PROMPT === Final results (avg of runs #2 and #3, first run dropped as warm-up) ===
SELECT
  REGEXP_REPLACE(method, '#\d+$', '')  AS method,
  ROUND(AVG(CASE WHEN method LIKE '%#2' OR method LIKE '%#3' THEN elapsed_ms END), 1) AS avg_ms,
  MIN(elapsed_ms) AS min_ms,
  MAX(elapsed_ms) AS max_ms,
  MAX(rows_n)     AS rows_n
FROM   bench_results
GROUP  BY REGEXP_REPLACE(method, '#\d+$', '')
ORDER  BY avg_ms;

EXIT
