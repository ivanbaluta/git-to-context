#!/usr/bin/env python3
"""
Flatten a GitHub repo into a single static HTML page for fast skimming and Ctrl+F.
"""

from __future__ import annotations
import argparse
import html
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import webbrowser
from dataclasses import dataclass
from typing import List

# External deps
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_for_filename, TextLexer
import markdown

MAX_DEFAULT_BYTES = 50 * 1024
BINARY_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".svg",
    ".ico",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
    ".7z",
    ".rar",
    ".mp3",
    ".mp4",
    ".mov",
    ".avi",
    ".mkv",
    ".wav",
    ".ogg",
    ".flac",
    ".ttf",
    ".otf",
    ".eot",
    ".woff",
    ".woff2",
    ".so",
    ".dll",
    ".dylib",
    ".class",
    ".jar",
    ".exe",
    ".bin",
}
MARKDOWN_EXTENSIONS = {".md", ".markdown", ".mdown", ".mkd", ".mkdn"}


@dataclass
class RenderDecision:
    include: bool
    reason: str  # "ok" | "binary" | "too_large" | "ignored"


@dataclass
class FileInfo:
    path: pathlib.Path  # absolute path on disk
    rel: str  # path relative to repo root (slash-separated)
    size: int
    decision: RenderDecision


def run(
    cmd: List[str], cwd: str | None = None, check: bool = True
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=cwd, check=check, text=True, capture_output=True, encoding="utf-8"
    )


def git_clone(
    url: str, dst: str, ref: str | None = None, full_history: bool = False
) -> None:
    """Clone a git repository, optionally at a specific branch or with full history for diffing."""
    cmd = ["git", "clone"]
    if not full_history:
        cmd.extend(["--depth", "1"])
    if ref:
        cmd.extend(["--branch", ref])
    cmd.extend([url, dst])
    run(cmd)


def git_head_commit(repo_dir: str) -> str:
    try:
        cp = run(["git", "rev-parse", "HEAD"], cwd=repo_dir)
        return cp.stdout.strip()
    except Exception:
        return "(unknown)"


def bytes_human(n: int) -> str:
    """Human-readable bytes: 1 decimal for KiB and above, integer for B."""
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    f = float(n)
    i = 0
    while f >= 1024.0 and i < len(units) - 1:
        f /= 1024.0
        i += 1
    if i == 0:
        return f"{int(f)} {units[i]}"
    else:
        return f"{f:.1f} {units[i]}"


def looks_binary(path: pathlib.Path) -> bool:
    ext = path.suffix.lower()
    if ext in BINARY_EXTENSIONS:
        return True
    try:
        with path.open("rb") as f:
            chunk = f.read(8192)
        if b"\x00" in chunk:
            return True
        # Heuristic: try UTF-8 decode; if it hard-fails, likely binary
        try:
            chunk.decode("utf-8")
        except UnicodeDecodeError:
            return True
        return False
    except Exception:
        # If unreadable, treat as binary to be safe
        return True


def decide_file(
    path: pathlib.Path, repo_root: pathlib.Path, max_bytes: int
) -> FileInfo:
    rel = str(path.relative_to(repo_root)).replace(os.sep, "/")
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        size = 0
    # Ignore VCS and build junk
    if "/.git/" in f"/{rel}/" or rel.startswith(".git/"):
        return FileInfo(path, rel, size, RenderDecision(False, "ignored"))
    if size > max_bytes:
        return FileInfo(path, rel, size, RenderDecision(False, "too_large"))
    if looks_binary(path):
        return FileInfo(path, rel, size, RenderDecision(False, "binary"))
    return FileInfo(path, rel, size, RenderDecision(True, "ok"))


def get_git_files(repo_root: pathlib.Path) -> List[pathlib.Path]:
    """
    Retrieve a list of repository files using git, respecting .gitignore.
    Falls back to a recursive glob if not in a git repository.
    """
    try:
        # --cached: tracked files
        # --others: untracked files
        # --exclude-standard: respect .gitignore
        # -z: zero-byte separated output
        cp = run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=str(repo_root),
        )
        rel_paths = [p for p in cp.stdout.split("\0") if p]
        return [repo_root / p for p in rel_paths]
    except Exception:
        # Fallback for non-git directories
        return [
            p for p in repo_root.rglob("*") if p.is_file() and ".git" not in p.parts
        ]


