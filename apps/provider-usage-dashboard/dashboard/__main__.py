import argparse
import asyncio
import json
import logging
import os
from datetime import date, datetime, timezone

from .model import PROVIDERS, months_before


def main():
    parser = argparse.ArgumentParser(description="Independent provider reporting dashboard")
    parser.add_argument("command", choices=["serve", "sync", "demo"])
    parser.add_argument("--env-file", help="Explicit local environment file; never loaded automatically")
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--provider", choices=list(PROVIDERS), action="append")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8080")))
    args = parser.parse_args()
    if args.env_file:
        from dotenv import load_dotenv
        load_dotenv(args.env_file, override=False)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    # HTTP client logs include request URLs; source errors are sanitized explicitly.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.command == "sync":
        from .collector import run
        from .store import make_store
        today = datetime.now(timezone.utc).date()
        if args.end and not args.start:
            parser.error("--end requires --start")
        if args.start and (args.start < months_before(today, 13) or args.start > (args.end or today) or (args.end or today) > today):
            parser.error("Reporting dates must be ordered and within the retained 13 months.")
        print(json.dumps(asyncio.run(run(make_store(), start=args.start, end=args.end, selected=args.provider))))
    else:
        import uvicorn
        from .app import create_app
        store = None
        if args.command == "demo":
            if os.getenv("K_SERVICE"):
                parser.error("Demo data cannot run in the deployed service.")
            from .demo import demo_store
            store = demo_store()
        uvicorn.run(create_app(store, mode="demo" if args.command == "demo" else None),
                    host="0.0.0.0" if os.getenv("K_SERVICE") else "127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
