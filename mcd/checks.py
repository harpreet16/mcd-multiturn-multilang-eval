"""Deterministic rule checks. Each takes the model reply and the rule's params
and returns True when the reply FOLLOWS the rule.

To add a check type: write a function and register it in CHECKS. The rulebook
editor offers every key in CHECKS.
"""
import ast
import re

DEVANAGARI = "\u0900-\u097F"
WORD = rf"[\w{DEVANAGARI}]+"          # \w alone splits Hindi words at vowel marks
SENTENCE_END = r"[.!?\u0964\u0965]+"   # includes the Hindi danda and double danda


def reply_lang(text):
    """'hi' if most letters are Devanagari, else 'en'."""
    hindi = len(re.findall(f"[{DEVANAGARI}]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    return "hi" if hindi > latin else "en"


def _in_range(value, lo=None, hi=None):
    return (lo is None or value >= lo) and (hi is None or value <= hi)


def contains(text, terms, mode="all", **_):
    """terms: strings, or lists of accepted alternatives (for example the English word and its Hindi spelling)."""
    found = [any(a.lower() in text.lower() for a in (t if isinstance(t, list) else [t])) for t in terms]
    return all(found) if mode == "all" else any(found)


def regex(text, pattern, expect=True, **_):
    return bool(re.search(pattern, text, re.IGNORECASE | re.MULTILINE)) == expect


def regex_count(text, pattern, min=None, max=None, **_):
    return _in_range(len(re.findall(pattern, text, re.MULTILINE)), min, max)


def word_count(text, min=None, max=None, **_):
    return _in_range(len(re.findall(WORD, text)), min, max)


def sentence_count(text, min=None, max=None, **_):
    return _in_range(len([s for s in re.split(SENTENCE_END, text) if s.strip()]), min, max)


INTRO = re.compile(r"\s*(of course|absolutely|sure|certainly|here|यहाँ|यह रहा|ठीक है|जी हाँ|बिलकुल)", re.I)
OUTRO = re.compile(r"\s*(let me know|would you like|i hope|if you'd like|if you would like|✅|✨|💡|🔍|📌|मुझे आशा|अगर आप|यदि आप)", re.I)
RULE_LINE = r"\n\s*(?:-{3,}|\*{3,}|_{3,})\s*\n"


def deliverable(text):
    """The part of a reply that is the requested document, without the model's chat wrapper.

    Replies often look like  "Of course! Here is ...\n---\n<document>\n---\n✅ Why this works ...".
    If the reply has horizontal rules, the longest block that is not a recap is the document; otherwise a leading
    "Here is ...:" paragraph and trailing "Let me know ..." / "✅ ..." paragraphs are dropped.
    """
    segments = [seg.strip() for seg in re.split(RULE_LINE, "\n" + text.strip() + "\n") if seg.strip()]
    if len(segments) >= 2:
        body = [seg for seg in segments if not OUTRO.match(seg)] or segments
        return max(body, key=lambda seg: len(re.findall(WORD, seg)))
    paras = _paragraphs(text)
    while len(paras) > 1 and INTRO.match(paras[0]) and (paras[0].rstrip().endswith(":") or len(re.findall(WORD, paras[0])) < 60):
        paras = paras[1:]
    while len(paras) > 1 and OUTRO.match(paras[-1]):
        paras = paras[:-1]
    return "\n\n".join(paras)


def _paragraphs(text):
    return [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]


def paragraph_count(text, min=None, max=None, min_words=0, min_lines=1, body=False, **_):
    """Blank-line separated blocks. min_words / min_lines ignore short blocks such as
    a salutation, sign-off or a one-line title, so only real paragraphs / stanzas count.
    body=True first removes the model's chat wrapper (see deliverable)."""
    real = [p for p in _paragraphs(deliverable(text) if body else text)
            if len(re.findall(WORD, p)) >= min_words and len([l for l in p.splitlines() if l.strip()]) >= min_lines]
    return _in_range(len(real), min, max)


def max_words_per_paragraph(text, max, body=False, **_):
    return all(len(re.findall(WORD, p)) <= max for p in _paragraphs(deliverable(text) if body else text))


def reply_language(text, expect="en", **_):
    return reply_lang(text) == expect


def lowercase_only(text, **_):
    return text == text.lower()


def ends_with(text, phrase, **_):
    return text.strip().strip('"').lower().endswith(phrase.strip().lower())


def python_function(text, function, require, **_):
    """True when the reply contains a Python function `function` that has the property `require`.

    The code is parsed with ast and never executed. require is one of: defined, docstring, caching (an lru_cache /
    cache / memoize decorator), method_default_recursive (parameter method defaults to "recursive"), value_error
    (raises ValueError), float_handling (calls int() or tests isinstance(..., float)), loop (for/while), recursion
    (the function calls itself).
    """
    blocks = re.findall(r"```(?:python|py)?[ \t]*\n(.*?)```", text, re.S) or [text]
    node = None
    for code in blocks:
        try:
            tree = ast.parse(code)
        except SyntaxError:
            continue
        node = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == function), node)
    if node is None:
        return False
    walk = list(ast.walk(node))
    if require == "defined":
        return True
    if require == "docstring":
        return bool((ast.get_docstring(node) or "").strip())
    if require == "caching":
        return any(re.search(r"lru_cache|\bcache\b|memoize", ast.unparse(d)) for d in node.decorator_list)
    if require == "method_default_recursive":
        args = node.args.args + node.args.kwonlyargs
        defaults = [None] * (len(node.args.args) - len(node.args.defaults)) + node.args.defaults + list(node.args.kw_defaults)
        return any(a.arg == "method" and isinstance(d, ast.Constant) and d.value == "recursive" for a, d in zip(args, defaults))
    if require == "value_error":
        return any(isinstance(n, ast.Raise) and n.exc is not None and "ValueError" in ast.unparse(n.exc) for n in walk)
    if require == "float_handling":
        return any(isinstance(n, ast.Call) and ((isinstance(n.func, ast.Name) and n.func.id == "int")
                   or (isinstance(n.func, ast.Name) and n.func.id == "isinstance" and "float" in ast.unparse(n))) for n in walk)
    if require == "loop":
        return any(isinstance(n, (ast.For, ast.While)) for n in walk)
    if require == "recursion":
        return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == function for n in walk)
    raise ValueError(f"unknown python_function requirement: {require}")


CHECKS = {
    "contains": contains,
    "regex": regex,
    "regex_count": regex_count,
    "word_count": word_count,
    "sentence_count": sentence_count,
    "paragraph_count": paragraph_count,
    "max_words_per_paragraph": max_words_per_paragraph,
    "lowercase_only": lowercase_only,
    "ends_with": ends_with,
    "reply_language": reply_language,
    "python_function": python_function,
}
# "judge" is the one non-deterministic type: a yes/no question put to a model.
CHECK_TYPES = sorted(CHECKS) + ["judge"]


def run_check(check, reply):
    params = {k: v for k, v in check.items() if k != "type"}
    return CHECKS[check["type"]](reply, **params)