def get_changed_files(repo_root: pathlib.Path, diff_ref: str) -> List[pathlib.Path]:
    """Get list of modified or added files compared to a specific ref."""
    try:
        # --diff-filter=ACMR excludes deleted files since we can't read their contents anyway
        cp = run(
            ["git", "diff", "--name-only", "--diff-filter=ACMR", diff_ref],
            cwd=str(repo_root),
        )
        rel_paths = [p for p in cp.stdout.split("\n") if p.strip()]
        return [repo_root / p for p in rel_paths]
    except Exception as e:
        print(f"⚠️ Warning: Failed to get changed files: {e}", file=sys.stderr)
        return []


def get_patch(repo_root: pathlib.Path, diff_ref: str) -> str:
    """Get the raw unified git diff patch."""
    try:
        cp = run(["git", "diff", diff_ref], cwd=str(repo_root))
        return cp.stdout
    except Exception:
        return ""


def collect_files(
    repo_root: pathlib.Path, max_bytes: int, diff_ref: str | None = None
) -> List[FileInfo]:
    """Collect and classify files, optionally filtering only those changed against diff_ref."""
    infos: List[FileInfo] = []

    if diff_ref:
        file_paths = get_changed_files(repo_root, diff_ref)
    else:
        file_paths = get_git_files(repo_root)

    for p in file_paths:
        if p.is_symlink() or not p.is_file():
            continue
        infos.append(decide_file(p, repo_root, max_bytes))

    infos.sort(key=lambda x: x.rel)
    return infos


def generate_tree_from_infos(infos: List[FileInfo], root_name: str) -> str:
    """
    Generate an ASCII directory tree based only on the files that passed filtering.
    """
    if not infos:
        return f"{root_name}\n└── (empty)"

    tree_dict = {}
    for info in infos:
        parts = info.rel.split("/")
        current = tree_dict
        for part in parts:
            current = current.setdefault(part, {})

    lines = [root_name]

    def walk(node: dict, prefix: str = ""):
        # Sort entries: directories (having children) first, then files alphabetically
        entries = sorted(node.items(), key=lambda x: (not bool(x[1]), x[0].lower()))
        for i, (name, children) in enumerate(entries):
            is_last = i == len(entries) - 1
            branch = "└── " if is_last else "├── "
            lines.append(prefix + branch + name)
            if children:
                extension = "    " if is_last else "│   "
                walk(children, prefix + extension)

    walk(tree_dict)
    return "\n".join(lines)


def read_text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def render_markdown_text(md_text: str) -> str:
    return markdown.markdown(md_text, extensions=["fenced_code", "tables", "toc"])  # type: ignore


def highlight_code(text: str, filename: str, formatter: HtmlFormatter) -> str:
    try:
        lexer = get_lexer_for_filename(filename, stripall=False)
    except Exception:
        lexer = TextLexer(stripall=False)
    return highlight(text, lexer, formatter)


def slugify(path_str: str) -> str:
    # Simple slug: keep alnum, dash, underscore; replace others with '-'
    out = []
    for ch in path_str:
        if ch.isalnum() or ch in {"-", "_"}:
            out.append(ch)
        else:
            out.append("-")
    return "".join(out)


def generate_cxml_text(
    infos: List[FileInfo], repo_dir: pathlib.Path, patch_text: str = ""
) -> str:
    """Generate CXML format text for LLM consumption, optionally including a git patch."""
    lines = ["<documents>"]

    if patch_text.strip():
        lines.append('<document index="0">')
        lines.append("<source>git_diff.patch</source>")
        lines.append("<document_content>")
        lines.append(patch_text)
        lines.append("</document_content>")
        lines.append("</document>")

    rendered = [i for i in infos if i.decision.include]
    for index, i in enumerate(rendered, 1):
        lines.append(f'<document index="{index}">')
        lines.append(f"<source>{i.rel}</source>")
        lines.append("<document_content>")

        try:
            text = read_text(i.path)
            lines.append(text)
        except Exception as e:
            lines.append(f"Failed to read: {str(e)}")

        lines.append("</document_content>")
        lines.append("</document>")

    lines.append("</documents>")
    return "\n".join(lines)


