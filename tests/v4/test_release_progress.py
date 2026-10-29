"""Release progress per Jira version for the Command Center and Business screens (JAIF-184)."""
import json
import uuid

from conftest import add_ticket
from test_business import DOM, STATE, business
from test_console import run_script
from test_release_readiness import issue, sync, verify, VERSION
from aiflow.v4.control import release_progress
from aiflow.v4.control.jira import Jira

TRACKER = "TESTWP-99"
RUN = str(uuid.UUID(int=7))


def history(*items: tuple[str, dict]) -> dict:
    return {"histories": [{"created": at, "items": [i]} for at, i in items]}


def added(version_id: str, at: str) -> tuple[str, dict]:
    return at, {"field": "Fix Version", "from": None, "to": version_id, "toString": "1.0"}


def started(at: str) -> tuple[str, dict]:
    return at, {"field": "status", "fromString": "To Do", "toString": "In Progress"}


def with_history(i: dict, *items: tuple[str, dict]) -> dict:
    return {**i, "changelog": history(*items)}


def progress(ctx, version_id: str = "100") -> dict:
    [v] = [v for v in release_progress.project_versions(ctx.db, "TESTWP") if v["version_id"] == version_id]
    return v


def audit(db, action: str, result: str = "", payload=None, ticket: str = TRACKER) -> None:
    db.run("INSERT INTO write_audit (target,action,ticket,run_id,fence,actor,accepted,result,payload)"
           " VALUES ('github',%s,%s,%s,1,'control',true,%s,%s)", (action, ticket, RUN, result, json.dumps(payload or {})))


def release_row(db, status: str, **values) -> None:
    db.run("INSERT INTO version_releases (project, version_id, version_name, tracker_key, status, tag,"
           " github_release_url, gate_detail) VALUES ('TESTWP','100','1.0',%s,%s,%s,%s,%s)",
           (TRACKER, status, values.get("tag", ""), values.get("url", ""), values.get("detail", "")))


# --- AC1: versions from Jira Fix versions, Done / total and open tickets ------------------------------------------
def test_versions_show_done_over_total_and_the_open_tickets(ctx):
    sync(ctx, [issue("TESTWP-1"), issue("TESTWP-2"), issue("TESTWP-3", "In Progress"), issue("TESTWP-4", "To Do")],
         full=True)
    v = progress(ctx)
    assert (v["version"], v["released"], v["ticket_count"], v["done_count"]) == ("1.0", False, 4, 2)
    assert [(t["key"], t["status"]) for t in v["open_tickets"]] == [("TESTWP-3", "In Progress"), ("TESTWP-4", "To Do")]


def test_released_versions_are_listed_too_and_state_and_business_api_carry_them(client, ctx):
    ctx.jira.versions_data["TESTWP"] = [VERSION, {"id": "200", "name": "0.9", "released": True,
                                                  "release_date": "2026-10-01"}]
    ctx.jira.search_data = [issue("TESTWP-1"), issue("TESTWP-2", versions=("200",))]
    from aiflow.v4.control.loops import jira_sync
    jira_sync(ctx, full=True)
    state = {v["version_id"]: v for v in client.get("/v1/state").json()["release_progress"]}
    assert set(state) == {"100", "200"} and state["200"]["released"] is True and state["200"]["reasons"] == []
    biz = client.get("/v1/business/TESTWP/release-progress")
    assert biz.status_code == 200 and {v["version_id"]: v for v in biz.json()["versions"]} == state
    assert client.get("/v1/business/UNKNOWN/release-progress").status_code == 404


# --- AC2: added late, with the date from the Jira change history ---------------------------------------------------
def test_ticket_attached_after_the_first_in_progress_is_marked_late_with_its_date(ctx):
    sync(ctx, [with_history(issue("TESTWP-1", "In Progress"), added("100", "2026-10-01T09:00:00.000+0000"),
                            started("2026-10-02T09:00:00.000+0000")),
               with_history(issue("TESTWP-2", "To Do"), added("100", "2026-10-05T10:30:00.000+0000")),
               with_history(issue("TESTWP-3", "To Do"), added("100", "2026-10-01T10:00:00.000+0000"))], full=True)
    v = progress(ctx)
    assert v["started_at"] == "2026-10-02T09:00:00+00:00"
    assert [(t["key"], t["added_late"], t["added_at"]) for t in v["tickets"]] == [
        ("TESTWP-1", False, "2026-10-01T09:00:00+00:00"),
        ("TESTWP-2", True, "2026-10-05T10:30:00+00:00"),
        ("TESTWP-3", False, "2026-10-01T10:00:00+00:00")]
    assert [t["key"] for t in v["late_tickets"]] == ["TESTWP-2"]
    assert v["ticket_count"] == 3  # the late ticket counts in the total like any other


