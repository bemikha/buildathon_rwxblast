"""Given a unified diff against the base sqlglot commit, pick the tests likely to fail.

Signals (all precomputed at image build time from the base commit):
  * per-test line coverage           -> changes inside function bodies
  * per-test class-attribute reads   -> changes to scalar class settings (e.g. SUPPORTS_X = True)
  * per-test table-key lookups       -> changes to entries of class-level dicts/sets
                                        (FUNCTIONS, TRANSFORMS, generator dispatch, token sets, ...)
  * test source text                 -> fallback for things the traces cannot see
"""
import os, pickle, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
import gzip
IDX = pickle.loads(gzip.decompress(open(os.path.join(HERE, "index.pkl.gz"), "rb").read()))
REPO = os.environ.get("SQLGLOT_REPO", os.path.join(HERE, "sqlglot"))
TESTS = IDX["tests"]; DURS = IDX["durs"]
LINEMAP = IDX["linemap"]; SPANS = IDX["spans"]
TSRC = IDX["test_src"]; TSPANS = IDX["test_spans"]
READS = IDX["reads"]; KEYS = IDX["keys"]; SUB = IDX["sub"]; CLS_USE = IDX["cls_use"]
STRUCT = IDX["structure"]; EXPR_KEYS = IDX["expr_keys"]
FILES = IDX["files"]; STRSPANS = IDX["strspans"]; STMTS = IDX["stmts"]; COVSRC = IDX["covsrc"]; ODEP = {f: set(v) for f, v in IDX.get("odep", {}).items()}; DOCSPANS = IDX.get("docspans", {})
EXPR_USE = IDX.get("expr_use", {}); EXPR_CLASSES = set(IDX.get("expr_classes", ()))


def expr_mask(cq):
    m = 0
    for d in subtree(cq):
        m |= EXPR_USE.get(d, 0)
    return m


IMPORTS = IDX["imports"]


def lines_in_file_mentioning(f, names):
    out = 0
    src = COVSRC.get(f, {})
    pat = re.compile(r"\b(" + "|".join(map(re.escape, names)) + r")\b")
    for ln, m in LINEMAP.get(f, {}).items():
        if pat.search(src.get(ln, "")): out |= m
    return out

import ast as _ast
METHOD_COV = {}
for _f, _sp in SPANS.items():
    _lm = LINEMAP.get(_f, {})
    for _a, _b, _n in _sp:
        _m = 0
        for _l in range(_a, _b + 1): _m |= _lm.get(_l, 0)
        METHOD_COV[_n] = METHOD_COV.get(_n, 0) | _m
MODULE_ASSIGNS = IDX.get("module_assigns", {})

# index (class, attr) -> list of (key, mask) for quick per-attr lookups
KEYS_BY_ATTR = {}
for (c, a, k), m in KEYS.items():
    KEYS_BY_ATTR.setdefault((c, a), []).append((k, m))


def parse_patch(text):
    files = {}; cur = None; o = 0; prev_path = None
    hunk_old = None
    for line in text.split("\n"):
        if line.startswith("diff --git"):
            cur = None; continue
        if line.startswith("--- "):
            prev_path = line[4:].strip().split("\t")[0]
            prev_path = prev_path[2:] if prev_path.startswith("a/") else prev_path
            continue
        if line.startswith("+++ "):
            p = line[4:].strip().split("\t")[0]
            p = p[2:] if p.startswith("b/") else p
            deleted = p == "/dev/null"
            if deleted:
                p = prev_path  # file deleted: attribute its lines to the old path
            cur = files.setdefault(p, {"old": set(), "ins": {}, "minus": {}, "plus": [], "pair": {}, "paired_ins": set()})
            cur["deleted"] = deleted
            run_minus = []; plus_seen = 0
            continue
        m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
        if m:
            o = int(m.group(1)); run_minus = []; plus_seen = 0; continue
        if cur is None:
            continue
        if line.startswith("-"):
            if not run_minus or plus_seen: run_minus = []; plus_seen = 0
            cur["old"].add(o); cur["minus"][o] = line[1:]; run_minus.append(o); o += 1
        elif line.startswith("+"):
            # inserted before old line o
            cur["ins"].setdefault(o, []).append(line[1:]); cur["plus"].append(line[1:])
            if run_minus and plus_seen < len(run_minus):
                cur["pair"][run_minus[plus_seen]] = line[1:]
                cur["paired_ins"].add((o, len(cur["ins"][o]) - 1))
            plus_seen += 1
        elif line.startswith(" ") or line == "":
            o += 1; run_minus = []; plus_seen = 0
    return files


