"""
Binance Square OpenAPI HTTP Client & Official Node.js Skill Runner.
Handles:
1. Dry-run simulation.
2. Direct REST text posting.
3. Official post-image.mjs execution from Binance Skills Hub for chart PNGs.
4. Strict API key protection (keys are never logged or exposed).
"""
from __future__ import annotations
import asyncio
import logging
import os
import shutil
from typing import Optional
import httpx

from bot.config import settings
from bot.square.models import SquarePostResult, SquareStatus

log = logging.getLogger("square")

SQUARE_OPENAPI_URL = "https://www.binance.com/bapi/composite/v1/public/pgc/openApi/content/add"


def find_node_executable() -> Optional[str]:
    """Locate Node.js executable on Windows or Linux."""
    candidate = shutil.which("node")
    if candidate:
        return candidate
    common_paths = [
        r"C:\Program Files\nodejs\node.exe",
        r"C:\Program Files (x86)\nodejs\node.exe",
        os.path.expanduser(r"~\AppData\Roaming\nvm\current\node.exe"),
        "/usr/bin/node",
        "/usr/local/bin/node",
    ]
    for p in common_paths:
        if os.path.exists(p):
            return p
    return None


class BinanceSquareClient:
    def __init__(self, api_key: str = ""):
        self._api_key = api_key or settings.square_openapi_key

    async def post_image(
        self, content: str, chart_path: str, dry_run: bool = True
    ) -> SquarePostResult:
        """Publish content with local chart PNG using official Binance post-image.mjs script."""
        if dry_run:
            log.info(
                "\n+------------------------------------------------------------+\n"
                "| [SQUARE DRY RUN] Simulated Official Image Post Successful   |\n"
                "+------------------------------------------------------------+\n"
                "%s\n"
                "  * Local Chart PNG (Official Script): %s\n"
                "+------------------------------------------------------------+",
                content,
                chart_path,
            )
            return SquarePostResult(
                success=True,
                status=SquareStatus.SKIPPED_DRY_RUN,
                post_id="DRY_RUN_ID",
                content=content,
            )

        if not self._api_key or not self._api_key.strip():
            log.error("[SQUARE] BINANCE_SQUARE_OPENAPI_KEY is not configured in .env")
            return SquarePostResult(
                success=False,
                status=SquareStatus.FAILED,
                error_code="MISSING_API_KEY",
                error_message="BINANCE_SQUARE_OPENAPI_KEY not configured",
                content=content,
            )

        node_bin = find_node_executable()
        if not node_bin:
            err = "Node.js executable not found. Please ensure Node.js 18+ is installed."
            log.error("[SQUARE] %s", err)
            return SquarePostResult(
                success=False,
                status=SquareStatus.FAILED,
                error_code="NODE_NOT_FOUND",
                error_message=err,
                content=content,
            )

        script_path = os.path.join(
            os.path.dirname(__file__), "binance_skill", "scripts", "post-image.mjs"
        )
        if not os.path.exists(script_path):
            err = f"Official post-image.mjs not found at {script_path}"
            log.error("[SQUARE] %s", err)
            return SquarePostResult(
                success=False,
                status=SquareStatus.FAILED,
                error_code="SCRIPT_NOT_FOUND",
                error_message=err,
                content=content,
            )

        # Command arguments: never pass API key on the CLI
        cmd = [
            node_bin,
            script_path,
            "--text",
            content,
            "--images",
            chart_path,
        ]

        # Pass API key securely via subprocess environment
        sub_env = os.environ.copy()
        sub_env["BINANCE_SQUARE_OPENAPI_KEY"] = self._api_key.strip()

        log.info("[SQUARE] Executing official Binance post-image.mjs for chart: %s", os.path.basename(chart_path))
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=sub_env,
            )
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=45.0)
            stdout = stdout_b.decode("utf-8", errors="replace")
            stderr = stderr_b.decode("utf-8", errors="replace")

            if proc.returncode == 0:
                post_id = "PUBLISHED"
                for line in stdout.splitlines():
                    if line.startswith("ID:"):
                        post_id = line.split("ID:", 1)[1].strip() or "PUBLISHED"
                log.info("[SQUARE] Official post-image.mjs SUCCESS! Post ID: %s", post_id)
                return SquarePostResult(
                    success=True,
                    status=SquareStatus.SUCCESS,
                    post_id=post_id,
                    content=content,
                )
            else:
                err_msg = stderr.strip() or stdout.strip() or f"Process exited with code {proc.returncode}"
                err_msg = err_msg.replace(self._api_key.strip(), "***")
                log.error("[SQUARE] Official post-image.mjs FAILED: %s", err_msg)
                return SquarePostResult(
                    success=False,
                    status=SquareStatus.FAILED,
                    error_code=f"NODE_EXIT_{proc.returncode}",
                    error_message=err_msg,
                    content=content,
                )

        except asyncio.TimeoutError:
            log.error("[SQUARE] Official post-image.mjs timed out after 45s")
            return SquarePostResult(
                success=False,
                status=SquareStatus.FAILED,
                error_code="TIMEOUT",
                error_message="post-image.mjs execution timed out after 45 seconds",
                content=content,
            )
        except Exception as exc:
            log.exception("[SQUARE] Subprocess execution error: %s", exc)
            return SquarePostResult(
                success=False,
                status=SquareStatus.FAILED,
                error_code="SUBPROCESS_EXCEPTION",
                error_message=str(exc),
                content=content,
            )

    async def post_content(
        self, content: str, dry_run: bool = True
    ) -> SquarePostResult:
        """Publish text-only content to Binance Square or simulate during dry-run."""
        if dry_run:
            log.info(
                "\n+------------------------------------------------------------+\n"
                "| [SQUARE DRY RUN] Simulated Post Successful                  |\n"
                "+------------------------------------------------------------+\n"
                "%s\n"
                "+------------------------------------------------------------+",
                content,
            )
            return SquarePostResult(
                success=True,
                status=SquareStatus.SKIPPED_DRY_RUN,
                post_id="DRY_RUN_ID",
                content=content,
            )

        if not self._api_key or not self._api_key.strip():
            log.error("[SQUARE] BINANCE_SQUARE_OPENAPI_KEY is not configured in .env")
            return SquarePostResult(
                success=False,
                status=SquareStatus.FAILED,
                error_code="MISSING_API_KEY",
                error_message="BINANCE_SQUARE_OPENAPI_KEY not configured",
                content=content,
            )

        headers = {
            "X-Square-OpenAPI-Key": self._api_key.strip(),
            "clienttype": "binanceSkill",
            "Content-Type": "application/json",
        }
        payload = {
            "bodyTextOnly": content,
        }

        delays = [5, 15, 60]
        last_error_code = None
        last_error_msg = None

        for attempt in range(1, settings.square_max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(SQUARE_OPENAPI_URL, headers=headers, json=payload)
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("success") is True or data.get("code") == "000000":
                            post_id = str(data.get("data", {}).get("id") or "PUBLISHED")
                            return SquarePostResult(
                                success=True,
                                status=SquareStatus.SUCCESS,
                                post_id=post_id,
                                content=content,
                            )
                        else:
                            last_error_code = str(data.get("code", "API_REJECTED"))
                            last_error_msg = str(data.get("message", "Unknown error from Binance Square"))
                    else:
                        last_error_code = f"HTTP_{resp.status_code}"
                        last_error_msg = f"HTTP error {resp.status_code}"

            except (httpx.TimeoutException, httpx.NetworkError, httpx.RequestError) as exc:
                last_error_code = "NETWORK_ERROR"
                last_error_msg = str(exc)

            if attempt < settings.square_max_retries:
                delay = delays[min(attempt - 1, len(delays) - 1)]
                log.warning(
                    "[SQUARE] Attempt %d/%d failed (%s: %s). Retrying in %ds...",
                    attempt,
                    settings.square_max_retries,
                    last_error_code,
                    last_error_msg,
                    delay,
                )
                await asyncio.sleep(delay)

        log.error(
            "[SQUARE] FAILED to publish after %d attempts (code=%s, error=%s)",
            settings.square_max_retries,
            last_error_code,
            last_error_msg,
        )
        return SquarePostResult(
            success=False,
            status=SquareStatus.FAILED,
            error_code=last_error_code,
            error_message=last_error_msg,
            content=content,
        )



