"""Interactive chat REPL against a running Remote Gateway instance.

Talks to /sessions + /sessions/{id}/messages the same way you'd use the
Anthropic Messages API, but routed through a local driver (Claude Code,
Ollama, LM Studio, vLLM) instead of api.anthropic.com.

Usage:
    python scripts/chat.py
    python scripts/chat.py --driver claude-code --model claude-code:opus
    python scripts/chat.py --driver ollama --model ollama:mistral
    python scripts/chat.py --dir C:\\path\\to\\project --token mysecret

While a turn is running, press Ctrl+C to interrupt it (SIGINT-style, not a
kill) instead of killing the whole REPL. Type /exit or /quit to leave.
"""
import argparse
import os
import sys

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description="Chat with a local LLM through Remote Gateway.")
    parser.add_argument("--base-url", default="http://127.0.0.1:9000")
    parser.add_argument("--driver", default="claude-code")
    parser.add_argument("--model", default="claude-code:default")
    parser.add_argument("--dir", default=os.getcwd(), help="working_directory for the session (default: cwd)")
    parser.add_argument("--token", default="")
    args = parser.parse_args()

    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}

    with httpx.Client(base_url=args.base_url, headers=headers, timeout=300) as client:
        try:
            response = client.post("/sessions", json={
                "driver": args.driver, "model": args.model, "working_directory": args.dir,
            })
            response.raise_for_status()
        except httpx.HTTPError as exc:
            print(f"Could not reach Remote Gateway at {args.base_url}: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc

        session_id = response.json()["session_id"]
        print(f"Session {session_id} ({args.driver}, {args.dir})")
        print("Type your message. /exit to quit. Ctrl+C interrupts the current turn.\n")

        try:
            _repl(client, args, session_id, headers)
        finally:
            try:
                client.post(f"/sessions/{session_id}/stop")
            except httpx.HTTPError:
                pass
            print("\nSession stopped.")


def _repl(client: httpx.Client, args: argparse.Namespace, session_id: str, headers: dict) -> None:
    while True:
        try:
            user_input = input("you> ").strip()
        except EOFError:
            return
        if not user_input:
            continue
        if user_input in ("/exit", "/quit"):
            return

        try:
            reply = client.post(f"/sessions/{session_id}/messages", json={
                "model": args.model, "messages": [{"role": "user", "content": user_input}],
            })
        except KeyboardInterrupt:
            try:
                httpx.post(f"{args.base_url}/sessions/{session_id}/interrupt", headers=headers, timeout=10)
            except httpx.HTTPError:
                pass
            print("\n[interrupted]\n")
            continue
        except httpx.HTTPError as exc:
            print(f"[request failed: {exc}]\n")
            continue

        if reply.status_code >= 400:
            print(f"[error {reply.status_code}: {reply.text}]\n")
            continue

        body = reply.json()
        text = "".join(block.get("text", "") for block in body.get("content", []))
        print(f"assistant> {text}\n")


if __name__ == "__main__":
    main()
