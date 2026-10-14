import json

from gateway.distill.validators import detect, validate


def test_label_set() -> None:
    spec = {"type": "label_set", "labels": ["card_arrival", "lost_or_stolen_card"]}
    assert validate(spec, "card_arrival")
    assert validate(spec, " `card_arrival`. ")  # formatting noise is tolerated
    assert not validate(spec, "card arrival")
    assert not validate(spec, None)


def test_json_keys() -> None:
    spec = {"type": "json_keys", "keys": ["location", "person"]}
    assert validate(spec, '{"person": ["Ada"], "location": []}')
    assert not validate(spec, '{"person": ["Ada"]}')  # missing key
    assert not validate(spec, '{"person": "Ada", "location": []}')  # not a list
    assert not validate(spec, "not json")


def test_sql_select() -> None:
    spec = {"type": "sql_select"}
    assert validate(spec, 'SELECT "Name" FROM t WHERE "Age" > 3;')
    assert not validate(spec, "DROP TABLE t")
    assert not validate(spec, "SELECT 1; DROP TABLE t")


def test_detect_closed_label_set() -> None:
    outputs = ["a_label", "b_label", "a_label", "c_label", "b_label", "c_label"] * 5
    spec = detect(outputs)
    assert spec == {"type": "label_set", "labels": ["a_label", "b_label", "c_label"]}


def test_detect_json_and_sql_and_free_text() -> None:
    js = [json.dumps({"person": [f"p{i}"], "location": []}) for i in range(40)]
    assert detect(js) == {"type": "json_keys", "keys": ["location", "person"]}
    sql = [f'SELECT "c" FROM t WHERE "x" = {i}' for i in range(40)]
    assert detect(sql) == {"type": "sql_select"}
    prose = [f"Here is a long answer number {i}.\nWith several lines of text." for i in range(40)]
    assert detect(prose) is None
