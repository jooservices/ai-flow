import io
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from aiflow import runner
from aiflow.lock import Redis, RedisError, decode, encode


class FakeServer:
    """A tiny RESP server. `script` is a list of per-connection handlers: each gets (command parts) and returns bytes,
    or None to drop the connection."""

    def __init__(self, handler):
        self.handler, self.commands, self.connections = handler, [], 0
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self.talk, args=(conn, self.connections), daemon=True).start()

    def talk(self, conn, number):
        stream = conn.makefile("rb")
        try:
            while True:
                parts = decode(stream)
                self.commands.append(parts)
                reply = self.handler(number, parts)
                if reply is None:
                    conn.close()
                    return
                conn.sendall(reply)
        except Exception:
            conn.close()

    def stop(self):
        self.sock.close()


def ok(number, parts):
    name = parts[0].upper()
    return {"AUTH": b"+OK\r\n", "SELECT": b"+OK\r\n", "SET": b"+OK\r\n", "GET": b"$3\r\nfoo\r\n", "PING": b"+PONG\r\n"}.get(name, b"-ERR unknown\r\n")


class ProtocolTests(unittest.TestCase):
    def test_round_trip(self):
        self.assertEqual(decode(io.BytesIO(b"$5\r\nhello\r\n")), "hello")
        self.assertEqual(decode(io.BytesIO(b":42\r\n")), 42)
        self.assertIsNone(decode(io.BytesIO(b"$-1\r\n")))
        self.assertEqual(decode(io.BytesIO(b"*2\r\n$1\r\na\r\n$-1\r\n")), ["a", None])
        self.assertEqual(encode("SET", "k", "vé"), b"*3\r\n$3\r\nSET\r\n$1\r\nk\r\n$3\r\nv\xc3\xa9\r\n")

    def test_error_reply_raises(self):
        with self.assertRaises(RedisError):
            decode(io.BytesIO(b"-WRONGPASS nope\r\n"))