def items_at(f, ln):
    """innermost structural items (class / attr / method) containing old line ln"""
    out = []
    for a, b, kind, info in STRUCT.get(f, ()):
        if a <= ln <= b:
            out.append((a, b, kind, info))
    return out


def subtree(cq):
    return SUB.get(cq, [cq])


IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
STR = re.compile(r"\"([^\"]{2,})\"|'([^']{2,})'")
STOP = set("self cls return None True False exp expression expressions this args kwargs def class "
           "import from elif else pass and not for while with lambda isinstance super TokenType "
           "seq_get build_formatted_time Dialect Parser Generator Tokenizer".split())


ENUMS = set(IDX.get("enums", ()))
_ENUM_ALT = None


def _norm_eq(s):
    global _ENUM_ALT
    if _ENUM_ALT is None:
        names = "|".join(sorted(map(re.escape, ENUMS))) or "NoEnum"
        _ENUM_ALT = re.compile(r"(?:\b(?:exp\.|tokens\.)?(?:%s)\.[A-Z][A-Z0-9_]*\b|\bNone\b)" % names)
    # `x is not None` == `x != None`; `x is Enum.MEMBER` == `x == Enum.MEMBER`
    def sub(m):
        return m.group(0)
    s = re.sub(r"\bis not\s+(?=(%s))" % _ENUM_ALT.pattern, "!= ", s)
    s = re.sub(r"\bis\s+(?=(%s))" % _ENUM_ALT.pattern, "== ", s)
    s = re.sub(r"(%s)\s+is not\b" % _ENUM_ALT.pattern, r"\1 !=", s)
    s = re.sub(r"(%s)\s+is\b" % _ENUM_ALT.pattern, r"\1 ==", s)
    return s


def equivalent(a, b):
    """True when two versions of a line cannot behave differently (identity vs equality
    against None or an enum member, or only whitespace)."""
    if a == b or a.split() == b.split():
        return True
    return _norm_eq(a).split() == _norm_eq(b).split()


def key_of_text(s):
    s = s.strip()
    m = re.match(r"(?:exp|TokenType|t)\.([A-Za-z_][A-Za-z0-9_]*)\s*[:,]?", s)
    if m: return m.group(1)
    m = re.match(r"[\"']([^\"']+)[\"']\s*[:,]", s)
    if m: return m.group(1)
    m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*[:,]", s)
    if m and not s.startswith(("def ", "return", "if ")): return m.group(1)
    return None


def key_mask(cq, attr, key):
    m = 0
    names = {key, key.upper()} if key else set()
    for d in subtree(cq):
        if key is None:
            m |= READS.get((d, attr), 0)
            continue
        for n in names:
            m |= KEYS.get((d, attr, n), 0)
            if attr == "TRANSFORMS":
                m |= KEYS.get((d, "__dispatch__", n), 0)
    return m


def attr_mask(cq, attr):
    m = 0
    for d in subtree(cq):
        m |= READS.get((d, attr), 0)
    return m


def class_use(cq):
    m = 0
    for d in subtree(cq):
        m |= CLS_USE.get(d, 0)
    return m


def source_lines_mentioning(names):
    out = 0
    if not names: return 0
    pat = re.compile(r"\b(" + "|".join(map(re.escape, names)) + r")\b")
    for f, d in LINEMAP.items():
        src = COVSRC.get(f, {})
        for ln, m in d.items():
            if pat.search(src.get(ln, "")): out |= m
    return out


def tests_mentioning(tokens):
    out = 0
    toks = [t for t in tokens if len(t) >= 3]
    if not toks: return 0
    pat = re.compile(r"(?i)(?<![A-Za-z0-9_])(" + "|".join(map(re.escape, toks)) + r")(?![A-Za-z0-9_])")
    for i, s in TSRC.items():
        if pat.search(s): out |= 1 << i
    return out


def fallback(lines, scope_mask=None):
    idents = {w for l in lines for w in IDENT.findall(l.split("#")[0])} - STOP
    strs = {a or b for l in lines for a, b in STR.findall(l)}
    sql_tokens = {s for s in strs if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_ ]*", s)}
    m = source_lines_mentioning(sorted(idents))
    m |= tests_mentioning(sorted(sql_tokens | {i for i in idents if i[:1].isupper()}))
    if scope_mask is not None:
        m &= scope_mask
    return m


