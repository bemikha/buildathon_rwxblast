"""Run a few candidate tests against a patched sqlglot, inside this container.

Used only to prove that a small, cheap candidate set all PASSES, in which case the commit
can run nothing. Any doubt (patch does not apply, crash, timeout, unexpected output) returns
None and the caller keeps its static selection.
"""
import importlib.abc
import importlib.util
import json
import os
import re
import select
import signal
import sys
import time

BASE = os.environ.get("BR_SQLGLOT", "/opt/sqlglot")


def apply_patch(patch_text, base=BASE):
    """-> {relpath: new_text} for every file in the patch, or None if any hunk fails."""
    out = {}
    cur_path = None
    hunks = {}
    order = []
    for line in patch_text.split("\n"):
        if line.startswith("diff --git"):
            cur_path = None
            continue
        if line.startswith("--- "):
            continue
        if line.startswith("+++ "):
            p = line[4:].strip().split("\t")[0]
            if p == "/dev/null":
                return None
            cur_path = p[2:] if p.startswith("b/") else p
            hunks.setdefault(cur_path, [])
            order.append(cur_path)
            continue
        m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
        if m and cur_path is not None:
            hunks[cur_path].append([int(m.group(1)), []])
            continue
        if cur_path is not None and hunks[cur_path] and (line[:1] in (" ", "-", "+") or line == ""):
            hunks[cur_path][-1][1].append(line)
        elif line.startswith("\\"):
            continue
    for path in order:
        full = os.path.join(base, path)
        try:
            with open(full, encoding="utf-8") as f:
                src = f.read()
        except OSError:
            return None
        lines = src.split("\n")
        new = []
        pos = 0  # index into lines (0-based)
        for start, body in hunks[path]:
            # drop a trailing empty artefact line produced by the final newline of the patch
            while body and body[-1] == "":
                body = body[:-1]
            old_seq = [l[1:] if l else "" for l in body if l[:1] in (" ", "-") or l == ""]
            new_seq = [l[1:] if l else "" for l in body if l[:1] in (" ", "+") or l == ""]
            s = start - 1 if start > 0 else 0
            if lines[s:s + len(old_seq)] != old_seq:
                return None
            new.extend(lines[pos:s])
            new.extend(new_seq)
            pos = s + len(old_seq)
        new.extend(lines[pos:])
        out[path] = "\n".join(new)
    return out


class _PatchedFinder(importlib.abc.MetaPathFinder):
    def __init__(self, sources, base):
        # module name -> (path, source)
        self.mods = {}
        for rel, text in sources.items():
            name = rel[:-3].replace("/", ".")
            is_pkg = name.endswith(".__init__")
            if is_pkg:
                name = name[: -len(".__init__")]
            self.mods[name] = (os.path.join(base, rel), text, is_pkg)

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in self.mods:
            return None
        fpath, text, is_pkg = self.mods[fullname]

        class Loader(importlib.abc.SourceLoader):
            def get_filename(self, name):
                return fpath

            def get_data(self, p):
                if p == fpath:
                    return text.encode("utf-8")
                with open(p, "rb") as f:
                    return f.read()

            def path_stats(self, p):
                raise OSError  # never use a cached .pyc for the patched file

        spec = importlib.util.spec_from_file_location(
            fullname, fpath, loader=Loader(),
            submodule_search_locations=[os.path.dirname(fpath)] if is_pkg else None,
        )
        return spec


class _Collector:
    def __init__(self):
        self.failed = set()
        self.passed = set()
        self.collect_errors = False

    def pytest_runtest_logreport(self, report):
        nid = report.nodeid.split("[")[0]
        if report.failed:
            self.failed.add(nid)
        elif report.when == "call" and report.passed:
            self.passed.add(nid)

    def pytest_collectreport(self, report):
        if report.failed:
            self.collect_errors = True


