"""Filename generation, sanitisation, and prompt augmentation.

Split out of aai.py. Handles:
  - language -> extension mapping (_LANG_EXT_MAP, _lang_to_extension)
  - filename hint parsing from code-block first line (_FILENAME_HINT_PATTERNS,
    _parse_filename_from_first_line)
  - auto-generated snippet_N.<ext> names (_auto_filename)
  - full filename resolution pipeline (_assign_filenames)
  - safety checks (_is_writable_filename, _sanitize_filename,
    _FILENAME_HAS_EXTENSION_RE)
  - prompt augmentation that asks Arena to wrap code in artifact cards with
    real filenames (_PROMPT_AUGMENT, _build_final_prompt, _augment_prompt)
"""

import os
import re
import sys
import time

from typing import Any, Dict, List, Optional


class FilenameMixin:
    """Provides filename / prompt-augmentation helpers for ArenaAgent.

    All methods are @classmethod or @staticmethod so they can be reused
    without an instance.
    """

    # Pattern: filename must contain a dot AND not start with a dot, to match
    # things like "hello.py", "matrix_screensaver.py", "styles.css" but NOT
    # match "README", "Makefile", ".gitignore". This is the `*.*` rule from
    # the user's request -- they want files with extensions only.
    _FILENAME_HAS_EXTENSION_RE = re.compile(r"^[^./][^/]*\.[^/]+$")

    # Language label -> file extension map, used by _auto_filename() to
    # generate `snippet_N.<ext>` filenames for code blocks that don't carry
    # their own filename (e.g. data-code-block divs, plain <pre>).
    # Keys are lowercased; values include the leading dot.
    _LANG_EXT_MAP = {
        # Python
        "python": ".py", "py": ".py", "python3": ".py",
        # JavaScript / TypeScript
        "javascript": ".js", "js": ".js", "node": ".js", "jsx": ".jsx",
        "typescript": ".ts", "ts": ".ts", "tsx": ".tsx",
        # Web
        "html": ".html", "htm": ".html",
        "css": ".css", "scss": ".scss", "sass": ".sass", "less": ".less",
        # Data
        "json": ".json", "yaml": ".yaml", "yml": ".yaml",
        "xml": ".xml", "toml": ".toml", "ini": ".ini", "cfg": ".cfg",
        "csv": ".csv", "tsv": ".tsv",
        # Shell
        "bash": ".sh", "sh": ".sh", "shell": ".sh", "zsh": ".sh",
        "fish": ".sh", "bashrc": ".sh",
        # Systems
        "c": ".c", "h": ".h",
        "cpp": ".cpp", "c++": ".cpp", "cc": ".cpp", "cxx": ".cpp",
        "hpp": ".hpp", "h++": ".hpp",
        "rust": ".rs", "rs": ".rs",
        "go": ".go", "golang": ".go",
        "java": ".java",
        "kotlin": ".kt", "kt": ".kt",
        "swift": ".swift",
        "objc": ".m", "objective-c": ".m",
        # Scripting
        "ruby": ".rb", "rb": ".rb",
        "php": ".php",
        "perl": ".pl", "pl": ".pl",
        "lua": ".lua",
        "r": ".r", "rlang": ".r",
        # JVM
        "scala": ".scala",
        "groovy": ".groovy",
        "clojure": ".clj",
        # SQL / DB
        "sql": ".sql", "psql": ".sql", "mysql": ".sql",
        "postgres": ".sql", "postgresql": ".sql",
        # Markup / docs
        "markdown": ".md", "md": ".md",
        "text": ".txt", "txt": ".txt", "plain": ".txt", "plaintext": ".txt",
        # Config
        "dockerfile": "Dockerfile",
        "makefile": "Makefile",
        # Other
        "powershell": ".ps1", "ps1": ".ps1",
        "batch": ".bat", "bat": ".bat",
        "vim": ".vim", "viml": ".vim",
        "elixir": ".ex", "ex": ".ex", "exs": ".exs",
        "erlang": ".erl", "haskell": ".hs", "hs": ".hs",
        "julia": ".jl", "dart": ".dart",
        "kotlin": ".kt",
    }

    @classmethod
    def _lang_to_extension(cls, lang: str) -> str:
        """Convert a language label (e.g. 'Python', 'JSON', 'Bash') to a file
        extension (e.g. '.py', '.json', '.sh'). Returns '.txt' for unknown
        languages, or 'Dockerfile' / 'Makefile' for those special cases.
        """
        if not lang:
            return ".txt"
        key = lang.strip().lower()
        # Direct match.
        if key in cls._LANG_EXT_MAP:
            return cls._LANG_EXT_MAP[key]
        # Try matching without trailing version (e.g. "python3" -> "python").
        for suffix in ("3", "2", "1", "12", "14", "15", "16", "17", "18", "20"):
            if key.endswith(suffix):
                base = key[:-len(suffix)]
                if base in cls._LANG_EXT_MAP:
                    return cls._LANG_EXT_MAP[base]
        # Try matching the first word (e.g. "Objective-C++" -> "objective-c").
        first_word = key.split()[0].rstrip("+-")
        if first_word in cls._LANG_EXT_MAP:
            return cls._LANG_EXT_MAP[first_word]
        # Unknown -- fall back to .txt with a warning printed once.
        return ".txt"

    # The instruction appended to the prompt when --write-files is set and
    # --no-prompt-augment is NOT passed. Asks Arena to wrap every code block
    # in an artifact file viewer with a proper filename, so --write-files can
    # save each one. Also asks for the filename as the first line of the code
    # block as a fallback, in case the artifact UI isn't available.
    _PROMPT_AUGMENT = (
        "\n\n---\n"
        "SYSTEM INSTRUCTION (for the assistant, not the user's task above):\n"
        "When producing any code, configuration, or data output, ALWAYS wrap "
        "each distinct file in an artifact file viewer (the file-card UI that "
        "shows a filename and language badge), NOT an inline code block. "
        "You MUST give EVERY single file a real, descriptive filename with the "
        "correct extension (e.g. `main.py`, `utils.py`, `config.json`, `styles.css`). "
        "This is mandatory — no file may be unnamed or left without a filename. "
        "If you produce multiple files, give each its own artifact card with its own filename. "
        "Do not put multiple files in one code block. Do not omit filenames. "
        "Do not use generic names like 'untitled' or 'file'. "
        "Every code artifact you create MUST have an explicit, meaningful filename.\n\n"
        "AS A FALLBACK (in case artifact cards are unavailable), also include "
        "the filename as the FIRST LINE of each code block, as a comment in "
        "the language's syntax. Use one of these exact formats:\n"
        "  - Python / Bash / Shell / Ruby / R / Perl:  `# filename: hello.py`\n"
        "  - JavaScript / TypeScript / C / C++ / Java / Go / Rust / Swift / "
        "Kotlin / Scala / Groovy / CSS / SCSS:  `// filename: hello.js`\n"
        "  - HTML / XML / SVG:  `<!-- filename: hello.html -->`\n"
        "  - JSON / YAML / TOML / INI / Markdown / plain text:  `# filename: config.json`\n"
        "  - SQL:  `-- filename: schema.sql`\n"
        "The calling tool will parse and strip this line, so the saved file "
        "won't contain it. This is required so the calling tool can save each "
        "file individually even when artifact cards are not rendered."
    )

    @classmethod
    def _build_final_prompt(
        cls,
        prompt: str,
        system_prompts: Optional[List[str]] = None,
        augment_prompt: bool = True,
    ) -> str:
        """Combine any system prompt additions and the built-in prompt augmentation
        and prepend them to the user prompt.
        """
        system_parts = []

        # 1. User-supplied system prompt additions
        if system_prompts:
            for sp in system_prompts:
                if sp.strip():
                    system_parts.append(sp.strip())

        # 2. Built-in system prompt (from _PROMPT_AUGMENT)
        if augment_prompt:
            clean_built_in = cls._PROMPT_AUGMENT.replace("\n\n---\n", "").strip()
            system_parts.append(clean_built_in)

        if not system_parts:
            return prompt

        system_content = "\n\n".join(system_parts)

        # Ensure we have a system instruction header
        header = ""
        if not any(system_content.startswith(h) for h in ("SYSTEM INSTRUCTION", "SYSTEM PROMPT")):
            header = "SYSTEM INSTRUCTION:\n"

        final_prompt = (
            f"{header}{system_content}\n\n"
            f"---\n\n"
            f"USER PROMPT:\n"
            f"{prompt}"
        )
        return final_prompt

    @classmethod
    def _augment_prompt(cls, prompt: str) -> str:
        """Prepend the filename instruction to the user's prompt."""
        return cls._build_final_prompt(prompt, augment_prompt=True)

    # Regex patterns for detecting a filename hint on the first line of a
    # code block. Each pattern matches a comment-style line at the start of
    # the code that contains `filename:` or `file:` followed by a filename.
    # The filename is captured in group 1.
    _FILENAME_HINT_PATTERNS = [
        # `# filename: hello.py` or `# file: hello.py` (Python, Bash, Ruby, R,
        # Perl, YAML, TOML, INI, Markdown, plain text, etc.)
        re.compile(r"^\s*#\s*(?:filename|file)\s*:\s*([\w.\-/]+)\s*$", re.IGNORECASE),
        # `// filename: hello.js` or `// file: hello.js` (JS, TS, C, C++, Java,
        # Go, Rust, Swift, Kotlin, Scala, Groovy, CSS, SCSS, etc.)
        re.compile(r"^\s*//\s*(?:filename|file)\s*:\s*([\w.\-/]+)\s*$", re.IGNORECASE),
        # `<!-- filename: hello.html -->` (HTML, XML, SVG)
        re.compile(r"^\s*<!--\s*(?:filename|file)\s*:\s*([\w.\-/]+)\s*-->\s*$", re.IGNORECASE),
        # `-- filename: schema.sql` (SQL, Haskell, Ada)
        re.compile(r"^\s*--\s*(?:filename|file)\s*:\s*([\w.\-/]+)\s*$", re.IGNORECASE),
        # `; filename: autohotkey.ahk` (AutoHotkey, Assembly, Lisp comment)
        re.compile(r"^\s*;\s*(?:filename|file)\s*:\s*([\w.\-/]+)\s*$", re.IGNORECASE),
        # `% filename: matlab.m` (MATLAB, TeX, Erlang)
        re.compile(r"^\s*%\s*(?:filename|file)\s*:\s*([\w.\-/]+)\s*$", re.IGNORECASE),
        # `""" filename: python_alt.py """` (triple-quote, rare)
        re.compile(r'^\s*"{3}\s*(?:filename|file)\s*:\s*([\w.\-/]+)\s*"{3}\s*$', re.IGNORECASE),
    ]

    @classmethod
    def _parse_filename_from_first_line(cls, code: str) -> tuple:
        """Check the first line of `code` for a filename hint.

        Returns a tuple `(filename, stripped_code)`:
        - `filename` is the extracted filename (e.g. `hello.py`) or `None` if
          no hint was found.
        - `stripped_code` is the code with the first line removed (if a hint
          was found) or the original code (if not).

        Recognized formats (case-insensitive):
            # filename: hello.py      (Python, Bash, Ruby, YAML, etc.)
            // filename: hello.js     (JS, TS, C, C++, Java, Go, etc.)
            <!-- filename: hello.html -->  (HTML, XML)
            -- filename: schema.sql   (SQL, Haskell)
            ; filename: script.ahk    (AutoHotkey, Lisp)
            % filename: matlab.m      (MATLAB, TeX)
        """
        if not code:
            return None, code
        first_line = code.split("\n", 1)[0]
        for pattern in cls._FILENAME_HINT_PATTERNS:
            m = pattern.match(first_line)
            if m:
                filename = m.group(1).strip()
                # Strip the first line from the code.
                if "\n" in code:
                    stripped = code.split("\n", 1)[1]
                else:
                    stripped = ""
                return filename, stripped
        return None, code

    @classmethod
    def _auto_filename(
        cls,
        lang: str,
        index: int,
        existing_names: set,
    ) -> str:
        """Generate `snippet_N.<ext>` for a code block without a filename.

        `index` is 1-based; `existing_names` is the set of filenames already
        assigned in this response, used to avoid collisions.
        """
        ext = cls._lang_to_extension(lang)
        # Dockerfile / Makefile are special -- no extension, no number prefix.
        if ext in ("Dockerfile", "Makefile"):
            # If there's already one, fall back to numbered.
            if ext in existing_names:
                return f"{ext.lower()}_{index}.txt"
            return ext
        # Standard case: snippet_N.<ext>
        base = f"snippet_{index}"
        candidate = f"{base}{ext}"
        # Avoid collisions: if `snippet_1.py` already exists (rare), try
        # snippet_1a.py, snippet_1b.py, etc.
        suffix_char = ord("a")
        while candidate in existing_names:
            candidate = f"{base}{chr(suffix_char)}{ext}"
            suffix_char += 1
            if suffix_char > ord("z"):
                # Give up on de-collision; just use a timestamp-like suffix.
                candidate = f"{base}_{int(time.time()) % 10000}{ext}"
                break
        return candidate

    @classmethod
    def _assign_filenames(
        cls,
        code_blocks: List[Dict[str, Any]],
        auto_filename: bool = True,
    ) -> List[Dict[str, Any]]:
        """Return a copy of `code_blocks` with filenames assigned.

        Filename resolution order (first hit wins):
        1. Block already has a writable filename (artifact card, CodeMirror
           editor) -- left unchanged.
        2. First line of the code contains a filename hint (e.g.
           `# filename: hello.py`). The hint is parsed and STRIPPED from the
           code so the saved file doesn't contain it.
        3. Auto-generated `snippet_N.<ext>` based on language label, IF
           `auto_filename` is True.

        Returns a NEW list; the input is not mutated. Also deduplicates
        filenames: if two blocks end up with the same filename, the second
        one gets a suffix appended.
        """
        if not code_blocks:
            return code_blocks
        result = []
        seen = set()  # sanitized filenames already assigned
        hint_count = 0
        for i, block in enumerate(code_blocks, 1):
            b = dict(block)  # shallow copy
            raw_filename = (b.get("filename") or "").strip()
            if raw_filename and cls._is_writable_filename(raw_filename):
                # Source 1: existing filename from artifact / CodeMirror.
                safe = cls._sanitize_filename(raw_filename)
                # Deduplicate.
                if safe in seen:
                    base, dot, ext = safe.rpartition(".")
                    if dot:
                        safe = f"{base}_{i}.{ext}"
                    else:
                        safe = f"{safe}_{i}"
                b["filename"] = safe
                seen.add(safe)
            else:
                # Source 2: try parsing a filename hint from the first line.
                code = b.get("code") or ""
                hint_filename, stripped_code = cls._parse_filename_from_first_line(code)
                if hint_filename and cls._is_writable_filename(hint_filename):
                    safe = cls._sanitize_filename(hint_filename)
                    # Deduplicate.
                    if safe in seen:
                        base, dot, ext = safe.rpartition(".")
                        if dot:
                            safe = f"{base}_{i}.{ext}"
                        else:
                            safe = f"{safe}_{i}"
                    b["filename"] = safe
                    b["code"] = stripped_code
                    b["filename_source"] = "hint"
                    hint_count += 1
                    seen.add(safe)
                elif auto_filename:
                    # Source 3: auto-generate snippet_N.<ext>.
                    lang = (b.get("language") or "").strip()
                    auto = cls._auto_filename(lang, i, seen)
                    b["filename"] = auto
                    b["auto_filename"] = True  # mark for logging
                    seen.add(auto)
                # If auto_filename is False and no hint, leave filename empty.
            result.append(b)
        if hint_count:
            print(
                f"[info] Extracted {hint_count} filename(s) from code-block "
                f"first-line hints.",
                file=sys.stderr,
            )
        return result

    @classmethod
    def _is_writable_filename(cls, filename: str) -> bool:
        """Return True if `filename` looks like a real file with an extension.

        Rules:
        - Must match `*.*` (have an extension).
        - Must NOT be an absolute path (no leading /).
        - Must NOT contain path traversal (no .. segments).
        - Must NOT contain backslashes.
        - Must NOT start with a dot (skip .gitignore etc. -- they're real
          files but the user said "titled *." so we stick to extension files).
        """
        if not filename:
            return False
        if filename.startswith("/"):
            return False
        if "\\" in filename:
            return False
        if ".." in filename.split("/"):
            return False
        return bool(cls._FILENAME_HAS_EXTENSION_RE.match(filename))

    @staticmethod
    def _sanitize_filename(filename: str) -> str:
        """Make a filename safe to write to disk.

        - Strips any directory components (we always write to target_dir).
        - Replaces characters that are illegal on Windows / macOS / Linux
          filesystems with underscores.
        """
        # Take only the basename (strip any directory components).
        name = os.path.basename(filename)
        # Replace illegal chars: null, slash, backslash, colon, asterisk,
        # question mark, quote, less-than, greater-than, pipe.
        name = re.sub(r"[\x00/\\:*?\"<>|]", "_", name)
        # Collapse multiple underscores.
        name = re.sub(r"_+", "_", name)
        # Strip leading/trailing dots and spaces.
        name = name.strip(". ")
        return name

