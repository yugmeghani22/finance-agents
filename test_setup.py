"""Smoke test: checks that the Claude API and SEC EDGAR access both work."""

import os

from dotenv import load_dotenv

load_dotenv()

PLACEHOLDER_KEY = "your-anthropic-api-key-here"
PLACEHOLDER_AGENT = "Your Name your.email@example.com"


def test_claude() -> bool:
    print("--- Claude API ---")
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key or key == PLACEHOLDER_KEY:
        print("SKIPPED: set ANTHROPIC_API_KEY in .env")
        return False

    import anthropic

    client = anthropic.Anthropic()
    try:
        response = client.messages.create(
            model="claude-opus-5-5",
            max_tokens=256,
            messages=[{"role": "user", "content": "In one sentence, what is a 10-K filing?"}],
        )
    except anthropic.AuthenticationError:
        print("FAILED: ANTHROPIC_API_KEY was rejected - check the key in .env")
        return False
    except anthropic.APIError as e:
        print(f"FAILED: {e}")
        return False

    if response.stop_reason == "refusal":
        print("FAILED: the model declined the request")
        return False
    text = "".join(b.text for b in response.content if b.type == "text")
    print(f"Claude says: {text}")
    return True


def test_edgar() -> bool:
    print("--- SEC EDGAR (edgartools) ---")
    agent = os.getenv("SEC_USER_AGENT", "")
    if not agent or agent == PLACEHOLDER_AGENT:
        print('SKIPPED: set SEC_USER_AGENT in .env (format: "Name email@example.com")')
        return False

    from edgar import Company, set_identity

    set_identity(agent)
    try:
        filing = Company("AXP").get_filings(form="10-K").latest()
    except Exception as e:
        print(f"FAILED: {e}")
        return False

    print(f"American Express latest 10-K filed on: {filing.filing_date}")
    return True


if __name__ == "__main__":
    results = {"Claude API": test_claude(), "SEC EDGAR": test_edgar()}
    print("--- Summary ---")
    for name, ok in results.items():
        print(f"{name}: {'OK' if ok else 'not working yet'}")
