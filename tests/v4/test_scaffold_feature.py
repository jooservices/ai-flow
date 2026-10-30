"""The feature scaffold writes the folder of docs/v4/event-architecture.md and never overwrites (JAIF-456)."""
import pytest

from aiflow.v4.core import scaffold_feature
from tests.v4.test_feature_structure import feature_violations


def test_the_scaffold_creates_the_feature_files_registration_and_test(tmp_path):
    created = scaffold_feature.scaffold(tmp_path, "widgets")
    names = {str(path.relative_to(tmp_path)) for path in created}

    assert {"aiflow/v4/control/features/widgets/" + part for part in (
        "__init__.py", "routes.py", "service.py", "repository.py", "models.py", "events.py", "listeners.py")} <= names
    assert "aiflow/v4/control/routes/widgets.py" in names
    assert "tests/v4/features/test_widgets.py" in names
    assert feature_violations(tmp_path / "aiflow/v4/control/features") == []


def test_the_scaffold_refuses_a_bad_name_and_an_existing_feature(tmp_path):
    with pytest.raises(ValueError):
        scaffold_feature.scaffold(tmp_path, "Bad-Name")
    scaffold_feature.scaffold(tmp_path, "widgets")
    with pytest.raises(FileExistsError):
        scaffold_feature.scaffold(tmp_path, "widgets")


def test_the_command_prints_the_created_files_and_fails_on_a_bad_name(tmp_path, capsys):
    assert scaffold_feature.main(["gadgets", "--root", str(tmp_path)]) == 0
    assert "features/gadgets/service.py" in capsys.readouterr().out
    assert scaffold_feature.main(["1x", "--root", str(tmp_path)]) == 2
