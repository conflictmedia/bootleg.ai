"""JavaScript helpers injected into the Arena page for fast polling.

Split out from aai.py. The bulk of this file is the verbatim JS source
string that defines `window.__arena_poll`, `window.__arena_get_code_blocks`,
`window.__arena_extract_clean_code`, etc. These run inside the page so each
poll is a single `page.evaluate` round-trip instead of many Locator queries.
"""

import sys


class JSHelpersMixin:
    """Provides `_inject_js_helpers` for ArenaAgent."""

    def _inject_js_helpers(self):
        """Inject pure-JS helpers that run inside the page for cheap polling.

        Each poll becomes a single `page.evaluate("() => window.__arena_poll()")`
        round-trip instead of multiple Locator queries, which is dramatically
        faster.
        """
        js = r"""
            () => {
                if (window.__arena_helpers_loaded
                        && typeof window.__arena_get_code_blocks === 'function'
                        && typeof window.__arena_extract_clean_code === 'function') return;
                window.__arena_helpers_loaded = true;

                // Extract code text from a <pre>/<code> element without line-number
                // gutters or syntax-highlighting wrapper artifacts. This helper is
                // called by __arena_get_code_blocks(); it must exist before polling
                // starts or __arena_poll() will throw.
                window.__arena_extract_clean_code = function(codeEl) {
                    if (!codeEl) return '';

                    const cleanText = (text) => {
                        text = text || '';
                        if (typeof window.__arena_strip_line_numbers === 'function') {
                            text = window.__arena_strip_line_numbers(text);
                        }
                        return text;
                    };

                    // Shiki commonly renders one .line span per source line.
                    // Prefer these over raw innerText because parent <pre> nodes
                    // can also contain gutter/decoration text.
                    const shikiLines = codeEl.querySelectorAll('.line');
                    if (shikiLines && shikiLines.length) {
                        const lines = Array.from(shikiLines).map(line =>
                            line.innerText || line.textContent || ''
                        );
                        return cleanText(lines.join('\n'));
                    }

                    // If a nested <code> exists, it is usually the real source
                    // container. Use it unless it is just the same element.
                    const nestedCode = codeEl.matches && codeEl.matches('code')
                        ? null
                        : codeEl.querySelector && codeEl.querySelector('code');
                    if (nestedCode) {
                        const nestedLines = nestedCode.querySelectorAll('.line');
                        if (nestedLines && nestedLines.length) {
                            const lines = Array.from(nestedLines).map(line =>
                                line.innerText || line.textContent || ''
                            );
                            return cleanText(lines.join('\n'));
                        }
                        const nestedText = nestedCode.innerText || nestedCode.textContent || '';
                        if (nestedText && nestedText.trim()) return cleanText(nestedText);
                    }

                    // Generic fallback. innerText preserves visual newlines better
                    // in browsers; textContent is kept for environments where
                    // innerText is unavailable.
                    return cleanText(codeEl.innerText || codeEl.textContent || '');
                };

                window.__arena_get_assistant_bubbles = function() {
                    let candidates = document.querySelectorAll('[class*="bg-surface-primary"]');
                    if (!candidates.length) {
                        candidates = document.querySelectorAll('[data-message-author-role="assistant"]');
                    }
                    if (!candidates.length) {
                        candidates = document.querySelectorAll(
                            '.assistant-message, .message-assistant, [class*="assistant" i]'
                        );
                    }
                    // Filter: only keep bubbles that contain a .prose descendant.
                    // The `bg-surface-primary` class is also used by *inner*
                    // elements (e.g. the code-preview div inside an artifact
                    // card), and those would shadow the real outer message
                    // bubble when we pick "last". Requiring a .prose child
                    // keeps only the actual message containers.
                    let filtered = Array.from(candidates).filter(b => b.querySelector('.prose'));
                    if (filtered.length) return filtered;

                    // Fallback A: ancestor wrappers of any .prose element.
                    // Some Arena UI variants don't use bg-surface-primary on
                    // the outer message bubble at all -- the prose just sits
                    // inside a generic border/surface div. We treat the closest
                    // ancestor of each .prose that contains a <p> or <pre> as a
                    // candidate bubble.
                    const allProse = Array.from(document.querySelectorAll('.prose'));
                    const ancestors = new Set();
                    for (const p of allProse) {
                        // Walk up to find the first ancestor that does NOT
                        // itself have class .prose and that contains multiple
                        // child block elements (i.e. a real message container).
                        let parent = p.parentElement;
                        let steps = 0;
                        while (parent && steps < 8) {
                            const hasBlock = parent.querySelector('p, pre, ul, ol, h1, h2, h3, h4, h5, h6');
                            if (hasBlock && !parent.classList.contains('prose')) {
                                ancestors.add(parent);
                                break;
                            }
                            parent = parent.parentElement;
                            steps++;
                        }
                    }
                    if (ancestors.size) return Array.from(ancestors);

                    // Fallback B: just return the raw candidates as-is.
                    return Array.from(candidates);
                };

                window.__arena_get_text = function() {
                    const bubbles = window.__arena_get_assistant_bubbles();
                    if (!bubbles.length) return '';
                    const last = bubbles[bubbles.length - 1];
                    // Prefer .prose that is NOT inside a .not-prose block
                    // (thoughts / tool-call sections live inside .not-prose).
                    const proseEls = last.querySelectorAll('.prose');
                    for (const p of proseEls) {
                        if (p.closest('.not-prose')) continue;
                        // innerText is CSS-aware (honours visibility/white-space);
                        // fall back to textContent for headless DOMs that don't
                        // implement innerText fully (e.g. older JSDOM).
                        return p.innerText || p.textContent || '';
                    }
                    const anyProse = last.querySelector('.prose');
                    if (anyProse) return anyProse.innerText || anyProse.textContent || '';
                    return last.innerText || last.textContent || '';
                };

                window.__arena_is_generating = function() {
                    // 1) Global "Stop" button visible anywhere on the page.
                    //    This is the most reliable "still generating" signal.
                    const allBtns = document.querySelectorAll('button');
                    for (const b of allBtns) {
                        if (b.offsetParent === null) continue;  // not visible
                        const txt = (b.textContent || '').trim();
                        const aria = (b.getAttribute('aria-label') || '').trim();
                        // Match "Stop", "Stop generating", "Stop streaming" but
                        // not "Stop watching this thread" etc.
                        if (txt.length <= 25 && /^stop(\s|$)/i.test(txt)) return true;
                        if (aria.length <= 30 && /^stop(\s|$)/i.test(aria)) return true;
                    }

                    const bubbles = window.__arena_get_assistant_bubbles();
                    const last = bubbles.length ? bubbles[bubbles.length - 1] : null;

                    // 2) Spinner inside the last assistant bubble.
                    if (last) {
                        const spinners = last.querySelectorAll('svg[class*="animate-spin"]');
                        for (const s of spinners) {
                            if (s.offsetParent !== null) return true;
                        }
                    }

                    // 3) Loading / streaming indicator classes.
                    if (last) {
                        const loading = last.querySelectorAll(
                            '[class*="loading" i], [class*="streaming" i], ' +
                            '[class*="generating" i], [class*="thinking" i]'
                        );
                        for (const el of loading) {
                            if (el.offsetParent !== null) return true;
                        }
                    }

                    // 4) Tool-call / thought blocks WITHOUT a prose answer yet.
                    //    Arena wraps each tool call / thought in a `.not-prose`
                    //    block. While the agent is mid-execution, these blocks
                    //    appear WITHOUT a corresponding final prose answer. Once
                    //    the prose answer renders, generation is effectively done
                    //    (the tool blocks remain in the DOM as a record of what
                    //    happened, but they are no longer "active").
                    //
                    //    We deliberately do NOT match verbs like "Running" or
                    //    "Writing" inside tool blocks -- those labels persist
                    //    after completion (e.g. "Write matrix.py 181 lines")
                    //    and would false-positive on every completed tool call,
                    //    causing isGenerating to stay True forever.
                    if (last) {
                        const toolBlocks = last.querySelectorAll(
                            '.not-prose, [class*="tool-call" i], [class*="thinking" i], ' +
                            '[class*="tool_use" i], [data-tool-call]'
                        );
                        let visibleToolCount = 0;
                        for (const el of toolBlocks) {
                            if (el.offsetParent === null) continue;
                            visibleToolCount++;
                        }
                        // If we have visible tool blocks but no prose answer
                        // yet, the agent is mid-tool-execution.
                        const proseAnswer = last.querySelector('.prose');
                        if (visibleToolCount > 0 && !proseAnswer) return true;
                    }

                    return false;
                };

                window.__arena_get_code_blocks = function() {
                    const bubbles = window.__arena_get_assistant_bubbles();
                    const blocks = [];
                    const searchRoots = bubbles.length
                        ? [bubbles[bubbles.length - 1]]
                        : [document.body];  // broad fallback if no bubble detected

                    for (const last of searchRoots) {
                    // 1. Artifact file viewers (Tailwind `group/artifact` class).
                    //    These are the big "file preview" cards.
                    const artifactEls = last.querySelectorAll('[class*="group/artifact"]');
                    for (const art of artifactEls) {
                        // Skip nested artifacts; the outermost captures everything.
                        if (art.parentElement &&
                            art.parentElement.closest('[class*="group/artifact"]')) continue;

                        // Filename: the .truncate span inside the header row.
                        // Match the header span specifically to avoid grabbing
                        // the "181 lines" hint that also has .truncate.
                        let filename = '';
                        const filenameEl = art.querySelector(
                            '.min-w-0.flex-1.truncate, span.truncate.text-sm, .truncate.text-sm'
                        );
                        if (filenameEl) filename = filenameEl.textContent.trim();

                        // Language: the uppercase badge.
                        let lang = '';
                        const langEl = art.querySelector('[class*="uppercase"]');
                        if (langEl) lang = langEl.textContent.trim();

                        // Code content: the .whitespace-pre-wrap.font-mono div.
                        // innerText returns the full text even though the
                        // element is visually clipped to h-[260px].
                        let codeEl = art.querySelector('.whitespace-pre-wrap.font-mono');
                        if (!codeEl) codeEl = art.querySelector('.whitespace-pre-wrap');
                        if (!codeEl) codeEl = art.querySelector('pre code, pre');
                        if (!codeEl) {
                            // Last-ditch: any <code> anywhere in the artifact.
                            codeEl = art.querySelector('code');
                        }
                        const code = codeEl ? (codeEl.innerText || codeEl.textContent || '') : '';
                        if (code && code.trim()) {
                            blocks.push({
                                type: 'artifact',
                                filename: filename,
                                language: lang,
                                code: code
                            });
                        }
                    }

                    // 2. data-code-block containers (Arena's labeled code blocks).
                    //    These are div[data-code-block="true"] with a header
                    //    showing the language (e.g. "JSON", "Bash", "Python")
                    //    and a <pre class="shiki"> inside. They appear in prose
                    //    sections and are distinct from artifact file viewers.
                    //    We extract them separately to get the correct language
                    //    label from the header (the generic <pre> path would
                    //    just see "shiki" as the class).
                    const codeBlockEls = last.querySelectorAll('[data-code-block="true"]');
                    const capturedCodeBlockPres = new Set();
                    for (const cb of codeBlockEls) {
                        // Skip if this is inside an artifact (already captured).
                        if (cb.closest('[class*="group/artifact"]')) continue;

                        // Language: look for the header span. Arena uses either:
                        //   <span class="... uppercase ...">PYTHON</span>  (artifacts)
                        //   <span class="... text-sm font-medium">JSON</span>  (data-code-block)
                        // Try uppercase badge first, then the text-sm font-medium span.
                        let lang = '';
                        const uppercaseEl = cb.querySelector('[class*="uppercase"]');
                        if (uppercaseEl) {
                            lang = uppercaseEl.textContent.trim();
                        } else {
                            // Look for the language label span in the header.
                            // It's typically a <span> with class containing
                            // "text-sm" and "font-medium", inside the header div.
                            const headerSpans = cb.querySelectorAll(
                                'span.text-sm, span.font-medium, span[class*="text-text-secondary"]'
                            );
                            for (const sp of headerSpans) {
                                const txt = sp.textContent.trim();
                                // Language labels are short (1-20 chars) and
                                // don't contain spaces (e.g. "JSON", "Python",
                                // "Bash"). Skip "181 lines" or filenames.
                                if (txt && txt.length <= 20 && !/\s/.test(txt)
                                    && !/\d+\s*lines?/i.test(txt)) {
                                    lang = txt;
                                    break;
                                }
                            }
                        }

                        // Code content: the <pre> inside (usually shiki-highlighted).
                        // Use the shared helper that prefers .line spans over
                        // raw innerText (avoids line-number gutter contamination).
                        let codeEl = cb.querySelector('pre');
                        if (!codeEl) codeEl = cb.querySelector('code');
                        if (!codeEl) codeEl = cb.querySelector('.whitespace-pre-wrap');
                        const code = codeEl
                            ? window.__arena_extract_clean_code(codeEl)
                            : '';
                        if (code && code.trim()) {
                            // Track the <pre> element so the generic <pre> path
                            // below can skip it (avoids duplicate extraction).
                            if (codeEl && codeEl.tagName === 'PRE') {
                                capturedCodeBlockPres.add(codeEl);
                            } else if (codeEl) {
                                const parentPre = codeEl.closest('pre');
                                if (parentPre) capturedCodeBlockPres.add(parentPre);
                            }
                            blocks.push({
                                type: 'code-block',
                                filename: '',
                                language: lang,
                                code: code
                            });
                        }
                    }

                    // 3. Inline <pre> blocks in prose (shiki-highlighted or plain).
                    //    Skip any that live inside an artifact (already captured
                    //    by path 1) or inside a data-code-block (already captured
                    //    by path 2). Skip any <pre> that WRAPS another <pre> --
                    //    Arena nests the shiki-highlighted <pre> inside a plain
                    //    outer <pre>, and we only want the inner one.
                    const pres = last.querySelectorAll('pre');
                    for (const pre of pres) {
                        if (pre.closest('[class*="group/artifact"]')) continue;
                        if (pre.closest('[data-code-block="true"]')) continue;
                        if (pre.closest('.cm-editor')) continue;  // CodeMirror -- handled separately
                        if (pre.closest('.cm-scroller')) continue;  // CodeMirror scroller
                        if (pre.closest('.cm-content')) continue;  // CodeMirror content
                        if (pre.closest('.cm-theme')) continue;  // CodeMirror theme wrapper
                        if (pre.querySelector('.cm-line')) continue;  // contains CodeMirror lines
                        if (pre.querySelector('.cm-gutter')) continue;  // contains CodeMirror gutter
                        if (capturedCodeBlockPres.has(pre)) continue;
                        if (pre.querySelector('pre')) continue;  // outer wrapper; inner will be captured
                        // Extract code. Use innerText (CSS-aware), fall back to
                        // textContent. Then strip leading line numbers if the
                        // <pre> appears to contain gutter-numbered lines (a
                        // common artifact when CodeMirror content leaks into a
                        // parent <pre>).
                        let code = pre.innerText || pre.textContent || '';
                        if (code) {
                            code = window.__arena_strip_line_numbers(code);
                        }
                        if (!code || !code.trim()) continue;
                        // Try to detect language from <code> class.
                        let lang = '';
                        const codeEl = pre.querySelector('code');
                        if (codeEl) {
                            const cls = codeEl.className || '';
                            const m = cls.match(/language-([a-z0-9+#-]+)/i);
                            if (m) lang = m[1];
                        }
                        if (!lang) {
                            const cls = pre.className || '';
                            if (/shiki/i.test(cls)) lang = 'shiki';
                        }
                        blocks.push({
                            type: 'inline',
                            filename: '',
                            language: lang,
                            code: code
                        });
                    }

                    return blocks;
                    }  // end for (searchRoots)
                };

                // Diagnostic helper: dump the last assistant bubble's
                // outerHTML so we can see what's actually on the page when
                // extraction fails. Returns {html, numBubbles, numPres,
                // numArtifacts, bubbleClasses}.
                window.__arena_dump_dom = function() {
                    const bubbles = window.__arena_get_assistant_bubbles();
                    const last = bubbles.length ? bubbles[bubbles.length - 1] : null;
                    if (!last) {
                        return {
                            found: false,
                            html: '',
                            numBubbles: 0,
                            numPres: document.querySelectorAll('pre').length,
                            numArtifacts: document.querySelectorAll('[class*="group/artifact"]').length,
                            bubbleClasses: [],
                            bodyPreview: (document.body.innerText || '').slice(0, 500)
                        };
                    }
                    return {
                        found: true,
                        html: last.outerHTML,
                        numBubbles: bubbles.length,
                        numPres: last.querySelectorAll('pre').length,
                        numArtifacts: last.querySelectorAll('[class*="group/artifact"]').length,
                        bubbleClasses: bubbles.map(b => b.className.slice(0, 120)),
                        bodyPreview: ''
                    };
                };

                // Send-button state probe. Returns {found, disabled, visible}.
                // The send button carries the HTML `disabled` attribute while
                // the input is empty OR while a generation is in flight. It
                // becomes enabled once the input has text AND no generation is
                // running. We use this as a precise signal that:
                //   (a) the input has accepted our text (button enabled pre-submit),
                //   (b) the prompt was accepted by the server (button goes disabled
                //       immediately on submit),
                //   (c) the response has finished (button re-enabled).
                //
                // Selector order matches _get_send_selector() in Python.
                //
                // We deliberately check ONLY the HTML `disabled` attribute /
                // IDL property, NOT Tailwind classes like `pointer-events-none`.
                // Tailwind class strings also contain `disabled:pointer-events-none`
                // (a variant prefix), which would false-positive on every button.
                window.__arena_send_button_state = function() {
                    // Try aria-label selectors FIRST -- these are more specific
                    // than button[type="submit"] (which matches every form's
                    // submit button, including unrelated ones on the page).
                    const candidates = [
                        'button[aria-label="Send message" i]',
                        'button[aria-label="Send" i]',
                        'button[aria-label*="send message" i]',
                        'button[aria-label*="submit" i]',
                        'button[aria-label*="arrow" i]',
                        'button[type="submit"]'
                    ];
                    for (const sel of candidates) {
                        const list = Array.from(document.querySelectorAll(sel));
                        // Pick the first VISIBLE match (offsetParent !== null).
                        const btn = list.find(b => b.offsetParent !== null);
                        if (btn) {
                            const disabled = btn.disabled === true
                                || btn.hasAttribute('disabled');
                            return { found: true, disabled: !!disabled, visible: true };
                        }
                    }
                    return { found: false, disabled: false, visible: false };
                };

                // Copy-button state probe. Returns {found, visible}.
                // Arena renders a "Copy" button (aria-label="Copy") in the
                // action footer of each assistant message ONLY when the message
                // is fully complete and persisted. While streaming, the footer
                // either doesn't exist or shows a Stop button instead.
                //
                // This is the STRONGEST "generation done" signal -- stronger
                // than the Stop button disappearing (can be flaky) or the send
                // button re-enabling (can happen between turns). We look for
                // the Copy button specifically within the LAST assistant bubble
                // to avoid matching Copy buttons on earlier messages in the
                // conversation.
                window.__arena_copy_button_state = function() {
                    const bubbles = window.__arena_get_assistant_bubbles();
                    if (!bubbles.length) return { found: false, visible: false };
                    const last = bubbles[bubbles.length - 1];
                    // Try several selectors in order of specificity.
                    const candidates = [
                        'button[aria-label="Copy"]',
                        'button[aria-label="Copy" i]',
                        'button[aria-label*="copy" i]'
                    ];
                    for (const sel of candidates) {
                        const btns = last.querySelectorAll(sel);
                        for (const b of btns) {
                            // Must be visible (offsetParent !== null).
                            if (b.offsetParent !== null) {
                                return { found: true, visible: true };
                            }
                        }
                        // Found at least one matching button but not visible --
                        // still report found so we can distinguish "footer
                        // exists but button hidden" from "no button at all".
                        if (btns.length) {
                            return { found: true, visible: false };
                        }
                    }
                    return { found: false, visible: false };
                };

                // CodeMirror editor extractor (ASYNC -- not called from poll).
                // Arena renders some code blocks as CodeMirror 6 editors with
                // a header bar (filename + Download + Close buttons) and a
                // virtualized scrolling code area. The code lives in
                // `.cm-line` divs inside `.cm-content`, but only VISIBLE
                // lines are in the DOM (off-screen lines are represented by
                // `.cm-gap` spacer divs). To get the full file, we must
                // scroll the editor top-to-bottom, collecting lines as they
                // become visible, then pair them with gutter line numbers.
                //
                // Returns an array of {type, filename, language, code}.
                window.__arena_get_codemirror_blocks = async function() {
                    const blocks = [];
                    const editors = document.querySelectorAll('.cm-editor');
                    for (const editor of editors) {
                        // Find the card wrapper by walking up to the nearest
                        // ancestor that contains a Download button.
                        let card = editor.parentElement;
                        for (let i = 0; i < 10 && card; i++) {
                            if (card.querySelector('[aria-label="Download file"]')) break;
                            card = card.parentElement;
                        }

                        // Filename: look for the span with data-slot="tooltip-trigger"
                        // or any .truncate span in the header that isn't "Download".
                        let filename = '';
                        if (card) {
                            const fnEl = card.querySelector('[data-slot="tooltip-trigger"]');
                            if (fnEl && fnEl.textContent.trim()) {
                                filename = fnEl.textContent.trim();
                            }
                            if (!filename) {
                                const truncs = card.querySelectorAll('.truncate');
                                for (const t of truncs) {
                                    const txt = t.textContent.trim();
                                    if (txt && txt !== 'Download' && /\./.test(txt)) {
                                        filename = txt;
                                        break;
                                    }
                                }
                            }
                        }

                        // Language: from data-language on .cm-content.
                        let lang = '';
                        const cmContent = editor.querySelector('.cm-content');
                        if (cmContent) {
                            lang = cmContent.getAttribute('data-language') || '';
                        }

                        // Get the scroller element.
                        const scroller = editor.querySelector('.cm-scroller');
                        if (!scroller) {
                            // No scroller -- grab lines directly (no virtualization).
                            const lines = editor.querySelectorAll('.cm-line');
                            const code = Array.from(lines)
                                .map(l => l.textContent).join('\n');
                            if (code.trim()) {
                                blocks.push({
                                    type: 'codemirror',
                                    filename: filename,
                                    language: lang,
                                    code: code
                                });
                            }
                            continue;
                        }

                        // Virtualized: scroll to collect all lines.
                        // Use a Map keyed by line number for dedup.
                        const linesByNum = new Map();
                        const orphanLines = []; // lines without gutter numbers
                        const origScrollTop = scroller.scrollTop;

                        // Scroll to top, wait for render.
                        scroller.scrollTop = 0;
                        await new Promise(r => setTimeout(r, 80));

                        let lastCollected = 0;
                        let stableCount = 0;

                        while (true) {
                            const lineEls = editor.querySelectorAll('.cm-line');
                            // Gutter line numbers are in .cm-gutter.cm-lineNumbers
                            const gutterEls = editor.querySelectorAll(
                                '.cm-gutter.cm-lineNumbers .cm-gutterElement'
                            );

                            if (gutterEls.length > 0) {
                                // Pair lineEls with gutterEls by index.
                                // Note: gutterEls may include a hidden spacer
                                // (the "999" element with visibility:hidden).
                                // Filter to only visible numeric elements.
                                const pairs = [];
                                let gutterIdx = 0;
                                for (let i = 0; i < lineEls.length && gutterIdx < gutterEls.length; i++) {
                                    // Advance gutter to next numeric element.
                                    while (gutterIdx < gutterEls.length) {
                                        const txt = gutterEls[gutterIdx].textContent.trim();
                                        const num = parseInt(txt, 10);
                                        if (!isNaN(num) && txt.length > 0) {
                                            pairs.push([num, lineEls[i]]);
                                            gutterIdx++;
                                            break;
                                        }
                                        gutterIdx++;
                                    }
                                }
                                for (const [num, el] of pairs) {
                                    if (!linesByNum.has(num)) {
                                        linesByNum.set(num, el.textContent);
                                    }
                                }
                            } else {
                                // No gutter -- collect in order. Ded by
                                // consecutive-difference to avoid duplicates
                                // from overlapping scroll positions.
                                for (const el of lineEls) {
                                    orphanLines.push(el.textContent);
                                }
                            }

                            // Check if we've reached the bottom.
                            if (scroller.scrollTop + scroller.clientHeight
                                    >= scroller.scrollHeight - 1) break;

                            // Scroll down by 80% of visible height.
                            scroller.scrollTop += scroller.clientHeight * 0.8;
                            await new Promise(r => setTimeout(r, 80));

                            // Safety: stop if no new lines for 3 consecutive scrolls.
                            const total = linesByNum.size + orphanLines.length;
                            if (total === lastCollected) {
                                stableCount++;
                                if (stableCount >= 3) break;
                            } else {
                                stableCount = 0;
                            }
                            lastCollected = total;
                        }

                        // Restore scroll position.
                        scroller.scrollTop = origScrollTop;

                        // Build the code text.
                        let code;
                        if (linesByNum.size > 0) {
                            const sorted = Array.from(linesByNum.entries())
                                .sort((a, b) => a[0] - b[0]);
                            code = sorted.map(([_, text]) => text).join('\n');
                        } else {
                            // Deduplicate consecutive identical lines.
                            const deduped = [];
                            for (let i = 0; i < orphanLines.length; i++) {
                                if (i === 0 || orphanLines[i] !== orphanLines[i - 1]) {
                                    deduped.push(orphanLines[i]);
                                }
                            }
                            code = deduped.join('\n');
                        }

                        if (code.trim()) {
                            blocks.push({
                                type: 'codemirror',
                                filename: filename,
                                language: lang,
                                code: code
                            });
                        }
                    }
                    return blocks;
                };

                // Strip leading line numbers from code text.
                // When CodeMirror content leaks into a parent <pre> (or when
                // a shiki-highlighted block includes gutter numbers), the
                // extracted text looks like:
                //   3
                //   # The Majestic Aurora Borealis
                //   4
                //   5
                //   The Aurora Borealis...
                // This function detects and removes the standalone number
                // lines, preserving the actual code content. It only fires
                // if the text has >= 4 lines AND at least 30% of lines are
                // pure integers (to avoid false-positives on legitimate code
                // that happens to contain numbers on their own lines).
                window.__arena_strip_line_numbers = function(text) {
                    if (!text) return text;
                    const lines = text.split("\n");
                    if (lines.length < 4) return text;
                    // Count lines that are pure integers (the gutter numbers).
                    let numLineCount = 0;
                    const isNumLine = lines.map(l => {
                        const t = l.trim();
                        return t.length > 0 && /^\d+$/.test(t);
                    });
                    for (const b of isNumLine) if (b) numLineCount++;
                    // Only strip if >= 30% of lines are pure numbers AND the
                    // numbers form an increasing sequence (gutter pattern).
                    if (numLineCount / lines.length < 0.3) return text;
                    // Check that the numeric lines form a non-decreasing sequence.
                    let lastNum = 0;
                    let seqOk = true;
                    for (let i = 0; i < lines.length; i++) {
                        if (isNumLine[i]) {
                            const n = parseInt(lines[i].trim(), 10);
                            if (n < lastNum) { seqOk = false; break; }
                            lastNum = n;
                        }
                    }
                    if (!seqOk) return text;
                    // Strip the number lines.
                    const stripped = lines.filter((_, i) => !isNumLine[i]);
                    return stripped.join("\n");
                };

                // Workspace state probe. Returns {present, fileCount, files,
                // writing, signature}.
                //
                // Arena Agent Mode renders a "Workspace" panel (right side)
                // listing every file the agent created. The completion loop
                // uses this to avoid finalizing while files are still being
                // generated: `signature` changes whenever a file appears or a
                // file's rendered status changes (e.g. the "Write X.py 181
                // lines" tool-call status, whose line count grows as the file
                // is written). `writing` is true when a workspace spinner /
                // progress indicator is visible.
                //
                // We deliberately derive `signature` from things that CHANGE
                // during generation (not the persistent verb labels, which
                // remain after completion and would block finalize forever).
                window.__arena_workspace_state = function() {
                    const state = { present: false, fileCount: 0, files: [], writing: false, signature: '' };

                    // --- Locate file entries via per-file Download buttons ---
                    const dlBtns = Array.from(document.querySelectorAll(
                        'button[aria-label="Download file"], button[aria-label*="download file" i]'
                    )).filter(b => b.offsetParent !== null);

                    // Whole-workspace download button (broad marker that a
                    // Workspace panel exists even when no per-file buttons do).
                    const dlAll = Array.from(document.querySelectorAll(
                        'button[aria-label*="download" i]:not([aria-label*="file" i])'
                    )).filter(b => b.offsetParent !== null);

                    const wsMarker = document.querySelector(
                        '[data-testid*="workspace" i], [class*="workspace" i]'
                    );

                    state.present = dlBtns.length > 0 || dlAll.length > 0 || !!wsMarker;
                    state.fileCount = dlBtns.length;

                    // --- Collect filenames near each download button ---
                    const names = [];
                    for (const btn of dlBtns) {
                        let row = btn;
                        for (let i = 0; i < 6 && row; i++) {
                            const trunc = row.querySelector && row.querySelector(
                                '.truncate, [data-slot="tooltip-trigger"], span[class*="file" i]'
                            );
                            if (trunc) {
                                const t = (trunc.textContent || '').trim();
                                if (t && t !== 'Download' && /\./.test(t)) { names.push(t); break; }
                            }
                            row = row.parentElement;
                        }
                    }
                    state.files = names;

                    // --- Detect active writing (spinners / progress only) ---
                    const wsRoot = wsMarker || document.body;
                    if (wsRoot) {
                        const spins = wsRoot.querySelectorAll(
                            'svg[class*="animate-spin" i], [class*="progress" i], [role="progressbar" i]'
                        );
                        for (const s of spins) {
                            if (s.offsetParent !== null) { state.writing = true; break; }
                        }
                    }

                    // --- Signature: changes as files appear or statuses change ---
                    // Include the last assistant bubble's tool-call (.not-prose)
                    // status text -- e.g. "Write matrix.py 181 lines". The line
                    // count grows as the file is written, so the signature
                    // changes during generation and stabilizes when done.
                    const bubbles = window.__arena_get_assistant_bubbles();
                    const last = bubbles.length ? bubbles[bubbles.length - 1] : null;
                    let toolText = '';
                    if (last) {
                        const toolBlocks = last.querySelectorAll('.not-prose, [class*="tool" i]');
                        for (const tb of toolBlocks) {
                            if (tb.offsetParent === null) continue;
                            toolText += ' ' + (tb.innerText || '').replace(/\\s+/g, ' ').trim();
                        }
                    }
                    // Last 240 chars of tool status catches the most recent
                    // (often actively-changing) line; filenames catch new files.
                    // Include fileCount too so a new file is ALWAYS detected
                    // even if filename collection misses (e.g. unfamiliar DOM).
                    state.signature = 'n=' + names.length + '|f=' + state.fileCount
                        + '|' + names.slice().sort().join(',')
                        + '#' + toolText.slice(-240);

                    return state;
                };

                // WHOLE-DOCUMENT file-block scanner. __arena_get_code_blocks()
                // only scans the LAST chat bubble, but the Workspace panel is a
                // separate DOM subtree. This helper scans document.body for
                // artifact file viewers + CodeMirror editors + labeled code
                // blocks anywhere on the page, so the download fallback can
                // recover files that live in the Workspace panel. Returns the
                // same {type, filename, language, code} shape as the others.
                //
                // NOTE: this does NOT scroll virtualized CodeMirror editors
                // (that's __arena_get_codemirror_blocks' async job). It only
                // grabs what is currently rendered.
                window.__arena_get_all_code_blocks = function() {
                    const blocks = [];
                    const root = document.body;
                    if (!root) return blocks;

                    // Artifact file viewers anywhere in the document.
                    const artifactEls = root.querySelectorAll('[class*="group/artifact"]');
                    for (const art of artifactEls) {
                        if (art.parentElement &&
                            art.parentElement.closest('[class*="group/artifact"]')) continue;
                        let filename = '';
                        const filenameEl = art.querySelector(
                            '.min-w-0.flex-1.truncate, span.truncate.text-sm, .truncate.text-sm, [data-slot="tooltip-trigger"]'
                        );
                        if (filenameEl) filename = filenameEl.textContent.trim();
                        let lang = '';
                        const langEl = art.querySelector('[class*="uppercase"]');
                        if (langEl) lang = langEl.textContent.trim();
                        let codeEl = art.querySelector('.whitespace-pre-wrap.font-mono')
                            || art.querySelector('.whitespace-pre-wrap')
                            || art.querySelector('pre code, pre')
                            || art.querySelector('code');
                        const code = codeEl ? window.__arena_extract_clean_code(codeEl) : '';
                        if (code && code.trim()) {
                            blocks.push({ type: 'artifact', filename: filename, language: lang, code: code });
                        }
                    }

                    // Non-virtualized CodeMirror editors (lines present in DOM).
                    // Virtualized editors are handled by the async CodeMirror
                    // extractor; this is a cheap synchronous grab for any that
                    // aren't virtualized (e.g. small files fully rendered).
                    const editors = root.querySelectorAll('.cm-editor');
                    const seenEditors = new Set();
                    for (const editor of editors) {
                        // Walk up to the card to find a filename.
                        let card = editor.parentElement;
                        for (let i = 0; i < 10 && card; i++) {
                            if (card.querySelector('[aria-label="Download file"]')) break;
                            card = card.parentElement;
                        }
                        let filename = '';
                        if (card) {
                            const fnEl = card.querySelector('[data-slot="tooltip-trigger"]');
                            if (fnEl && fnEl.textContent.trim()) filename = fnEl.textContent.trim();
                            if (!filename) {
                                const truncs = card.querySelectorAll('.truncate');
                                for (const t of truncs) {
                                    const txt = t.textContent.trim();
                                    if (txt && txt !== 'Download' && /\./.test(txt)) { filename = txt; break; }
                                }
                            }
                        }
                        let lang = '';
                        const cmContent = editor.querySelector('.cm-content');
                        if (cmContent) lang = cmContent.getAttribute('data-language') || '';
                        const scroller = editor.querySelector('.cm-scroller');
                        if (scroller && scroller.scrollHeight > scroller.clientHeight * 1.5) {
                            // Virtualized -- skip here; async extractor handles it.
                            continue;
                        }
                        const lines = editor.querySelectorAll('.cm-line');
                        if (!lines.length) continue;
                        if (seenEditors.has(editor)) continue;
                        seenEditors.add(editor);
                        const code = Array.from(lines).map(l => l.textContent).join('\n');
                        if (code.trim()) {
                            blocks.push({ type: 'codemirror', filename: filename, language: lang, code: code });
                        }
                    }

                    return blocks;
                };

                // WORKSPACE diagnostic dump. Returns a plain object describing
                // every interactive control on the page so we can find the
                // real download button / link when the heuristics miss. Used by
                // _workspace_dump_dom() (auto-invoked when download fails, or
                // on demand via --workspace-debug-dom).
                window.__arena_dump_workspace = function() {
                    const out = {
                        wsFound: false,
                        wsRootCount: 0,
                        wsHtml: '',
                        buttons: [],
                        downloadLinks: [],
                        cmEditors: 0,
                        artifacts: 0,
                        pres: 0,
                        perFileButtons: 0
                    };

                    const wsRoots = Array.from(document.querySelectorAll(
                        '[data-testid*="workspace" i], [class*="workspace" i], [aria-label*="workspace" i]'
                    )).filter(e => e.offsetParent !== null);
                    out.wsRootCount = wsRoots.length;
                    out.wsFound = wsRoots.length > 0;
                    if (wsRoots.length) {
                        // Smallest-ish root is usually the panel itself.
                        out.wsHtml = (wsRoots[0].outerHTML || '').slice(0, 20000);
                    }

                    const interactive = Array.from(document.querySelectorAll(
                        'button, a, [role="button"]'
                    )).filter(b => b.offsetParent !== null);
                    let emitted = 0;
                    for (const b of interactive) {
                        emitted++;
                        if (emitted > 300) break;
                        const aria = (b.getAttribute('aria-label') || '').trim();
                        const text = (b.innerText || b.textContent || '').trim().slice(0, 60);
                        const tag = b.tagName.toLowerCase();
                        let cls = '';
                        try { cls = (b.className && b.className.toString) ? b.className.toString().slice(0, 100) : ''; } catch (e) {}
                        const box = b.getBoundingClientRect();
                        out.buttons.push({
                            tag: tag,
                            aria: aria,
                            text: text,
                            cls: cls,
                            href: b.getAttribute('href') || '',
                            x: Math.round(box.x),
                            y: Math.round(box.y),
                            isDownload: /download/i.test(aria + ' ' + text) || b.hasAttribute('download')
                        });
                    }

                    out.perFileButtons = document.querySelectorAll(
                        'button[aria-label="Download file"], button[aria-label*="download file" i]'
                    ).length;
                    out.cmEditors = document.querySelectorAll('.cm-editor').length;
                    out.artifacts = document.querySelectorAll('[class*="group/artifact"]').length;
                    out.pres = document.querySelectorAll('pre').length;

                    out.downloadLinks = Array.from(document.querySelectorAll('a[href]'))
                        .filter(a => {
                            const href = a.getAttribute('href') || '';
                            const dl = a.getAttribute('download') || '';
                            return /download/i.test(dl) || /\.(zip|tar|gz|tgz|py|js|ts|jsx|tsx|json|txt|html|css|md|csv|xml|yaml|yml)$/i.test(href);
                        })
                        .map(a => ({ href: (a.getAttribute('href')||'').slice(0,200), download: a.getAttribute('download') || '' }));

                    return out;
                };

                // Combined poll -- one round-trip returns everything we need.
                // NOTE: __arena_get_codemirror_blocks is ASYNC and expensive
                // (scrolls the editor), so it is NOT called here. It's called
                // separately from Python only when we need the final code
                // blocks (in _finalize_response / _extract_final).
                window.__arena_poll = function() {
                    return {
                        text: window.__arena_get_text(),
                        isGenerating: window.__arena_is_generating(),
                        codeBlocks: window.__arena_get_code_blocks(),
                        numBubbles: window.__arena_get_assistant_bubbles().length,
                        sendButton: window.__arena_send_button_state(),
                        copyButton: window.__arena_copy_button_state(),
                        workspace: window.__arena_workspace_state()
                    };
                };
            }
        """
        try:
            self.page.evaluate(js)
        except Exception as exc:
            print(f"[warn] Failed to inject JS helpers: {exc}", file=sys.stderr)

