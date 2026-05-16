-- Run the three pell analytical reports against ml_ratings_src.

SET SERVEROUTPUT ON
SET LINESIZE 140
SET PAGESIZE 60

PROMPT
PROMPT === Rating distribution ===
DECLARE
  l_dist reports_rating_dist.t_bucket_list;
BEGIN
  l_dist := reports_rating_dist.dist();
  DBMS_OUTPUT.PUT_LINE(RPAD('rating', 8) || RPAD('n', 10) || RPAD('pct', 8));
  DBMS_OUTPUT.PUT_LINE(RPAD('------', 8) || RPAD('--------', 10) || RPAD('------', 8));
  FOR i IN 1 .. l_dist.COUNT LOOP
    DBMS_OUTPUT.PUT_LINE(
      RPAD(l_dist(i).rating, 8)
      || RPAD(l_dist(i).n, 10)
      || RPAD(l_dist(i).pct || '%', 8)
    );
  END LOOP;
END;
/

PROMPT
PROMPT === Top 10 movies (min 50 ratings) ===
DECLARE
  l_top reports_top_movies.t_topmovie_list;
BEGIN
  l_top := reports_top_movies.top_n(50);
  DBMS_OUTPUT.PUT_LINE(RPAD('id', 6) || RPAD('n', 6) || RPAD('avg', 7) || 'title');
  DBMS_OUTPUT.PUT_LINE(RPAD('---', 6) || RPAD('---', 6) || RPAD('---', 7) || '-----');
  FOR i IN 1 .. l_top.COUNT LOOP
    DBMS_OUTPUT.PUT_LINE(
      RPAD(l_top(i).movie_id, 6)
      || RPAD(l_top(i).rating_count, 6)
      || RPAD(l_top(i).avg_rating, 7)
      || l_top(i).title
    );
  END LOOP;
END;
/

PROMPT
PROMPT === Top 10 most active users ===
DECLARE
  l_users reports_user_activity.t_userstat_list;
BEGIN
  l_users := reports_user_activity.top_raters(10);
  DBMS_OUTPUT.PUT_LINE(RPAD('user_id', 10) || RPAD('rated', 10) || 'avg');
  DBMS_OUTPUT.PUT_LINE(RPAD('-------', 10) || RPAD('-----', 10) || '---');
  FOR i IN 1 .. l_users.COUNT LOOP
    DBMS_OUTPUT.PUT_LINE(
      RPAD(l_users(i).user_id, 10)
      || RPAD(l_users(i).rating_count, 10)
      || l_users(i).avg_rating
    );
  END LOOP;
END;
/

EXIT
