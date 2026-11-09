"""Every Dockerfile under deploy/ is linted by the deploy lint workflow (JAIF-572)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_every_dockerfile_under_deploy_is_in_the_hadolint_matrix():
    workflow = (ROOT / ".github/workflows/lint-deploy.yml").read_text(encoding="utf-8")
    dockerfiles = sorted(str(path.relative_to(ROOT)) for path in (ROOT / "deploy").rglob("*ockerfile*"))
    assert dockerfiles, "no Dockerfile found under deploy/"
    missing = [path for path in dockerfiles if f"- {path}" not in workflow]
    assert missing == [], f"add these Dockerfiles to the matrix of .github/workflows/lint-deploy.yml: {missing}"
