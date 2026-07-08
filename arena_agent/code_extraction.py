"""Code-block extraction, formatting, file writing, and DOM dumping.

Split out of aai.py. Handles everything that happens AFTER the assistant
finishes responding:
  - merging CodeMirror editor blocks into the codeBlocks list
  - applying --code-only / --write-files / --dump-dom formatting
  - sanitising filenames and writing each block to disk
  - dumping the last assistant bubble's outerHTML for selector debugging
"""

import sys

from pathlib import Path
from typing import Any, Dict, List, Optional


class CodeExtractionMixin:
    """Provides code-block extraction / writing / dumping helpers."""

    @staticmethod
    def _code_blocks_signature(blocks: List[Dict[str, Any]]) -> str:
        """Return a compact signature for code-block stability checks.

        Text-only stability is not enough for --code-only / --write-files:
        Arena can keep filling an artifact or Shiki code block while the prose
        text stays unchanged. Include block metadata, code length, and a short
        tail so polling treats code growth as activity and does not finalize
        early.
        """
        if not blocks:
            return ""
        parts: List[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            code = block.get("code") or ""
            parts.append(
                f"{block.get('type', '')}:"
                f"{block.get('filename', '')}:"
                f"{block.get('language', '')}:"
                f"{len(code)}:"
                f"{code[-80:]}"
            )
        return "|".join(parts)


    def _merge_codemirror_blocks(
        self,
        existing_blocks: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Call the async CodeMirror extractor and merge results.

        CodeMirror editors are NOT extracted during polling (too expensive --
        requires scrolling). This method is called once at finalize time to
        collect any CodeMirror-based code blocks and append them to the
        existing block list.

        If the page has no CodeMirror editors, returns `existing_blocks`
        unchanged (no extra round-trip cost beyond the evaluate call).
        """
        # First, do a cheap sync probe to count CodeMirror editors. This
        # lets us log diagnostics even if the async extraction fails.
        try:
            cm_info = self.page.evaluate(
                "() => {"
                "  const editors = document.querySelectorAll('.cm-editor');"
                "  const lines = document.querySelectorAll('.cm-line');"
                "  return { editors: editors.length, lines: lines.length };"
                "}"
            ) or {}
        except Exception as exc:
            print(f"[debug] CodeMirror probe failed: {exc}", file=sys.stderr)
            cm_info = {}

        cm_editor_count = cm_info.get("editors", 0) if isinstance(cm_info, dict) else 0
        cm_line_count = cm_info.get("lines", 0) if isinstance(cm_info, dict) else 0

        if cm_editor_count == 0:
            # No CodeMirror editors -- nothing to extract.
            return existing_blocks

        print(
            f"[info] CodeMirror: {cm_editor_count} editor(s), {cm_line_count} "
            f"visible line(s). Extracting...",
            file=sys.stderr,
        )

        try:
            cm_blocks = self.page.evaluate(
                "async () => { return await window.__arena_get_codemirror_blocks(); }"
            ) or []
        except Exception as exc:
            print(f"[debug] CodeMirror extraction failed: {exc}", file=sys.stderr)
            cm_blocks = []
        if cm_blocks:
            print(
                f"[info] Extracted {len(cm_blocks)} CodeMirror editor block(s).",
                file=sys.stderr,
            )
            existing_blocks = list(existing_blocks) + cm_blocks
        else:
            print(
                f"[warn] CodeMirror extraction returned 0 blocks despite "
                f"{cm_editor_count} editor(s) being present.",
                file=sys.stderr,
            )
        return existing_blocks


    def _extract_final(
        self,
        code_only: bool = False,
        debug_dom: bool = False,
        debug_dom_path: Optional[str] = None,
        write_files: Optional[str] = None,
        dry_run: bool = False,
        auto_filename: bool = True,
    ) -> Optional[str]:
        """Read the final response from the page (used by --wait-seconds mode)."""
        try:
            poll = self.page.evaluate("() => window.__arena_poll()") or {}
        except Exception:
            poll = {}
        text = (poll.get("text") or "").strip()
        code_blocks = poll.get("codeBlocks") or []
        # Also extract CodeMirror editor blocks (async, may scroll).
        code_blocks = self._merge_codemirror_blocks(code_blocks)
        return self._finalize_response(
            text=text,
            code_blocks=code_blocks,
            code_only=code_only,
            debug_dom=debug_dom,
            debug_dom_path=debug_dom_path,
            write_files=write_files,
            dry_run=dry_run,
            auto_filename=auto_filename,
        )


    def _finalize_response(
        self,
        text: str,
        code_blocks: List[Dict[str, Any]],
        code_only: bool,
        debug_dom: bool,
        debug_dom_path: Optional[str],
        write_files: Optional[str] = None,
        dry_run: bool = False,
        auto_filename: bool = True,
    ) -> Optional[str]:
        """Apply output formatting (code_only / plain) and optionally dump DOM.

        If `code_only` is True and zero code blocks were found, ALWAYS dump the
        DOM (regardless of debug_dom) so the user can see what went wrong.

        If `write_files` is set, write each artifact code block whose filename
        matches `*.*` to that directory. Implicitly triggers DOM dump on zero
        blocks (same as code_only). If `auto_filename` is True, blocks without
        a filename get an auto-generated `snippet_N.<ext>` name based on
        language, so data-code-block divs and plain <pre> blocks can be
        written too.
        """
        # If write_files is set, we need code_blocks extracted. The polling
        # loop already collected them, so just pass through.
        should_extract_code = code_only or (write_files is not None)

        if should_extract_code:
            # If writing files, assign filenames to blocks that don't have one.
            # This MUTATES a copy (not the input list) so code_only formatting
            # below still sees the original block metadata.
            blocks_for_writing = code_blocks
            if write_files is not None and auto_filename:
                blocks_for_writing = self._assign_filenames(
                    code_blocks, auto_filename=True
                )
                auto_count = sum(1 for b in blocks_for_writing if b.get("auto_filename"))
                if auto_count:
                    print(
                        f"[info] Auto-generated filenames for {auto_count} "
                        f"block(s) without a filename.",
                        file=sys.stderr,
                    )

            formatted = self._format_code_blocks(code_blocks)
            if not formatted:
                # Auto-dump on zero-blocks so the user can see what's there.
                label = "--code-only" if code_only else "--write-files"
                print(
                    f"[warn] {label}: no code blocks found in the response.",
                    file=sys.stderr,
                )
                dump_path = self._dump_dom(debug_dom_path)
                if dump_path:
                    print(
                        f"[warn] Dumped last assistant bubble HTML to: {dump_path}",
                        file=sys.stderr,
                    )
                    print(
                        "[warn] Inspect this file to find the new selector for code blocks, "
                        "then either update aai.py or set ARENA_CODE_BLOCK_SELECTOR.",
                        file=sys.stderr,
                    )

            # Write files if requested (regardless of code_only stdout output).
            if write_files is not None:
                self._write_code_files(
                    blocks_for_writing,
                    target_dir=write_files,
                    dry_run=dry_run,
                )

            if code_only:
                return formatted
            # write_files but not code_only: return the plain text for stdout.
            return text if text else None

        # Non-code-only mode: dump only if explicitly requested.
        if debug_dom:
            self._dump_dom(debug_dom_path)
        return text if text else None


    def _write_code_files(
        self,
        code_blocks: List[Dict[str, Any]],
        target_dir: str,
        dry_run: bool = False,
    ) -> Dict[str, str]:
        """Write each artifact code block with a `*.*` filename to target_dir.

        Returns a dict mapping {written_path: status} for each candidate
        block (status is one of: 'written', 'skipped_no_filename',
        'skipped_no_extension', 'skipped_dry_run').

        Filenames are sanitized (basename only, illegal chars replaced). If
        two blocks share the same filename, later blocks overwrite earlier
        ones (last-write-wins) and a warning is logged.
        """
        if not code_blocks:
            print("[warn] --write-files: no code blocks to write.", file=sys.stderr)
            return {}

        target_path = Path(target_dir).expanduser().resolve()
        print(f"[info] --write-files target: {target_path}", file=sys.stderr)
        if dry_run:
            print("[info] --dry-run: no files will actually be written.", file=sys.stderr)
        else:
            target_path.mkdir(parents=True, exist_ok=True)
            if not target_path.is_dir():
                print(
                    f"[error] --write-files: target is not a directory: {target_path}",
                    file=sys.stderr,
                )
                return {}

        results: Dict[str, str] = {}
        seen_filenames: Dict[str, str] = {}  # sanitized_name -> block_index
        written_count = 0
        skipped_count = 0

        for i, block in enumerate(code_blocks):
            btype = block.get("type", "inline")
            raw_filename = (block.get("filename") or "").strip()
            code = block.get("code") or ""

            if not raw_filename:
                print(
                    f"[info] block [{i+1}/{len(code_blocks)}] type={btype}: "
                    f"skipped (no filename)",
                    file=sys.stderr,
                )
                results[f"<block {i+1}>"] = "skipped_no_filename"
                skipped_count += 1
                continue

            if not self._is_writable_filename(raw_filename):
                print(
                    f"[info] block [{i+1}/{len(code_blocks)}] filename={raw_filename!r}: "
                    f"skipped (does not match *.* pattern or has unsafe path)",
                    file=sys.stderr,
                )
                results[raw_filename] = "skipped_no_extension"
                skipped_count += 1
                continue

            safe_name = self._sanitize_filename(raw_filename)
            if not safe_name:
                print(
                    f"[warn] block [{i+1}/{len(code_blocks)}] filename={raw_filename!r}: "
                    f"skipped (sanitized to empty string)",
                    file=sys.stderr,
                )
                results[raw_filename] = "skipped_no_extension"
                skipped_count += 1
                continue

            out_path = target_path / safe_name

            if safe_name in seen_filenames:
                print(
                    f"[warn] block [{i+1}/{len(code_blocks)}] filename={safe_name!r}: "
                    f"duplicate of block {seen_filenames[safe_name]} (overwriting)",
                    file=sys.stderr,
                )
            seen_filenames[safe_name] = str(i + 1)

            if dry_run:
                auto_tag = " [auto-filename]" if block.get("auto_filename") else ""
                print(
                    f"[info] [dry-run] would write {out_path} "
                    f"({len(code)} chars){auto_tag}",
                    file=sys.stderr,
                )
                results[str(out_path)] = "skipped_dry_run"
                continue

            try:
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(code)
                    if not code.endswith("\n"):
                        f.write("\n")
                auto_tag = " [auto-filename]" if block.get("auto_filename") else ""
                print(
                    f"[info] wrote {out_path} ({len(code)} chars){auto_tag}",
                    file=sys.stderr,
                )
                results[str(out_path)] = "written"
                written_count += 1
            except OSError as exc:
                print(
                    f"[error] failed to write {out_path}: {exc}",
                    file=sys.stderr,
                )
                results[str(out_path)] = f"error: {exc}"
                skipped_count += 1

        print(
            f"[info] --write-files summary: {written_count} written, "
            f"{skipped_count} skipped, target={target_path}",
            file=sys.stderr,
        )
        return results


    def _dump_dom(self, path: Optional[str]) -> Optional[str]:
        """Dump diagnostic info about the last assistant bubble to a file.

        Writes outerHTML + summary stats. Returns the path written, or None on
        failure. Default path: arena_debug_dom.html in the CWD.
        """
        if not path:
            path = "arena_debug_dom.html"
        try:
            info = self.page.evaluate("() => window.__arena_dump_dom()") or {}
        except Exception as exc:
            print(f"[warn] _dump_dom: evaluate failed: {exc}", file=sys.stderr)
            return None
        with open(path, "w", encoding="utf-8") as f:
            f.write("<!-- arena_agent.py DOM dump -->\n")
            f.write(f"<!-- found={info.get('found')} "
                    f"numBubbles={info.get('numBubbles')} "
                    f"numPres={info.get('numPres')} "
                    f"numArtifacts={info.get('numArtifacts')} -->\n")
            if info.get("bubbleClasses"):
                f.write("<!-- bubbleClasses:\n")
                for i, cls in enumerate(info["bubbleClasses"]):
                    f.write(f"     [{i}] {cls}\n")
                f.write("-->\n")
            if not info.get("found"):
                f.write("<!-- No assistant bubble detected. "
                        f"Body preview (first 500 chars): -->\n")
                f.write(f"<!-- {info.get('bodyPreview', '')} -->\n")
            f.write(info.get("html", ""))
        print(
            f"[debug] DOM dump: found={info.get('found')} "
            f"bubbles={info.get('numBubbles')} "
            f"pres={info.get('numPres')} "
            f"artifacts={info.get('numArtifacts')}",
            file=sys.stderr,
        )
        return path


    @staticmethod
    def _format_code_blocks(blocks: List[Dict[str, Any]]) -> Optional[str]:
        """Format code blocks for `--code-only` output.

        Each block is preceded by a header line:
            ===== [N/total] <filename> (LANG) =====       # for artifacts
            ===== [N/total] CODE-BLOCK (LANG) =====        # for data-code-block divs
            ===== [N/total] INLINE (LANG) =====           # for plain <pre> blocks
        """
        if not blocks:
            return None
        out: List[str] = []
        total = len(blocks)
        for i, b in enumerate(blocks, 1):
            btype = b.get("type", "inline")
            filename = (b.get("filename") or "").strip()
            lang = (b.get("language") or "").strip()
            code = b.get("code") or ""

            if btype == "artifact" and filename:
                header = f"===== [{i}/{total}] {filename}"
            elif btype == "code-block":
                header = f"===== [{i}/{total}] CODE-BLOCK"
            elif btype == "codemirror":
                header = f"===== [{i}/{total}] CODEMIRROR"
                if filename:
                    header = f"===== [{i}/{total}] {filename}"
            else:
                header = f"===== [{i}/{total}] INLINE"
            if lang:
                header += f" ({lang})"
            header += " ====="

            out.append(header)
            # Strip leading whitespace from the first line of code (shiki
            # highlighters often indent the <pre> content, which gets captured
            # as leading whitespace). Preserve internal indentation.
            code_stripped = code.lstrip()
            out.append(code_stripped.rstrip())
            out.append("")  # blank line separator between blocks
        return "\n".join(out).rstrip() + "\n"

