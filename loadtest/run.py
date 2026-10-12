"""Load-test driver. Reproduces loadtest/RESULTS.md from one command:

    uv run python loadtest/run.py                 # full sweep: 1, 2, 3 replicas + failover test
    uv run python loadtest/run.py --replicas 1 --rates 100,200 --duration 10s   # quick check
    uv run python loadtest/run.py --report-only   # rebuild RESULTS.md from results/*.json
    uv run python loadtest/run.py --overload-only # 1 replica past saturation, shedding off vs on

For each replica count: scale `api`, restart nginx (it resolves replicas at startup), create a
high-limit key via the admin API, then step the arrival rate until the gateway can't keep up.
CPU per container is sampled mid-run to locate the bottleneck.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "loadtest" / "results"
COMPOSE = [
    "docker",
    "compose",
    "-f",
    "docker-compose.yml",
    "-f",
    "loadtest/docker-compose.loadtest.yml",
]
GATEWAY = "http://localhost:8000"
DEFAULT_RATES = "50,60,70,80,90,100,115,130,145,160,175,190,205,220"
# A step counts as "sustained" if the gateway kept up with the offered load, almost everything
# succeeded, and tail latency stayed sane (the mock adds a fixed 100ms).
MIN_ACHIEVED, MIN_SUCCESS, MAX_P99_MS = 0.95, 0.99, 1000.0
# Overload test: one replica (saturates at ~80 RPS) driven well past saturation.
OVERLOAD_RATES = (60, 80, 100, 130, 160, 200)
# In-flight cap per replica when shedding is on, picked by cap_study(): ~1.5x the in-flight count
# at saturation (Little's law: 80 RPS x 0.125 s = 10). See docs/decisions/0014.
OVERLOAD_CAP = 16
CAP_STUDY = ((8, 16, 32, 64), (80, 130, 200))


def sh(args: list[str], capture: bool = False) -> str:
    result = subprocess.run(args, cwd=ROOT, check=True, capture_output=capture, text=True)
    return result.stdout if capture else ""


def admin_token() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("ADMIN_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("ADMIN_TOKEN must be set in .env")


def scale(replicas: int, max_in_flight: int | None = None) -> None:
    print(f"\n=== scaling to {replicas} replica(s) ===", flush=True)
    # Compose interpolates MAX_IN_FLIGHT into the overlay; a changed value recreates the replicas.
    if max_in_flight is None:
        os.environ.pop("MAX_IN_FLIGHT", None)
    else:
        os.environ["MAX_IN_FLIGHT"] = str(max_in_flight)
    sh([*COMPOSE, "up", "-d", "--scale", f"api={replicas}", "mock-llm", "api", "nginx"])
    deadline = time.time() + 180
    while time.time() < deadline:
        out = sh([*COMPOSE, "ps", "api", "--format", "{{.Health}}"], capture=True).split()
        if len(out) == replicas and all(h == "healthy" for h in out):
            break
        time.sleep(2)
    else:
        raise SystemExit(f"api replicas not healthy: {out}")
    sh([*COMPOSE, "restart", "nginx"])
    for _ in range(60):
        try:
            if httpx.get(f"{GATEWAY}/readyz", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise SystemExit("gateway not ready through nginx")


def create_key() -> str:
    response = httpx.post(
        f"{GATEWAY}/admin/keys",
        headers={"Authorization": f"Bearer {admin_token()}"},
        json={
            "name": f"loadtest-{int(time.time())}",
            # Limits high enough never to reject, but set so the limiter paths do real work.
            "rpm_limit": 10_000_000,
            "tpm_limit": 1_000_000_000,
            "monthly_spend_cap_usd": "100000",
            "allowed_models": ["gpt-4o-mini"],  # mock only: no fallback can reach a real provider
        },
        timeout=10,
    )
    response.raise_for_status()
    return str(response.json()["key"])


class StatsSampler(threading.Thread):
    """Samples `docker stats` once, `delay_s` into a run."""

    def __init__(self, delay_s: float) -> None:
        super().__init__(daemon=True)
        self.delay_s = delay_s
        self.cpu: dict[str, float] = {}

    def run(self) -> None:
        time.sleep(self.delay_s)
        out = sh(
            ["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.CPUPerc}}"], capture=True
        )
        for line in out.splitlines():
            name, cpu = line.split("\t")
            if name.startswith("project1-"):
                self.cpu[name.removeprefix("project1-")] = float(cpu.rstrip("%") or 0)


def run_k6(rate: int, duration: str, key: str, label: str, stream: bool = False) -> dict[str, Any]:
    out = f"/results/{label}.json"
    seconds = float(duration.rstrip("s"))
    sampler = StatsSampler(delay_s=seconds / 2 + 3)  # +3s: `compose run` startup
    sampler.start()
    sh([
        *COMPOSE, "run", "--rm", "-e", f"RATE={rate}", "-e", f"DURATION={duration}",
        "-e", f"KEY={key}", "-e", f"OUT={out}", "-e", f"STREAM={'1' if stream else '0'}",
        "k6", "run", "--quiet", "/loadtest/chat.js",
    ])  # fmt: skip
    sampler.join(timeout=30)
    summary = json.loads((RESULTS / f"{label}.json").read_text(encoding="utf-8"))
    return summarize(summary, rate, sampler.cpu)


def summarize(summary: dict[str, Any], rate: int, cpu: dict[str, float]) -> dict[str, Any]:
    m = summary["metrics"]

    def trend(name: str) -> dict[str, float]:
        values = m.get(name, {}).get("values", {})
        return {k: round(float(values.get(k, 0)), 2) for k in ("med", "p(95)", "p(99)")}

    achieved = float(m["http_reqs"]["values"]["rate"])
    success = float(m.get("checks", {}).get("values", {}).get("rate", 0))
    latency = trend("http_req_duration")
    total = float(m["http_reqs"]["values"]["count"]) or 1.0
    return {
        "target_rps": rate,
        "achieved_rps": round(achieved, 1),
        "success_rate": round(success, 4),
        "latency_ms": latency,
        "gateway_overhead_ms": trend("gateway_overhead_ms"),
        "ok_latency_ms": trend("http_req_duration{expected_response:true}"),
        "goodput_rps": round(achieved * success, 1),
        "shed_rate": round(float(m.get("shed", {}).get("values", {}).get("count", 0)) / total, 4),
        "dropped_iterations": int(
            m.get("dropped_iterations", {}).get("values", {}).get("count", 0)
        ),
        "cpu_percent": cpu,
        "sustained": achieved >= MIN_ACHIEVED * rate
        and success >= MIN_SUCCESS
        and latency["p(99)"] <= MAX_P99_MS,
    }


def sweep(replicas: int, rates: list[int], duration: str) -> dict[str, Any]:
    scale(replicas)
    key = create_key()
    run_k6(50, "5s", key, f"warmup-{replicas}")
    steps = []
    for rate in rates:
        step = run_k6(rate, duration, key, f"r{replicas}-{rate}")
        steps.append(step)
        overhead = step["gateway_overhead_ms"]
        print(
            f"  {replicas} replica(s) @ {rate:>5} rps -> achieved {step['achieved_rps']:>7} "
            f"ok {step['success_rate']:.3f} p99 {step['latency_ms']['p(99)']:>8}ms "
            f"overhead p50/p99 {overhead['med']}/{overhead['p(99)']}ms "
            f"{'OK' if step['sustained'] else 'SATURATED'}",
            flush=True,
        )
        if not step["sustained"]:
            break
    # The highest *offered* rate that was sustained. k6's achieved rate slightly overstates
    # (it divides by active time), so it isn't used as the headline number.
    best = max((float(s["target_rps"]) for s in steps if s["sustained"]), default=0.0)
    return {"replicas": replicas, "max_sustained_rps": best, "steps": steps}


def failover_test(rate: int = 100) -> dict[str, Any]:
    """3 replicas under steady load; stop one 10s in. nginx should route around it.
    The rate must be one the 2 surviving replicas can sustain, or this measures overload."""
    scale(3)
    key = create_key()
    victim = sh([*COMPOSE, "ps", "api", "-q"], capture=True).split()[0]

    def kill() -> None:
        time.sleep(13)  # ~10s into the k6 run
        subprocess.run(["docker", "stop", victim], check=True, capture_output=True)

    killer = threading.Thread(target=kill, daemon=True)
    killer.start()
    result = run_k6(rate, "30s", key, "failover")
    killer.join()
    subprocess.run(["docker", "start", victim], check=True, capture_output=True)
    print(f"  failover @ {rate} rps: success {result['success_rate']:.4f}", flush=True)
    return result


def streaming_test(rate: int = 40) -> dict[str, Any]:
    """At a rate one replica sustains, so overhead isn't queueing."""
    scale(1)
    key = create_key()
    result = run_k6(rate, "20s", key, "stream", stream=True)
    print(
        f"  streaming @ {rate} rps: overhead to first chunk {result['gateway_overhead_ms']}",
        flush=True,
    )
    return result


