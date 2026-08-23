from agentforge.backends._thinking import strip_inline_think


def test_strips_real_cot_prose():
    content = "<think>I should read the file first.</think>\n\nHere is the answer."
    out, thinking = strip_inline_think(content)
    assert out == "Here is the answer."
    assert thinking == "I should read the file first."


def test_preserves_think_tags_inside_python_fence():
    """The bug: final answers quoting this backend's own regex got mangled."""
    content = """\
Quoted from ollama.py:

```python
match = re.search(r"<think>(.*?)</think>", content, re.DOTALL)
content = re.sub(r"<think>.*?</think>\\s*", "", content, flags=re.DOTALL).strip()
```

Done.
"""
    out, thinking = strip_inline_think(content)
    assert thinking is None
    assert 'r"<think>(.*?)</think>"' in out
    assert r'r"<think>.*?</think>\s*"' in out
    assert "```python" in out


def test_preserves_inline_code_with_think_tags():
    content = 'See `r"<think>...</think>"` in the docs.'
    out, thinking = strip_inline_think(content)
    assert thinking is None
    assert '`r"<think>...</think>"`' in out


def test_strips_cot_but_keeps_fenced_example():
    content = """\
<think>Need to explain the regex carefully.</think>

The strip line is:

```python
content = re.sub(r"<think>.*?</think>\\s*", "", content, flags=re.DOTALL).strip()
```
"""
    out, thinking = strip_inline_think(content)
    assert thinking == "Need to explain the regex carefully."
    assert 'r"<think>.*?</think>' in out
    assert "<think>Need to explain" not in out


def test_noop_without_think_tags():
    content = "plain answer with no special tags"
    out, thinking = strip_inline_think(content)
    assert out == content
    assert thinking is None
