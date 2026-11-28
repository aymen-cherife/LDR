"""The record parity fixture must exercise real attrs code as well as toolz."""
import importlib.util
from pathlib import Path


def test_attrs_record_fixture_uses_attrs_and_has_both_edits():
    path=Path(__file__).parents[1]/'evaluation/same_records.py'
    import sys
    sys.path.insert(0,str(path.parent))
    spec=importlib.util.spec_from_file_location('same_records',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    for version in ('first','second'):
        text=module.source(version,pytest=True,library='attrs')
        assert 'from attrs import' in text
        assert 'from toolz import' not in text
        import ast
        tree=ast.parse(text)
        operation=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='test_c1_attrs_operations')
        assert {'define','evolve'} <= {node.id for node in ast.walk(operation) if isinstance(node,ast.Name)}
        # Test the edit protocol without requiring the downloaded fixture's deps.
        tree.body=[node for node in tree.body if not isinstance(node,ast.ImportFrom) and node is not operation]
        scope={};exec(compile(tree,'fixture','exec'),scope)
        for name,function in scope.items():
            if name.startswith('test_'): function()


def test_record_gate_disables_the_existing_timeout(tmp_path,monkeypatch):
    import sys
    sys.path.insert(0,str(Path(__file__).parents[1]/'evaluation'))
    import same_records
    (tmp_path/'tests').mkdir()
    calls=[]
    monkeypatch.setattr(same_records.overhead,'environment',lambda *args:{})
    monkeypatch.setattr(same_records.overhead,'clean',lambda *args:None)
    monkeypatch.setattr(same_records.overhead,'timed',lambda command,*args:calls.append(command))
    monkeypatch.setattr(same_records,'dump',lambda *args,**kw:{})
    traced=[sys.executable,'-m','ldr','pytest','tests/upstream.py','--','-p','no:cacheprovider']
    same_records.run_and_dump('attrs',(tmp_path,None,traced),tmp_path)
    assert len(calls)==2
    assert all('--timeout=0' in command[:command.index('--')] for command in calls)