def overload_test() -> dict[str, Any]:
    """One replica, past saturation, with load shedding off and then on (same rates)."""
    runs: dict[str, list[dict[str, Any]]] = {}
    for label, cap in (("off", 0), ("on", OVERLOAD_CAP)):
        scale(1, max_in_flight=cap)
        key = create_key()
        run_k6(50, "5s", key, f"warmup-shed-{label}")
        runs[label] = []
        for rate in OVERLOAD_RATES:
            step = run_k6(rate, "20s", key, f"shed-{label}-{rate}")
            runs[label].append(step)
            print(
                f"  shedding {label:>3} @ {rate:>4} rps -> goodput {step['goodput_rps']:>6} "
                f"shed {step['shed_rate'] * 100:5.1f}% "
                f"ok-p99 {step['ok_latency_ms']['p(99)']:>8}ms "
                f"dropped {step['dropped_iterations']}",
                flush=True,
            )
    return {"replicas": 1, "cap": OVERLOAD_CAP, "runs": runs, "cap_study": cap_study()}


def cap_study() -> list[dict[str, Any]]:
    """Too low a cap leaves the replica idle; too high lets the queue (and latency) grow back."""
    caps, rates = CAP_STUDY
    rows = []
    for cap in caps:
        scale(1, max_in_flight=cap)
        key = create_key()
        run_k6(50, "5s", key, f"warmup-cap{cap}")
        for rate in rates:
            step = run_k6(rate, "20s", key, f"cap{cap}-{rate}")
            rows.append({"cap": cap, **step})
            print(
                f"  cap {cap:>3} @ {rate:>4} rps -> goodput {step['goodput_rps']:>6} "
                f"ok-p99 {step['ok_latency_ms']['p(99)']:>8}ms",
                flush=True,
            )
    return rows


