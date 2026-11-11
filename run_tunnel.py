import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env", override=True)

PINGGY_HOST = "qr@free.pinggy.io"
PINGGY_URL_PATTERN = re.compile(
    r"https://[A-Za-z0-9.-]+\.(?:free\.pinggy\.net|run\.pinggy-free\.link)\b"
)


def wait_for_server(process, port):
    health_url = f"http://127.0.0.1:{port}/health"
    deadline = time.monotonic() + 30

    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Gunicorn exited before the health check passed.")
        try:
            with urllib.request.urlopen(health_url, timeout=2) as response:
                if response.status == 200:
                    return
        except urllib.error.URLError:
            time.sleep(0.25)

    raise TimeoutError(f"Gunicorn did not become healthy at {health_url}.")


def is_model_ready(base_url, api_key):
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/models",
        headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status == 200
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"Model endpoint returned HTTP {exc.code} at {request.full_url}."
        ) from exc
    except urllib.error.URLError:
        return False


def wait_for_model(process, base_url, api_key, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError("The `apfel --serve` process exited before the model became ready.")
        if is_model_ready(base_url, api_key):
            print("Apple Intelligence model endpoint is ready.")
            return
        time.sleep(0.5)
    raise TimeoutError(
        f"Apple Intelligence model did not become ready at {base_url.rstrip('/')}/models "
        f"within {timeout} seconds."
    )


def set_telegram_webhook(public_url, token):
    webhook_url = f"{public_url}/webhook"
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/setWebhook",
        data=urllib.parse.urlencode({"url": webhook_url}).encode(),
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.loads(response.read())

    if not result.get("ok"):
        raise RuntimeError(f"Telegram rejected the webhook: {result}")
    print(f"Telegram webhook set to {webhook_url}")


def stop_process(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in .env before starting.")

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    model_base_url = os.environ.get(
        "OPENAI_BASE_URL", "http://127.0.0.1:11434/v1"
    )
    model_api_key = os.environ.get("OPENAI_API_KEY", "not-needed")
    server = tunnel = model = None

    try:
        subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "check_ynab.py")],
            cwd=PROJECT_ROOT,
            check=True,
        )

        if is_model_ready(model_base_url, model_api_key):
            print("Apple Intelligence model endpoint is already running.")
        else:
            apfel = shutil.which("apfel")
            if not apfel:
                raise RuntimeError(
                    "`apfel` was not found on PATH. Install or activate Apple "
                    "Intelligence before starting the bot."
                )
            model = subprocess.Popen(
                [apfel, "--serve"],
                cwd=PROJECT_ROOT,
            )
            wait_for_model(model, model_base_url, model_api_key)

        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "gunicorn",
                "--bind",
                f"{host}:{port}",
                "bot:app",
            ],
            cwd=PROJECT_ROOT,
        )
        wait_for_server(server, port)
        print(f"Gunicorn is healthy on port {port}; opening Pinggy tunnel.")

        tunnel = subprocess.Popen(
            [
                "ssh",
                "-T",
                "-p",
                "443",
                "-R",
                f"0:localhost:{port}",
                PINGGY_HOST,
            ],
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        output_lines = queue.Queue()

        def read_tunnel_output():
            for line in tunnel.stdout:
                output_lines.put(line)

        threading.Thread(target=read_tunnel_output, daemon=True).start()
        tunnel_deadline = time.monotonic() + 60
        public_url = None

        while time.monotonic() < tunnel_deadline:
            if tunnel.poll() is not None:
                raise RuntimeError(f"Pinggy SSH exited with status {tunnel.returncode}.")
            if server.poll() is not None:
                raise RuntimeError("Gunicorn exited while the Pinggy tunnel was starting.")
            try:
                line = output_lines.get(timeout=0.25)
            except queue.Empty:
                continue
            print(line, end="", flush=True)
            match = PINGGY_URL_PATTERN.search(line)
            if match:
                public_url = match.group(0)
                break

        if not public_url:
            raise TimeoutError("Timed out waiting for Pinggy to print its HTTPS URL.")

        set_telegram_webhook(public_url, token)
        print("Bot is running. Press Ctrl+C to stop.")

        while tunnel.poll() is None and server.poll() is None:
            try:
                print(output_lines.get(timeout=0.25), end="", flush=True)
            except queue.Empty:
                pass

        if tunnel.poll() is not None:
            raise RuntimeError(f"Pinggy SSH exited with status {tunnel.returncode}.")
        raise RuntimeError("Gunicorn exited while the tunnel was running.")
    except KeyboardInterrupt:
        print("\nStopping bot and tunnel.")
    finally:
        stop_process(tunnel)
        stop_process(server)
        stop_process(model)


if __name__ == "__main__":
    main()
