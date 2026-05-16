-- Raw PL/SQL chunked-FORALL benchmarks for comparison with pell's bulk
-- methods. Same shape as pell would produce if it had a chunked-FORALL
-- surface (it currently doesn't — B2 collects all 100k at once; R* show
-- the standard production chunk-size sweep).
--
-- Functions (not procedures) so the driver can call them uniformly.

CREATE OR REPLACE FUNCTION bench_r1 RETURN NUMBER IS
  -- chunk LIMIT 100
  CURSOR c IS SELECT user_id, movie_id, rating, ts FROM ml_ratings_src;
  TYPE t_buf IS TABLE OF ml_ratings_src%ROWTYPE INDEX BY PLS_INTEGER;
  l_buf t_buf;
  t0 NUMBER; t1 NUMBER;
BEGIN
  t0 := DBMS_UTILITY.GET_TIME;
  OPEN c;
  LOOP
    FETCH c BULK COLLECT INTO l_buf LIMIT 100;
    EXIT WHEN l_buf.COUNT = 0;
    FORALL i IN 1 .. l_buf.COUNT
      INSERT INTO ml_ratings_r1(user_id, movie_id, rating, ts)
      VALUES (l_buf(i).user_id, l_buf(i).movie_id, l_buf(i).rating, l_buf(i).ts);
  END LOOP;
  CLOSE c;
  t1 := DBMS_UTILITY.GET_TIME;
  RETURN (t1 - t0) * 10;
END;
/

CREATE OR REPLACE FUNCTION bench_r2 RETURN NUMBER IS
  -- chunk LIMIT 1000
  CURSOR c IS SELECT user_id, movie_id, rating, ts FROM ml_ratings_src;
  TYPE t_buf IS TABLE OF ml_ratings_src%ROWTYPE INDEX BY PLS_INTEGER;
  l_buf t_buf;
  t0 NUMBER; t1 NUMBER;
BEGIN
  t0 := DBMS_UTILITY.GET_TIME;
  OPEN c;
  LOOP
    FETCH c BULK COLLECT INTO l_buf LIMIT 1000;
    EXIT WHEN l_buf.COUNT = 0;
    FORALL i IN 1 .. l_buf.COUNT
      INSERT INTO ml_ratings_r2(user_id, movie_id, rating, ts)
      VALUES (l_buf(i).user_id, l_buf(i).movie_id, l_buf(i).rating, l_buf(i).ts);
  END LOOP;
  CLOSE c;
  t1 := DBMS_UTILITY.GET_TIME;
  RETURN (t1 - t0) * 10;
END;
/

CREATE OR REPLACE FUNCTION bench_r3 RETURN NUMBER IS
  -- chunk LIMIT 10000
  CURSOR c IS SELECT user_id, movie_id, rating, ts FROM ml_ratings_src;
  TYPE t_buf IS TABLE OF ml_ratings_src%ROWTYPE INDEX BY PLS_INTEGER;
  l_buf t_buf;
  t0 NUMBER; t1 NUMBER;
BEGIN
  t0 := DBMS_UTILITY.GET_TIME;
  OPEN c;
  LOOP
    FETCH c BULK COLLECT INTO l_buf LIMIT 10000;
    EXIT WHEN l_buf.COUNT = 0;
    FORALL i IN 1 .. l_buf.COUNT
      INSERT INTO ml_ratings_r3(user_id, movie_id, rating, ts)
      VALUES (l_buf(i).user_id, l_buf(i).movie_id, l_buf(i).rating, l_buf(i).ts);
  END LOOP;
  CLOSE c;
  t1 := DBMS_UTILITY.GET_TIME;
  RETURN (t1 - t0) * 10;
END;
/