def write_report(data: dict[str, Any]) -> None:
    from loadtest.report import render  # local import: report is plain formatting

    (ROOT / "loadtest" / "RESULTS.md").write_text(render(data), encoding="utf-8")
    print(f"wrote {ROOT / 'loadtest' / 'RESULTS.md'}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--replicas", default="1,2,3")
    parser.add_argument("--rates", default=DEFAULT_RATES)
    parser.add_argument("--duration", default="20s")
    parser.add_argument("--skip-extras", action="store_true", help="no failover/streaming tests")
    parser.add_argument("--no-build", action="store_true", help="use already-built images")
    parser.add_argument(
        "--overload-only", action="store_true", help="rerun the load-shedding test only"
    )
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument(
        "--extras-only", action="store_true", help="rerun streaming + failover, keep the sweeps"
    )
    args = parser.parse_args()
    results_file = RESULTS / "results.json"
    if args.report_only:
        write_report(json.loads(results_file.read_text(encoding="utf-8")))
        return
    RESULTS.mkdir(exist_ok=True)
    if not args.no_build:  # OneDrive checkouts: scripts/build-images.sh, then --no-build
        sh([*COMPOSE, "build", "api"])
    if args.overload_only:
        data = json.loads(results_file.read_text(encoding="utf-8"))
        data["overload"] = overload_test()
        _restore_and_save(data, results_file)
        return
    if args.extras_only:
        data = json.loads(results_file.read_text(encoding="utf-8"))
        data["streaming"], data["failover"] = streaming_test(), failover_test()
        _restore_and_save(data, results_file)
        return
    rates = [int(r) for r in args.rates.split(",")]
    data: dict[str, Any] = {
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "machine": {
            "cpus": os.cpu_count(),
            "docker": sh(
                ["docker", "info", "--format", "{{.NCPU}} CPUs, {{.MemTotal}} bytes"], capture=True
            ).strip(),
        },
        "duration": args.duration,
        "sweeps": [sweep(int(n), rates, args.duration) for n in args.replicas.split(",")],
    }
    if not args.skip_extras:
        data["streaming"] = streaming_test()
        data["failover"] = failover_test()
        data["overload"] = overload_test()
    _restore_and_save(data, results_file)


def _restore_and_save(data: dict[str, Any], results_file: Path) -> None:
    os.environ.pop("MAX_IN_FLIGHT", None)
    sh([*COMPOSE, "up", "-d", "--scale", "api=1", "api", "nginx"])
    sh([*COMPOSE, "restart", "nginx"])
    results_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    write_report(data)


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
