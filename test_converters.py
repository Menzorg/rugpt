"""
Send 3 spreadsheet files to two conversion APIs and compare results.

Usage:
  python test_converters.py                        # all 6 requests in parallel
  python test_converters.py --sequentially         # one request at a time
  python test_converters.py --loop 5               # repeat the batch 5 times
  python test_converters.py --only unoserver       # target only unoserver
  python test_converters.py --only officeconverter # target only officeconverter
  python test_converters.py --only unoserver --loop 10 --sequentially

Converted files are saved to ./converted/<api>/<original_stem>.xlsx
"""

import argparse
import asyncio
import aiohttp
import time
from pathlib import Path

UNOSERVER_URL = "http://192.168.1.48:8123/request"        # field: file + convert-to
OFFICECONVERTER_URL = "http://192.168.1.48:8345/lool/convert-to/xlsx"  # field: data

ROOT = Path(__file__).parent
OUTPUT_DIR = ROOT / "converted"
FILES = [
    ROOT / "report_csv.csv",
    ROOT / "report_ods.ods",
    ROOT / "report_xls.xls",
    ROOT / "nums.csv",
    ROOT / "report_xlsm.xlsm",
]


async def send_unoserver(session: aiohttp.ClientSession, path: Path) -> dict:
    start = time.monotonic()
    try:
        data = aiohttp.FormData()
        data.add_field(
            "file",
            open(path, "rb"),
            filename=path.name,
            content_type="application/octet-stream",
        )
        data.add_field("convert-to", "xlsx")
        async with session.post(UNOSERVER_URL, data=data, timeout=aiohttp.ClientTimeout(total=300)) as resp:
            body = await resp.read()
            elapsed = time.monotonic() - start
            return {
                "api": "unoserver",
                "file": path.name,
                "stem": path.stem,
                "status": resp.status,
                "content_type": resp.content_type,
                "body": body,
                "bytes": len(body),
                "elapsed_s": round(elapsed, 2),
                "ok": resp.status == 200,
            }
    except Exception as exc:
        return {
            "api": "unoserver",
            "file": path.name,
            "stem": path.stem,
            "status": None,
            "error": str(exc),
            "body": b"",
            "bytes": 0,
            "elapsed_s": round(time.monotonic() - start, 2),
            "ok": False,
        }


async def send_officeconverter(session: aiohttp.ClientSession, path: Path) -> dict:
    start = time.monotonic()
    try:
        data = aiohttp.FormData()
        data.add_field(
            "data",
            open(path, "rb"),
            filename=path.name,
            content_type="application/octet-stream",
        )
        async with session.post(OFFICECONVERTER_URL, data=data, timeout=aiohttp.ClientTimeout(total=300)) as resp:
            body = await resp.read()
            elapsed = time.monotonic() - start
            return {
                "api": "officeconverter",
                "file": path.name,
                "stem": path.stem,
                "status": resp.status,
                "content_type": resp.content_type,
                "body": body,
                "bytes": len(body),
                "elapsed_s": round(elapsed, 2),
                "ok": resp.status == 200,
            }
    except Exception as exc:
        return {
            "api": "officeconverter",
            "file": path.name,
            "stem": path.stem,
            "status": None,
            "error": str(exc),
            "body": b"",
            "bytes": 0,
            "elapsed_s": round(time.monotonic() - start, 2),
            "ok": False,
        }


def save_result(r: dict) -> Path | None:
    if not r["ok"] or not r["body"]:
        return None
    out_dir = OUTPUT_DIR / r["api"]
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{r['stem']}.xlsx"
    out_path.write_bytes(r["body"])
    return out_path


def print_result(r: dict, saved_to: Path | None) -> None:
    tag = "OK" if r["ok"] else "FAIL"
    if r["ok"]:
        print(f"  [{tag}] {r['api']:<18} {r['file']:<45} status={r['status']}  {r['bytes']} bytes  {r['elapsed_s']}s  -> {saved_to}")
    else:
        err = r.get("error") or f"HTTP {r['status']}"
        print(f"  [{tag}] {r['api']:<18} {r['file']:<45} {err}  {r['elapsed_s']}s")


async def run_batch(session: aiohttp.ClientSession, sequentially: bool, only: str | None) -> list[dict]:
    senders = []
    if only != "officeconverter":
        senders += [(path, send_unoserver) for path in FILES]
    if only != "unoserver":
        senders += [(path, send_officeconverter) for path in FILES]

    results = []
    if sequentially:
        for path, fn in senders:
            r = await fn(session, path)
            saved_to = save_result(r)
            print_result(r, saved_to)
            results.append(r)
    else:
        results = list(await asyncio.gather(*(fn(session, path) for path, fn in senders)))
        for r in results:
            saved_to = save_result(r)
            print_result(r, saved_to)
    return results


async def run_flood(session: aiohttp.ClientSession, only: str | None, n: int) -> list[dict]:
    senders = []
    if only != "officeconverter":
        senders += [(path, send_unoserver) for path in FILES]
    if only != "unoserver":
        senders += [(path, send_officeconverter) for path in FILES]

    tasks = [fn(session, path) for path, fn in senders for _ in range(n)]
    print(f"Flooding {only or 'both APIs'} with {len(tasks)} concurrent requests ({len(senders)} files x {n})...\n")
    results = list(await asyncio.gather(*tasks))
    for r in results:
        saved_to = save_result(r)
        print_result(r, saved_to)
    return results


async def main(sequentially: bool, loop: int, only: str | None, flood: int | None) -> None:
    missing = [f for f in FILES if not f.exists()]
    if missing:
        for f in missing:
            print(f"File not found: {f}")
        return

    async with aiohttp.ClientSession() as session:
        if flood is not None:
            results = await run_flood(session, only, flood)
            passed = sum(1 for r in results if r["ok"])
            print(f"\n  {passed}/{len(results)} succeeded.")
            return

        mode = "sequentially" if sequentially else "simultaneously"
        target = only or "both APIs"
        total_passed = 0
        total_requests = 0

        for i in range(loop):
            if loop > 1:
                print(f"\n--- Iteration {i + 1}/{loop} ---")
            print(f"Sending {len(FILES)} files to {target} {mode}...\n")
            results = await run_batch(session, sequentially, only)
            passed = sum(1 for r in results if r["ok"])
            total_passed += passed
            total_requests += len(results)
            print(f"\n  {passed}/{len(results)} succeeded.")

        if loop > 1:
            print(f"\n=== Total: {total_passed}/{total_requests} succeeded across {loop} iterations ===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sequentially", action="store_true", help="Send requests one at a time instead of all at once")
    parser.add_argument("--loop", type=int, default=1, metavar="N", help="Repeat the batch N times (default: 1)")
    parser.add_argument("--only", choices=["unoserver", "officeconverter"], help="Target only one API")
    parser.add_argument("--flood", type=int, metavar="N", help="Fire all files N times simultaneously in a single asyncio.gather burst")
    args = parser.parse_args()
    asyncio.run(main(args.sequentially, args.loop, args.only, args.flood))
