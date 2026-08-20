"""Render a Markdown document to PDF, with Mermaid diagrams as real vectors.

    python scripts/render_pdf.py docs/architecture.md
    python scripts/render_pdf.py docs/architecture.md -o out/arch.pdf --keep-html

Why it is written this way
--------------------------
The obvious routes are unavailable here: there is no network (so no mermaid.ink,
no CDN, no `npx @mermaid-js/mermaid-cli`), and pandoc / wkhtmltopdf / weasyprint
are not installed. What *is* available is a Chromium-based browser and a local
copy of ``mermaid.min.js`` that ships inside Visual Studio, SQL Server
Management Studio and VS Code.

So the pipeline is:

1. Markdown -> HTML with a small self-contained converter (no `markdown` package
   either), keeping ```mermaid fences aside.
2. Headless Chrome loads that HTML with the local Mermaid bundle and renders
   each diagram to inline **SVG**; the DOM is dumped back out.
3. Each diagram's page orientation is chosen from its own aspect ratio, and the
   resulting text height is computed in points so legibility is verified rather
   than hoped for.
4. Headless Chrome prints the static, script-free HTML to PDF.

The output is a real PDF: selectable text, and diagrams that stay sharp at any
zoom because they are vector SVG, not screenshots.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Page geometry (A4). Content width = page width - margins.
# ---------------------------------------------------------------------------
A4_WIDTH_MM = 210.0
A4_HEIGHT_MM = 297.0
MARGIN_MM = 15.0
DIAGRAM_MARGIN_MM = 10.0

PORTRAIT_CONTENT_MM = A4_WIDTH_MM - 2 * DIAGRAM_MARGIN_MM        # 190mm
LANDSCAPE_CONTENT_MM = A4_HEIGHT_MM - 2 * DIAGRAM_MARGIN_MM      # 277mm

# Mermaid's base label size, in CSS px, at the size it reports in its viewBox.
MERMAID_FONT_PX = 16.0
# Below roughly 8pt, diagram labels stop being comfortably readable in print.
MIN_LABEL_PT = 8.0
MM_PER_PT = 25.4 / 72.0

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]

MERMAID_CANDIDATES = [
    r"C:\Program Files\Microsoft Visual Studio\2022\Community\Common7\IDE"
    r"\CommonExtensions\Microsoft\Markdown\Preview\Dependencies\mermaidJS\mermaid.min.js",
    r"C:\Program Files\Microsoft Visual Studio\2022\Professional\Common7\IDE"
    r"\CommonExtensions\Microsoft\Markdown\Preview\Dependencies\mermaidJS\mermaid.min.js",
    r"C:\Program Files\Microsoft SQL Server Management Studio 21\Release\Common7\IDE"
    r"\CommonExtensions\Microsoft\Markdown\Preview\Dependencies\mermaidJS\mermaid.min.js",
]


def find_binary(candidates: list[str], names: list[str], what: str) -> Path:
    for name in names:
        if found := shutil.which(name):
            return Path(found)
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return path
    raise SystemExit(
        f"Could not find {what}. Looked in:\n  "
        + "\n  ".join(candidates)
        + f"\nPass an explicit path, or install {what}."
    )


# ===========================================================================
# Markdown -> HTML
# ===========================================================================

_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?!\*)")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_HR = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_ULI = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_OLI = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def slugify(text: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    return re.sub(r"[\s_]+", "-", slug)


def inline(text: str) -> str:
    """Escape, then apply inline markup. Code spans are protected first."""
    spans: list[str] = []

    def stash_code(match: re.Match) -> str:
        spans.append(f"<code>{html.escape(match.group(1))}</code>")
        return f"\x00{len(spans) - 1}\x00"

    text = _INLINE_CODE.sub(stash_code, text)
    text = html.escape(text, quote=False)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)
    text = _LINK.sub(
        lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>',
        text,
    )
    text = re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], text)
    return text


def split_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    # Split on pipes that are not escaped.
    return [c.strip() for c in re.split(r"(?<!\\)\|", line)]


def alignments(separator: str) -> list[str]:
    result = []
    for cell in split_row(separator):
        left, right = cell.startswith(":"), cell.endswith(":")
        result.append("center" if left and right else "right" if right else "left")
    return result


class Renderer:
    """Small block-level Markdown renderer, sufficient for this repo's docs."""

    def __init__(self) -> None:
        self.out: list[str] = []
        self.diagrams: list[str] = []

    def render(self, text: str) -> str:
        lines = text.replace("\r\n", "\n").split("\n")
        i = 0
        while i < len(lines):
            line = lines[i]

            # fenced code / mermaid
            fence = re.match(r"^\s*```+\s*(\w*)\s*$", line)
            if fence:
                lang = fence.group(1).lower()
                body, i = self._collect_fence(lines, i + 1)
                if lang == "mermaid":
                    index = len(self.diagrams)
                    self.diagrams.append(body)
                    self.out.append(
                        f'<figure class="diagram" data-index="{index}">'
                        f'<div class="mermaid-target" id="diagram-{index}"></div>'
                        f"</figure>"
                    )
                else:
                    cls = f' class="lang-{lang}"' if lang else ""
                    self.out.append(
                        f"<pre{cls}><code>{html.escape(body)}</code></pre>"
                    )
                continue

            if not line.strip():
                i += 1
                continue

            if _HR.match(line):
                self.out.append('<hr class="rule">')
                i += 1
                continue

            if heading := _HEADING.match(line):
                level = len(heading.group(1))
                content = heading.group(2).strip()
                self.out.append(
                    f'<h{level} id="{slugify(content)}">{inline(content)}</h{level}>'
                )
                i += 1
                continue

            # table: a header row followed by a separator row
            if "|" in line and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
                i = self._table(lines, i)
                continue

            if line.lstrip().startswith(">"):
                i = self._blockquote(lines, i)
                continue

            if _ULI.match(line) or _OLI.match(line):
                i = self._list(lines, i)
                continue

            i = self._paragraph(lines, i)

        return "\n".join(self.out)

    # -- block handlers ----------------------------------------------------

    @staticmethod
    def _collect_fence(lines: list[str], i: int) -> tuple[str, int]:
        body: list[str] = []
        while i < len(lines) and not re.match(r"^\s*```+\s*$", lines[i]):
            body.append(lines[i])
            i += 1
        return "\n".join(body), i + 1

    def _table(self, lines: list[str], i: int) -> int:
        header = split_row(lines[i])
        aligns = alignments(lines[i + 1])
        i += 2
        rows: list[list[str]] = []
        while i < len(lines) and "|" in lines[i] and lines[i].strip():
            rows.append(split_row(lines[i]))
            i += 1

        def cell(tag: str, value: str, index: int) -> str:
            align = aligns[index] if index < len(aligns) else "left"
            return f'<{tag} class="a-{align}">{inline(value)}</{tag}>'

        out = ['<div class="table-wrap"><table><thead><tr>']
        out += [cell("th", c, n) for n, c in enumerate(header)]
        out.append("</tr></thead><tbody>")
        for row in rows:
            out.append("<tr>")
            out += [cell("td", c, n) for n, c in enumerate(row)]
            out.append("</tr>")
        out.append("</tbody></table></div>")
        self.out.append("".join(out))
        return i

    def _blockquote(self, lines: list[str], i: int) -> int:
        body: list[str] = []
        while i < len(lines) and lines[i].lstrip().startswith(">"):
            body.append(lines[i].lstrip()[1:].lstrip())
            i += 1
        inner = Renderer()
        inner.diagrams = self.diagrams
        self.out.append(f"<blockquote>{inner.render(chr(10).join(body))}</blockquote>")
        return i

    def _list(self, lines: list[str], i: int) -> int:
        ordered = bool(_OLI.match(lines[i]))
        base_indent = len(re.match(r"^(\s*)", lines[i]).group(1))
        items: list[list[str]] = []

        while i < len(lines):
            line = lines[i]
            match = _OLI.match(line) if ordered else _ULI.match(line)
            other = _ULI.match(line) if ordered else _OLI.match(line)

            if match and len(match.group(1)) <= base_indent + 1:
                items.append([match.group(3) if ordered else match.group(2)])
                i += 1
                continue
            if not line.strip():
                # A blank line ends the list unless the next line continues it.
                nxt = lines[i + 1] if i + 1 < len(lines) else ""
                if not (nxt.strip() and (_ULI.match(nxt) or _OLI.match(nxt)
                                         or nxt.startswith(" " * (base_indent + 2)))):
                    i += 1
                    break
                i += 1
                continue
            if items and (line.startswith(" " * (base_indent + 2)) or other):
                items[-1].append(line.strip())
                i += 1
                continue
            break

        tag = "ol" if ordered else "ul"
        rendered = "".join(f"<li>{inline(' '.join(item))}</li>" for item in items)
        self.out.append(f"<{tag}>{rendered}</{tag}>")
        return i

    def _paragraph(self, lines: list[str], i: int) -> int:
        body: list[str] = []
        while i < len(lines) and lines[i].strip():
            line = lines[i]
            if (_HEADING.match(line) or _HR.match(line)
                    or re.match(r"^\s*```", line)
                    or line.lstrip().startswith(">")
                    or _ULI.match(line) or _OLI.match(line)):
                break
            body.append(line.strip())
            i += 1
        if body:
            self.out.append(f"<p>{inline(' '.join(body))}</p>")
        return i


