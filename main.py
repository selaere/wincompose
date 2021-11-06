# -*- coding: utf-8 -*-

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from functools import cache, reduce
from itertools import product
from logging import info, warning, basicConfig
from os.path import exists, expanduser
from string import ascii_letters
from typing import Iterable, TextIO, Optional, NewType
from unicodedata import normalize
from urllib.request import urlopen

from data import custom_dia, diacritics, ligatures

basicConfig(level=10)


@dataclass(frozen=True)
class Chardata:
    name: str
    cat: str
    deco: str
    upper: Optional[str]
    lower: Optional[str]


Keys = NewType('Keys', str)

ESCAPE    = " {}"   # escapes one non-core character
ESCAPE2   = "↹{}↵"  # escapes more than one character
LIGATURE  = "&{}↵"
COMBINING = Keys("?")

SORT  = True  # this sorts the sequences and saves them in definitions_sorted
CHECK = True  # enable or disable checking for duplicates and shadows

# these can be used with diacritics (["a] => ä) but not as standalone sequences ([a])
coreltrs = ascii_letters

blocks: list[tuple[int, int, str]] = []

# a dict of user-defined basic sequences
# example: {"′": ["prime", "pr1"], ...}
definitions: dict[str, list[Keys]] = defaultdict(list)

# a list of the macros
# example: ["kʟ̝̊", "kl"]
macros: list[tuple[str, Keys]] = []

udata: dict[str, Chardata] = defaultdict(lambda: Chardata("UNNAMED", "Cn", "", None, None))


def escape_1(mapp: Keys) -> Keys:
    return Keys(ESCAPE.format(mapp)) if len(mapp) > 1 and mapp[0] in coreltrs else mapp


def escape(*maps: Keys) -> Keys:
    assert len(maps) > 0
    if len(maps) == 1:
        return escape_1(maps[0])
    else:
        return Keys(ESCAPE2.format(''.join(escape_1(i) for i in maps)))


# udata.normalize doesnt give enough information, so this is a bit more manual
# https://www.unicode.org/reports/tr44/#Character_Decomposition_Mappings
# this actually parses some invalid decompositions but who cares (i don't)
def decompose(char: str) -> tuple[Optional[str], str]:
    deco = udata[char].deco

    # turns "<sus> 0D9E 1F9EF" into ("<sus>", "ඞ🧯"])
    if not deco:
        return None, char
    typ, words = None, deco.split()
    if words[0].startswith("<"):
        typ, *words = words
    return typ, ''.join(chr(int(x, 16)) for x in words)


def make_diacritic_sequences(diacs: Iterable[str], deco: str) -> Iterable[Keys]:
    return (
        reduce(add_diacritic, diacs, Keys(escape(*maps)))
        for maps in product(*map(getmap, deco))
    ) if all(it in diacritics for it in diacs) else []


def add_diacritic(keys: Keys, diac: str) -> Keys:
    assert diac in diacritics
    return Keys(diacritics[diac] + keys)


def getmap(char: str) -> list[Keys]:
    if char in coreltrs: return [Keys(char)]
    if '\x21' <= char <= '\x7E' and "ascii" in diacritics:
        return [add_diacritic(Keys(char), "ascii")]
    return findmap(char)


@cache
def findmap(char: str) -> list[Keys]:

    maps = definitions[char]  # definitions is a defaultdict, this can't fail

    # does custom diacritic stuff
    for dia, values in custom_dia.items():
        if (index := values[1].find(char)) >= 0:
            maps.extend(make_diacritic_sequences((dia,), values[0][index]))

    # these few always seem to cause problems with automatic methods. set them in definitions or
    # custom_dia
    if char in ("Å" "©®🄫🄬" "ºªᵌ" "άέήίόύώΆΈΉΐΊΰΎΌΏ΅" "﹉﹊﹋﹌﹍﹎﹏" "︴🅋"):
        return maps

    # combining diacritic
    if "◌" + char in diacritics: maps.append(add_diacritic(COMBINING, "◌" + char))

    # canonical decomposition (splits ü into u + ◌̈ into ["u])
    normal = normalize('NFD', char)
    if len(normal) > 1:
        maps.extend(make_diacritic_sequences(["◌" + x for x in normal[1:]], normal[0]))

    # compatibility decomposition (does circled letters and superscripts and stuff)
    types, deco = decompose(char)
    if types == "<compat>" and deco.startswith("(") and deco.endswith(")"):
        maps.extend(make_diacritic_sequences(("parens",), deco[1:-1]))
    elif types:
        maps.extend(make_diacritic_sequences((types,), deco))

    # makes ligatures
    if char in ligatures: maps.extend(
        Keys(LIGATURE.format(''.join(mapp)))
        for mapp in product(*map(getmap, ligatures[char]))
    )

    return maps


rules: list[Keys] = []