def build_html(
    source_name: str,
    repo_dir: pathlib.Path,
    head_commit: str,
    infos: List[FileInfo],
    patch_text: str = "",
    diff_ref: str = "",
) -> str:
    formatter = HtmlFormatter(nowrap=False)
    pygments_css = formatter.get_style_defs(".highlight")

    # Stats
    rendered = [i for i in infos if i.decision.include]
    skipped_binary = [i for i in infos if i.decision.reason == "binary"]
    skipped_large = [i for i in infos if i.decision.reason == "too_large"]
    skipped_ignored = [i for i in infos if i.decision.reason == "ignored"]
    total_files = (
        len(rendered) + len(skipped_binary) + len(skipped_large) + len(skipped_ignored)
    )

    tree_text = generate_tree_from_infos(infos, repo_dir.name)
    cxml_text = generate_cxml_text(infos, repo_dir, patch_text)

    toc_items: List[str] = []
    sections: List[str] = []

    if patch_text.strip():
        try:
            lexer = get_lexer_for_filename("dummy.diff", stripall=False)
            code_html = highlight(patch_text, lexer, formatter)
            sections.append(f"""
<section class="file-section" id="file-git-patch">
  <h2>Git Patch <span class="muted">(Changes vs {html.escape(diff_ref)})</span></h2>
  <div class="file-body"><div class="highlight">{code_html}</div></div>
  <div class="back-top"><a href="#top">↑ Back to top</a></div>
</section>
""")
            toc_items.append(
                f'<li><a href="#file-git-patch"><strong>Git Patch</strong></a> <span class="muted">(vs {html.escape(diff_ref)})</span></li>'
            )
        except Exception:
            pass

    for i in rendered:
        anchor = slugify(i.rel)
        toc_items.append(
            f'<li><a href="#file-{anchor}">{html.escape(i.rel)}</a> '
            f'<span class="muted">({bytes_human(i.size)})</span></li>'
        )
    toc_html = "".join(toc_items)

    for i in rendered:
        anchor = slugify(i.rel)
        p = i.path
        ext = p.suffix.lower()
        try:
            text = read_text(p)
            if ext in MARKDOWN_EXTENSIONS:
                body_html = render_markdown_text(text)
            else:
                code_html = highlight_code(text, i.rel, formatter)
                body_html = f'<div class="highlight">{code_html}</div>'
        except Exception as e:
            body_html = (
                f'<pre class="error">Failed to render: {html.escape(str(e))}</pre>'
            )
        sections.append(f"""
<section class="file-section" id="file-{anchor}">
  <h2>{html.escape(i.rel)} <span class="muted">({bytes_human(i.size)})</span></h2>
  <div class="file-body">{body_html}</div>
  <div class="back-top"><a href="#top">↑ Back to top</a></div>
</section>
""")

    # Skips lists
    def render_skip_list(title: str, items: List[FileInfo]) -> str:
        if not items:
            return ""
        lis = [
            f"<li><code>{html.escape(i.rel)}</code> "
            f"<span class='muted'>({bytes_human(i.size)})</span></li>"
            for i in items
        ]
        return (
            f"<details open><summary>{html.escape(title)} ({len(items)})</summary>"
            f"<ul class='skip-list'>\n" + "\n".join(lis) + "\n</ul></details>"
        )

    skipped_html = render_skip_list(
        "Skipped binaries", skipped_binary
    ) + render_skip_list("Skipped large files", skipped_large)

    # HTML with left sidebar TOC
    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Flattened repo – {html.escape(source_name)}</title>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, 'Apple Color Emoji','Segoe UI Emoji';
    margin: 0; padding: 0; line-height: 1.45;
  }}
  .container {{ max-width: 1100px; margin: 0 auto; padding: 0 1rem; }}
  .meta small {{ color: #666; }}
  .counts {{ margin-top: 0.25rem; color: #333; }}
  .muted {{ color: #777; font-weight: normal; font-size: 0.9em; }}

  /* Layout with sidebar */
  .page {{ display: grid; grid-template-columns: 320px minmax(0,1fr); gap: 0; }}
  #sidebar {{
    position: sticky; top: 0; align-self: start;
    height: 100vh; overflow: auto;
    border-right: 1px solid #eee; background: #fafbfc;
  }}
  #sidebar .sidebar-inner {{ padding: 0.75rem; }}
  #sidebar h2 {{ margin: 0 0 0.5rem 0; font-size: 1rem; }}

  .toc {{ list-style: none; padding-left: 0; margin: 0; overflow-x: auto; }}
  .toc li {{ padding: 0.15rem 0; white-space: nowrap; }}
  .toc a {{ text-decoration: none; color: #0366d6; display: inline-block; text-decoration: none; }}
  .toc a:hover {{ text-decoration: underline; }}

  main.container {{ padding-top: 1rem; }}

  pre {{ background: #f6f8fa; padding: 0.75rem; overflow: auto; border-radius: 6px; }}
  code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono','Courier New', monospace; }}
  .highlight {{ overflow-x: auto; }}
  .file-section {{ padding: 1rem; border-top: 1px solid #eee; }}
  .file-section h2 {{ margin: 0 0 0.5rem 0; font-size: 1.1rem; }}
  .file-body {{ margin-bottom: 0.5rem; }}
  .back-top {{ font-size: 0.9rem; }}
  .skip-list code {{ background: #f6f8fa; padding: 0.1rem 0.3rem; border-radius: 4px; }}
  .error {{ color: #b00020; background: #fff3f3; }}

  /* Hide duplicate top TOC on wide screens */
  .toc-top {{ display: block; }}
  @media (min-width: 1000px) {{ .toc-top {{ display: none; }} }}

  :target {{ scroll-margin-top: 8px; }}

  /* View toggle */
  .view-toggle {{
    margin: 1rem 0;
    display: flex;
    gap: 0.5rem;
    align-items: center;
  }}
  .toggle-btn {{
    padding: 0.5rem 1rem;
    border: 1px solid #d1d9e0;
    background: white;
    cursor: pointer;
    border-radius: 6px;
    font-size: 0.9rem;
  }}
  .toggle-btn.active {{
    background: #0366d6;
    color: white;
    border-color: #0366d6;
  }}
  .toggle-btn:hover:not(.active) {{
    background: #f6f8fa;
  }}

  /* LLM view */
  #llm-view {{ display: none; }}
  #llm-text {{
    width: 100%;
    height: 70vh;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 0.85em;
    border: 1px solid #d1d9e0;
    border-radius: 6px;
    padding: 1rem;
    resize: vertical;
  }}
  .copy-hint {{
    margin-top: 0.5rem;
    color: #666;
    font-size: 0.9em;
  }}

  /* Pygments */
  {pygments_css}
</style>
</head>
<body>
<a id="top"></a>

<div class="page">
  <nav id="sidebar"><div class="sidebar-inner">
      <h2>Contents ({len(rendered)})</h2>
      <ul class="toc toc-sidebar">
        <li><a href="#top">↑ Back to top</a></li>
        {toc_html}
      </ul>
  </div></nav>

  <main class="container">

    <section>
        <div class="meta">
        <div><strong>Repository:</strong> <a href="{html.escape(source_name)}">{html.escape(source_name)}</a></div>
        <small><strong>HEAD commit:</strong> {html.escape(head_commit)}</small>
        <div class="counts">
            <strong>Total files:</strong> {total_files} · <strong>Rendered:</strong> {len(rendered)} · <strong>Skipped:</strong> {len(skipped_binary) + len(skipped_large) + len(skipped_ignored)}
        </div>
        </div>
    </section>

    <div class="view-toggle">
      <strong>View:</strong>
      <button class="toggle-btn active" onclick="showHumanView()">👤 Human</button>
      <button class="toggle-btn" onclick="showLLMView()">🤖 LLM</button>
    </div>

    <div id="human-view">
      <section>
        <h2>Directory tree</h2>
        <pre>{html.escape(tree_text)}</pre>
      </section>

      <section class="toc-top">
        <h2>Table of contents ({len(rendered)})</h2>
        <ul class="toc">{toc_html}</ul>
      </section>

      <section>
        <h2>Skipped items</h2>
        {skipped_html}
      </section>

      {"".join(sections)}
    </div>

    <div id="llm-view">
      <section>
        <h2>🤖 LLM View - CXML Format</h2>
        <p>Copy the text below and paste it to an LLM for analysis:</p>
        <textarea id="llm-text" readonly>{html.escape(cxml_text)}</textarea>
        <div class="copy-hint">
          💡 <strong>Tip:</strong> Click in the text area and press Ctrl+A (Cmd+A on Mac) to select all, then Ctrl+C (Cmd+C) to copy.
        </div>
      </section>
    </div>
  </main>
</div>

<script>
function showHumanView() {{
  document.getElementById('human-view').style.display = 'block';
  document.getElementById('llm-view').style.display = 'none';
  document.querySelectorAll('.toggle-btn').forEach(btn => btn.classList.remove('active'));
  event.target.classList.add('active');
}}

function showLLMView() {{
  document.getElementById('human-view').style.display = 'none';
  document.getElementById('llm-view').style.display = 'block';
  document.querySelectorAll('.toggle-btn').forEach(btn => btn.classList.remove('active'));
  event.target.classList.add('active');

  // Auto-select all text when switching to LLM view for easy copying
  setTimeout(() => {{
    const textArea = document.getElementById('llm-text');
    textArea.focus();
    textArea.select();
  }}, 100);
}}
</script>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Flatten a GitHub repo or local directory to a single HTML page"
    )
    ap.add_argument("source", help="GitHub repo URL or local directory path")
    ap.add_argument(
        "-r",
        "--ref",
        help="Specific branch, tag, or commit to render (e.g., main, v1.0.0, a1b2c3d)",
    )
    ap.add_argument(
        "-d",
        "--diff",
        help="Render only changes compared to this ref (e.g., main, HEAD~1)",
    )
    ap.add_argument(
        "-o", "--out", help="Output HTML file path (default: temporary file)"
    )
    ap.add_argument(
        "--max-bytes",
        type=int,
        default=MAX_DEFAULT_BYTES,
        help="Max file size to render",
    )
    ap.add_argument(
        "--no-open", action="store_true", help="Don't open the HTML file in browser"
    )
    args = ap.parse_args()

    is_url = args.source.startswith(("http://", "https://", "git@"))
    needs_clone = is_url or args.ref is not None
    tmpdir = None

    if needs_clone:
        tmpdir = tempfile.mkdtemp(prefix="flatten_repo_")
        repo_dir = pathlib.Path(tmpdir, "repo")

        if is_url:
            repo_name = args.source.rstrip("/").split("/")[-1].replace(".git", "")
            print(
                f"📁 Cloning {args.source} (ref: {args.ref or 'HEAD'}) to temporary directory...",
                file=sys.stderr,
            )
            git_clone(
                args.source, str(repo_dir), args.ref, full_history=bool(args.diff)
            )
        else:
            repo_dir_source = pathlib.Path(args.source).resolve()
            if not repo_dir_source.is_dir():
                print(
                    f"❌ Error: Directory {repo_dir_source} does not exist.",
                    file=sys.stderr,
                )
                return 1
            repo_name = repo_dir_source.name
            print(
                f"📁 Cloning local repo to temporary directory to checkout ref '{args.ref}'...",
                file=sys.stderr,
            )
            run(["git", "clone", str(repo_dir_source), str(repo_dir)])
            run(["git", "checkout", args.ref], cwd=str(repo_dir))
    else:
        repo_dir = pathlib.Path(args.source).resolve()
        if not repo_dir.is_dir():
            print(f"❌ Error: Directory {repo_dir} does not exist.", file=sys.stderr)
            return 1
        repo_name = repo_dir.name
        print(f"📁 Using local directory: {repo_dir}", file=sys.stderr)

    if args.out is None:
        args.out = str(pathlib.Path(tempfile.gettempdir()) / f"{repo_name}.html")

    try:
        head = git_head_commit(str(repo_dir))
        status_msg = (
            f"✓ Ready (HEAD: {head[:8]})"
            if head != "(unknown)"
            else "✓ Ready (Not a git repo or no commits)"
        )
        print(status_msg, file=sys.stderr)

        patch_text = ""
        if args.diff:
            print(f"🔍 Calculating diff against '{args.diff}'...", file=sys.stderr)
            patch_text = get_patch(repo_dir, args.diff)
            if not patch_text.strip():
                print(
                    f"ℹ️  No changes found compared to '{args.diff}'.", file=sys.stderr
                )

        print(f"📊 Scanning files in {repo_dir}...", file=sys.stderr)
        infos = collect_files(repo_dir, args.max_bytes, args.diff)

        rendered_count = sum(1 for i in infos if i.decision.include)
        skipped_count = len(infos) - rendered_count
        print(
            f"✓ Found {len(infos)} files total ({rendered_count} will be rendered, {skipped_count} skipped)",
            file=sys.stderr,
        )

        print(f"🔨 Generating HTML...", file=sys.stderr)
        display_name = args.source if is_url else str(repo_dir.resolve())
        html_out = build_html(
            display_name, repo_dir, head, infos, patch_text, args.diff
        )

        out_path = pathlib.Path(args.out)
        print(f"💾 Writing HTML file: {out_path.resolve()}", file=sys.stderr)
        out_path.write_text(html_out, encoding="utf-8")
        file_size = out_path.stat().st_size
        print(f"✓ Wrote {bytes_human(file_size)} to {out_path}", file=sys.stderr)

        if not args.no_open:
            print(f"🌐 Opening {out_path} in browser...", file=sys.stderr)
            webbrowser.open(f"file://{out_path.resolve()}")

        return 0
    finally:
        if tmpdir is not None:
            print(f"🗑️  Cleaning up temporary directory: {tmpdir}", file=sys.stderr)
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