# ===========================================================================
# HTML shell
# ===========================================================================

CSS = """
:root {
  --ink: #16181d; --muted: #5b6270; --line: #d7dbe3; --accent: #1f4fbf;
  --code-bg: #f4f5f8; --thead: #eef1f6;
}
* { box-sizing: border-box; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body {
  margin: 0; color: var(--ink); background: #fff;
  font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif;
  font-size: 10.2pt; line-height: 1.5;
}
h1, h2, h3, h4 { line-height: 1.25; break-after: avoid; page-break-after: avoid; }
h1 { font-size: 21pt; margin: 0 0 4mm; letter-spacing: -.01em; }
h2 {
  font-size: 14.5pt; margin: 9mm 0 3mm; padding-bottom: 1.5mm;
  border-bottom: 1px solid var(--line); break-before: auto;
}
h3 { font-size: 11.5pt; margin: 6mm 0 2mm; }
h4 { font-size: 10.5pt; margin: 5mm 0 2mm; color: var(--muted); }
p { margin: 0 0 3mm; orphans: 3; widows: 3; }
ul, ol { margin: 0 0 3mm; padding-left: 6mm; }
li { margin-bottom: 1.2mm; }
a { color: var(--accent); text-decoration: none; }
strong { font-weight: 640; }
code {
  font-family: "Cascadia Mono", Consolas, "SF Mono", monospace;
  font-size: 8.8pt; background: var(--code-bg);
  padding: 0.3mm 1mm; border-radius: 1mm;
}
pre {
  background: var(--code-bg); border: 1px solid var(--line); border-radius: 1.5mm;
  padding: 2.5mm 3mm; overflow: hidden; margin: 0 0 4mm;
  break-inside: avoid; page-break-inside: avoid;
}
pre code { background: none; padding: 0; font-size: 8.2pt; line-height: 1.42; }
blockquote {
  margin: 0 0 4mm; padding: 1mm 0 1mm 4mm;
  border-left: 2px solid var(--accent); color: var(--muted);
}
blockquote p { margin: 0 0 1mm; }
hr.rule { border: 0; border-top: 1px solid var(--line); margin: 7mm 0; }

.table-wrap { margin: 0 0 4mm; break-inside: avoid; page-break-inside: avoid; }
table { width: 100%; border-collapse: collapse; font-size: 8.8pt; }
th, td {
  border: 1px solid var(--line); padding: 1.4mm 2mm; vertical-align: top;
  text-align: left;
}
th { background: var(--thead); font-weight: 640; }
tr:nth-child(even) td { background: #fafbfd; }
td.a-right, th.a-right { text-align: right; }
td.a-center, th.a-center { text-align: center; }
td code, th code { font-size: 8pt; }

/* --- diagrams ---------------------------------------------------------- */
figure.diagram {
  margin: 5mm 0 6mm; text-align: center;
  break-inside: avoid; page-break-inside: avoid;
}
figure.diagram svg {
  max-width: 100% !important; width: 100%; height: auto; display: block;
  margin: 0 auto;
}
/* A diagram wide enough that portrait would shrink its labels below the
   legibility floor gets its own landscape page. */
@page portrait { size: A4 portrait; margin: __MARGIN__mm; }
@page landscape { size: A4 landscape; margin: __DIAGRAM_MARGIN__mm; }
@page fullportrait { size: A4 portrait; margin: __DIAGRAM_MARGIN__mm; }
body { page: portrait; }
figure.diagram.landscape { page: landscape; break-before: page; break-after: page; }
figure.diagram.fullpage { page: fullportrait; break-before: page; break-after: page; }
figure.diagram.landscape svg, figure.diagram.fullpage svg { max-height: 88vh; }

figcaption {
  font-size: 8pt; color: var(--muted); margin-top: 2mm; font-style: italic;
}
.doc-meta { color: var(--muted); font-size: 8.6pt; margin: 0 0 6mm; }
"""