# this is the function you'll probably want to change if you're using this for something other than
# wincompose (and also the path at the bottom and that stuff)
def add_rule(stream, keys: Keys, result: str, comment: str) -> None:
    # https://github.com/samhocevar/wincompose/blob/7f273636087bd55cbedc178babf5c36375a836f4/src/wincompose/sequences/Key.cs#L55
    replacements = {
        "⎄": "Multi_key",
        "←": "Left", "↑": "Up", "→": "Right", "↓": "Down",
        "⇱": "Home", "⇲": "End", "⌫": "Backspace", "⌦": "Delete", "↹": "Tab", "↵": "Return",
        ":": "colon", "<": "less", ">": "greater",
    }
    stream.write("<Multi_key><{}> : \"{}\" #{}\n".format(
        '><'.join(replacements.get(i, i) for i in str(keys)),
        result.replace(r'"', r'\0x0022'),  # \" doesnt work for some reason
        comment))
    if CHECK and keys in rules: warning(f"[{keys}] found more than once ({comment})")
    rules.append(keys)


def write_to_file(
    f: TextIO,
    definitions: dict[str, list[Keys]],
    macros: list[tuple[str, Keys]],
) -> None:
    for cp in range(0x1FFFF):  # change this if you're using characters outside BMP/SMP
        try:
            charname = udata[chr(cp)].name
        except KeyError:
            charname = "UNNAMED"
        else:  # note that this means that only characters with name will be processed
            for rule in findmap(chr(cp)): add_rule(f, rule, chr(cp), charname)

    info("writing macros...")
    for text, rule in macros:
        add_rule(f, rule, text, "macro")

    if CHECK:
        info("looking for shadows...")
        for rule in rules:
            for n in range(1, len(rule)):  # should this be `enumerate`?
                if rule[:n] in rules: warning(f"[{rule[:n]}] shadows [{rule}]")


def read_from_file(f: TextIO) -> None:
    global definitions, macros

    for line in f:
        if line.isspace() or line.startswith("//"): continue
        char, mapp = line.strip("\n").split("::", maxsplit=1)
        char = char[1] if char[0] == "◌" and len(char) > 1 else char
        mapp = Keys(mapp.replace("␣", " "))
        if len(char) == 2 and udata[char[0]].lower == char[1]:
            # "Ææ::ae" = "Æ::AE" + "æ::ae"
            definitions[char[0]].append(Keys(mapp.upper()))
            definitions[char[1]].append(Keys(mapp.lower()))
        elif len(char) > 1:
            macros.append((char, mapp))
        else:
            definitions[char].append(mapp)


def block_of(char: str) -> str:
    return next((name for start, end, name in blocks if start <= ord(char) <= end), "not assigned")


def sort(definitions: dict[str, list[Keys]], macros: list[tuple[str, Keys]]) -> None:
    info("sorting...")
    ignore = []
    last_block = "not assigned\n"
    with open(r"definitions_sorted.txt", 'w', encoding="utf-8") as f:

        f.write("// this file was automatically generated\n")
        for char, v in sorted(definitions.items(), key=lambda x: ord(x[0][0])):

            data = udata[char]
            if last_block != (new := block_of(char)):
                last_block = new
                f.write("\n// " + new.upper() + "\n")

            for ruleno, rule in enumerate(sorted(v)):
                if rule.lower() in ignore: continue
                low, upp = data.lower or char, data.upper or char
                if (low != upp
                        and char in (upp, low)
                        and udata[udata[low].upper or "a"].lower == low):
                    try:  # XXX: this is Bad and Ugly
                        lrule = definitions[low][ruleno]
                        urule = definitions[upp][ruleno]
                    except IndexError: pass
                    else:
                        if lrule == urule.lower():
                            ignore.append(lrule)
                            f.write(upp + low + "::" + lrule.replace(' ', '␣') + "\n")
                            continue

                f.write(("◌" if data.cat in ("Mc", "Me", "Mn", "Lm") else "")
                        + char + "::" + rule.replace(' ', '␣') + "\n")

        f.write("\n// MACROS\n\n")
        for text, rule in macros:
            f.write(text + "::" + rule.replace(' ', '␣') + "\n")
    info("sorting done")


def request(res):
    path = "./res/" + res
    if not exists(path):
        url = 'https://www.unicode.org/Public/UCD/latest/ucd/' + res
        info(f"file {path} not found. downloading from unicode.org...")
        with urlopen(url) as response, open(path, 'w', encoding="utf-8") as f:
            f.write(response.read().decode('utf-8'))
        info("download done")
    return open(path)


def main() -> None:

    with request("UnicodeData.txt") as f:
        for line in f:
            if line.isspace() or line[0] == "#": continue
            # https://www.unicode.org/reports/tr44/#UnicodeData.txt
            cp, name, cat, _, _, deco, _, _, _, _, _, _, upper, lower, _ = line.split(";")
            udata[chr(int(cp, 16))] = Chardata(
                name, cat, deco,
                upper=chr(int(upper, 16)) if upper else None,
                lower=chr(int(lower, 16)) if lower else None,
            )

    with request("Blocks.txt") as f:
        for line in f:
            if line.isspace() or line[0] == "#": continue
            bounds, blockname = line.split("; ", maxsplit=1)
            start, end = bounds.split("..", maxsplit=1)
            blocks.append((int(start, 16), int(end, 16), blockname))

    info(udata["ß"].upper)

    then = datetime.now()

    info("reading...")

    with open(r"definitions.txt", encoding="utf-8") as f:
        read_from_file(f)

    info("reading done")

    if SORT:
        sort(definitions, macros)

    info("writing characters...")

    with open(expanduser("~/.XCompose"), 'w', encoding='utf-8') as f:
        write_to_file(f, definitions, macros)

    info(f"done! {datetime.now() - then}")


if __name__ == "__main__":
    main()
