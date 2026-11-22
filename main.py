"""Entry point: read /data, print one JSON line per commit."""
import json, os, re, sys, time
t0 = time.process_time()
DATA = os.environ.get("BR_DATA", "/data")
import select_tests as S

DYN = os.environ.get("BR_DYN", "1") == "1"
DYN_MAX_EST = 0.9          # CPU seconds we are willing to spend proving one commit harmless
DYN_MAX_FRACTION = 0.45    # ... and at most this share of what running its tests would cost
DYN_BUDGET_WALL = 60.0     # stop trying after this much wall time in total

def split(items, k):
    ws = [[] for _ in range(k)]; loads = [0] * k
    for ms, n in sorted(items, reverse=True):
        j = loads.index(min(loads)); ws[j].append(n); loads[j] += ms
    return ws, (max(loads) if items else 0)

def best_split(items):
    if not items: return []
    best = None
    for k in range(1, 9):
        ws, mx = split(items, k)
        cost = mx + 2000 * k
        if best is None or cost < best[0]: best = (cost, ws)
    return [w for w in best[1] if w]

wall0 = time.monotonic()


def static_cost(workers, by_num):
    if not workers:
        return 0.0
    return max(sum(round(by_num[n]["seconds"] * 1000) for n in w) for w in workers) / 1000 + 2 * len(workers)


def maybe_run_nothing(cid, text, sel, workers, by_num):
    """If running the few candidate tests here is cheap and they all pass, run nothing."""
    import dynverify as D
    est = D.estimate_cpu(sel)
    cost = static_cost(workers, by_num)
    if est > DYN_MAX_EST or est > DYN_MAX_FRACTION * cost:
        return workers
    verdict, detail = D.run_tests(text, sel, wall_timeout=max(8.0, 6 * est), per_test_timeout=8)
    print(f"{cid}: dynamic {verdict} est={est:.2f} static={cost:.2f} {str(detail)[:80]}", file=sys.stderr)
    return [] if verdict == "pass" else workers


# --- heavy-test proxy -------------------------------------------------------------------
# A few tests (test_max_nodes ~23 s, test_execute_tpcds ~22 s, ...) set the busiest worker of
# any commit that selects them. Run the cheap part of the selection here instead: if all of it
# passes, assume the edit is harmless for the heavy tests too and drop them (and the passing
# cheap tests). Memory / retention tests check something the cheap tests cannot, so they are
# never dropped this way.
PROXY = os.environ.get("BR_PROXY", "0") == "1"  # off: on mutants the cheap part passed while test_execute_tpcds failed
HEAVY_MIN_S = float(os.environ.get("BR_HEAVY_MIN", "2.0"))     # recorded seconds = "heavy"
PROXY_MAX_EST = float(os.environ.get("BR_PROXY_EST", "1.5"))   # CPU s we spend per commit
PROXY_MIN_SAVING = 6.0                                          # simulated s it must save
NEVER_PROXY = re.compile(os.environ.get("BR_NEVER_PROXY", r"memory|retain"))


def maybe_drop_heavy(cid, text, sel, workers, by_id, by_num):
    heavy = [t for t in sel if by_id[t]["seconds"] >= HEAVY_MIN_S]
    if not heavy:
        return workers
    keep = [t for t in heavy if NEVER_PROXY.search(t)]
    light = [t for t in sel if t not in heavy]
    if not light:
        return workers  # nothing cheap covers the edit: no proxy to run
    import dynverify as D
    est = D.estimate_cpu(light)
    after = best_split([(round(by_id[t]["seconds"] * 1000), by_id[t]["number"]) for t in keep])
    saving = static_cost(workers, by_num) - static_cost(after, by_num)
    if est > PROXY_MAX_EST or saving < PROXY_MIN_SAVING + est:
        return workers
    verdict, detail = D.run_tests(text, light, wall_timeout=max(10.0, 6 * est), per_test_timeout=8)
    print(f"{cid}: proxy {verdict} light={len(light)} est={est:.2f} drop={len(heavy) - len(keep)} "
          f"saving={saving:.1f} {str(detail)[:80]}", file=sys.stderr)
    return after if verdict == "pass" else workers


def main():
    tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    by_id = {t["id"]: t for t in tests}
    by_num = {t["number"]: t for t in tests}
    zero_ms = [t["number"] for t in tests if round(t["seconds"] * 1000) == 0]
    cdir = os.path.join(DATA, "commits")
    out = []
    for fn in sorted(os.listdir(cdir)):
        if not fn.endswith(".patch"): continue
        cid = fn[:-6]
        text = open(os.path.join(cdir, fn), encoding="utf-8", errors="replace").read()
        try:
            sel = S.select(text)
        except Exception as e:  # never crash: fall back to everything
            print(f"{cid}: selector error {e!r}", file=sys.stderr)
            sel = [t["id"] for t in tests]
        items = [(round(by_id[t]["seconds"] * 1000), by_id[t]["number"]) for t in sel if t in by_id]
        workers = best_split(items)
        if workers and DYN and time.monotonic() - wall0 < DYN_BUDGET_WALL:
            try:
                workers = maybe_run_nothing(cid, text, sel, workers, by_num)
            except Exception as e:  # any doubt: keep the static selection
                print(f"{cid}: dynamic check error {e!r}", file=sys.stderr)
        if workers and PROXY and time.monotonic() - wall0 < DYN_BUDGET_WALL:
            try:
                workers = maybe_drop_heavy(cid, text, sel, workers, by_id, by_num)
            except Exception as e:  # any doubt: keep the static selection
                print(f"{cid}: proxy error {e!r}", file=sys.stderr)
        if workers:
            # tests recorded at 0 ms cost nothing once a worker exists: free insurance
            have = {n for w in workers for n in w}
            workers[-1].extend(n for n in zero_ms if n not in have)
        out.append(json.dumps({"commit": cid, "workers": workers}))
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))
    print(f"cpu {time.process_time() - t0:.2f}s", file=sys.stderr)

if __name__ == "__main__":
    main()