def select_mask(patch_text):
    files = parse_patch(patch_text)
    sel = 0
    for f, info in files.items():
        if f.startswith("tests/") and f.endswith(".py"):
            touched = set(info["old"]) | set(info["ins"])
            hit = False
            for a, b, i in TSPANS.get(f, ()):
                if any(a <= l <= b for l in touched): sel |= 1 << i; hit = True
            if not hit:
                for a, b, i in TSPANS.get(f, ()): sel |= 1 << i
            continue
        if f.startswith("tests/"):
            m = FILES.get(f, 0)
            if not m:
                base = os.path.basename(f).rsplit(".", 1)[0]
                m = tests_mentioning([base])
            sel |= m
            continue
        if not (f.startswith("sqlglot/") and f.endswith(".py")):
            continue
        lm = LINEMAP.get(f, {})
        if info.get("deleted"):
            for m in lm.values(): sel |= m
            mod = f[:-3].replace("/", ".")
            for c, m in CLS_USE.items():
                if c.startswith(mod + "."): sel |= m
            continue
        # events: (old line, text, is_insert)
        equiv = {ln for ln, p in info["pair"].items() if equivalent(info["minus"][ln], p)}
        events = [(ln, txt, False) for ln, txt in info["minus"].items() if ln not in equiv]
        for ln, txts in info["ins"].items():
            for j, t in enumerate(txts):
                if (ln, j) in info["paired_ins"]:
                    continue  # the removed line it replaces stands for it
                events.append((ln, t, True))
        # contiguous inserted blocks that define a brand-new def/class/import are inert on their own:
        # their behaviour only matters through call sites, which show up as other changed lines.
        inert = set()
        for ln, txts in info["ins"].items():
            block_kind = None; base_indent = None; in_doc = False
            for j, t in enumerate(txts):
                st = t.strip()
                ind = len(t) - len(t.lstrip())
                if not st:
                    continue
                if block_kind and ind > base_indent:
                    inert.add((ln, j)); continue
                block_kind = None
                if re.match(r"(async\s+)?def\s|class\s|@", st) or st.startswith(("import ", "from ")):
                    block_kind = "def"; base_indent = ind; inert.add((ln, j)); continue
        # docstring / comment lines
        def is_doc(t):
            st = t.strip()
            return st.startswith(("#", '"""', "'''")) or st.endswith('"""')
        for ln, txt, is_ins in events:
            stripped = txt.strip()
            if any(a <= ln <= b for a, b in DOCSPANS.get(f, ())) and (not is_ins or any(a < ln <= b for a, b in DOCSPANS.get(f, ()))):
                continue  # docstring text: nothing reads it
            if any(a < ln <= b for a, b in STRSPANS.get(f, ())):
                # text added/removed inside a multi-line string literal (SQL templates, docstrings)
                for a, b in STRSPANS.get(f, ()):
                    if a < ln <= b:
                        m = 0
                        for l2 in range(a, b + 1): m |= lm.get(l2, 0)
                        enc = [x for x in SPANS.get(f, ()) if x[0] <= a <= x[1]]
                        if not m and enc:
                            for l2 in range(enc[-1][0], enc[-1][1] + 1): m |= lm.get(l2, 0)
                        sel |= m
                        break
                continue
            if not stripped or stripped.startswith("#") or is_doc(txt):
                continue
            if is_ins and (ln, info["ins"][ln].index(txt)) in inert:
                md = re.match(r"\s*def\s+([A-Za-z_0-9]+)", txt)
                its0 = items_at(f, max(ln - 1, 1))
                cls = [x for x in its0 if x[2] == "class" and x[0] <= ln - 1 and ln <= x[1] + 1]
                if md and cls:
                    cq = cls[-1][3]; name = md.group(1)
                    if name.endswith("_sql") and name[:-4] in EXPR_KEYS:
                        sel |= key_mask(cq, "TRANSFORMS", EXPR_KEYS[name[:-4]])
                    else:
                        # overriding an inherited method: tests that ran the inherited one via this class
                        base_m = METHOD_COV.get(name, 0)
                        sel |= base_m & class_use(cq)
                continue
            if not is_ins and re.match(r"\s*(async\s+)?def\s|\s*@", txt):
                for a, b, _ in SPANS.get(f, ()):
                    if a <= ln <= b:
                        for l2 in range(a, b + 1): sel |= lm.get(l2, 0)
                        break
                continue
            imp_hits = [x for x in IMPORTS.get(f, ()) if x[3] <= ln <= x[4]] if not is_ins else []
            if imp_hits or (not is_ins and stripped.startswith(("import ", "from "))):
                # a removed or edited import: code in this file still using a dropped name raises NameError
                names = [x[2] for x in imp_hits if x[0] <= ln <= x[1]] or [x[2] for x in imp_hits]
                added = "\n".join(info["plus"])
                gone = [n for n in names if not re.search(r"\b%s\b" % re.escape(n), added)]
                if gone:
                    sel |= lines_in_file_mentioning(f, gone)
                continue
            its = items_at(f, ln if not is_ins else max(ln - 1, 1))
            if is_ins:
                # an insertion sits between old lines ln-1 and ln; use the item containing both if any
                its = [x for x in its if x[0] <= ln - 1 and ln <= x[1] + 1] or items_at(f, ln)
            inner = its[-1] if its else None
            # 1) inside a method / function body: line coverage
            in_func = any(a <= ln <= b for a, b, _ in SPANS.get(f, ()) if a < ln)
            if inner and inner[2] == "method" or in_func:
                cand = [ln] if not is_ins else [ln - 1, ln]
                m = 0
                od = ODEP.get(f, ())
                if any(c in od for c in cand):
                    # runs once (import time, caches, dispatch building): credit goes to whichever test ran
                    # first, so take everything the outermost enclosing function (and its closures) reaches
                    outer = [x for x in SPANS.get(f, ()) if x[0] <= ln <= x[1]]
                    if outer:
                        a0, b0, _ = min(outer, key=lambda x: x[0])
                        for l2 in range(a0, b0 + 1): m |= lm.get(l2, 0)
                for c in cand:
                    m |= lm.get(c, 0)
                    # the whole statement / condition the line belongs to (short-circuited lines are never 'covered')
                    for a, b in STMTS.get(f, ()):
                        if a <= c <= b and b - a < 40:
                            for l2 in range(a, b + 1): m |= lm.get(l2, 0)
                if not m and is_ins:
                    # an insertion between two never-run lines: widen to the enclosing function
                    for a, b, _ in SPANS.get(f, ()):
                        if a <= ln <= b:
                            for l2 in range(a, b + 1): m |= lm.get(l2, 0)
                            break
                if not m and is_ins and inner and inner[2] == "method":
                    m = class_use(inner[3][0]) & fallback([txt])
                sel |= m
                continue
            # 2) class-level attribute
            if inner and inner[2] in ("attr", "class") and (inner[3][0] if inner[2] == "attr" else inner[3]) in EXPR_CLASSES:
                # expression node settings (arg_types, names, flags): every test that builds such a node
                sel |= expr_mask(inner[3][0] if inner[2] == "attr" else inner[3])
                continue
            if inner and inner[2] == "attr":
                cq, attr, ents = inner[3]
                keys = []
                here = [e for e in ents if e[0] <= ln <= e[1]]
                if here and not is_ins and ln in info["pair"]:
                    # which entry did the edit touch? compare the old and new line column by column
                    a_, b_ = txt, info["pair"][ln]
                    i0 = 0
                    while i0 < min(len(a_), len(b_)) and a_[i0] == b_[i0]: i0 += 1
                    j0 = 0
                    while j0 < min(len(a_), len(b_)) - i0 and a_[-1 - j0] == b_[-1 - j0]: j0 += 1
                    lo, hi = i0, len(a_) - j0
                    for e in here:
                        s0 = e[3] if e[0] == ln else 0
                        e0 = e[4] if e[1] == ln else 10 ** 6
                        if s0 <= max(hi, lo + 1) - 1 and lo <= e0:
                            keys.append(e[2])
                    if not keys:
                        keys = [None]  # edit outside every entry (the attribute name, the brackets...)
                elif here:
                    keys = [e[2] for e in here]
                elif is_ins:
                    keys = [key_of_text(stripped)]
                else:
                    keys = [None]
                for key in keys:
                    if key:
                        m = key_mask(cq, attr, key)
                        if not m and not KEYS_BY_ATTR.get((cq, attr)):
                            # table never looked up at runtime (e.g. compiled into a trie at import)
                            m = class_use(cq) & tests_mentioning([key])
                    else:
                        m = attr_mask(cq, attr)
                    sel |= m
                continue
            # 3) new method / attr inserted directly in a class body
            if inner and inner[2] == "class":
                cq = inner[3]
                md = re.match(r"\s*def\s+([A-Za-z_0-9]+)", txt)
                if md:
                    name = md.group(1)
                    if name.endswith("_sql") and name[:-4] in EXPR_KEYS:
                        sel |= key_mask(cq, "TRANSFORMS", EXPR_KEYS[name[:-4]])
                    else:
                        sel |= class_use(cq) & fallback([txt])
                    continue
                ma = re.match(r"\s*([A-Z][A-Z0-9_]+)\s*[:=]", txt)
                if ma:
                    sel |= attr_mask(cq, ma.group(1))
                    continue
                sel |= class_use(cq) & fallback([txt])
                continue
            # 4) module level statement (constant / helper table): tests running lines that use the name
            mn = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=]*)?=", txt)
            if mn:
                sel |= source_lines_mentioning([mn.group(1)])
            else:
                enclosing_assign = [x for x in MODULE_ASSIGNS.get(f, ()) if x[0] <= ln <= x[1]]
                if enclosing_assign:
                    sel |= source_lines_mentioning([enclosing_assign[-1][2]])
                else:
                    sel |= lm.get(ln, 0)
    return sel