class _InlinePool:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            initializer(*initargs)

    def submit(self, fn, *args, **kwargs):
        import concurrent.futures as cf
        import pickle
        f = cf.Future()
        try:
            fn2, a2, k2 = pickle.loads(pickle.dumps((fn, args, kwargs)))
            f.set_result(pickle.loads(pickle.dumps(fn2(*a2, **k2))))
        except BaseException as e:  # noqa: BLE001
            f.set_exception(e)
        return f

    def map(self, fn, *its, **kw):
        return map(fn, *its)

    def shutdown(self, wait=True, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _child(sources, nodeids, wfd, per_test_timeout):
    try:
        import concurrent.futures as cf
        import concurrent.futures.process as cfp
        cf.ProcessPoolExecutor = _InlinePool
        cfp.ProcessPoolExecutor = _InlinePool
        os.environ["SKIP_INTEGRATION"] = "0"
        os.chdir(BASE)
        sys.path.insert(0, BASE)
        sys.meta_path.insert(0, _PatchedFinder(sources, BASE))
        import pytest
        col = _Collector()
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        args = ["-q", "-p", "no:cacheprovider", "-p", "no:xdist", "-p", "no:randomly",
                "--timeout", str(per_test_timeout), "-o", "addopts=", "--rootdir", BASE] + list(nodeids)
        rc = pytest.main(args, plugins=[col])
        res = {"rc": int(rc), "failed": sorted(col.failed), "passed": sorted(col.passed),
               "collect_errors": col.collect_errors}
        os.write(wfd, json.dumps(res).encode())
    except BaseException as e:  # noqa: BLE001
        try:
            os.write(wfd, json.dumps({"error": repr(e)}).encode())
        except Exception:
            pass
    finally:
        os._exit(0)


def run_tests(patch_text, nodeids, wall_timeout=20.0, per_test_timeout=10):
    """-> ("pass" | "fail" | None, detail). None means: could not decide, keep static."""
    sources = apply_patch(patch_text)
    if sources is None:
        return None, "patch did not apply"
    if any(not (p.startswith("sqlglot/") and p.endswith(".py")) for p in sources):
        return None, "touches files outside sqlglot/*.py"
    import pytest  # noqa: F401  (imported once in the parent, shared with every forked child)
    sys.stdout.flush(); sys.stderr.flush()
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        _child(sources, nodeids, w, per_test_timeout)
    os.close(w)
    buf = b""
    deadline = time.monotonic() + wall_timeout
    timed_out = False
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            timed_out = True
            break
        ready, _, _ = select.select([r], [], [], left)
        if not ready:
            continue
        chunk = os.read(r, 1 << 20)
        if not chunk:
            break
        buf += chunk
    os.close(r)
    if timed_out:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        os.waitpid(pid, 0)
    except OSError:
        pass
    if timed_out:
        return None, "timeout"
    try:
        res = json.loads(buf.decode() or "{}")
    except ValueError:
        return None, "bad child output"
    if "error" in res or "rc" not in res:
        return None, res.get("error", "no result")
    if res["collect_errors"]:
        return "fail", "collection error"
    if res["failed"]:
        return "fail", res["failed"]
    # every requested test must have been seen passing
    want = {n.split("[")[0] for n in nodeids}
    if not want <= set(res["passed"]):
        return None, "not all tests ran: %s" % sorted(want - set(res["passed"]))[:3]
    if res["rc"] != 0:
        return None, "pytest rc %s" % res["rc"]
    return "pass", ""


_CPU = None


def estimate_cpu(nodeids, scale=1.5, child_overhead=0.35):
    """Rough CPU seconds to run these tests here (measured in-process at build time, scaled up)."""
    global _CPU
    if _CPU is None:
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "cpu_est.json")) as f:
                _CPU = json.load(f)
        except OSError:
            _CPU = {"test": {}, "class": {}}
    t = sum(_CPU["test"].get(n, 0.004) for n in nodeids)
    classes = {n.rsplit("::", 1)[0] for n in nodeids}
    t += sum(_CPU["class"].get(c, 0.0) for c in classes)
    return child_overhead + scale * t