PAGE_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>__TITLE__</title>
<style>__CSS__</style>
__HEAD_EXTRA__
</head>
<body>
__BODY__
</body></html>
"""

RENDER_SCRIPT = """
<script src="__MERMAID__"></script>
<script>
(async () => {
  const ns = window.__esbuild_esm_mermaid_nm;
  const mermaid = (ns && ns.mermaid && (ns.mermaid.default || ns.mermaid))
                  || window.mermaid;
  const graphs = __GRAPHS__;
  mermaid.initialize({
    startOnLoad: false,
    theme: 'neutral',
    securityLevel: 'loose',
    fontFamily: 'Segoe UI, Helvetica Neue, Arial, sans-serif',
    fontSize: 16,
    flowchart: { htmlLabels: true, curve: 'basis', nodeSpacing: 42,
                 rankSpacing: 52, padding: 10, useMaxWidth: false }
  });
  for (let i = 0; i < graphs.length; i++) {
    const target = document.getElementById('diagram-' + i);
    try {
      const { svg } = await mermaid.render('mmd-' + i, graphs[i]);
      target.innerHTML = svg;
      const el = target.querySelector('svg');
      if (el) {
        const vb = (el.getAttribute('viewBox') || '').split(/\\s+/).map(Number);
        target.parentElement.setAttribute('data-w', vb[2] || 0);
        target.parentElement.setAttribute('data-h', vb[3] || 0);
        el.removeAttribute('style');
        el.removeAttribute('width');
        el.removeAttribute('height');
      }
    } catch (e) {
      target.textContent = 'DIAGRAM ERROR: ' + e.message;
      target.parentElement.setAttribute('data-error', e.message);
    }
  }
  document.documentElement.setAttribute('data-mermaid', 'done');
})();
</script>
"""


# ===========================================================================
# Chrome
# ===========================================================================


def run_chrome(chrome: Path, args: list[str], timeout: int = 180) -> str:
    command = [
        str(chrome), "--headless=new", "--disable-gpu", "--no-sandbox",
        "--no-first-run", "--no-default-browser-check",
        "--disable-extensions", "--disable-dev-shm-usage",
        "--allow-file-access-from-files",
        "--run-all-compositor-stages-before-draw",
        *args,
    ]
    result = subprocess.run(command, capture_output=True, text=True,
                            timeout=timeout, encoding="utf-8", errors="replace")
    if result.returncode != 0 and not result.stdout:
        raise SystemExit(
            f"Chrome failed (exit {result.returncode}):\n{result.stderr[:1500]}"
        )
    return result.stdout


def as_file_url(path: Path) -> str:
    return path.resolve().as_uri()


# ===========================================================================
# Legibility
# ===========================================================================


def choose_layout(width_px: float, height_px: float) -> tuple[str, float]:
    """Pick a page for this diagram and report its resulting label size in pt.

    The diagram is scaled to the content width of whichever page it lands on,
    so the label height in millimetres is::

        label_mm = MERMAID_FONT_PX * (content_mm / width_px)

    Portrait is preferred; a diagram only takes a landscape page when portrait
    would push its labels below the legibility floor. That keeps the document
    readable in one orientation wherever it can be.
    """
    if width_px <= 0:
        return "inline", 0.0

    def label_pt(content_mm: float) -> float:
        return (MERMAID_FONT_PX * (content_mm / width_px)) / MM_PER_PT

    # Inline in the text column first, if it is legible there.
    inline_pt = label_pt(A4_WIDTH_MM - 2 * MARGIN_MM)
    if inline_pt >= MIN_LABEL_PT:
        return "inline", inline_pt

    # Then a full portrait page with reduced margins.
    portrait_pt = label_pt(PORTRAIT_CONTENT_MM)
    landscape_pt = label_pt(LANDSCAPE_CONTENT_MM)

    # A landscape page is only useful if the diagram is wide relative to the
    # page it would sit on -- a tall diagram would be clipped by page height.
    landscape_fits = (height_px / width_px) <= (
        (A4_WIDTH_MM - 2 * DIAGRAM_MARGIN_MM) / LANDSCAPE_CONTENT_MM
    )

    if portrait_pt >= MIN_LABEL_PT:
        return "fullpage", portrait_pt
    if landscape_fits:
        return "landscape", landscape_pt
    return "fullpage", portrait_pt


# ===========================================================================
# Main
# ===========================================================================


def build(source: Path, output: Path, chrome: Path, mermaid: Path,
          keep_html: bool) -> None:
    markdown = source.read_text(encoding="utf-8")

    renderer = Renderer()
    body = renderer.render(markdown)

    title = source.stem.replace("-", " ").replace("_", " ").title()
    first_heading = re.search(r"^#\s+(.+)$", markdown, re.M)
    if first_heading:
        title = first_heading.group(1).strip()

    workdir = Path(tempfile.mkdtemp(prefix="mdpdf-"))
    try:
        # Chrome will not load a file:// script from outside the page's own
        # directory reliably, so the bundle is copied next to the page.
        local_mermaid = workdir / "mermaid.min.js"
        shutil.copyfile(mermaid, local_mermaid)

        script = (
            RENDER_SCRIPT
            .replace("__MERMAID__", "mermaid.min.js")
            .replace("__GRAPHS__", json.dumps(renderer.diagrams))
        )
        css = (CSS
               .replace("__MARGIN__", f"{MARGIN_MM:g}")
               .replace("__DIAGRAM_MARGIN__", f"{DIAGRAM_MARGIN_MM:g}"))

        stage1 = workdir / "stage1.html"
        stage1.write_text(
            PAGE_TEMPLATE
            .replace("__TITLE__", html.escape(title))
            .replace("__CSS__", css)
            .replace("__HEAD_EXTRA__", "")
            .replace("__BODY__", body + script),
            encoding="utf-8",
        )

        print(f"  rendering {len(renderer.diagrams)} diagram(s) with Mermaid…")
        dom = run_chrome(chrome, [
            "--virtual-time-budget=45000",
            "--dump-dom", as_file_url(stage1),
        ])
        if 'data-mermaid="done"' not in dom:
            raise SystemExit(
                "Mermaid did not finish rendering. The bundle may be an "
                "incompatible version; try another mermaid.min.js with --mermaid."
            )

        for match in re.finditer(r'data-error="([^"]*)"', dom):
            print(f"  !! diagram error: {html.unescape(match.group(1))}")

        # Decide each diagram's page from its own measured aspect ratio.
        def assign(match: re.Match) -> str:
            attrs = match.group(1)
            width = float(re.search(r'data-w="([\d.]+)"', attrs).group(1)) \
                if re.search(r'data-w="([\d.]+)"', attrs) else 0.0
            height = float(re.search(r'data-h="([\d.]+)"', attrs).group(1)) \
                if re.search(r'data-h="([\d.]+)"', attrs) else 0.0
            index = re.search(r'data-index="(\d+)"', attrs)
            layout, pt = choose_layout(width, height)
            report.append((int(index.group(1)) if index else -1,
                           width, height, layout, pt))
            klass = "" if layout == "inline" else f" {layout}"
            return f'<figure class="diagram{klass}"{attrs[len("class=\"diagram\""):]}'

        report: list[tuple[int, float, float, str, float]] = []
        dom = re.sub(r'<figure (class="diagram"[^>]*)', assign, dom)

        # The 2.7 MB bundle and the render script are dead weight for printing.
        dom = re.sub(r"<script[^>]*>.*?</script>", "", dom, flags=re.S)

        stage2 = workdir / "stage2.html"
        stage2.write_text(dom, encoding="utf-8")

        output.parent.mkdir(parents=True, exist_ok=True)
        print(f"  printing to {output}…")
        run_chrome(chrome, [
            "--no-pdf-header-footer",
            "--virtual-time-budget=15000",
            f"--print-to-pdf={output.resolve()}",
            as_file_url(stage2),
        ])
        if not output.exists():
            raise SystemExit("Chrome did not produce a PDF.")

        print(f"\n  {output}  ({output.stat().st_size / 1024:.0f} KB)")
        print(f"\n  {'#':<3} {'size (px)':<14} {'page':<10} {'label':<8} legible")
        for index, width, height, layout, pt in sorted(report):
            ok = "yes" if pt >= MIN_LABEL_PT else "NO"
            print(f"  {index:<3} {int(width):>5}x{int(height):<7} "
                  f"{layout:<10} {pt:>5.1f}pt  {ok}")
        worst = min((pt for *_, pt in report), default=99)
        if worst < MIN_LABEL_PT:
            print(f"\n  WARNING: smallest diagram label is {worst:.1f}pt "
                  f"(floor is {MIN_LABEL_PT}pt). Consider splitting that diagram.")

        if keep_html:
            kept = output.with_suffix(".html")
            shutil.copyfile(stage2, kept)
            print(f"  kept HTML at {kept}")
    finally:
        if not keep_html:
            shutil.rmtree(workdir, ignore_errors=True)
        else:
            print(f"  work dir: {workdir}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=Path, help="markdown file to render")
    parser.add_argument("-o", "--output", type=Path, default=None,
                        help="output PDF (default: alongside the source)")
    parser.add_argument("--chrome", type=Path, default=None)
    parser.add_argument("--mermaid", type=Path, default=None,
                        help="path to a mermaid.min.js bundle")
    parser.add_argument("--keep-html", action="store_true",
                        help="also write the intermediate HTML, for inspection")
    args = parser.parse_args()

    source = args.source if args.source.is_absolute() else REPO_ROOT / args.source
    if not source.exists():
        raise SystemExit(f"not found: {source}")
    output = args.output or source.with_suffix(".pdf")
    if not output.is_absolute():
        output = REPO_ROOT / output

    chrome = args.chrome or find_binary(
        CHROME_CANDIDATES, ["chrome", "google-chrome", "chromium", "msedge"],
        "a Chromium-based browser",
    )
    mermaid = args.mermaid or find_binary(MERMAID_CANDIDATES, [], "mermaid.min.js")

    print(f"\n  source:  {source}")
    print(f"  chrome:  {chrome}")
    print(f"  mermaid: {mermaid}\n")

    build(source, output, chrome, mermaid, args.keep_html)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
