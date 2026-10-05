"""SQL-injection meta-pattern detection (layer 2 of 2).

Layer 1 (structural, makes injection impossible): every DB access uses SQLAlchemy bound parameters.
Layer 2 (this file): reject tool arguments that *look like* SQL injection BEFORE execution and log a
security event, so attempts are visible/alertable even though they could never succeed.

Two modes to keep false positives low on legitimate support text:
  strict    for identifier-like args (IDs, enums): any quote, ';', '--', '/*', '%', backslash, whitespace => attack
  freetext  for natural-language args (KB query, ticket summary, reason): phrase-level attack patterns only,
            so "please update my email; thanks" or "select the Pro plan" are NOT flagged.
Input is normalised first (NFKC, URL-decode, zero-width strip, SQL comment collapsing, whitespace collapse)
so '%27%20OR%201%3D1', 'UN/**/ION SEL/**/ECT' and fullwidth quotes are caught.
Guardrail ids: G-TOOL-03 (tool args), G-IN-05 (raw customer message, strong patterns only).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import unquote

_ZW = re.compile(r"[​-‏⁠﻿]")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_WS = re.compile(r"\s+")


def normalise(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = _ZW.sub("", s)
    for _ in range(2):  # double-encoded payloads
        u = unquote(s)
        if u == s:
            break
        s = u
    s = _BLOCK_COMMENT.sub(" ", s)  # UN/**/ION -> UN ION is handled by also checking the glued form below
    return _WS.sub(" ", s).strip().lower()


def _glue(s: str) -> str:
    """Variant with inline comments removed WITHOUT leaving a space (UN/**/ION -> UNION)."""
    s = unicodedata.normalize("NFKC", _ZW.sub("", s))
    s = unquote(unquote(s))
    return _WS.sub(" ", _BLOCK_COMMENT.sub("", s)).strip().lower()


_STRICT_BAD = re.compile(r"""['"`;\\%]|--|/\*|\*/|\s|=|<|>|\(|\)|\bor\b|\band\b""", re.I)

_FREETEXT_PATTERNS: list[tuple[str, re.Pattern]] = [(n, re.compile(p, re.I)) for n, p in [
    ("union_select", r"\bunion\b\s*(all\s+|distinct\s+)?\bselect\b"),
    ("stacked_query", r";\s*(select|insert|update|delete|drop|alter|create|truncate|exec|execute|declare|grant|shutdown)\b"),
    ("drop_table", r"\b(drop|truncate)\s+(table|database|schema|view|index)\b"),
    ("delete_from", r"\bdelete\s+from\s+\w+\s*(?:where\b|;|--|#|$)"),
    ("insert_into", r"\binsert\s+into\s+\w+\s*(?:\(|values\b|select\b)"),
    ("update_set", r"\bupdate\s+\w+\s+set\s+\w+\s*="),
    ("tautology_quote", r"['\"`]\s*(or|and)\s+['\"`]?[\w ]{1,20}['\"`]?\s*(=|like)\s*['\"`]?[\w ]{0,20}"),
    ("tautology_num", r"\b(or|and)\s+\d+\s*=\s*\d+"),
    ("tautology_true", r"\b(or|and)\s+(true|false)\b\s*(--|#|;|$)"),
    ("quote_comment", r"['\"`]\s*\)?\s*(--|#|/\*)"),
    ("quote_semicolon", r"['\"`]\s*;"),
    ("comment_terminator", r";\s*--"),
    ("schema_probe", r"\b(information_schema|sqlite_master|sqlite_schema|pg_catalog|pg_tables|sys\.objects|sysobjects|mysql\.user)\b"),
    ("time_based", r"\b(pg_sleep|sleep|benchmark|waitfor\s+delay|dbms_pipe\.receive_message)\s*\("),
    ("os_exec", r"\b(xp_cmdshell|load_file|into\s+(out|dump)file|utl_http|lo_import|copy\s+\w+\s+from\s+program)\b"),
    ("hex_blob", r"\b0x[0-9a-f]{12,}\b"),
    ("char_concat", r"\b(char|chr|concat|concat_ws|group_concat|string_agg)\s*\(\s*\d"),
    ("select_from_probe", r"\bselect\b[^.!?]{0,60}\bfrom\b\s+(users?|customers?|accounts?|cards?|transactions?|transfers?|disputes?|passwords?|credentials?|api_credentials|audit_log|tickets)\b"),
    ("having_groupby_probe", r"\b(order|group)\s+by\s+\d+\s*(--|#|;)"),
    ("version_probe", r"(@@version|version\(\)|sqlite_version\(\)|current_user\(\)|database\(\))"),
    ("numeric_tautology", r"\b(\d{2,})\s*=\s*\1\b"),
    ("string_tautology", r"'(\w{1,12})'\s*=\s*'\1'|\"(\w{1,12})\"\s*=\s*\"\2\""),
    ("empty_tautology", r"''\s*=\s*''|\"\"\s*=\s*\"\"|'\s*=\s*'|\"\s*=\s*\""),
    ("or_and_select", r"\b(or|and)\s*\(+\s*select\b|\b(or|and)\s+select\b"),
    ("where_tautology", r"\bwhere\s+\(?\d+\)?\s*=\s*\(?\d+"),
    ("having_tautology", r"\bhaving\s+\d+\s*=\s*\d+"),
    ("order_by_comment", r"\border\s+by\s+\d+\s*(--|#|/\*|$)"),
    ("sql_vendor_funcs", r"\b(updatexml|extractvalue|make_set|ctxsys|xmltype|utl_inaddr|dbms_\w+)\b|rdb\$database|\bsysibm\.|\ball_users\b|\bfrom\s+dual\b|\bv\$version\b|\bsys\.databases\b"),
    ("count_star", r"\bcount\s*\(\s*\*\s*\)\s*(from|>|=|<)"),
    ("select_case", r"\bselect\s+case\s+when\b|\bif\s*\(\s*\d+\s*=\s*\d+"),
    ("quote_boolean", r"['\"]\s*\)*\s*(and|or)\b\s*[\(\d'\"]"),
    ("paren_boolean_num", r"\)\s*(and|or)\s+\(?\s*\d+\s*=\s*\d+"),
    ("trailing_comment_num", r"\d\s*(--|#)[\s\-]*$"),
    ("pipe_concat_select", r"\|\|\s*\(?\s*select\b"),
    ("substring_select", r"\b(substr(ing)?|ascii|ord|mid|length|len)\s*\(\s*\(?\s*select\b"),
    ("cast_convert_select", r"\b(cast|convert)\s*\(.{0,40}\bselect\b"),
    ("limit_offset_comment", r"\blimit\s+\d+(\s*,\s*\d+|\s+offset\s+\d+)?\s*(--|#)"),
]]

# Patterns safe to apply to the *whole customer message* (very low false-positive rate)
_MESSAGE_STRONG = {"numeric_tautology", "string_tautology", "empty_tautology", "or_and_select", "where_tautology", "having_tautology",
                   "order_by_comment", "sql_vendor_funcs", "count_star", "select_case", "quote_boolean", "paren_boolean_num",
                   "trailing_comment_num", "pipe_concat_select", "substring_select", "cast_convert_select", "limit_offset_comment",
                   "version_probe", "tautology_true", "char_concat", "hex_blob", "having_groupby_probe", "union_select", "stacked_query", "drop_table", "tautology_quote", "tautology_num", "comment_terminator",
                   "schema_probe", "time_based", "os_exec", "select_from_probe", "quote_comment", "delete_from", "insert_into", "update_set"}


@dataclass
class SQLiVerdict:
    flagged: bool
    patterns: list[str]
    mode: str

    def __bool__(self) -> bool:
        return self.flagged


def check_strict(value: str) -> SQLiVerdict:
    n = normalise(value)
    hits = ["strict_meta_chars"] if _STRICT_BAD.search(n) or _STRICT_BAD.search(value) else []
    return SQLiVerdict(bool(hits), hits, "strict")


def check_freetext(value: str, *, only: set[str] | None = None) -> SQLiVerdict:
    hits: list[str] = []
    for variant in {normalise(value), _glue(value), value.lower()}:
        for name, rx in _FREETEXT_PATTERNS:
            if only is not None and name not in only:
                continue
            if rx.search(variant) and name not in hits:
                hits.append(name)
    return SQLiVerdict(bool(hits), hits, "freetext")


def check_message(value: str) -> SQLiVerdict:
    v = check_freetext(value, only=_MESSAGE_STRONG)
    v.mode = "message"
    return v


def scan_args(args: dict, *, strict_keys: set[str], free_keys: set[str]) -> SQLiVerdict:
    """Scan a tool-call argument dict. Keys not in either set are scanned in freetext mode if they are strings."""
    all_hits: list[str] = []
    for k, v in args.items():
        if isinstance(v, str):
            r = check_strict(v) if k in strict_keys else check_freetext(v)
            if r:
                all_hits += [f"{k}:{p}" for p in r.patterns]
        elif isinstance(v, (list, tuple)):
            for item in v:
                if isinstance(item, str) and check_freetext(item):
                    all_hits.append(f"{k}[]:freetext")
    return SQLiVerdict(bool(all_hits), all_hits, "args")
