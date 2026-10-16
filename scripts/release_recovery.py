"""Restart only the named isolated release services; retain all data and volumes."""

import json
import subprocess
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = [
    "docker",
    "compose",
    "--env-file",
    str(ROOT / ".local/release/release.env"),
    "-p",
    "enterprise-integration-release",
    "-f",
    str(ROOT / "compose.release.yaml"),
    "-f",
    str(ROOT / "compose.rehearsal.yaml"),
]


def run(*args: str, input_text: str | None = None, expected: int = 0) -> str:
    result = subprocess.run(
        COMPOSE + list(args),
        check=False,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert result.returncode == expected, (args[:3], result.returncode)
    return result.stdout.strip()


def wait(check, seconds: int = 180):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            if value := check():
                return value
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError("Bounded isolated restart condition did not complete")


def main() -> None:
    env = dotenv_values(ROOT / ".local/release/release.env")
    assert env["APP_ENV"] == "test" and env["AI_OPENAI_ENABLED"] == "false"
    checks = []
    with httpx.Client(base_url="http://127.0.0.1:3300", timeout=25) as client:

        def get(path):
            r = client.get("/api/platform/" + path)
            assert r.status_code == 200
            return r.json()

        def post(path, data):
            csrf = get("auth/csrf")["csrf_token"]
            r = client.post(
                "/api/platform/" + path,
                json=data,
                headers={"Origin": "http://127.0.0.1:3300", "X-CSRF-Token": csrf},
            )
            assert r.status_code == 200
            return r.json()

        post(
            "auth/login",
            {"email": env["BOOTSTRAP_EMAIL"], "password": env["BOOTSTRAP_PASSWORD"]},
        )
        tenant = next(t for t in get("tenants") if t["slug"] == "demo-recruiting-alpha")
        post("tenant/switch", {"tenant_id": tenant["id"]})
        connections = get("connections?limit=100")["total"]
        deliveries = get("notifications/deliveries?limit=100")["total"]
        worker = run("exec", "-T", "worker", "cat", "/tmp/worker-id")
        run("stop", "worker")
        assert worker not in {r["id"] for r in get("worker-status")["active_workers"]}
        checks.append("graceful_worker_stop_not_active")
        run("up", "-d", "--wait", "--wait-timeout", "240", "worker")
        new = run("exec", "-T", "worker", "cat", "/tmp/worker-id")
        assert new != worker
        run(
            "exec",
            "-T",
            "worker",
            "python",
            "-c",
            f"from pathlib import Path;Path('/tmp/old-worker-id').write_text('{worker}')",
        )
        run(
            "exec",
            "-T",
            "-e",
            "WORKER_HEALTH_FILE=/tmp/old-worker-id",
            "worker",
            "python",
            "container_health.py",
            "worker",
            expected=1,
        )
        run("exec", "-T", "worker", "python", "container_health.py", "worker")
        checks.append("own_worker_probe_not_masked_by_other_healthy_worker")
        run("kill", "-s", "SIGKILL", "worker")
        run("up", "-d", "--wait", "--wait-timeout", "240", "worker")
        killed = new
        current = run("exec", "-T", "worker", "cat", "/tmp/worker-id")
        assert current != killed
        wait(
            lambda: (
                killed not in {r["id"] for r in get("worker-status")["active_workers"]}
            ),
            60,
        )
        checks.append("idle_worker_sigkill_restart_and_stale_detection")
        run("restart", "api", "frontend")
        wait(lambda: client.get("/login").status_code == 200)
        wait(lambda: client.get("/api/platform/auth/me").status_code == 200)
        assert get("connections?limit=100")["total"] == connections
        assert get("notifications/deliveries?limit=100")["total"] == deliveries
        assert httpx.get("http://127.0.0.1:18000/ready", timeout=10).status_code == 200
        checks.append("same_session_after_api_frontend_restart")
        checks.append("durable_records_and_no_duplicate_deliveries")
        run("stop", "worker")
        run("stop", "postgres")
        assert httpx.get("http://127.0.0.1:18000/health", timeout=10).status_code == 200
        assert httpx.get("http://127.0.0.1:18000/ready", timeout=10).status_code == 503
        run("up", "-d", "--wait", "--wait-timeout", "240", "postgres")
        wait(
            lambda: (
                httpx.get("http://127.0.0.1:18000/ready", timeout=10).status_code == 200
            )
        )
        assert get("connections?limit=100")["total"] == connections
        run("up", "-d", "--wait", "--wait-timeout", "240", "worker")
        checks.append("isolated_database_outage_readiness_503_and_pool_recovery")
        post("auth/logout", {})
    (ROOT / "docs/evidence/phase12-restart.json").write_text(
        json.dumps(
            {
                "passed": len(checks),
                "checks": checks,
                "scope": "Isolated idle-worker SIGKILL and graceful service restarts; no after-effect crash cut claimed",
            },
            indent=2,
        )
        + "\n"
    )
    print(f"PASS: {len(checks)} isolated restart/health checks; volumes retained.")


if __name__ == "__main__":
    main()
