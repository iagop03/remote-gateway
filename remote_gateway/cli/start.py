"""`python -m remote_gateway start` — starts the server via the same config
Settings() would load from the environment/.env, without needing to remember
the uvicorn invocation and module path by hand."""
import uvicorn

from ..config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run("remote_gateway.main:app", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
