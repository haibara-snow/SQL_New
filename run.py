from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time

from app.config import load_settings
from app.seed import seed_extended_all


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the distributed library demo services")
    parser.add_argument("--init-only", action="store_true", help="initialize all branch databases and exit")
    args = parser.parse_args()
    settings = load_settings()
    seed_extended_all(settings)
    if args.init_only:
        print("Initialized sample data in each branch database.")
        return 0

    children: list[subprocess.Popen] = []
    try:
        for site in settings.sites:
            env = os.environ.copy()
            env["SITE_ID"] = site.site_id
            children.append(subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.shard:app", "--host", site.host, "--port", str(site.port)],
                cwd=settings.root,
                env=env,
            ))
        time.sleep(0.5)
        children.append(subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", settings.main_host, "--port", str(settings.main_port)],
            cwd=settings.root,
        ))
        print("Distributed library services are starting:")
        main_host = "127.0.0.1" if settings.main_host in ("0.0.0.0", "::") else settings.main_host
        print(f"  Main: http://{main_host}:{settings.main_port}/")
        for site in settings.sites:
            print(f"  Site {site.site_id} (branch {site.branch_id}): {site.url}/")
        print("Press Ctrl+C to stop all services.")
        while True:
            for child in children:
                code = child.poll()
                if code is not None:
                    raise RuntimeError(f"A service exited unexpectedly with status {code}")
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"Service startup failed: {exc}", file=sys.stderr)
        return 1
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.send_signal(signal.SIGTERM)
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