def test_nothing_is_late_before_any_ticket_is_in_progress_or_without_history(ctx):
    sync(ctx, [with_history(issue("TESTWP-1", "To Do"), added("100", "2026-10-01T09:00:00.000+0000")),
               with_history(issue("TESTWP-2", "To Do"), added("100", "2026-10-09T09:00:00.000+0000")),
               issue("TESTWP-3", "To Do")], full=True)
    assert progress(ctx)["late_tickets"] == []
    sync(ctx, [with_history(issue("TESTWP-1", "In Progress"), started("2026-10-03T09:00:00.000+0000"))])
    assert [t["key"] for t in progress(ctx)["late_tickets"]] == ["TESTWP-2"]  # no history for TESTWP-3: not late


def test_history_parser_keeps_the_latest_attach_and_the_first_in_progress():
    got = Jira.history({"changelog": history(
        added("100", "2026-10-01T09:00:00.000+0000"),
        ("2026-10-02T09:00:00.000+0000", {"field": "Fix Version", "from": "100", "to": None}),
        added("100", "2026-10-03T09:00:00.000+0000"),
        started("2026-10-04T09:00:00.000+0000"), started("2026-10-06T09:00:00.000+0000"))})
    assert {k: v.isoformat() for k, v in got["fix_version_added"].items()} == {"100": "2026-10-03T09:00:00+00:00"}
    assert got["first_in_progress"].isoformat() == "2026-10-04T09:00:00+00:00"
    assert Jira.history({}) == {"fix_version_added": {}, "first_in_progress": None}


# --- AC3: reasons --------------------------------------------------------------------------------------------------
def test_reasons_cover_open_rework_bug_empty_and_a_running_or_failed_check(ctx):
    sync(ctx, [issue("TESTWP-1", "To Do"), issue("TESTWP-2", labels=["rework"]),
               issue("TESTWP-3", blocked_by=["TESTWP-9"]),
               issue("TESTWP-9", "To Do", versions=(), type_="Bug", labels=["release-qa"])], full=True)
    verify(ctx.db, "TESTWP-3")
    release_row(ctx.db, "checking")
    causes = [(r["cause"], r["ticket"]) for r in progress(ctx)["reasons"]]
    assert causes == [("not_done", "TESTWP-1"), ("rework", "TESTWP-2"), ("blocked_by_bug", "TESTWP-3"),
                      ("release_running", None), ("scale_proof_missing", None)]
    ctx.db.run("UPDATE version_releases SET status='failed', gate_detail='pytest failed'")
    failed = next(reason for reason in progress(ctx)["reasons"] if reason["cause"] == "release_failed")
    assert failed["detail"] == "pytest failed"
    sync(ctx, [], full=True)
    ctx.db.run("DELETE FROM ticket_versions")
    assert progress(ctx)["reasons"][0]["cause"] == "empty"


# --- AC4 / AC6: release steps with run ID and node, tag and GitHub Release -----------------------------------------
def release_flow(ctx, through: str = "release") -> None:
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome)"
               " VALUES (%s,%s,'TESTWP','version_release','codex-01','codex','node7',1,'running','')", (RUN, TRACKER))
    flow = [("github-release-files", "abc123", {"files": ["CHANGELOG.md"]}),
            ("github-merge", "f" * 40, {"pr_number": 12, "pr_url": "https://github.com/o/r/pull/12"}),
            ("github-tag", "created", {"tag": "v1.0"}),
            ("github-release", "https://github.com/o/r/releases/tag/v1.0", {"tag": "v1.0"})]
    for action, result, payload in flow:
        audit(ctx.db, action, result, payload)
        if action == {"changelog": "github-release-files", "merge": "github-merge", "tag": "github-tag",
                      "release": "github-release"}[through]:
            break


def test_command_center_steps_carry_run_id_and_node(ctx):
    sync(ctx, [issue("TESTWP-1")], full=True)
    verify(ctx.db, "TESTWP-1")
    release_row(ctx.db, "checking")
    release_flow(ctx, through="merge")
    steps = {s["step"]: s for s in progress(ctx)["release"]["steps"]}
    assert list(steps) == ["check", "changelog", "pr", "merge", "tag", "release"]
    assert [steps[n]["state"] for n in steps] == ["done", "done", "done", "done", "running", "pending"]
    assert (steps["merge"]["run_id"], steps["merge"]["node"]) == (RUN, "node7")
    assert steps["pr"]["detail"] == "#12" and steps["pr"]["url"].endswith("/pull/12")
    assert steps["changelog"]["detail"] == "CHANGELOG.md"


