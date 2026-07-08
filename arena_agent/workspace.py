"""Download files from Arena Agent Mode's Workspace panel.

Arena's Agent Mode (arena.ai/agent, canaryarena.ai/agent) maintains a
"Workspace" panel on the right side of the screen containing every file
the agent created during its task. This mixin downloads those files to a
local directory.

Strategy (tried in order -- first success wins):
  1. Click the Workspace's "download all" control -> Playwright captures a
     ZIP download -> save and (optionally) extract it. This is Arena's
     documented one-click "download the zip file" feature and the most
     reliable path because it fetches the real server-side files.
  2. Per-file download buttons: click each file's Download button and
     capture each download individually.
  3. DOM extraction fallback: read each rendered CodeMirror editor /
     artifact's content straight from the page (reuses the existing
     extraction machinery) and write the files to disk.

Every selector can be overridden via env vars (ARENA_WORKSPACE_*),
consistent with the rest of the codebase.
"""

import os
import sys
import time
import zipfile

from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.sync_api import TimeoutError as PlaywrightTimeout


class WorkspaceMixin:
    """Provides Workspace file downloading for ArenaAgent.

    Composed into ArenaAgent alongside the other mixins; depends on
    ``self.page``, ``self._wait_visible``, ``self._assign_filenames``,
    ``self._write_code_files`` and ``self._sanitize_filename`` which are
    provided by the other mixins / the core class.
    """

    # CSS selectors for the Workspace panel's "download all" control.
    # Tried in order; the first VISIBLE match is clicked inside a
    # page.expect_download() block so the ZIP is captured.
    _WORKSPACE_DOWNLOAD_SELECTORS = [
        # Workspace-scoped download button (header toolbar icon).
        '[data-testid*="workspace" i] button[aria-label*="download" i]:visible',
        '[class*="workspace" i] button[aria-label*="download" i]:visible',
        # Explicit aria-labels Arena is known/likely to use.
        'button[aria-label="Download"]:visible',
        'button[aria-label*="Download all" i]:visible',
        'button[aria-label*="download workspace" i]:visible',
        'button[aria-label*="Download files" i]:visible',
        # A workspace panel button whose text says "Download".
        '[class*="workspace" i] button:has-text("Download"):visible',
        # Generic last resort: any download-named button that isn't a
        # per-file "Download file" button (those are handled in strategy 2).
        'button[aria-label*="download" i]:not([aria-label*="file" i]):visible',
    ]

    # Selectors for per-file Download buttons inside the Workspace file list.
    _WORKSPACE_FILE_DOWNLOAD_SELECTORS = [
        'button[aria-label="Download file"]',
        'button[aria-label*="download file" i]',
        '[data-testid*="file" i] button[aria-label*="download" i]',
        'button[aria-label="Download file"]:visible',
    ]

    # Selectors used to expand the Workspace panel if it appears collapsed.
    _WORKSPACE_TOGGLE_SELECTORS = [
        'button:has-text("Workspace"):visible',
        '[role="tab"]:has-text("Workspace"):visible',
        'button[aria-label*="Workspace" i]:visible',
        'button:has-text("Files"):visible',
        '[role="tab"]:has-text("Files"):visible',
    ]

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def download_workspace(
        self,
        target_dir: str,
        dry_run: bool = False,
        extract_zip: bool = True,
        keep_zip: bool = False,
        debug_dom_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Download all files from the Workspace into ``target_dir``.

        Returns a result dict::

            {
              "strategy": "zip-download" | "per-file-download" |
                           "dom-extraction" | None,
              "files":   [<written filenames>],
              "zip_path": "<path to saved zip or None>",
              "errors":  [<error messages>],
            }

        ``strategy`` is None when nothing could be downloaded.
        """
        target = Path(target_dir).expanduser().resolve()
        results: Dict[str, Any] = {
            "strategy": None,
            "files": [],
            "zip_path": None,
            "errors": [],
        }

        if not dry_run:
            target.mkdir(parents=True, exist_ok=True)

        # Where to write the diagnostic dump (auto-written on failure, or
        # always when --workspace-debug-dom is passed). Defaults to a file
        # next to the target dir.
        self.workspace_debug_dom_path = (
            debug_dom_path
            or os.environ.get("ARENA_WORKSPACE_DEBUG_DOM")
            or str(target / "workspace_debug_dom.html")
        )

        print(
            f"[info] --workspace-files: downloading Workspace "
            f"to {target}",
            file=sys.stderr,
        )

        # Make sure the Workspace panel is open before probing for controls.
        self._ensure_workspace_visible()

        # Wait for the Workspace to settle before downloading. This guards the
        # download-only path (--resume --workspace-files, where send_prompt is
        # not called and the completion loop never ran) and is otherwise a fast
        # no-op: it polls the workspace signature until it stops changing.
        self._wait_for_workspace_stable(timeout_s=120.0, stable_s=5.0)

        # Strategy 1: whole-workspace ZIP download.
        if self._workspace_download_zip(
            target, dry_run, extract_zip, keep_zip, results
        ):
            results["strategy"] = "zip-download"
            return results

        # Strategy 2: click each per-file download button.
        if self._workspace_download_per_file(target, dry_run, results):
            results["strategy"] = "per-file-download"
            return results

        # Strategy 3: read file contents from the rendered DOM.
        if self._workspace_extract_from_dom(target, dry_run, results):
            results["strategy"] = "dom-extraction"
            return results

        # All strategies failed. Dump the Workspace DOM so the failure is
        # diagnosable: the report lists every button/link on the page so the
        # real download control can be located and wired up.
        dump_path = self._workspace_dump_dom(self.workspace_debug_dom_path)
        print(
            "[warn] --workspace-files: could not download any Workspace "
            "files.",
            file=sys.stderr,
        )
        if dump_path:
            print(
                f"[warn] Wrote a Workspace diagnostic dump to: {dump_path}\n"
                "[warn] Re-run with --headed to watch the Workspace panel "
                "live, or set ARENA_WORKSPACE_DOWNLOAD_SELECTOR to the "
                "exact CSS selector of the download control (visible in "
                "the dump under 'download-ish buttons').",
                file=sys.stderr,
            )
        else:
            print(
                "[warn] Set ARENA_WORKSPACE_DOWNLOAD_SELECTOR to the exact "
                "CSS selector of the download button.",
                file=sys.stderr,
            )
        return results

    # ------------------------------------------------------------------
    # Strategy 1: whole-workspace ZIP download
    # ------------------------------------------------------------------

    def _workspace_download_zip(
        self,
        target: Path,
        dry_run: bool,
        extract_zip: bool,
        keep_zip: bool,
        results: Dict[str, Any],
    ) -> bool:
        """Click the Workspace download button and capture the ZIP.

        Returns True if a ZIP was successfully downloaded (and optionally
        extracted), False otherwise.
        """
        btn = self._find_workspace_download_button()
        if btn is None:
            # Heuristic fallback: enumerate every visible button/link whose
            # label mentions "download" and try each. This catches download
            # controls whose aria-label we didn't anticipate.
            print(
                "[info] No 'download all' button matched the known "
                "selectors; enumerating download-ish controls...",
                file=sys.stderr,
            )
            btn = self._find_download_control_by_heuristic(exclude_per_file=True)
        if btn is None:
            print(
                "[info] No Workspace 'download all' button found; "
                "trying per-file downloads.",
                file=sys.stderr,
            )
            return False

        if dry_run:
            print(
                f"[info] [dry-run] would click the Workspace download "
                f"button and save the ZIP to {target}",
                file=sys.stderr,
            )
            results["files"] = ["<workspace.zip (dry-run)>"]
            return True

        try:
            with self.page.expect_download(timeout=20_000) as dl_info:
                btn.click()
            download = dl_info.value
        except PlaywrightTimeout:
            print(
                "[warn] Clicking the Workspace download button did not "
                "trigger a download (timed out after 20s). It may open a "
                "menu instead, or be behind a paywall / disabled.",
                file=sys.stderr,
            )
            results["errors"].append("zip-download-timeout")
            return False
        except Exception as exc:
            print(f"[warn] Workspace ZIP download failed: {exc}", file=sys.stderr)
            results["errors"].append(f"zip-download-failed: {exc}")
            return False

        suggested = download.suggested_filename or "workspace.zip"
        if not suggested.lower().endswith(".zip"):
            suggested += ".zip"
        zip_path = target / self._sanitize_filename(suggested)
        # Avoid clobbering an existing zip from a previous run.
        if zip_path.exists():
            zip_path = target / f"workspace_{int(time.time())}.zip"

        try:
            download.save_as(str(zip_path))
        except Exception as exc:
            print(f"[warn] Failed to save the Workspace ZIP: {exc}", file=sys.stderr)
            results["errors"].append(f"zip-save-failed: {exc}")
            return False

        results["zip_path"] = str(zip_path)
        print(f"[info] Saved Workspace ZIP: {zip_path}", file=sys.stderr)

        extracted: List[str] = []
        if extract_zip:
            extracted = self._safe_extract_zip(zip_path, target)
            print(
                f"[info] Extracted {len(extracted)} file(s) from the ZIP "
                f"into {target}",
                file=sys.stderr,
            )
            results["files"] = extracted

        # Keep the zip around only if requested AND we extracted (otherwise
        # the zip IS the deliverable).
        if extract_zip and not keep_zip:
            try:
                zip_path.unlink()
                print("[info] Removed the ZIP after extraction (use "
                      "--workspace-keep-zip to keep it).", file=sys.stderr)
            except OSError:
                pass
        else:
            if not results["files"]:
                results["files"] = [zip_path.name]

        return True

    # ------------------------------------------------------------------
    # Strategy 2: per-file downloads
    # ------------------------------------------------------------------

    def _workspace_download_per_file(
        self,
        target: Path,
        dry_run: bool,
        results: Dict[str, Any],
    ) -> bool:
        """Click each per-file Download button and capture each file.

        Returns True if at least one file was downloaded.
        """
        buttons = self._collect_visible_buttons(self._WORKSPACE_FILE_DOWNLOAD_SELECTORS)
        if not buttons:
            print(
                "[info] No per-file download buttons found in the "
                "Workspace; falling back to DOM extraction.",
                file=sys.stderr,
            )
            return False

        print(
            f"[info] Found {len(buttons)} per-file download button(s) "
            f"in the Workspace.",
            file=sys.stderr,
        )

        saved: List[str] = []
        for idx, btn in enumerate(buttons, 1):
            if dry_run:
                print(
                    f"[info] [dry-run] would download workspace file "
                    f"[{idx}/{len(buttons)}]",
                    file=sys.stderr,
                )
                saved.append(f"<workspace_file_{idx} (dry-run)>")
                continue
            try:
                with self.page.expect_download(timeout=15_000) as dl_info:
                    btn.click()
                download = dl_info.value
                fname = download.suggested_filename or f"workspace_file_{idx}"
                safe = self._sanitize_filename(fname) or f"workspace_file_{idx}"
                out_path = target / safe
                if out_path.exists():
                    out_path = target / f"{out_path.stem}_{idx}{out_path.suffix}"
                download.save_as(str(out_path))
                saved.append(out_path.name)
                print(
                    f"[info] Downloaded workspace file "
                    f"[{idx}/{len(buttons)}]: {out_path.name}",
                    file=sys.stderr,
                )
            except PlaywrightTimeout:
                print(
                    f"[warn] Workspace file [{idx}/{len(buttons)}] "
                    f"download did not trigger (timed out).",
                    file=sys.stderr,
                )
                results["errors"].append(f"per-file-timeout-{idx}")
            except Exception as exc:
                print(
                    f"[warn] Workspace file [{idx}/{len(buttons)}] "
                    f"download failed: {exc}",
                    file=sys.stderr,
                )
                results["errors"].append(f"per-file-failed-{idx}: {exc}")

        results["files"] = saved
        return bool(saved)

    # ------------------------------------------------------------------
    # Strategy 3: DOM extraction fallback
    # ------------------------------------------------------------------

    def _workspace_extract_from_dom(
        self,
        target: Path,
        dry_run: bool,
        results: Dict[str, Any],
    ) -> int:
        """Read file contents from the page DOM and write them.

        Reuses the existing CodeMirror + artifact extraction helpers.
        Returns the number of files written.
        """
        print(
            "[info] Extracting Workspace files from the rendered DOM "
            "(no download button / ZIP available).",
            file=sys.stderr,
        )

        blocks: List[Dict[str, Any]] = []

        def _as_blocks(val) -> List[Dict[str, Any]]:
            """Coerce an evaluate() result into a list of block dicts.

            Guards against malformed page responses: a dict, string, or None
            must never be passed to list.extend() (which would iterate a
            dict's keys and corrupt the block list).
            """
            if isinstance(val, list):
                return [b for b in val if isinstance(b, dict)]
            return []

        # 1. WHOLE-DOCUMENT scan: artifact cards + rendered CodeMirror editors
        #    anywhere on the page (including the Workspace panel, which is a
        #    separate DOM subtree from the chat bubble).
        try:
            all_blocks = self.page.evaluate(
                "() => window.__arena_get_all_code_blocks && window.__arena_get_all_code_blocks()"
            )
            all_blocks = _as_blocks(all_blocks)
            if all_blocks:
                print(
                    f"[info] Whole-document scan found {len(all_blocks)} "
                    f"file block(s).",
                    file=sys.stderr,
                )
            blocks.extend(all_blocks)
        except Exception as exc:
            print(f"[debug] DOM extraction (all-code-blocks) failed: {exc}", file=sys.stderr)

        # 2. Virtualized CodeMirror editors (async -- scrolls to collect lines).
        try:
            cm_blocks = self.page.evaluate(
                "async () => { return await window.__arena_get_codemirror_blocks(); }"
            )
            cm_blocks = _as_blocks(cm_blocks)
            if cm_blocks:
                print(
                    f"[info] CodeMirror scan found {len(cm_blocks)} "
                    f"file block(s).",
                    file=sys.stderr,
                )
            blocks.extend(cm_blocks)
        except Exception as exc:
            print(f"[debug] DOM extraction (CodeMirror) failed: {exc}", file=sys.stderr)

        # 3. Bubble-scoped code blocks (inline <pre>, data-code-blocks in the
        #    last assistant message) -- cheap, and catches code that isn't in
        #    an artifact/CodeMirror.
        try:
            poll = self.page.evaluate("() => window.__arena_poll()") or {}
            blocks.extend(_as_blocks(poll.get("codeBlocks")))
        except Exception as exc:
            print(f"[debug] DOM extraction (poll) failed: {exc}", file=sys.stderr)

        if not blocks:
            print(
                "[warn] DOM extraction found 0 renderable code blocks. "
                "The agent may not have created any files, or the "
                "Workspace is empty.",
                file=sys.stderr,
            )
            return 0

        assigned = self._assign_filenames(blocks, auto_filename=True)
        write_results = self._write_code_files(
            assigned, target_dir=str(target), dry_run=dry_run
        )
        written = [
            Path(p).name
            for p, status in write_results.items()
            if status in ("written", "skipped_dry_run")
        ]
        results["files"] = written
        return len(written)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_workspace_download_button(self):
        """Return the first visible Workspace download button, or None."""
        override = os.environ.get("ARENA_WORKSPACE_DOWNLOAD_SELECTOR")
        selectors = [override] if override else self._WORKSPACE_DOWNLOAD_SELECTORS
        for selector in selectors:
            loc = self._wait_visible(selector, timeout_ms=800)
            if loc is not None:
                print(
                    f"[info] Found Workspace download control: {selector}",
                    file=sys.stderr,
                )
                return loc
        return None

    def _find_download_control_by_heuristic(self, exclude_per_file: bool = True):
        """Locate a download control by enumerating labelled buttons/links.

        Asks the page for every visible button/anchor, returns the first
        whose aria-label or text mentions "download" (case-insensitive),
        optionally excluding per-file "Download file" buttons. Used as a
        fallback when the fixed selector list misses Arena's actual DOM.
        """
        try:
            controls = self.page.evaluate(
                """
                (excludePerFile) => {
                    const out = [];
                    const els = Array.from(document.querySelectorAll(
                        'button, a, [role="button"]'
                    )).filter(b => b.offsetParent !== null);
                    for (const b of els) {
                        const aria = (b.getAttribute('aria-label') || '').trim();
                        const text = (b.innerText || b.textContent || '').trim();
                        if (!/download/i.test(aria + ' ' + text)) continue;
                        if (excludePerFile && /download file/i.test(aria + ' ' + text)) continue;
                        out.push({ aria: aria, text: text.slice(0, 40) });
                    }
                    return out;
                }
                """,
                exclude_per_file,
            ) or []
        except Exception as exc:
            print(f"[debug] download-control heuristic enumerate failed: {exc}", file=sys.stderr)
            return None

        if not controls:
            return None
        print(
            f"[info] Heuristic found {len(controls)} download-ish control(s): "
            + ", ".join(f"{c['aria'] or c['text']!r}" for c in controls[:5]),
            file=sys.stderr,
        )
        # Click the first via a fresh locator that re-finds it. We use a
        # permissive matcher so small label variations still resolve.
        for c in controls:
            label = c.get("aria") or c.get("text") or ""
            if not label:
                continue
            safe = label.replace('"', '\\"')
            for sel in (
                f'button[aria-label="{safe}" i]',
                f'a[aria-label="{safe}" i]',
                f'[aria-label="{safe}" i]',
                f'button:has-text("{safe}")',
            ):
                loc = self._wait_visible(sel, timeout_ms=400)
                if loc is not None:
                    print(f"[info] Using heuristic download control: {sel}", file=sys.stderr)
                    return loc
        return None

    def _workspace_dump_dom(self, path: Optional[str]) -> Optional[str]:
        """Write a Workspace diagnostic report and log a summary.

        The report is an HTML file containing the Workspace panel's markup
        plus a JSON description of every button/link on the page (with
        aria-labels, text, classes, coordinates, and a download flag). This
        is the key tool for finding the real download selector when the
        heuristics miss. Returns the written path, or None on failure.
        """
        try:
            info = self.page.evaluate(
                "() => window.__arena_dump_workspace && window.__arena_dump_workspace()"
            ) or {}
        except Exception as exc:
            print(f"[warn] workspace dump failed: {exc}", file=sys.stderr)
            return None
        if not isinstance(info, dict):
            return None

        ws_found = bool(info.get("wsFound"))
        cm = int(info.get("cmEditors") or 0)
        arts = int(info.get("artifacts") or 0)
        pres = int(info.get("pres") or 0)
        per_file = int(info.get("perFileButtons") or 0)
        buttons = info.get("buttons") or []
        dl_links = info.get("downloadLinks") or []
        downloadish = [b for b in buttons if b.get("isDownload")]

        print(
            f"[info] Workspace dump: wsFound={ws_found} "
            f"cmEditors={cm} artifacts={arts} pre={pres} "
            f"perFileButtons={per_file} totalButtons={len(buttons)} "
            f"downloadLinks={len(dl_links)} downloadishButtons={len(downloadish)}",
            file=sys.stderr,
        )
        if downloadish:
            print("[info] Download-ish controls on the page:", file=sys.stderr)
            for b in downloadish[:12]:
                print(
                    f"        <{b.get('tag')}> aria={b.get('aria')!r} "
                    f"text={b.get('text')!r} at ({b.get('x')},{b.get('y')}) "
                    f"href={b.get('href')!r}",
                    file=sys.stderr,
                )

        if not path:
            path = "workspace_debug_dom.html"
        try:
            import json as _json
            with open(path, "w", encoding="utf-8") as f:
                f.write("<!-- workspace diagnostic dump -->\n")
                f.write(
                    f"<!-- wsFound={ws_found} cmEditors={cm} artifacts={arts} "
                    f"pre={pres} perFileButtons={per_file} "
                    f"downloadish={len(downloadish)} -->\n"
                )
                f.write("<details open><summary>Workspace panel HTML</summary>\n")
                f.write("<pre>")
                f.write((info.get("wsHtml") or "<no workspace element matched>").replace("<", "&lt;"))
                f.write("</pre></details>\n")
                f.write("<details><summary>Buttons/links (JSON)</summary>\n")
                f.write("<pre>")
                f.write(_json.dumps(buttons, indent=2).replace("<", "&lt;"))
                f.write("</pre></details>\n")
                f.write("<details><summary>Download links (JSON)</summary>\n")
                f.write("<pre>")
                f.write(_json.dumps(dl_links, indent=2).replace("<", "&lt;"))
                f.write("</pre></details>\n")
        except OSError as exc:
            print(f"[warn] could not write workspace dump: {exc}", file=sys.stderr)
            return None
        return path

    def _wait_for_workspace_stable(
        self, timeout_s: float = 120.0, stable_s: float = 5.0
    ) -> bool:
        """Poll the Workspace signature until it stops changing.

        Returns True if the Workspace settled (or was never present), False if
        the timeout elapsed while it kept changing. Uses the same
        ``__arena_workspace_state`` helper the completion loop relies on, so
        files still appearing / being written keep resetting the timer.
        """
        try:
            state = self.page.evaluate(
                "() => window.__arena_workspace_state && window.__arena_workspace_state()"
            ) or {}
        except Exception:
            state = {}
        present = bool(state.get("present"))
        if not present:
            # No Workspace panel -- nothing to wait for.
            return True

        interval = 0.5
        needed = max(2, int(stable_s / interval))
        stable = 0
        last_sig = state.get("signature") or ""
        start = time.time()
        last_file_count = -1

        while time.time() - start < timeout_s:
            try:
                state = self.page.evaluate(
                    "() => window.__arena_workspace_state()"
                ) or {}
            except Exception:
                state = {}
            sig = state.get("signature") or ""
            try:
                fc = int(state.get("fileCount") or 0)
            except (TypeError, ValueError):
                fc = 0
            writing = bool(state.get("writing"))

            if fc != last_file_count:
                print(
                    f"[info] Workspace has {fc} file(s); waiting for "
                    f"files to finish generating...",
                    file=sys.stderr,
                )
                last_file_count = fc

            if sig == last_sig and not writing:
                stable += 1
            else:
                stable = 0
            last_sig = sig

            if stable >= needed:
                print(
                    f"[info] Workspace settled ({fc} file(s), stable for "
                    f"{stable * interval:.1f}s). Proceeding to download.",
                    file=sys.stderr,
                )
                return True
            time.sleep(interval)

        print(
            f"[warn] Workspace did not settle within {timeout_s}s "
            f"(last {last_file_count} file(s)); downloading anyway.",
            file=sys.stderr,
        )
        return False

    def _collect_visible_buttons(self, selector_list: List[str]) -> List:
        """Collect visible buttons matching any selector in ``selector_list``.

        De-duplicates by screen position so the same button matched by
        multiple selectors isn't clicked twice.
        """
        seen_positions = set()
        collected: List = []
        for selector in selector_list:
            try:
                locs = self.page.locator(selector)
                count = locs.count()
            except Exception:
                count = 0
            for i in range(count):
                btn = locs.nth(i)
                try:
                    if not btn.is_visible():
                        continue
                except Exception:
                    continue
                try:
                    box = btn.bounding_box()
                    key = (
                        (round(box["x"], 1), round(box["y"], 1))
                        if box else f"no-box-{id(btn)}"
                    )
                except Exception:
                    key = f"no-box-{id(btn)}"
                if key in seen_positions:
                    continue
                seen_positions.add(key)
                collected.append(btn)
            if collected:
                # If we found buttons with the first matching selector,
                # prefer that set over re-matching later (broader) ones.
                break
        return collected

    def _ensure_workspace_visible(self):
        """Best-effort: expand the Workspace panel if it is collapsed.

        Clicks a "Workspace" / "Files" tab or toggle if one is present.
        Errors are swallowed -- this is purely opportunistic.
        """
        for selector in self._WORKSPACE_TOGGLE_SELECTORS:
            loc = self._wait_visible(selector, timeout_ms=400)
            if loc is not None:
                try:
                    loc.click()
                    time.sleep(0.3)
                    print(
                        f"[info] Opened Workspace panel via: {selector}",
                        file=sys.stderr,
                    )
                except Exception:
                    pass
                return

    def _safe_extract_zip(
        self, zip_path: Path, dest_dir: Path
    ) -> List[str]:
        """Extract ``zip_path`` into ``dest_dir`` guarding against zip-slip.

        Returns the basenames of the files extracted. Directory entries and
        unsafe (path-traversal) members are skipped.
        """
        dest = Path(dest_dir).resolve()
        extracted: List[str] = []
        try:
            with zipfile.ZipFile(zip_path) as zf:
                for member in zf.infolist():
                    if member.is_dir():
                        continue
                    name = member.filename
                    target = (dest / name).resolve()
                    try:
                        target.relative_to(dest)
                    except ValueError:
                        print(
                            f"[warn] Skipping unsafe zip member: {name}",
                            file=sys.stderr,
                        )
                        continue
                    zf.extract(member, dest)
                    # Report the member's relative path (e.g.
                    # "project/main.py") so the results list reflects the
                    # on-disk layout exactly.
                    extracted.append(name)
        except (zipfile.BadZipFile, OSError) as exc:
            print(
                f"[warn] Failed to extract ZIP {zip_path}: {exc}",
                file=sys.stderr,
            )
        return extracted
