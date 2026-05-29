from uuid import uuid4
from src.engine.models.project import Project


def test_to_dict_includes_department_id():
    dep = uuid4()
    p = Project(name="P", department_id=dep)
    d = p.to_dict()
    assert d["department_id"] == str(dep)


def test_to_dict_department_id_none():
    p = Project(name="P")  # default department_id None
    assert p.to_dict()["department_id"] is None
