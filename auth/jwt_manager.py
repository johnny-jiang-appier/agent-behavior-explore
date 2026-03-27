"""JWT management: fake or real (via headless Chrome)."""

import logging
from pathlib import Path

import jwt

logger = logging.getLogger(__name__)

_PROFILE_DIR = Path(__file__).resolve().parent.parent / ".auth_profile"


def generate_fake_jwt(email: str) -> str:
    """Generate a fake JWT that passes verify_signature=False checks."""
    return jwt.encode(
        {
            "email": email,
            "sub": "test",
            "https://appier.com": {"email": email},
        },
        "fake-secret",
        algorithm="HS256",
    )


def get_real_jwt(frontend_url: str = "http://localhost:8778/") -> str:
    """Open headless Chrome with saved profile, intercept JWT from request headers."""
    from playwright.sync_api import sync_playwright

    if not _PROFILE_DIR.exists():
        raise RuntimeError(
            f"Auth profile not found at {_PROFILE_DIR}. "
            "Run 'python -m auth.refresh_jwt' to log in first."
        )

    captured_jwt = None

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(_PROFILE_DIR),
            channel="chrome",
            headless=True,
            args=["--disable-session-crashed-bubble"],
            ignore_default_args=["--enable-automation"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def on_request(req):
            nonlocal captured_jwt
            auth = req.headers.get("authorization", "")
            if auth.startswith("Bearer ") and not captured_jwt:
                captured_jwt = auth[7:]

        page.on("request", on_request)
        page.goto(frontend_url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(8_000)
        ctx.close()

    if not captured_jwt:
        raise RuntimeError("Failed to capture JWT from frontend. Session may have expired. Run 'python -m auth.refresh_jwt'.")

    logger.info("Captured real JWT (%d chars)", len(captured_jwt))
    return captured_jwt


def get_jwt(use_real: bool, email: str) -> str:
    """Get a JWT token based on config."""
    if use_real:
        return get_real_jwt()
    return generate_fake_jwt(email)