LAZY_LOAD = "tests/dialects/test_dialect.py::TestDialect::test_lazy_load"
TYPING_META = "tests/test_typing.py::TestTyping::test_no_redundant_expression_metadata"
SQLGLOTC = ["tests/test_sqlglotc.py::TestSQLGlotC::test_build_removes_obsolete_extensions",
            "tests/test_sqlglotc.py::TestSQLGlotC::test_dry_run_preserves_obsolete_extensions",
            "tests/test_sqlglotc.py::TestSQLGlotC::test_editable_install_removes_obsolete_extensions"]
CORE_IMPORTED = ("sqlglot/__init__.py", "sqlglot/dialects/__init__.py", "sqlglot/dialects/dialect.py",
                 "sqlglot/parsers/__init__.py", "sqlglot/generators/__init__.py")


def extra_tests(patch_text):
    """Tests the maps cannot see (they work in subprocesses or read source files)."""
    out = []
    files = parse_patch(patch_text)
    for f, info in files.items():
        eq = {ln for ln, p in info["pair"].items() if equivalent(info["minus"][ln], p)}
        changed = [l for ln, l in info["minus"].items() if ln not in eq]
        changed += [l for ln, ls in info["ins"].items() for j, l in enumerate(ls)
                    if not any(o in eq and info["pair"][o] == l for o in info["pair"])]
        changed = [l for l in changed if l.strip() and not l.strip().startswith("#")]
        if not changed:
            continue
        if f.startswith("sqlglot/typing/"):
            out.append(TYPING_META)
        if f.startswith("sqlglotc/") or f in ("setup.py", "pyproject.toml", "setup.cfg"):
            out += SQLGLOTC
        # what `import sqlglot` loads eagerly: module-level statements in the core modules, or imports
        module_level = [l for l in changed if re.match(r"[A-Za-z_]", l) and not re.match(r"(def|class|async def)\b", l)]
        imports = [l for l in changed if re.match(r"\s*(import |from \S+ import )", l)]
        if f.startswith("sqlglot/") and (imports or (f in CORE_IMPORTED and module_level)):
            if not f.startswith(("sqlglot/parsers/", "sqlglot/generators/", "sqlglot/tokenizers/", "sqlglot/typing/")) \
                    and not (f.startswith("sqlglot/dialects/") and not f.endswith(("__init__.py", "dialect.py"))):
                out.append(LAZY_LOAD)
    return out


def select(patch_text):
    sel = select_mask(patch_text)
    res = [TESTS[i] for i in range(len(TESTS)) if sel >> i & 1]
    try:
        for x in extra_tests(patch_text):
            if x not in res: res.append(x)
    except Exception:
        pass
    return res


if __name__ == "__main__":
    for p in sys.argv[1:]:
        r = select(open(p).read())
        D = dict(zip(TESTS, DURS))
        print(os.path.basename(p), len(r), round(sum(D[t] for t in r), 2))
