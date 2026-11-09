"""The console_login feature (JAIF-496): sign-in, sessions and users, without services.

Each test names the acceptance criterion it pins: the route set and order are unchanged, every SQL statement lives in
the repository, and the service decisions answer as they did in `login_signin`, `login_sessions` and `login_users`."""
from __future__ import annotations

from types import SimpleNamespace

from aiflow.v4.control.features.console_login import repository, routes, service
from aiflow.v4.control.features.console_login.session_cookies import (CSRF_COOKIE, clear_session_cookies,
                                                                       set_session_cookies)


class RecordingDatabase:
    def __init__(self, one=None, rows=()):
        self.statements: list[tuple[str, tuple]] = []
        self._one, self._rows = one, list(rows)

    def run(self, sql, params=()):
        self.statements.append((sql, params))

    def one(self, sql, params=()):
        self.statements.append((sql, params))
        return self._one

    def all(self, sql, params=()):
        self.statements.append((sql, params))
        return self._rows


class RecordingApp:
    def __init__(self):
        self.registered = []
        self.state = SimpleNamespace()

    def __getattr__(self, method):
        def factory(path, **_options):
            return lambda handler: self.registered.append((method.upper(), path, handler.__name__)) or handler
        return factory


def test_install_registers_the_same_routes_in_the_same_order():
    app = RecordingApp()
    routes.install(app, SimpleNamespace(settings=SimpleNamespace(public_url="")))
    assert app.registered == [("GET", "/login", "login_page"), ("POST", "/v1/auth/login", "login"),
                              ("POST", "/v1/auth/logout", "logout"), ("POST", "/logout", "logout_page"),
                              ("GET", "/v1/auth/me", "me"), ("GET", "/v1/auth/users", "console_users"),
                              ("POST", "/v1/auth/users", "create_console_user")]
    assert callable(app.state.set_session_cookies)


def test_credentials_from_a_sign_in_body():
    assert routes.credentials_from({"email": " A@Example.test ", "password": "pw", "code": "123456"}) == (
        "a@example.test", "pw", "123456")
    assert routes.credentials_from({"email": "a@example.test", "password": "pw", "code": 5}) == (
        "a@example.test", "pw", "")
    assert routes.credentials_from({"email": "not an email", "password": "pw"}) == ("", "pw", "")
    assert routes.credentials_from({"email": "a@example.test"}) is None
    assert routes.credentials_from(["a"]) is None
    assert routes.credentials_from({"email": "a@example.test", "password": "x" * 2000, "code": "9" * 80}) == (
        "a@example.test", "x" * 1024, "9" * 64)


def test_user_json_names_the_first_role_and_the_landing_page():
    body = service.user_json("a@example.test", ["administrator"], "Ada")
    assert body["email"] == "a@example.test" and body["name"] == "Ada"
    assert body["role"] == "administrator" and body["roles"] == ["administrator"]
    assert body["role_label"] == body["role_labels"][0] and body["landing_page"]


def test_a_spent_budget_answers_429_unless_a_valid_second_factor_came_with_it():
    assert service.limited_signin_response(False, True, True, True, "totp", 5) is None
    assert service.limited_signin_response(True, True, True, True, "totp", 5) is None
    assert service.limited_signin_response(True, True, True, True, "", 7) == (
        429, {"detail": "too many sign-in attempts; try again later"}, {"Retry-After": "7"})
    assert service.limited_signin_response(True, True, False, True, "totp", 3)[0] == 429


def test_a_good_password_of_an_enrolled_user_without_a_code_asks_for_the_code():
    state = service.AttemptState({}, False, True, False, True, True, "")
    assert service.needs_second_factor_code(state, " ") is True
    assert service.needs_second_factor_code(state, "123456") is False


def test_a_wrong_password_counts_a_failure_and_answers_the_same_refusal():
    class Leases:
        p = "t:"

        class r:
            @staticmethod
            def pipeline():
                return SimpleNamespace(incr=lambda *_: None, expire=lambda *_: None, execute=lambda: [1])

    database = RecordingDatabase()
    ctx = SimpleNamespace(db=database, leases=Leases)
    assert service.failed_signin_response(ctx, "a@example.test", True, False, False) == (
        401, {"detail": service.REFUSED}, {})
    assert "failed_attempts=failed_attempts+1" in database.statements[0][0]
    assert database.statements[1][1][:2] == ("console", "login")  # the failed sign-in audit row


def test_create_console_user_refuses_a_malformed_body_before_any_write():
    ctx, settings = SimpleNamespace(db=RecordingDatabase()), SimpleNamespace(license_public_key="")
    assert service.create_console_user(ctx, settings, "admin", None, "ip") == (
        422, {"detail": "Enter an email address and choose a role."})
    assert service.create_console_user(ctx, settings, "admin", {"email": "a@example.test", "role": 3}, "ip")[0] == 422
    assert service.create_console_user(ctx, settings, "admin",
                                       {"email": "a@example.test", "role": "lead", "display_name": "x" * 121},
                                       "ip") == (422, {"detail": "Display name must be 120 characters or fewer."})
    assert ctx.db.statements == []


def test_session_revoked_reads_the_repository_and_fails_closed():
    assert service.session_revoked(SimpleNamespace(db=RecordingDatabase(one={"x": 1})), "sid") is False
    assert service.session_revoked(SimpleNamespace(db=RecordingDatabase(one=None)), "sid") is True


def test_the_repository_owns_the_statements_of_console_users_and_sessions():
    database = RecordingDatabase(one={"display_name": "Ada", "count": 2})
    repository.clear_expired_lock(database, "a@example.test")
    repository.record_failed_attempt(database, "a@example.test", 5, 900)
    repository.record_signed_in(database, "a@example.test")
    repository.open_session(database, "sid", "a@example.test", "lead", "10.0.0.1", 60)
    repository.end_session(database, "sid", "a@example.test")
    assert repository.display_name(database, "a@example.test") == "Ada"
    assert repository.count_active_users(database) == 2
    tables = [sql.split()[1:3] for sql, _ in database.statements]
    assert tables[0] == ["console_users", "SET"] and tables[3] == ["INTO", "console_sessions"]
    assert database.statements[1][1] == (5, 900, "a@example.test")


def test_cookies_are_strict_and_the_csrf_cookie_is_readable_by_the_console_script():
    cookies = []

    class Response:
        def set_cookie(self, name, value, **options):
            cookies.append((name, options))

        def delete_cookie(self, name, **options):
            cookies.append((name, options))

    settings = SimpleNamespace(public_url="https://flow.example.test", console_session_secret="s" * 32)
    set_session_cookies(settings, Response(), "token", 60)
    clear_session_cookies(settings, Response())
    by_name = dict(cookies)
    assert by_name[CSRF_COOKIE]["samesite"] == "strict" and by_name[CSRF_COOKIE]["secure"] is True
    assert [name for name, _ in cookies].count(CSRF_COOKIE) == 2
