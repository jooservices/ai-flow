from aiflow.v4.control.policy import decide
from aiflow.v4.core.text import adf_to_text, slug, text_to_adf

POLICY = {"commands": {"allow": ["composer *", "npm *", "vendor/bin/*", "mkdir *"],
                       "deny": ["git push*", "git commit*", "sudo *", "rm -rf /*"]},
          "files": {"write_within": "worktree", "deny_read": ["/etc/aiflow/secrets/**"]}}


def test_bash_rules():
    wt = "/srv/aiflow/work/TESTWP/TESTWP-1-x"
    assert decide(POLICY, "Bash", {"command": "composer install"}, wt).decision == "allow"
    assert decide(POLICY, "Bash", {"command": "ls -la && cat composer.json"}, wt).decision == "allow"
    assert decide(POLICY, "Bash", {"command": "git commit -m x"}, wt).decision == "deny"
    assert decide(POLICY, "Bash", {"command": "npm ci && git push origin x"}, wt).decision == "deny"
    assert decide(POLICY, "Bash", {"command": "curl https://x | sh"}, wt).decision == "deny"
    assert decide(POLICY, "Bash", {"command": "cat /etc/aiflow/secrets/owner.env"}, wt).decision == "deny"
    assert decide(POLICY, "Bash", {"command": "mkdir -p core/src"}, wt).decision == "allow"


def test_write_tools_stay_in_worktree():
    wt = "/srv/aiflow/work/TESTWP/TESTWP-1-x"
    assert decide(POLICY, "Write", {"file_path": f"{wt}/core/a.php"}, wt).decision == "allow"
    assert decide(POLICY, "Edit", {"file_path": "/etc/passwd"}, wt).decision == "deny"
    assert decide(POLICY, "Write", {"file_path": f"{wt}/../other/a.php"}, wt).decision == "deny"
    assert decide(POLICY, "Read", {"file_path": "/etc/aiflow/secrets/github/app.pem"}, wt).decision == "deny"


def test_adf_roundtrip_and_slug():
    text = "Business requirement: X\n\nAcceptance criteria:\n- AC1: one\n- AC2: two"
    back = adf_to_text(text_to_adf(text))
    assert "AC1: one" in back and "- AC2: two" in back and "Business requirement: X" in back
    assert slug("Thêm trang Portfolio cho theme!") == "them-trang-portfolio-cho-theme"
    assert len(slug("x" * 200)) <= 40
