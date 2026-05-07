#!/usr/bin/env python3
"""POC for archive privacy span detection and redaction preview.

The default path uses deterministic rules so the POC runs without external
dependencies. If the optional OpenAI Privacy Filter package is installed, pass
`--enable-opf` to merge OPF spans into the same result schema.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


LABELS: dict[str, dict[str, str]] = {
    "personal_name": {"name": "姓名", "placeholder": "<姓名>"},
    "personal_id": {"name": "身份证件号", "placeholder": "<身份证件号>"},
    "personal_phone": {"name": "联系电话", "placeholder": "<联系电话>"},
    "personal_email": {"name": "电子邮箱", "placeholder": "<电子邮箱>"},
    "personal_address": {"name": "住址/地址", "placeholder": "<住址>"},
    "personal_date": {"name": "个人日期", "placeholder": "<个人日期>"},
    "financial_account": {"name": "金融账号", "placeholder": "<金融账号>"},
    "family_member": {"name": "家庭成员", "placeholder": "<家庭成员>"},
    "minor_info": {"name": "未成年人信息", "placeholder": "<未成年人信息>"},
    "case_number": {"name": "案号", "placeholder": "<案号>"},
    "secrecy_mark": {"name": "密级标识", "placeholder": "<密级标识>"},
    "private_url": {"name": "个人/敏感链接", "placeholder": "<链接>"},
    "secret": {"name": "口令/密钥", "placeholder": "<密钥>"},
}

LABEL_PRIORITY: dict[str, int] = {
    "secrecy_mark": 100,
    "secret": 95,
    "personal_id": 90,
    "financial_account": 85,
    "personal_phone": 80,
    "personal_email": 80,
    "case_number": 75,
    "minor_info": 72,
    "family_member": 70,
    "personal_address": 65,
    "personal_name": 60,
    "private_url": 96,
    "personal_date": 50,
}

OPF_LABEL_MAP = {
    "private_person": "personal_name",
    "other_person": "personal_name",
    "personal_name": "personal_name",
    "private_address": "personal_address",
    "personal_location": "personal_address",
    "private_phone": "personal_phone",
    "personal_phone": "personal_phone",
    "private_email": "personal_email",
    "personal_email": "personal_email",
    "private_date": "personal_date",
    "personal_date": "personal_date",
    "account_number": "financial_account",
    "personal_fin_id": "financial_account",
    "personal_gov_id": "personal_id",
    "private_url": "private_url",
    "personal_url": "private_url",
    "secret": "secret",
    "secret_url": "secret",
}

CN_NAME_STOPWORDS = {
    "电话",
    "住址",
    "地址",
    "邮箱",
    "材料",
    "记录",
    "样本",
    "案号",
    "证人电话",
}

OPF_SUPPRESS_CONTEXT = {
    "personal_phone": ("公开电话", "办公室公开电话", "值班电话"),
    "personal_date": ("票据号", "项目编号", "档号", "案号"),
}


@dataclass(frozen=True)
class Span:
    label: str
    start: int
    end: int
    text: str
    engine: str
    rule_id: str = ""
    score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if payload["score"] is None:
            payload.pop("score")
        return payload


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"{path}:{line_no}: expected object record")
            if not isinstance(record.get("text"), str):
                raise ValueError(f"{path}:{line_no}: record.text must be a string")
            records.append(record)
    return records


def write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")


def valid_chinese_id(value: str) -> bool:
    value = value.upper()
    if not re.fullmatch(r"[1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dX]", value):
        return False
    weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    checks = "10X98765432"
    total = sum(int(value[idx]) * weights[idx] for idx in range(17))
    return checks[total % 11] == value[-1]


def valid_cn_name(value: str) -> bool:
    return bool(re.fullmatch(r"[\u4e00-\u9fa5·]{2,12}", value)) and value not in CN_NAME_STOPWORDS


def valid_short_cn_name(value: str) -> bool:
    return bool(re.fullmatch(r"[\u4e00-\u9fa5·]{2,4}", value)) and value not in CN_NAME_STOPWORDS


def valid_address(value: str) -> bool:
    if "://" in value:
        return False
    return bool(re.search(r"(?:省|市|区|县|路|街|巷|村|号|室|幢|单元)", value))


def add_regex_spans(
    spans: list[Span],
    *,
    text: str,
    pattern: str,
    label: str,
    rule_id: str,
    flags: int = 0,
    group: int = 0,
    validator: Any | None = None,
) -> None:
    for match in re.finditer(pattern, text, flags):
        value = match.group(group)
        if not value:
            continue
        start, end = match.span(group)
        if start < 0 or end <= start:
            continue
        if validator is not None and not validator(value):
            continue
        spans.append(Span(label=label, start=start, end=end, text=value, engine="rule", rule_id=rule_id))


def detect_rules(text: str) -> list[Span]:
    spans: list[Span] = []

    add_regex_spans(
        spans,
        text=text,
        pattern=r"(?<!\d)([1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])(?!\d)",
        label="personal_id",
        rule_id="cn_resident_id",
        group=1,
        validator=valid_chinese_id,
    )
    add_regex_spans(
        spans,
        text=text,
        pattern=r"(?<!\d)(?:\+?86[- ]?)?(1[3-9]\d{9})(?!\d)",
        label="personal_phone",
        rule_id="cn_mobile",
        group=1,
    )
    add_regex_spans(
        spans,
        text=text,
        pattern=r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})(?![A-Za-z0-9._%+-])",
        label="personal_email",
        rule_id="email",
        group=1,
    )
    add_regex_spans(
        spans,
        text=text,
        pattern=r"(?<!\d)((?:62|60|40|41|42|43|44|45|46|47|48|49|50|51|52|53|54|55|56|57|58|59)\d{12,18})(?!\d)",
        label="financial_account",
        rule_id="bank_or_account_number",
        group=1,
    )
    add_regex_spans(
        spans,
        text=text,
        pattern=r"(https?://[A-Za-z0-9./?&=%_:#\-]+)",
        label="private_url",
        rule_id="url",
        group=1,
    )
    add_regex_spans(
        spans,
        text=text,
        pattern=r"(?:密码|口令|密钥|token|api[_-]?key)[：:= ]+([A-Za-z0-9_\-]{6,})",
        label="secret",
        rule_id="secret_context",
        flags=re.IGNORECASE,
        group=1,
    )
    add_regex_spans(
        spans,
        text=text,
        pattern=r"(?:绝密|机密|秘密)[★☆·]?\s*(?:\d{1,2}年|长期)?",
        label="secrecy_mark",
        rule_id="secrecy_mark",
    )
    add_regex_spans(
        spans,
        text=text,
        pattern=r"((?:（|\()\d{4}(?:）|\))[\u4e00-\u9fa5]{1,6}\d{2,4}[\u4e00-\u9fa5]{1,6}\d+号)",
        label="case_number",
        rule_id="case_number",
        group=1,
    )

    table_name_context = (
        r"(?:姓\s*名|曾\s*用\s*名|原\s*名|别\s*名|申请人|当事人|联系人)"
        r"[：:\s]*(?:是|为)?([\u4e00-\u9fa5·]{2,6})"
        r"(?=\s*(?:性\s*别|男|女|出生|出\s*生|民族|年\s*龄|身份证|证件|电话|住址|地址|单位|职务|文化|籍贯|政治|婚姻|参加|入党|，|,|。|；|;|、|$))"
    )
    add_regex_spans(
        spans,
        text=text,
        pattern=table_name_context,
        label="personal_name",
        rule_id="table_name_field_context",
        group=1,
        validator=valid_short_cn_name,
    )
    name_context = (
        r"(?:姓名|申请人|当事人|证人|联系人|收件人|订单上的姓名|名字|责任人)"
        r"[：:\s]*(?:是|为)?([\u4e00-\u9fa5·]{2,12})(?=，|,|。|；|;|、|\s|$)"
    )
    add_regex_spans(
        spans,
        text=text,
        pattern=name_context,
        label="personal_name",
        rule_id="name_context",
        group=1,
        validator=valid_cn_name,
    )
    add_regex_spans(
        spans,
        text=text,
        pattern=r"([\u4e00-\u9fa5·]{2,4})[，,]\s*(?:男|女)[，,]",
        label="personal_name",
        rule_id="name_gender_context",
        group=1,
        validator=valid_short_cn_name,
    )
    add_regex_spans(
        spans,
        text=text,
        pattern=r"(?:妻子|丈夫|父亲|母亲|配偶)([\u4e00-\u9fa5·]{2,4})(?=患有|因|，|,|。|；|;|、|\s|$)",
        label="family_member",
        rule_id="family_member_inline_context",
        group=1,
        validator=valid_short_cn_name,
    )
    add_regex_spans(
        spans,
        text=text,
        pattern=r"(?:妻子|丈夫|父亲|母亲|配偶)[：: ]*([\u4e00-\u9fa5·]{2,4})(?=，|,|。|；|;|、|\s|$)",
        label="family_member",
        rule_id="family_member_context",
        group=1,
        validator=valid_cn_name,
    )
    add_regex_spans(
        spans,
        text=text,
        pattern=r"(?:未成年人|儿子|女儿)[：: ]*([\u4e00-\u9fa5·]{2,4})(?=[^。；;\n]{0,12}(?:出生|出生日期|生于))",
        label="minor_info",
        rule_id="minor_context",
        group=1,
        validator=valid_short_cn_name,
    )
    add_regex_spans(
        spans,
        text=text,
        pattern=r"(?:住址|地址|户籍地址|家庭住址|新地址|现住|寄到)[：: ]*([^\n，。；;]{6,80})",
        label="personal_address",
        rule_id="address_context",
        group=1,
        validator=valid_address,
    )

    # Dates are over-redacted if treated globally, so this POC only emits dates
    # when the nearby context suggests a personal date.
    personal_date_pattern = (
        r"(?:出生日期|出生年月|出生于|生于|生日)[：: ]*"
        r"((?:18|19|20)\d{2}[-/.年](?:0?[1-9]|1[0-2])[-/.月](?:0?[1-9]|[12]\d|3[01])日?)"
    )
    add_regex_spans(
        spans,
        text=text,
        pattern=personal_date_pattern,
        label="personal_date",
        rule_id="personal_date_context",
        group=1,
    )

    return spans


class OPFDetector:
    def __init__(self, *, device: str = "cpu", checkpoint: str | None = None) -> None:
        try:
            from opf import OPF  # type: ignore
        except Exception as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("OPF package is not importable; install openai/privacy-filter first") from exc
        kwargs: dict[str, Any] = {"device": device, "output_mode": "typed"}
        if checkpoint:
            kwargs["model"] = checkpoint
        self._redactor = OPF(**kwargs)

    def detect(self, text: str) -> list[Span]:
        result = self._redactor.redact(text)
        detected = getattr(result, "detected_spans", ())
        spans: list[Span] = []
        for span in detected:
            raw_label = getattr(span, "label", "")
            label = OPF_LABEL_MAP.get(raw_label)
            if not label:
                continue
            start = int(getattr(span, "start"))
            end = int(getattr(span, "end"))
            value = str(getattr(span, "text"))
            if suppress_opf_span(text, label, start, end):
                continue
            spans.append(
                Span(
                    label=label,
                    start=start,
                    end=end,
                    text=value,
                    engine="opf",
                    rule_id=f"opf:{raw_label}",
                )
            )
        return spans


def suppress_opf_span(text: str, label: str, start: int, end: int) -> bool:
    context = text[max(0, start - 12) : min(len(text), end + 12)]
    for phrase in OPF_SUPPRESS_CONTEXT.get(label, ()):
        if phrase in context:
            return True
    return False


def valid_span(span: Span, text: str) -> bool:
    return (
        span.label in LABELS
        and 0 <= span.start < span.end <= len(text)
        and text[span.start : span.end] == span.text
    )


def merge_spans(spans: list[Span], text: str) -> list[Span]:
    valid = [span for span in spans if valid_span(span, text)]
    valid.sort(
        key=lambda span: (
            span.start,
            span.engine != "rule",
            -LABEL_PRIORITY.get(span.label, 0),
            -(span.end - span.start),
        )
    )
    kept: list[Span] = []
    for span in valid:
        overlaps = [
            idx
            for idx, current in enumerate(kept)
            if not (span.end <= current.start or span.start >= current.end)
        ]
        if not overlaps:
            kept.append(span)
            continue
        replace = True
        for idx in overlaps:
            current = kept[idx]
            if span_rank(span) <= span_rank(current):
                replace = False
                break
        if replace:
            kept = [current for idx, current in enumerate(kept) if idx not in overlaps]
            kept.append(span)
    kept.sort(key=lambda span: (span.start, span.end))
    return kept


def span_rank(span: Span) -> tuple[int, int, int]:
    """Rank spans for overlap resolution.

    Deterministic rules win over model spans when they overlap. OPF is valuable
    as a recall layer, but in this POC rule hits for structured Chinese IDs,
    phones, dates, and addresses are treated as stronger evidence.
    """
    engine_score = 1 if span.engine == "rule" else 0
    return (engine_score, LABEL_PRIORITY.get(span.label, 0), span.end - span.start)


def placeholder_for(label: str) -> str:
    return LABELS.get(label, {"placeholder": "<敏感信息>"})["placeholder"]


def redact_text(text: str, spans: list[Span]) -> str:
    pieces: list[str] = []
    cursor = 0
    for span in sorted(spans, key=lambda item: item.start):
        if span.start < cursor:
            continue
        pieces.append(text[cursor : span.start])
        pieces.append(placeholder_for(span.label))
        cursor = span.end
    pieces.append(text[cursor:])
    return "".join(pieces)


def resolve_gold_spans(record: dict[str, Any]) -> list[Span]:
    text = record["text"]
    explicit = record.get("gold_spans")
    if isinstance(explicit, list):
        out: list[Span] = []
        for raw in explicit:
            if not isinstance(raw, dict):
                continue
            label = str(raw.get("label", ""))
            start = int(raw.get("start", -1))
            end = int(raw.get("end", -1))
            value = text[start:end] if 0 <= start < end <= len(text) else str(raw.get("text", ""))
            span = Span(label=label, start=start, end=end, text=value, engine="gold", rule_id="gold")
            if valid_span(span, text):
                out.append(span)
        return out

    mentions = record.get("gold_mentions", [])
    out = []
    used: set[tuple[int, int, str]] = set()
    if not isinstance(mentions, list):
        return out
    for raw in mentions:
        if not isinstance(raw, dict):
            continue
        label = str(raw.get("label", ""))
        value = str(raw.get("text", ""))
        occurrence = int(raw.get("occurrence", 1))
        if not label or not value:
            continue
        start = find_occurrence(text, value, occurrence)
        if start < 0:
            raise ValueError(f"{record.get('id', '<unknown>')}: gold mention not found: {value!r}")
        end = start + len(value)
        key = (start, end, label)
        if key in used:
            continue
        used.add(key)
        out.append(Span(label=label, start=start, end=end, text=value, engine="gold", rule_id="gold"))
    out.sort(key=lambda span: (span.start, span.end, span.label))
    return out


def find_occurrence(text: str, value: str, occurrence: int) -> int:
    if occurrence <= 0:
        occurrence = 1
    cursor = 0
    found = -1
    for _ in range(occurrence):
        found = text.find(value, cursor)
        if found < 0:
            return -1
        cursor = found + len(value)
    return found


def span_overlap(a: Span, b: Span) -> int:
    return max(0, min(a.end, b.end) - max(a.start, b.start))


def match_spans(predicted: list[Span], gold: list[Span], *, typed: bool) -> tuple[int, int, int]:
    matched_gold: set[int] = set()
    tp = 0
    for pred in predicted:
        best_idx = None
        best_overlap = 0
        for idx, item in enumerate(gold):
            if idx in matched_gold:
                continue
            if typed and pred.label != item.label:
                continue
            overlap = span_overlap(pred, item)
            if overlap <= 0:
                continue
            min_len = min(pred.end - pred.start, item.end - item.start)
            if overlap >= max(1, int(min_len * 0.5)) and overlap > best_overlap:
                best_idx = idx
                best_overlap = overlap
        if best_idx is not None:
            matched_gold.add(best_idx)
            tp += 1
    fp = len(predicted) - tp
    fn = len(gold) - tp
    return tp, fp, fn


def prf(tp: int, fp: int, fn: int) -> dict[str, float | int]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def compute_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    typed_tp = typed_fp = typed_fn = 0
    untyped_tp = untyped_fp = untyped_fn = 0
    labels = sorted(LABELS)
    per_label_counts: dict[str, Counter[str]] = {label: Counter() for label in labels}
    for result in results:
        pred = [Span(**item) for item in result["merged_spans"]]
        gold = [Span(**item) for item in result["gold_spans"]]
        tp, fp, fn = match_spans(pred, gold, typed=True)
        typed_tp += tp
        typed_fp += fp
        typed_fn += fn
        tp, fp, fn = match_spans(pred, gold, typed=False)
        untyped_tp += tp
        untyped_fp += fp
        untyped_fn += fn
        for label in labels:
            label_pred = [span for span in pred if span.label == label]
            label_gold = [span for span in gold if span.label == label]
            tp_l, fp_l, fn_l = match_spans(label_pred, label_gold, typed=True)
            per_label_counts[label].update({"tp": tp_l, "fp": fp_l, "fn": fn_l})

    return {
        "typed": prf(typed_tp, typed_fp, typed_fn),
        "untyped": prf(untyped_tp, untyped_fp, untyped_fn),
        "per_label": {
            label: prf(counts["tp"], counts["fp"], counts["fn"])
            for label, counts in per_label_counts.items()
            if counts["tp"] or counts["fp"] or counts["fn"]
        },
    }


def detect_record(record: dict[str, Any], opf_detector: OPFDetector | None) -> dict[str, Any]:
    text = record["text"]
    raw_spans = detect_rules(text)
    if opf_detector is not None:
        raw_spans.extend(opf_detector.detect(text))
    merged = merge_spans(raw_spans, text)
    gold = resolve_gold_spans(record)
    return {
        "id": record.get("id", ""),
        "title": record.get("title", ""),
        "text": text,
        "raw_spans": [span.to_dict() for span in raw_spans if valid_span(span, text)],
        "merged_spans": [span.to_dict() for span in merged],
        "gold_spans": [span.to_dict() for span in gold],
        "redacted_text": redact_text(text, merged),
    }


def build_markdown_report(
    *,
    results: list[dict[str, Any]],
    metrics: dict[str, Any],
    opf_status: str,
) -> str:
    lines = [
        "# 档案个人信息脱敏 POC 运行报告",
        "",
        f"- 样本数：{len(results)}",
        f"- OPF 状态：{opf_status}",
        f"- typed precision/recall/F1：{metrics['typed']['precision']} / {metrics['typed']['recall']} / {metrics['typed']['f1']}",
        f"- untyped precision/recall/F1：{metrics['untyped']['precision']} / {metrics['untyped']['recall']} / {metrics['untyped']['f1']}",
        "",
        "## 标签指标",
        "",
        "| 标签 | TP | FP | FN | Precision | Recall | F1 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for label, item in metrics["per_label"].items():
        lines.append(
            f"| {LABELS.get(label, {}).get('name', label)} `{label}` | {item['tp']} | {item['fp']} | {item['fn']} | {item['precision']} | {item['recall']} | {item['f1']} |"
        )
    lines.extend(["", "## 样本结果", ""])
    for result in results:
        lines.extend(
            [
                f"### {result['id']} {result['title']}",
                "",
                "**预测 span**",
                "",
            ]
        )
        spans = result["merged_spans"]
        if spans:
            for span in spans:
                lines.append(
                    f"- `{span['label']}` {span['start']}-{span['end']} `{span['text']}` ({span['engine']}:{span.get('rule_id', '')})"
                )
        else:
            lines.append("- 无")
        lines.extend(["", "**脱敏预览**", "", "```text", result["redacted_text"], "```", ""])
    return "\n".join(lines)


def highlight_html(text: str, spans: list[Span]) -> str:
    color_by_label = {
        "personal_name": "#fde68a",
        "personal_id": "#fecaca",
        "personal_phone": "#bfdbfe",
        "personal_email": "#c7d2fe",
        "personal_address": "#bbf7d0",
        "personal_date": "#fed7aa",
        "financial_account": "#fbcfe8",
        "family_member": "#ddd6fe",
        "minor_info": "#fda4af",
        "case_number": "#e5e7eb",
        "secrecy_mark": "#fca5a5",
        "private_url": "#bae6fd",
        "secret": "#f9a8d4",
    }
    pieces: list[str] = []
    cursor = 0
    for span in spans:
        pieces.append(html.escape(text[cursor : span.start]))
        label_name = LABELS.get(span.label, {}).get("name", span.label)
        color = color_by_label.get(span.label, "#e5e7eb")
        pieces.append(
            f'<mark class="hit" style="background:{color}" title="{html.escape(label_name)} | {span.engine}:{html.escape(span.rule_id)}">'
            f"{html.escape(span.text)}</mark>"
        )
        cursor = span.end
    pieces.append(html.escape(text[cursor:]))
    return "".join(pieces).replace("\n", "<br>")


def build_review_html(results: list[dict[str, Any]], metrics: dict[str, Any], opf_status: str) -> str:
    cards: list[str] = []
    hit_links: list[str] = []
    for result in results:
        text = result["text"]
        spans = [Span(**item) for item in result["merged_spans"]]
        doc_id = str(result["id"])
        labels = Counter(span.label for span in spans)
        label_badges = " ".join(
            f'<span class="badge">{html.escape(LABELS.get(label, {}).get("name", label))} x {count}</span>'
            for label, count in sorted(labels.items())
        )
        hit_count = len(spans)
        hit_summary = label_badges if label_badges else '<span class="muted">无命中</span>'
        card_class = "card has-hits" if spans else "card"
        if spans:
            hit_links.append(
                f'<a href="#{html.escape(doc_id)}">{html.escape(doc_id)} <strong>{hit_count}</strong></a>'
            )
        cards.append(
            f"""
            <section class="{card_class}" id="{html.escape(doc_id)}">
              <h2>{html.escape(doc_id)} {html.escape(str(result['title']))}</h2>
              <div class="hit-row">命中 {hit_count} 处 {hit_summary}</div>
              <h3>高亮原文</h3>
              <p class="doc">{highlight_html(text, spans)}</p>
              <h3>脱敏预览</h3>
              <p class="doc redacted">{html.escape(result['redacted_text']).replace(chr(10), '<br>')}</p>
            </section>
            """
        )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>档案个人信息脱敏 POC</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f6f7f9; color: #17202a; }}
    header {{ padding: 24px 32px; background: #17202a; color: white; }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 24px; }}
    .summary {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 18px; }}
    .metric, .card, .hit-index {{ background: white; border: 1px solid #d9dee7; border-radius: 8px; box-shadow: 0 1px 2px rgba(15,23,42,.04); }}
    .metric {{ padding: 14px; }}
    .metric strong {{ display: block; font-size: 24px; margin-top: 6px; }}
    .card {{ padding: 18px; margin: 18px 0; }}
    .has-hits {{ border-left: 6px solid #dc2626; }}
    .hit-index {{ padding: 16px 18px; margin-bottom: 18px; }}
    .hit-index a {{ display: inline-block; margin: 6px 8px 0 0; padding: 6px 10px; color: #991b1b; background: #fee2e2; border: 1px solid #fecaca; border-radius: 6px; text-decoration: none; }}
    .hit-row {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; color: #4b5563; }}
    .badge {{ display: inline-block; padding: 3px 7px; color: #7f1d1d; background: #fee2e2; border: 1px solid #fecaca; border-radius: 999px; font-size: 13px; }}
    .muted {{ color: #6b7280; }}
    .doc {{ line-height: 1.85; white-space: normal; background: #fbfcfe; border: 1px solid #e5e7eb; border-radius: 6px; padding: 14px; }}
    mark.hit {{ padding: 2px 4px; border: 2px solid #dc2626; border-radius: 4px; font-weight: 700; color: #111827; box-shadow: 0 0 0 2px rgba(220,38,38,.12); }}
    h1, h2, h3 {{ margin-top: 0; }}
    h3 {{ color: #4b5563; font-size: 15px; margin-top: 18px; }}
  </style>
</head>
<body>
  <header>
    <h1>档案个人信息脱敏 POC</h1>
    <div>OPF 状态：{html.escape(opf_status)}</div>
  </header>
  <main>
    <div class="summary">
      <div class="metric">样本数<strong>{len(results)}</strong></div>
      <div class="metric">Typed F1<strong>{metrics['typed']['f1']}</strong></div>
      <div class="metric">Typed Recall<strong>{metrics['typed']['recall']}</strong></div>
      <div class="metric">Typed Precision<strong>{metrics['typed']['precision']}</strong></div>
    </div>
    <section class="hit-index">
      <h2>命中文档</h2>
      <p>{''.join(hit_links) if hit_links else '<span class="muted">当前没有命中。</span>'}</p>
    </section>
    {''.join(cards)}
  </main>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run archive privacy redaction POC.")
    parser.add_argument("--input", type=Path, default=Path(__file__).with_name("sample_dataset.jsonl"))
    parser.add_argument("--out-dir", type=Path, default=Path(__file__).with_name("out"))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--enable-opf", action="store_true", help="Merge OpenAI Privacy Filter spans if opf is installed.")
    parser.add_argument("--opf-device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--opf-checkpoint", default=None)
    args = parser.parse_args(argv)

    records = read_jsonl(args.input)
    if args.limit is not None:
        records = records[: args.limit]
    args.out_dir.mkdir(parents=True, exist_ok=True)

    opf_detector = None
    opf_status = "disabled"
    if args.enable_opf:
        try:
            opf_detector = OPFDetector(device=args.opf_device, checkpoint=args.opf_checkpoint)
            opf_status = f"enabled ({args.opf_device})"
        except Exception as exc:
            opf_status = f"unavailable: {exc}"
            print(f"WARNING: {opf_status}", file=sys.stderr)

    results = [detect_record(record, opf_detector) for record in records]
    metrics = compute_metrics(results)

    write_jsonl(args.out_dir / "predictions.jsonl", results)
    (args.out_dir / "summary.json").write_text(
        json.dumps({"opf_status": opf_status, "metrics": metrics}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (args.out_dir / "report.md").write_text(
        build_markdown_report(results=results, metrics=metrics, opf_status=opf_status),
        encoding="utf-8",
    )
    (args.out_dir / "review.html").write_text(
        build_review_html(results=results, metrics=metrics, opf_status=opf_status),
        encoding="utf-8",
    )

    print(json.dumps({"opf_status": opf_status, "metrics": metrics}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
