"""Render Mermaid diagrams into the PDF as native vector graphics.

The Markdown gets standard ```mermaid fences (GitHub, VS Code, Obsidian and
Claude all render those). For the PDF there is no browser available, so this
module parses the flowchart/mindmap subset we ask Claude for and draws it with
ReportLab: layered layout, boxes, arrows and edge labels - no external
dependency. If `mmdc` (mermaid-cli) happens to be installed it is used instead,
since it handles every diagram type.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field

from reportlab.graphics.shapes import (
    Circle,
    Drawing,
    Group,
    Line,
    Polygon,
    Rect,
    String,
)
from reportlab.lib import colors
from reportlab.pdfbase.pdfmetrics import stringWidth

from .textutil import latin1_safe

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------

NODE_FILL = colors.HexColor("#eef3f9")
NODE_LINE = colors.HexColor("#1f4e79")
NODE_TEXT = colors.HexColor("#12293f")
EDGE_LINE = colors.HexColor("#5a6b7d")
EDGE_TEXT = colors.HexColor("#44566a")

FONT = "Helvetica"
FONT_BOLD = "Helvetica-Bold"
FONT_SIZE = 8.5
LABEL_SIZE = 7.0
LINE_HEIGHT = 11.0

PAD_X, PAD_Y = 12.0, 9.0
MIN_W, MIN_H = 74.0, 30.0
WRAP_WIDTH = 128.0          # max text width inside a node, points
LAYER_GAP = 58.0
NODE_GAP = 20.0


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass
class Node:
    key: str
    label: str
    shape: str = "rect"          # rect | round | stadium | diamond | circle
    lines: list[str] = field(default_factory=list)
    w: float = 0.0
    h: float = 0.0
    x: float = 0.0               # centre
    y: float = 0.0
    layer: int = 0
    order: float = 0.0


@dataclass
class Edge:
    src: str
    dst: str
    label: str = ""
    dashed: bool = False
    arrow: bool = True


@dataclass
class Graph:
    direction: str = "TD"        # TD | LR
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)

    def node(self, key: str, label: str | None = None, shape: str | None = None) -> Node:
        if key not in self.nodes:
            self.nodes[key] = Node(key, label if label is not None else key)
        n = self.nodes[key]
        if label:
            n.label = label
        if shape:
            n.shape = shape
        return n


# ---------------------------------------------------------------------------
# Mermaid parsing (flowchart + mindmap subset)
# ---------------------------------------------------------------------------

CONNECTOR = re.compile(r"(<?-\.-+>?|<?=+>|<?--+>?|<?\.\.+>)")
NODE_TOKEN = re.compile(
    r"""^\s*(?P<id>[A-Za-z0-9_][A-Za-z0-9_.\-]*)\s*
        (?P<body>
            \(\(.*?\)\) | \[\[.*?\]\] | \[\(.*?\)\] | \[/.*?/\] | \[\\.*?\\\] |
            \{\{.*?\}\} | \[.*?\] | \(.*?\) | \{.*?\} | >.*?\]
        )?\s*$""",
    re.X | re.S,
)
SKIP_LINE = re.compile(
    r"^\s*(%%|classDef\b|class\b|style\b|linkStyle\b|click\b|direction\b|end\b|"
    r"subgraph\b|accTitle\b|accDescr\b)",
    re.I,
)

_SHAPE_BY_WRAPPER = [
    ("((", "))", "circle"),
    ("[[", "]]", "rect"),
    ("[(", ")]", "stadium"),
    ("{{", "}}", "diamond"),
    ("[", "]", "rect"),
    ("(", ")", "round"),
    ("{", "}", "diamond"),
    (">", "]", "rect"),
]


def _clean_label(text: str) -> str:
    text = latin1_safe(text).strip().strip('"').strip("'").strip()
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = text.replace("&quot;", '"').replace("&amp;", "&")
    text = re.sub(r"#(\d+);", "", text)
    return text.strip()


def _parse_node_token(token: str) -> tuple[str, str | None, str | None] | None:
    """'A[Data plane]' -> ('A', 'Data plane', 'rect')."""
    token = token.strip()
    if not token:
        return None
    m = NODE_TOKEN.match(token)
    if not m:
        return None
    key = m.group("id")
    body = m.group("body")
    if not body:
        return key, None, None
    for open_w, close_w, shape in _SHAPE_BY_WRAPPER:
        if body.startswith(open_w) and body.endswith(close_w):
            return key, _clean_label(body[len(open_w): -len(close_w)]), shape
    return key, _clean_label(body), "rect"


def _parse_flowchart(lines: list[str], direction: str) -> Graph:
    g = Graph(direction=direction)

    for raw in lines:
        line = raw.strip()
        if not line or SKIP_LINE.match(line):
            continue
        line = re.sub(r"\s*;\s*$", "", line)

        parts = CONNECTOR.split(line)
        if len(parts) == 1:
            parsed = _parse_node_token(line)
            if parsed:
                key, label, shape = parsed
                g.node(key, label, shape)
            continue

        # parts alternate: text, connector, text, connector, ...
        i = 0
        prev_key: str | None = None
        pending_label = ""
        while i < len(parts):
            chunk = parts[i].strip()
            is_connector = bool(i % 2)

            if is_connector:
                dashed = "." in chunk or "=" in chunk
                arrow = ">" in chunk
                i += 1
                if i >= len(parts):
                    break
                target = parts[i].strip()

                # `A -- yes --> B`: bare text between an arrow-less connector
                # and an arrow. A chain (`A --> B --> C`) is not a label because
                # its leading connector already carries the arrowhead.
                if (
                    not arrow
                    and i + 1 < len(parts)
                    and CONNECTOR.fullmatch(parts[i + 1].strip())
                    and not re.search(r"[\[\](){}]", target)
                ):
                    pending_label = _clean_label(target)
                    i += 1
                    continue

                # `A -->|yes| B`
                m = re.match(r"\|(?P<lab>[^|]*)\|\s*(?P<rest>.*)$", target, re.S)
                if m:
                    pending_label = _clean_label(m.group("lab"))
                    target = m.group("rest").strip()

                parsed = _parse_node_token(target)
                if not parsed:
                    i += 1
                    continue
                key, label, shape = parsed
                g.node(key, label, shape)
                if prev_key:
                    g.edges.append(
                        Edge(prev_key, key, pending_label, dashed=dashed, arrow=arrow)
                    )
                pending_label = ""
                prev_key = key
                i += 1
                continue

            parsed = _parse_node_token(chunk)
            if parsed:
                key, label, shape = parsed
                g.node(key, label, shape)
                prev_key = key
            i += 1

    return g


def _parse_mindmap(lines: list[str]) -> Graph:
    """Indentation-based mindmap -> left-to-right tree."""
    g = Graph(direction="LR")
    stack: list[tuple[int, str]] = []
    counter = 0
    for raw in lines:
        if not raw.strip() or SKIP_LINE.match(raw):
            continue
        indent = len(raw) - len(raw.lstrip())
        text = raw.strip()
        shape = "round"
        m = re.match(r"^[A-Za-z0-9_]+(\(\(|\[|\()", text)
        parsed = _parse_node_token(text) if m else None
        if parsed and parsed[1]:
            label, shape = parsed[1], parsed[2] or "round"
        else:
            label = _clean_label(re.sub(r"^[-*]\s*", "", text))
        if not label:
            continue
        counter += 1
        key = f"m{counter}"
        g.node(key, label, shape)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if stack:
            g.edges.append(Edge(stack[-1][1], key))
        stack.append((indent, key))
    return g


def parse_mermaid(source: str) -> Graph | None:
    """Parse the supported Mermaid subset. Returns None if unsupported/empty."""
    lines = [ln for ln in source.splitlines() if ln.strip()]
    if not lines:
        return None
    header = lines[0].strip()

    if re.match(r"^mindmap\b", header, re.I):
        g = _parse_mindmap(lines[1:])
    else:
        m = re.match(r"^(?:flowchart|graph)\s+([A-Za-z]{2})\b", header, re.I)
        if not m:
            return None
        raw_dir = m.group(1).upper()
        direction = "LR" if raw_dir in ("LR", "RL") else "TD"
        g = _parse_flowchart(lines[1:], direction)

    if not g.nodes:
        return None
    return g


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


def _wrap(label: str, font: str = FONT, size: float = FONT_SIZE) -> list[str]:
    out: list[str] = []
    for hard_line in label.split("\n"):
        words = hard_line.split()
        if not words:
            continue
        cur = words[0]
        for word in words[1:]:
            trial = f"{cur} {word}"
            if stringWidth(trial, font, size) <= WRAP_WIDTH:
                cur = trial
            else:
                out.append(cur)
                cur = word
        out.append(cur)
    return out or [label]


def _size_nodes(g: Graph) -> None:
    for n in g.nodes.values():
        n.lines = _wrap(n.label)
        text_w = max(stringWidth(ln, FONT, FONT_SIZE) for ln in n.lines)
        n.w = max(MIN_W, text_w + 2 * PAD_X)
        n.h = max(MIN_H, len(n.lines) * LINE_HEIGHT + 2 * PAD_Y)
        if n.shape == "diamond":
            n.w += 22
            n.h += 12
        elif n.shape == "circle":
            side = max(n.w, n.h) * 0.92
            n.w = n.h = side


def _assign_layers(g: Graph) -> None:
    incoming = {k: 0 for k in g.nodes}
    for e in g.edges:
        if e.dst in incoming and e.src in g.nodes and e.src != e.dst:
            incoming[e.dst] += 1
    for n in g.nodes.values():
        n.layer = 0
    for _ in range(len(g.nodes) + 1):
        changed = False
        for e in g.edges:
            if e.src not in g.nodes or e.dst not in g.nodes or e.src == e.dst:
                continue
            want = g.nodes[e.src].layer + 1
            if g.nodes[e.dst].layer < want:
                g.nodes[e.dst].layer = want
                changed = True
        if not changed:
            break


def _order_layers(g: Graph) -> list[list[Node]]:
    layers: dict[int, list[Node]] = {}
    for i, n in enumerate(g.nodes.values()):
        n.order = float(i)
        layers.setdefault(n.layer, []).append(n)
    ordered = [layers[k] for k in sorted(layers)]

    preds: dict[str, list[str]] = {k: [] for k in g.nodes}
    for e in g.edges:
        if e.src in g.nodes and e.dst in g.nodes:
            preds[e.dst].append(e.src)

    for _ in range(2):  # barycentre sweeps to reduce edge crossings
        for layer in ordered[1:]:
            for n in layer:
                parents = [g.nodes[p].order for p in preds[n.key] if p in g.nodes]
                if parents:
                    n.order = sum(parents) / len(parents)
            layer.sort(key=lambda n: n.order)
            for i, n in enumerate(layer):
                n.order = float(i)
    return ordered


def layout(g: Graph) -> tuple[float, float, list[list[Node]]]:
    _size_nodes(g)
    _assign_layers(g)
    layers = _order_layers(g)
    vertical = g.direction == "TD"

    if vertical:
        layer_extent = [max(n.h for n in layer) for layer in layers]
        cross_extent = [
            sum(n.w for n in layer) + NODE_GAP * (len(layer) - 1) for layer in layers
        ]
        total_main = sum(layer_extent) + LAYER_GAP * (len(layers) - 1)
        total_cross = max(cross_extent)
        pos = 0.0
        for layer, extent in zip(layers, layer_extent):
            start = (total_cross - (sum(n.w for n in layer)
                                    + NODE_GAP * (len(layer) - 1))) / 2
            for n in layer:
                n.x = start + n.w / 2
                n.y = total_main - (pos + extent / 2)
                start += n.w + NODE_GAP
            pos += extent + LAYER_GAP
        return total_cross, total_main, layers

    layer_extent = [max(n.w for n in layer) for layer in layers]
    cross_extent = [
        sum(n.h for n in layer) + NODE_GAP * (len(layer) - 1) for layer in layers
    ]
    total_main = sum(layer_extent) + LAYER_GAP * (len(layers) - 1)
    total_cross = max(cross_extent)
    pos = 0.0
    for layer, extent in zip(layers, layer_extent):
        start = (total_cross - (sum(n.h for n in layer)
                                + NODE_GAP * (len(layer) - 1))) / 2
        for n in layer:
            n.x = pos + extent / 2
            n.y = total_cross - (start + n.h / 2)
            start += n.h + NODE_GAP
        pos += extent + LAYER_GAP
    return total_main, total_cross, layers


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------


def _boundary_point(n: Node, tx: float, ty: float) -> tuple[float, float]:
    """Where the ray from the node centre toward (tx, ty) leaves the node."""
    dx, dy = tx - n.x, ty - n.y
    if dx == 0 and dy == 0:
        return n.x, n.y
    hw, hh = n.w / 2, n.h / 2
    if n.shape == "circle":
        r = max(hw, hh)
        dist = (dx * dx + dy * dy) ** 0.5
        return n.x + dx / dist * r, n.y + dy / dist * r
    if n.shape == "diamond":
        t = 1.0 / (abs(dx) / hw + abs(dy) / hh)
        return n.x + dx * t, n.y + dy * t
    scale = min(hw / abs(dx) if dx else float("inf"),
                hh / abs(dy) if dy else float("inf"))
    return n.x + dx * scale, n.y + dy * scale


def _arrow_head(g: Group, x1: float, y1: float, x2: float, y2: float) -> None:
    dx, dy = x2 - x1, y2 - y1
    dist = (dx * dx + dy * dy) ** 0.5 or 1.0
    ux, uy = dx / dist, dy / dist
    size, half = 6.5, 2.8
    bx, by = x2 - ux * size, y2 - uy * size
    g.add(Polygon(
        [x2, y2, bx - uy * half, by + ux * half, bx + uy * half, by - ux * half],
        fillColor=EDGE_LINE, strokeColor=EDGE_LINE, strokeWidth=0.3,
    ))


def _draw_node(grp: Group, n: Node) -> None:
    x, y = n.x - n.w / 2, n.y - n.h / 2
    if n.shape == "circle":
        grp.add(Circle(n.x, n.y, max(n.w, n.h) / 2, fillColor=NODE_FILL,
                       strokeColor=NODE_LINE, strokeWidth=0.9))
    elif n.shape == "diamond":
        grp.add(Polygon([n.x, n.y + n.h / 2, n.x + n.w / 2, n.y,
                         n.x, n.y - n.h / 2, n.x - n.w / 2, n.y],
                        fillColor=NODE_FILL, strokeColor=NODE_LINE, strokeWidth=0.9))
    else:
        radius = {"round": 7, "stadium": n.h / 2}.get(n.shape, 2.5)
        grp.add(Rect(x, y, n.w, n.h, rx=radius, ry=radius, fillColor=NODE_FILL,
                     strokeColor=NODE_LINE, strokeWidth=0.9))

    total = len(n.lines) * LINE_HEIGHT
    top = n.y + total / 2 - LINE_HEIGHT + 3.0
    for i, line in enumerate(n.lines):
        grp.add(String(n.x, top - i * LINE_HEIGHT, line, fontName=FONT,
                       fontSize=FONT_SIZE, fillColor=NODE_TEXT, textAnchor="middle"))


def _draw_edge_label(grp: Group, text: str, x: float, y: float) -> None:
    width = stringWidth(text, FONT, LABEL_SIZE) + 5
    grp.add(Rect(x - width / 2, y - 4, width, 10, fillColor=colors.white,
                 strokeColor=None))
    grp.add(String(x, y - 1.5, text, fontName=FONT, fontSize=LABEL_SIZE,
                   fillColor=EDGE_TEXT, textAnchor="middle"))


def graph_to_drawing(g: Graph, max_width: float) -> Drawing:
    width, height, _ = layout(g)
    grp = Group()

    for e in g.edges:
        src, dst = g.nodes.get(e.src), g.nodes.get(e.dst)
        if not src or not dst or src is dst:
            continue
        x1, y1 = _boundary_point(src, dst.x, dst.y)
        x2, y2 = _boundary_point(dst, src.x, src.y)
        grp.add(Line(x1, y1, x2, y2, strokeColor=EDGE_LINE, strokeWidth=0.9,
                     strokeDashArray=[2.5, 2.5] if e.dashed else None))
        if e.arrow:
            _arrow_head(grp, x1, y1, x2, y2)
        if e.label:
            _draw_edge_label(grp, e.label, (x1 + x2) / 2, (y1 + y2) / 2)

    for n in g.nodes.values():
        _draw_node(grp, n)

    pad = 4.0
    scale = min(1.0, (max_width - 2 * pad) / width) if width else 1.0
    drawing = Drawing(width * scale + 2 * pad, height * scale + 2 * pad)
    grp.transform = (scale, 0, 0, scale, pad, pad)
    drawing.add(grp)
    return drawing


# ---------------------------------------------------------------------------
# Optional mermaid-cli path
# ---------------------------------------------------------------------------


def render_with_mmdc(source: str, out_dir: str) -> str | None:
    """Render via mermaid-cli when it is installed; returns a PNG path or None."""
    mmdc = shutil.which("mmdc")
    if not mmdc:
        return None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".mmd", dir=out_dir,
                                         delete=False, encoding="utf-8") as fh:
            fh.write(source)
            src_path = fh.name
        png_path = src_path[:-4] + ".png"
        subprocess.run(
            [mmdc, "-i", src_path, "-o", png_path, "-b", "white", "-s", "3"],
            check=True, capture_output=True, timeout=90,
        )
        return png_path if os.path.exists(png_path) else None
    except (subprocess.SubprocessError, OSError):
        return None


def mermaid_to_flowable(source: str, max_width: float, out_dir: str | None = None):
    """Best-available rendering of a mermaid block, or None if unsupported."""
    if out_dir:
        png = render_with_mmdc(source, out_dir)
        if png:
            from reportlab.platypus import Image

            from PIL import Image as PILImage  # noqa: PLC0415 - optional dependency

            with PILImage.open(png) as im:
                iw, ih = im.size
            scale = min(1.0, max_width / iw)
            return Image(png, width=iw * scale, height=ih * scale)

    graph = parse_mermaid(source)
    if graph is None:
        return None
    try:
        return graph_to_drawing(graph, max_width)
    except Exception:  # noqa: BLE001 - never let a diagram break the document
        return None
