"""Collection debug-shadow query engine (pell/debug_query.py)."""
import pytest
from pell.debug_query import eval_query, QueryError

SHADOW = ('{"count":3,"rows":['
          '{"user_id":1,"movie_id":10,"rating":5,"ts":1001},'
          '{"user_id":2,"movie_id":20,"rating":5,"ts":1002},'
          '{"user_id":3,"movie_id":30,"rating":5,"ts":1003}]}')


@pytest.mark.parametrize("expr,want", [
    ("rows[1].user_id", 1),                                  # 1-based index
    ("rows.at(2).movie_id", 20),
    ("rows.len()", 3),
    ("rows.count()", 3),
    ("rows.where(user_id == 1).movie_id", [10]),             # filter -> projection (list)
    ("rows.where(user_id == 1).first().movie_id", 10),       # first -> scalar
    ("rows.where(rating >= 5).user_id", [1, 2, 3]),
    ("rows.where(user_id > 1 and rating == 5).movie_id", [20, 30]),   # compound
    ("rows.where(user_id == 99).first()", None),             # no match
    ("rows[100].user_id", None),                             # out of range -> null
    ("rows.movie_id", [10, 20, 30]),                         # project whole list
    ("rows.where(user_id == 2).count()", 1),
])
def test_query(expr, want):
    assert eval_query(SHADOW, expr) == want


def test_scalar_list_shadow():
    # list<scalar> serializes to a bare array
    assert eval_query('[10, 20, 30]', "rows[2]") == 20
    assert eval_query('{"count":3,"rows":[10,20,30]}', "rows.len()") == 3


def test_bad_expr_raises():
    with pytest.raises(QueryError):
        eval_query(SHADOW, "rows.where(user_id)")        # predicate needs OP value
    with pytest.raises(QueryError):
        eval_query("not json", "rows[1]")
