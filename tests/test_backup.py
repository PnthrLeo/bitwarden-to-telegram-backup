import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
import unittest


REPO = Path(__file__).resolve().parents[1]
REAL_AGE = shutil.which("age")
REAL_KEYGEN = shutil.which("age-keygen")
REAL_TAR = shutil.which("tar")


@unittest.skipUnless(REAL_AGE and REAL_KEYGEN, "Install age and age-keygen to run integration tests")
class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bw backup tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("id", "stat", "realpath", "du", "df", "flock", "timeout", "runuser", "sleep",
                     "sha256sum", "split", "tee", "tar", "age", "docker", "curl", "setsid"):
            (self.bin / name).symlink_to(REPO / "tests" / "fake_tools.py")
        self.install = self.root / "install"
        self.bwdata = self.install / "bwdata"
        for name in ("env", "mssql/data", "docker", "core/attachments"):
            (self.bwdata / name).mkdir(parents=True, exist_ok=True)
        (self.install / "bitwarden.sh").symlink_to(REPO / "tests" / "fake_tools.py")
        (self.bwdata / "env/global.override.env").write_text(
            "globalSettings__sqlServer__connectionString=Data Source=tcp:mssql,1433;Database=vault;Password=secret\n")
        (self.bwdata / "docker/docker-compose.yml").write_text("# fake compose\n")
        self.payload = self.bwdata / "core/attachments/payload.bin"
        self.payload.write_bytes(os.urandom(4096))
        self.payload.chmod(0o640)
        (self.bwdata / "core/attachments/link").symlink_to("payload.bin")
        os.link(self.payload, self.bwdata / "core/attachments/hardlink")
        self.backups = self.root / "backups"
        self.backups.mkdir(mode=0o700)
        self.state_file = self.root / "state.json"
        self.state_file.write_text(json.dumps({"running": True}))
        self.key = self.root / "identity.txt"
        subprocess.run([REAL_KEYGEN, "-o", str(self.key)], check=True, capture_output=True)
        recipient = subprocess.check_output([REAL_KEYGEN, "-y", str(self.key)], text=True).strip()
        self.token = "123456789:unit_test_token_never_log_me"
        self.config = self.root / "backup.conf"
        self.config.write_text(
            f"BITWARDEN_DIR='{self.install}'\nBITWARDEN_USER=bitwarden\n"
            f"BACKUP_DIR='{self.backups}'\nAGE_RECIPIENT='{recipient}'\n"
            f"TELEGRAM_BOT_TOKEN='{self.token}'\nTELEGRAM_CHAT_ID=123456789\n"
            "PART_BYTES=1500\nREADY_TIMEOUT=1\nSERVICE_TIMEOUT=1\nHTTP_TIMEOUT=1\n")
        self.config.chmod(0o600)
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        FAKE_ROOT=str(self.root), REAL_AGE=REAL_AGE, REAL_TAR=REAL_TAR,
                        TEST_TOKEN=self.token)
        self.command = ["/bin/bash", str(REPO / "backup.sh"), "--config", str(self.config)]

    def execute(self, *args, **extra):
        result = subprocess.run([*self.command, *args], env=dict(self.env, **extra),
                                capture_output=True, text=True, timeout=40)
        self.assertNotIn(self.token, result.stdout + result.stderr)
        return result

    def events(self):
        return [json.loads(line) for line in (self.root / "events.jsonl").read_text().splitlines()]

    def messages(self):
        return [e["text"] for e in self.events() if e["event"] == "telegram" and e["method"] == "sendMessage"]

    def runs(self):
        return sorted(p for p in self.backups.iterdir() if p.is_dir() and not p.name.startswith("."))

    def assert_running(self):
        self.assertTrue(json.loads(self.state_file.read_text())["running"])

    def assert_no_success(self):
        self.assertFalse(any("Бэкап завершён" in message for message in self.messages()))

    def test_round_trip_notification_order_and_metadata(self):
        result = self.execute()
        self.assertEqual(result.returncode, 0, result.stderr)
        run = self.runs()[0]
        manifest = json.loads((run / "manifest.json").read_text())
        self.assertGreater(len(manifest["parts"]), 1)
        archive = (run / manifest["archive"]).read_bytes()
        combined = b"".join((self.root / "uploads" / p["name"]).read_bytes() for p in manifest["parts"])
        self.assertEqual(combined, archive)
        self.assertEqual(hashlib.sha256(archive).hexdigest(), manifest["archive_sha256"])
        for part in manifest["parts"]:
            data = (run / part["name"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), part["sha256"])
            self.assertLessEqual(len(data), 1500)
        plain = subprocess.check_output([REAL_AGE, "-d", "-i", str(self.key), str(run / manifest["archive"])])
        import io
        with tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz") as archive_file:
            payload = archive_file.getmember("bwdata/core/attachments/payload.bin")
            # The hardlink may be chosen as the data-bearing member by tar.
            self.assertEqual(archive_file.extractfile(payload).read(), self.payload.read_bytes())
            self.assertEqual(payload.mode, 0o640)
            self.assertTrue(archive_file.getmember("bwdata/core/attachments/link").issym())
            linked = archive_file.getmember("bwdata/core/attachments/hardlink")
            self.assertTrue(payload.islnk() or linked.islnk())
        events = self.events()
        start_message = next(i for i, e in enumerate(events) if "Останавливаю" in e.get("text", ""))
        stopped = next(i for i, e in enumerate(events) if e["event"] == "stop")
        archived = next(i for i, e in enumerate(events) if e["event"] == "archive")
        restarted = next(i for i, e in enumerate(events) if e["event"] == "start")
        ready = next(i for i, e in enumerate(events) if "Начинаю отправку архива" in e.get("text", ""))
        uploads = [i for i, e in enumerate(events) if e.get("method") == "sendDocument"]
        final = next(i for i, e in enumerate(events) if "Бэкап завершён" in e.get("text", ""))
        self.assertTrue(start_message < stopped < archived < restarted < ready < min(uploads) <= max(uploads) < final)
        self.assertEqual(events[max(uploads)]["document"], "manifest.json")
        self.assertTrue((run / ".delivered").is_file())
        self.assertTrue(all(run.name in message for message in self.messages()))
        self.assertTrue(all(p.stat().st_mode & 0o077 == 0 for p in run.iterdir()))
        self.assertFalse(list(self.backups.glob(".work.*")))
        self.assert_running()

    def test_check_has_no_service_changes_or_notifications(self):
        result = self.execute("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.messages())
        self.assertFalse(self.runs())
        self.assertFalse(any(e["event"] in ("stop", "start") for e in self.events()))

    def test_single_file_archive_is_sent_without_splitting(self):
        with self.config.open("a") as stream:
            stream.write("PART_BYTES=45000000\n")
        result = self.execute()
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads((self.runs()[0] / "manifest.json").read_text())
        self.assertEqual(len(manifest["parts"]), 1)
        self.assertEqual(manifest["parts"][0]["name"], manifest["archive"])
        self.assertFalse(list(self.runs()[0].glob("*.part*")))

    def test_external_database_is_rejected_before_stop(self):
        (self.bwdata / "env/global.override.env").write_text(
            "globalSettings__sqlServer__connectionString=Data Source=tcp:external,1433;Password=secret\n")
        result = self.execute()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("built-in MSSQL", result.stderr)
        self.assertFalse(any(e["event"] == "stop" for e in self.events()))

    def test_external_and_symlinked_mounts_are_rejected_before_stop(self):
        external = self.root / "external"
        external.mkdir()
        (self.bwdata / "external-link").symlink_to(external, target_is_directory=True)
        for variable in ("EXTERNAL_MOUNT", "SYMLINK_MOUNT"):
            with self.subTest(variable=variable):
                result = self.execute(**{variable: "1"})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(e["event"] == "stop" for e in self.events()))

    def test_database_without_persistent_data_mount_is_rejected(self):
        result = self.execute(MISSING_DB_MOUNT="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MSSQL data directory", result.stderr)
        self.assertFalse(any(e["event"] == "stop" for e in self.events()))

    def test_failed_check_sends_no_notification(self):
        result = self.execute("--check", FAIL_DISK="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.messages())

    def test_initial_notification_failure_does_not_stop(self):
        result = self.execute(FAIL_MESSAGE="Останавливаю")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(e["event"] == "stop" for e in self.events()))
        self.assertFalse(self.runs())
        self.assert_no_success()

    def test_disk_failure_does_not_stop(self):
        result = self.execute(FAIL_DISK="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(e["event"] == "stop" for e in self.events()))
        self.assert_no_success()

    def test_archive_and_encryption_failures_restore_service(self):
        for variable in ("FAIL_TAR", "FAIL_AGE", "FAIL_WRITE", "FAIL_STOP", "STOP_LIES"):
            with self.subTest(variable=variable):
                (self.root / "events.jsonl").unlink(missing_ok=True)
                result = self.execute(**{variable: "1"})
                self.assertNotEqual(result.returncode, 0)
                self.assert_running()
                self.assertFalse(self.runs())
                self.assert_no_success()
                self.assertTrue(any(e["event"] == "start" for e in self.events()))

    def test_signal_after_stop_restores_service(self):
        marker = self.root / "archiving"
        process = subprocess.Popen(self.command, env=dict(self.env, HOLD_TAR=str(marker)),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 10
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(marker.exists())
        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=15)
        self.assertNotEqual(process.returncode, 0, stderr)
        self.assert_running()
        self.assert_no_success()

    def test_fallback_start_uses_existing_images(self):
        result = self.execute(FAIL_BW_START="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        fallback = next(e for e in self.events() if e["event"] == "fallback-start")
        self.assertIn("never", fallback["argv"])
        self.assertIn("--no-build", fallback["argv"])
        self.assert_running()

    def test_start_failure_never_announces_readiness_or_success(self):
        result = self.execute(FAIL_START="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("готов к работе" in text for text in self.messages()))
        self.assert_no_success()
        self.assertTrue(list(self.runs()[0].glob("*.age")))
        self.assertFalse((self.runs()[0] / ".delivered").exists())

    def test_failed_health_check_never_announces_readiness(self):
        result = self.execute(FAIL_HEALTH="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("готов к работе" in text for text in self.messages()))
        self.assert_no_success()

    def test_intermediate_notification_failure_still_uploads(self):
        result = self.execute(FAIL_MESSAGE="Начинаю отправку архива")
        self.assertNotEqual(result.returncode, 0)
        self.assert_running()
        self.assertTrue((self.runs()[0] / ".delivered").exists())
        self.assertTrue(any(e.get("document") == "manifest.json" for e in self.events()))

    def test_upload_and_manifest_failures_are_not_marked_delivered(self):
        for variable in ("FAIL_UPLOAD", "FAIL_MANIFEST"):
            with self.subTest(variable=variable):
                (self.root / "events.jsonl").unlink(missing_ok=True)
                result = self.execute(**{variable: "1"})
                self.assertNotEqual(result.returncode, 0)
                self.assert_running()
                latest = self.runs()[-1]
                self.assertTrue(list(latest.glob("*.age")))
                self.assertFalse((latest / ".delivered").exists())
                self.assert_no_success()

    def test_retries_rate_limit_server_error_and_network(self):
        for failure in ("429", "503", "network"):
            with self.subTest(failure=failure):
                self.state_file.write_text(json.dumps({"running": True}))
                (self.root / "events.jsonl").unlink(missing_ok=True)
                result = self.execute(RETRY_ONCE=failure)
                self.assertEqual(result.returncode, 0, result.stderr)
                uploads = [e for e in self.events() if e.get("method") == "sendDocument"]
                self.assertNotEqual(uploads[0]["code"], 200)
                self.assertEqual(uploads[1]["code"], 200)
                self.assertEqual(uploads[0]["document"], uploads[1]["document"])
                if failure == "429":
                    self.assertTrue(any(e["event"] == "sleep" and e["seconds"] == 2 for e in self.events()))

    def test_network_unavailable_before_stop_exhausts_retries(self):
        result = self.execute(FAIL_NETWORK="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(e["event"] == "stop" for e in self.events()))
        attempts = [e for e in self.events() if "Останавливаю" in e.get("text", "")]
        self.assertEqual(len(attempts), 4)

    def test_resend_verifies_archive_and_does_not_touch_service(self):
        self.assertNotEqual(self.execute(FAIL_UPLOAD="1").returncode, 0)
        run = self.runs()[0]
        (self.root / "events.jsonl").unlink()
        # Resend works even if the installation has disappeared.
        shutil.rmtree(self.install)
        result = self.execute("--send-existing", str(run))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(e["event"] in ("stop", "start") for e in self.events()))
        self.assertTrue(any("Повторная отправка завершена" in message for message in self.messages()))
        self.assertTrue((run / ".delivered").exists())
        archive = next(run.glob("*.age"))
        data = archive.read_bytes()
        archive.write_bytes(bytes([data[0] ^ 1]) + data[1:])
        result = self.execute("--send-existing", str(run))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum mismatch", result.stderr)

    def test_resend_after_start_failure_uses_original_streaming_digest(self):
        failed = self.execute(FAIL_START="1")
        self.assertNotEqual(failed.returncode, 0)
        run = self.runs()[0]
        self.assertFalse((run / "manifest.json").exists())
        self.assertTrue((run / ".archive.sha256").exists())
        resent = self.execute("--send-existing", str(run))
        self.assertEqual(resent.returncode, 0, resent.stderr)

    def test_retention_removes_only_old_delivered_sets_after_success(self):
        old = self.backups / "20200101T000000Z-1"
        unsent = self.backups / "20200102T000000Z-2"
        recent = self.backups / "20200103T000000Z-3"
        for path in (old, unsent, recent):
            path.mkdir(mode=0o700)
        for path in (old, recent):
            (path / ".delivered").touch()
        os.utime(old / ".delivered", (1, 1))
        failed = self.execute(FAIL_MESSAGE="Бэкап завершён")
        self.assertNotEqual(failed.returncode, 0)
        self.assertTrue(old.exists())
        succeeded = self.execute()
        self.assertEqual(succeeded.returncode, 0, succeeded.stderr)
        self.assertFalse(old.exists())
        self.assertTrue(unsent.exists())
        self.assertTrue(recent.exists())

    def test_lock_blocks_parallel_run(self):
        with (self.backups / ".lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.execute()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Another backup", result.stderr)

    def test_insecure_configuration_is_rejected(self):
        self.config.chmod(0o644)
        result = self.execute()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("0600/0400", result.stderr)


if __name__ == "__main__":
    unittest.main()
