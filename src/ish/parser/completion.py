"""Small, non-evaluating shell lexer for the word at the cursor.

It tracks quotes, escapes, operators and nested command substitutions. Dynamic
words are left to the shell; completion never evaluates a substitution.
"""
from dataclasses import dataclass


@dataclass
class CompletionWord:
    start: int
    value: str
    command: bool
    dynamic: bool = False


def completion_word(text: str) -> CompletionWord:
    start = 0
    value = []
    active = False
    quote = None
    dynamic = False
    need_command = True
    redirect = False
    stack = []
    i = 0

    def end_word():
        nonlocal active, value, dynamic, need_command, redirect
        if active:
            assignment = ''.join(value).partition('=')[0]
            if not redirect and not ('=' in value and assignment.isidentifier()):
                need_command = False
            redirect = False
        active, value, dynamic = False, [], False

    while i < len(text):
        c = text[i]
        if not active and c not in ' \t\r\n;&|()<>':
            start, active = i, True
        if c == '\\' and quote != "'":
            if i + 1 < len(text):
                nxt = text[i + 1]
                if quote == '"' and nxt not in '$`"\\\n':
                    value.append('\\')
                if nxt != '\n':
                    value.append(nxt)
                i += 2
                continue
            dynamic = True  # A trailing escape is not yet a complete prefix.
        elif quote == "'":
            if c == "'":
                quote = None
            else:
                value.append(c)
        elif text.startswith('$((', i):
            # Arithmetic is an expression, not a command position.
            end = text.find('))', i + 3)
            if end < 0:
                return CompletionWord(start, '', False, True)
            dynamic = True
            i = end + 2
            continue
        elif text.startswith('$(', i) or (c == '`' and (not stack or stack[-1][-1] != '`')):
            stack.append((start, value, active, quote, need_command, redirect, ')' if c == '$' else '`'))
            i += 2 if c == '$' else 1
            start, value, active, quote, dynamic = i, [], False, None, False
            need_command, redirect = True, False
            continue
        elif stack and c == stack[-1][-1] and quote is None:
            start, value, active, quote, need_command, redirect, _ = stack.pop()
            dynamic = True
        elif quote == '"':
            if c == '"':
                quote = None
            else:
                value.append(c)
                dynamic |= c == '$'
        elif c in "'\"":
            quote = c
        elif c in ' \t\r':
            end_word()
            start = i + 1
        elif c in ';|&\n()':
            end_word()
            need_command, redirect, start = True, False, i + 1
        elif c in '<>':
            end_word()
            redirect, start = True, i + 1
        elif c == '#' and not value:
            return CompletionWord(len(text), '', False, True)
        else:
            value.append(c)
            dynamic |= c in '$`*?[]{}'
        i += 1
    return CompletionWord(start, ''.join(value), need_command and not redirect, dynamic)
