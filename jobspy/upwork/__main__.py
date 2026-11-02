"""``python -m jobspy.upwork <login|tools|search|watch>``"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m jobspy.upwork", description=__doc__
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser(
        "login", help="authorize your Upwork account (opens a browser, once)"
    )
    sub.add_parser("tools", help="list the MCP server's tools and their input schemas")
    search = sub.add_parser("search", help="run one job search and print the results")
    search.add_argument("term")
    search.add_argument("-n", type=int, default=10)
    watch = sub.add_parser("watch", help="poll for new jobs and send alerts")
    watch.add_argument("terms", nargs="+", help="one or more search terms")
    watch.add_argument("--interval", type=int, default=600, help="seconds (min 60)")
    watch.add_argument("-n", type=int, default=20, help="results fetched per term")
    watch.add_argument("--notify-existing", action="store_true",
                       help="also alert on jobs already listed at first run")  # fmt: skip
    args = parser.parse_args()

    if args.cmd == "login":
        return asyncio.run(_login())
    if args.cmd == "tools":
        return asyncio.run(_tools())
    if args.cmd == "search":
        from jobspy import scrape_jobs

        jobs = scrape_jobs(
            site_name="upwork", search_term=args.term, results_wanted=args.n
        )
        if err := jobs.attrs["errors"].get("upwork"):
            print(f"error: {err}", file=sys.stderr)
            return 1
        print(
            jobs[
                ["title", "date_posted", "min_amount", "max_amount", "job_url"]
            ].to_string()
        )
        return 0
    from jobspy.upwork.watch import watch as run_watch

    run_watch(args.terms, args.interval, args.n, args.notify_existing)
    return 0


async def _login() -> int:
    from jobspy.upwork import connect, list_all_tools

    async with connect(interactive=True) as client:
        tools = await list_all_tools(client)
    print(f"Logged in. The server offers {len(tools)} tools. Tokens saved.")
    return 0


async def _tools() -> int:
    from jobspy.upwork import connect, list_all_tools, pick_search_tool

    async with connect(interactive=False) as client:
        tools = await list_all_tools(client)
    try:
        chosen = pick_search_tool(tools).name
    except RuntimeError:
        chosen = None
    for tool in tools:
        mark = "  <== will be used for job search" if tool.name == chosen else ""
        print(f"\n# {tool.name}{mark}\n{tool.description or ''}")
        print(json.dumps(tool.input_schema, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
