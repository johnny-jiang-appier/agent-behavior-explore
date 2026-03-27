"""CLI: Open Chrome for manual login to save .auth_profile."""

from pathlib import Path

from playwright.sync_api import sync_playwright

_PROFILE_DIR = Path(__file__).resolve().parent.parent / ".auth_profile"


def main():
    print("Opening Chrome for login...")
    print(f"Profile dir: {_PROFILE_DIR}")
    print("Log in manually, then CLOSE the browser tab.")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(_PROFILE_DIR),
            channel="chrome",
            headless=False,
            accept_downloads=True,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("http://localhost:8778/")

        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass

        ctx.close()
        print(f"\nProfile saved to: {_PROFILE_DIR}/")
        print("You can now run tests with USE_REAL_JWT=true")


if __name__ == "__main__":
    main()
