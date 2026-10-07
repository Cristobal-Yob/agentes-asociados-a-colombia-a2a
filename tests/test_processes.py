from scripts.smoke import run_probe


async def test_independent_processes_and_real_http_a2a():
    report = await run_probe(samples=2)
    assert report["latency"]["classroom_concurrency_8"]["n"] == 8