def test_failed_check_marks_the_check_step_failed(ctx):
    sync(ctx, [issue("TESTWP-1")], full=True)
    release_row(ctx.db, "failed", detail="pytest failed")
    ctx.events.emit("version_release.check", ticket=TRACKER, project="TESTWP", step="version_release",
                    run_id=RUN, outcome="failed", reason="pytest failed")
    steps = progress(ctx)["release"]["steps"]
    assert (steps[0]["step"], steps[0]["state"], steps[0]["detail"]) == ("check", "failed", "pytest failed")


def test_released_version_shows_tag_and_github_release_link(ctx):
    ctx.jira.versions_data["TESTWP"] = [{**VERSION, "released": True, "release_date": "2026-10-20"}]
    sync(ctx, [issue("TESTWP-1")], full=True)
    ctx.db.run("UPDATE versions SET released=true")
    release_row(ctx.db, "released", tag="v1.0", url="https://github.com/o/r/releases/tag/v1.0")
    release_flow(ctx)
    v = progress(ctx)
    assert (v["released"], v["ready"], v["reasons"]) == (True, True, [])
    assert (v["release"]["status"], v["release"]["tag"]) == ("released", "v1.0")
    assert v["release"]["github_release_url"] == "https://github.com/o/r/releases/tag/v1.0"
    assert all(s["state"] == "done" for s in v["release"]["steps"])


# --- Screens ---------------------------------------------------------------------------------------------------------
STEPS = [{"step": s, "state": "done", "run_id": RUN, "node": "node7", "detail": "", "at": None, "url": ""}
         for s in ("check", "changelog", "pr", "merge", "tag", "release")]
PROGRESS = [
    {"project": "TESTFLOW", "version_id": "100", "version": "1.0", "released": False, "ready": False,
     "ticket_count": 4, "done_count": 2, "release_date": None,
     "late_tickets": [{"key": "TESTFLOW-13", "added_late": True, "added_at": "2026-10-05T10:30:00+00:00"}],
     "open_tickets": [{"key": "TESTFLOW-12", "status": "To Do"}, {"key": "TESTFLOW-13", "status": "In Progress"}],
     "reasons": [{"cause": "not_done", "ticket": "TESTFLOW-12", "summary": "", "status": "To Do"},
                 {"cause": "release_failed", "ticket": None, "summary": "", "detail": "pytest failed"}],
     "release": {"status": "failed", "tracker": "TESTFLOW-99", "tag": "", "github_release_url": "", "steps": STEPS}},
    {"project": "TESTFLOW", "version_id": "90", "version": "0.9", "released": True, "ready": True,
     "ticket_count": 3, "done_count": 3, "release_date": "2026-10-01", "late_tickets": [], "open_tickets": [],
     "reasons": [], "release": {"status": "released", "tracker": "TESTFLOW-90", "tag": "v0.9",
                                "github_release_url": "https://github.com/o/r/releases/tag/v0.9", "steps": STEPS}},
    {"project": "OTHER", "version_id": "300", "version": "3.0", "released": False, "ready": True, "ticket_count": 1,
     "done_count": 1, "late_tickets": [], "open_tickets": [], "reasons": [], "release": {"status": "none", "steps": []}},
]


def test_command_center_panel_shows_steps_with_run_id_and_node():
    out = json.loads(run_script(DOM + f"""
console.log(JSON.stringify({{cc: progressPanel({json.dumps(PROGRESS)}, 'en', 'p-ready', true)}}));
"""))["cc"]
    assert "2 / 4 Done" in out and "TESTFLOW-13 · added late on 2026-10-05" in out
    assert 'data-open="TESTFLOW-12"' in out and "TESTFLOW-12: not Done (status To Do)" in out
    assert "The release check failed: pytest failed" in out
    for step in ("check", "changelog", "pr", "merge", "tag", "release"):
        assert f'data-step="{step}"' in out
    assert "node7" in out and RUN[:8] in out
    assert "Tag v0.9" in out and 'href="https://github.com/o/r/releases/tag/v0.9"' in out


def test_business_screen_is_compact_with_late_tickets_reasons_tag_and_link():
    out = business("renderBBody(); console.log(JSON.stringify({biz: el('#bbody').innerHTML}));",
                   state={**STATE, "release_progress": PROGRESS})["biz"]
    assert 'id="b-ready"' in out and "Tiến độ release" in out
    assert "2 / 4 Done" in out and "TESTFLOW-13 · thêm sau ngày 2026-10-05" in out
    assert "TESTFLOW-12: chưa Done (đang ở To Do)" in out and "Kiểm tra release bị lỗi: pytest failed" in out
    assert "Tag v0.9" in out and 'href="https://github.com/o/r/releases/tag/v0.9"' in out
    assert "data-step" not in out and "node7" not in out  # steps stay in the Command Center
    assert "3.0" not in out