class ClientTests(unittest.TestCase):
    def test_auth_user_and_select_are_sent_on_connect(self):
        server = FakeServer(ok)
        self.addCleanup(server.stop)
        client = Redis("127.0.0.1", server.port, 5, "p:", "secret", user="aiflow", retries=1)
        self.assertEqual(client.get("x"), "foo")
        self.assertEqual(server.commands[0], ["AUTH", "aiflow", "secret"])
        self.assertEqual(server.commands[1], ["SELECT", "5"])
        self.assertEqual(server.commands[2], ["GET", "p:x"])

    def test_reconnects_after_a_dropped_connection(self):
        def handler(number, parts):
            if number == 1 and parts[0] == "GET":
                return None  # first connection dies on the command
            return ok(number, parts)
        server = FakeServer(handler)
        self.addCleanup(server.stop)
        client = Redis("127.0.0.1", server.port, 0, "p:", "", retries=3)
        self.assertEqual(client.get("x"), "foo")
        self.assertEqual(server.connections, 2)

    def test_unreachable_server_raises_redis_error(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        with self.assertRaises(RedisError):
            Redis("127.0.0.1", port, 0, "p:", "", retries=2, timeout=1).get("x")

    def test_acquire_recognises_its_own_lease_after_a_lost_reply(self):
        def handler(number, parts):
            if parts[0] == "SET":
                return b"$-1\r\n"  # NX refused: the earlier, lost attempt had already set it
            if parts[0] == "GET":
                return b"$5\r\nowner\r\n"
            return ok(number, parts)
        server = FakeServer(handler)
        self.addCleanup(server.stop)
        client = Redis("127.0.0.1", server.port, 0, "p:", "", retries=1)
        self.assertTrue(client.acquire("lease:K", "owner", 60))
        self.assertFalse(client.acquire("lease:K", "someone-else", 60))

    def test_get_many_maps_missing_to_none(self):
        def handler(number, parts):
            if parts[0] == "MGET":
                return b"*2\r\n$1\r\n1\r\n$-1\r\n"
            return ok(number, parts)
        server = FakeServer(handler)
        self.addCleanup(server.stop)
        client = Redis("127.0.0.1", server.port, 0, "p:", "", retries=1)
        self.assertEqual(client.get_many(["a", "b"]), ["1", None])
        self.assertEqual(client.get_many([]), [])

    def test_persistent_values_are_written_without_expiry(self):
        server = FakeServer(ok)
        self.addCleanup(server.stop)
        client = Redis("127.0.0.1", server.port, 0, "p:", "", retries=1)

        client.set_persistent("dbticket:JAIF-7", "my-project")

        self.assertEqual(server.commands[-1], ["SET", "p:dbticket:JAIF-7", "my-project"])
        client.close()

    def test_deploy_enqueue_uses_atomic_lock_pending_and_dedupe_keys(self):
        def handler(number, parts):
            if parts[0] == "EVAL":
                return b":0\r\n"  # another worker owns the project lock, so the merge was queued
            return ok(number, parts)

        server = FakeServer(handler)
        self.addCleanup(server.stop)
        client = Redis("127.0.0.1", server.port, 0, "p:", "", retries=1)

        queued = client.enqueue_deploy("deploy-lock:JAIF", "deploy-pending:JAIF", "deploy-active:JAIF",
                                       "deploy-seen:JAIF:JAIF-3", "owner", "JAIF-3", '{"ticket":"JAIF-3"}',
                                       300, 2592000)

        self.assertEqual(queued, 0)
        command = server.commands[-1]
        self.assertEqual(command[:8], ["EVAL", command[1], "4", "p:deploy-lock:JAIF", "p:deploy-pending:JAIF",
                                        "p:deploy-active:JAIF", "p:deploy-seen:JAIF:JAIF-3", "owner"])
        self.assertEqual(command[8:], ["JAIF-3", '{"ticket":"JAIF-3"}', "300", "2592000"])
        client.close()

    def test_deploy_finish_transfers_an_active_batch_atomically(self):
        def handler(number, parts):
            if parts[0] == "EVAL":
                return b"$12\r\nsecond-event\r\n"
            return ok(number, parts)

        server = FakeServer(handler)
        self.addCleanup(server.stop)
        client = Redis("127.0.0.1", server.port, 0, "p:", "", retries=1)

        pending = client.finish_deploy("deploy-lock:JAIF", "deploy-pending:JAIF", "deploy-active:JAIF",
                                       "owner", 300, "first-event")

        self.assertEqual(pending, "second-event")
        command = server.commands[-1]
        self.assertEqual(command[:7], ["EVAL", command[1], "3", "p:deploy-lock:JAIF", "p:deploy-pending:JAIF",
                                        "p:deploy-active:JAIF", "owner"])
        self.assertEqual(command[7:], ["300", "first-event"])
        client.close()

    def test_ai_session_operations_use_prefixed_atomic_redis_scripts(self):
        server = FakeServer(lambda _number, parts: b":1\r\n" if parts[0] == "EVAL" else ok(_number, parts))
        self.addCleanup(server.stop)
        client = Redis("127.0.0.1", server.port, 0, "aiworkflow:", "", retries=1)
        self.addCleanup(client.close)
        self.assertTrue(client.acquire_session("wsl", "Codex", "worker-1", 120, limit=4))
        self.assertTrue(client.renew_session("wsl", "Codex", "worker-1", 120))
        self.assertTrue(client.release_session("wsl", "Codex", "worker-1"))
        scripts = [parts for parts in server.commands if parts[0] == "EVAL"]
        self.assertEqual(len(scripts), 3)
        self.assertIn("ZREMRANGEBYSCORE", scripts[0][1])
        self.assertEqual(scripts[0][2:4], ["2", "aiworkflow:sessions:wsl:codex"])
        self.assertIn("aiworkflow:sessionpeak:", scripts[0][4])
        self.assertEqual(scripts[0][6:], ["120", "4", "worker-1", str(10 * 24 * 60 * 60)])


class RunnerSafetyTests(unittest.TestCase):
    def test_ai_is_stopped_when_the_heartbeat_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            def boom():
                raise RedisError("down")
            started = []
            real_popen = subprocess.Popen

            def spy(*args, **kwargs):
                proc = real_popen(*args, **kwargs)
                started.append(proc)
                return proc
            subprocess.Popen = spy
            try:
                with self.assertRaises(RedisError):
                    runner.run(["sleep", "30"], None, Path(tmp), Path(tmp) / "x.log", {}, idle_seconds=60, max_seconds=60,
                               heartbeat_seconds=0, heartbeat=boom)
            finally:
                subprocess.Popen = real_popen
            self.assertIsNotNone(started[0].poll())  # the process was killed, not left running


if __name__ == "__main__":
    unittest.main()
